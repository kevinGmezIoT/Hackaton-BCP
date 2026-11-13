"""Code-calculated pre/post-deployment cohort error rate comparison and saturation checks."""

from __future__ import annotations

from datetime import datetime, timedelta

from vigia.domain.contracts import (
    CaseRecord,
    CohortComparisonResult,
    CohortRateWindow,
    ComparisonVerdict,
    ConnectorStatus,
    Evidence,
    LogQueryResult,
    RecoveryCriteriaConfig,
    ServiceConfig,
)
from vigia.domain.ports import ReleaseSourcePort, TelemetryPort


def _safe_rate(errors: int, requests: int | None) -> float | None:
    if requests is None or requests <= 0:
        return None
    return round(errors / requests, 6)


def _format_pct(rate: float | None) -> str:
    if rate is None:
        return "indeterminada"
    return f"{rate * 100:.2f}%"


def _extract_windows(
    *,
    label: str,
    result: LogQueryResult,
    expected_cohorts: list[str],
    min_coverage: float,
    min_volume: int,
    missing_data: list[str],
    limitations: list[str],
) -> list[CohortRateWindow]:
    windows: list[CohortRateWindow] = []
    if result.status in {ConnectorStatus.CONNECTOR_UNAVAILABLE, ConnectorStatus.UNAUTHORIZED}:
        missing_data.append(
            f"{label} logs unavailable ({result.status.value}): {result.note}"
        )
        return windows

    if result.coverage < min_coverage:
        missing_data.append(
            f"{label} telemetry coverage {result.coverage:.2f} is below required {min_coverage:.2f}"
        )

    all_cohorts = sorted(
        set(result.cohort_counts.keys())
        | set(result.cohort_requests.keys())
        | set(expected_cohorts if label == "post_deploy" else [])
    )
    if not all_cohorts:
        missing_data.append(f"{label} has no cohort traffic or denominator records")
        return windows

    limitation_note: str | None = None
    if result.numerator_kind == "log_events_per_request":
        limitation_note = (
            "Error numerator counts log events per request rather than deduplicated failed requests."
        )
        if limitation_note not in limitations:
            limitations.append(limitation_note)

    for coh in all_cohorts:
        errs = result.cohort_counts.get(coh, 0)
        reqs_raw = result.cohort_requests.get(coh)
        reqs = reqs_raw if (reqs_raw is not None and reqs_raw > 0) else None
        if coh == "unknown":
            missing_data.append(f"{label} contains records without version/cohort label ('unknown')")
            rate = None
        elif reqs is None:
            missing_data.append(f"{label} cohort '{coh}' has missing or zero request denominator")
            rate = None
        else:
            if reqs < min_volume:
                limitation_note = (
                    f"{label} cohort '{coh}' volume ({reqs}) is below min_volume ({min_volume})."
                )
                if limitation_note not in limitations:
                    limitations.append(limitation_note)
            rate = _safe_rate(errs, reqs)

        windows.append(
            CohortRateWindow(
                label=label,
                window_start=result.window_start,
                window_end=result.window_end,
                cohort=coh,
                errors=errs,
                requests=reqs,
                error_rate=rate,
                numerator_kind=result.numerator_kind,
                error_definition=result.error_definition or result.pattern,
                coverage=result.coverage,
                reproducible_query=result.reproducible_query,
                limitation=limitation_note,
            )
        )
    return windows


def find_reusable_comparison(
    case: CaseRecord,
    *,
    service: str,
    cutoff_at: datetime,
) -> Evidence | None:
    """Return existing dossier evidence if a valid cohort comparison already covers this cutoff."""
    target_cutoff = cutoff_at.isoformat()
    for ev in reversed(case.evidence):
        if ev.service.lower() != service.lower():
            continue
        if ev.observation_type == "cohort_comparison" and ev.observed_at.isoformat() == target_cutoff:
            return ev
    return None


def compare_service_cohort_rates(
    *,
    service: str,
    environment: str,
    cutoff_at: datetime,
    telemetry: TelemetryPort,
    releases: ReleaseSourcePort,
    service_config: ServiceConfig | None = None,
    pattern: str = "*",
    window_minutes: int | None = None,
    check_shared_saturation: bool = True,
) -> CohortComparisonResult:
    """Compare pre/post-deployment error rates across cohorts using code-computed denominators."""
    criteria = (
        service_config.recovery_criteria
        if service_config is not None
        else RecoveryCriteriaConfig()
    )
    active_res = releases.resolve_active_releases(service, environment, cutoff_at)
    recent_res = releases.list_recent_changes(service, environment, 24, cutoff_at)

    active_release = active_res.releases[0] if active_res.releases else None
    deployed_at = active_release.deployed_at if active_release else (cutoff_at - timedelta(minutes=15))
    release_id = active_release.release_id if active_release else None

    if cutoff_at > deployed_at:
        raw_duration = cutoff_at - deployed_at
    else:
        raw_duration = timedelta(minutes=15)
    if window_minutes is not None:
        raw_duration = min(raw_duration, timedelta(minutes=max(1, window_minutes)))
    duration = max(timedelta(minutes=1), raw_duration)

    post_start = deployed_at
    post_end = cutoff_at if cutoff_at > deployed_at else (deployed_at + duration)
    pre_end = deployed_at - timedelta(seconds=1)
    pre_start = pre_end - (post_end - post_start)

    missing_data: list[str] = []
    limitations: list[str] = []

    if active_res.status in {ConnectorStatus.CONNECTOR_UNAVAILABLE, ConnectorStatus.UNAUTHORIZED}:
        missing_data.append(
            f"Release source unavailable ({active_res.status.value}): {active_res.note}"
        )

    pre_logs = telemetry.search_logs(
        service,
        environment,
        pattern,
        max(1, int(duration.total_seconds() // 60)),
        cutoff_at,
        window_start=pre_start,
        window_end=pre_end,
    )
    post_logs = telemetry.search_logs(
        service,
        environment,
        pattern,
        max(1, int(duration.total_seconds() // 60)),
        cutoff_at,
        window_start=post_start,
        window_end=post_end,
    )

    stable_candidates = [
        rel.release_id
        for rel in recent_res.releases
        if release_id is None or rel.release_id != release_id
    ]
    expected_post_cohorts: list[str] = []
    if release_id:
        expected_post_cohorts.append(release_id)
    if stable_candidates:
        expected_post_cohorts.append(stable_candidates[0])

    pre_windows = _extract_windows(
        label="pre_deploy",
        result=pre_logs,
        expected_cohorts=[],
        min_coverage=criteria.min_coverage,
        min_volume=criteria.comparison_min_volume,
        missing_data=missing_data,
        limitations=limitations,
    )
    post_windows = _extract_windows(
        label="post_deploy",
        result=post_logs,
        expected_cohorts=expected_post_cohorts,
        min_coverage=criteria.min_coverage,
        min_volume=criteria.comparison_min_volume,
        missing_data=missing_data,
        limitations=limitations,
    )

    all_windows = [*pre_windows, *post_windows]
    pre_total_requests = sum(w.requests or 0 for w in pre_windows)
    post_total_requests = sum(w.requests or 0 for w in post_windows)

    shared_saturated = False
    if check_shared_saturation:
        sat_logs = telemetry.search_logs(
            service,
            environment,
            "saturat",
            max(1, int(duration.total_seconds() // 60)),
            cutoff_at,
            window_start=post_start,
            window_end=post_end,
        )
        if sat_logs.status == ConnectorStatus.OK and sat_logs.total > 0:
            shared_saturated = True
        else:
            sat_metric = telemetry.query_metrics(
                service,
                environment,
                "db_pool_utilization",
                max(1, int(duration.total_seconds() // 60)),
                cutoff_at,
            )
            if (
                sat_metric.status == ConnectorStatus.OK
                and sat_metric.current_value is not None
                and sat_metric.current_value >= 85.0
            ):
                shared_saturated = True

    volume_doubled_or_surged = (
        pre_total_requests > 0 and post_total_requests >= int(1.5 * pre_total_requests)
    )
    if volume_doubled_or_surged:
        shared_saturated = shared_saturated or any(
            w.error_rate is not None and w.error_rate >= criteria.error_rate_tolerance
            for w in post_windows
            if release_id is None or w.cohort != release_id
        )

    pre_valid_rates = [w.error_rate for w in pre_windows if w.error_rate is not None]
    post_by_cohort = {w.cohort: w for w in post_windows}
    new_window = post_by_cohort.get(release_id) if release_id else None
    stable_windows = [
        w for w in post_windows if release_id is None or w.cohort != release_id
    ]

    verdict = ComparisonVerdict.INDETERMINATE
    summary_parts: list[str] = []

    if missing_data or not pre_valid_rates or (release_id and new_window is None):
        verdict = ComparisonVerdict.INDETERMINATE
    elif new_window is not None and new_window.error_rate is None:
        verdict = ComparisonVerdict.INDETERMINATE
    elif stable_windows and any(w.error_rate is None for w in stable_windows):
        verdict = ComparisonVerdict.INDETERMINATE
    elif new_window is not None and new_window.error_rate is not None:
        baseline_rate = max(pre_valid_rates)
        new_rate = new_window.error_rate
        stable_rates = [w.error_rate for w in stable_windows if w.error_rate is not None]

        if stable_rates:
            max_stable_rate = max(stable_rates)
            both_worsened = (
                max_stable_rate >= criteria.error_rate_tolerance
                and new_rate >= criteria.error_rate_tolerance
                and max_stable_rate >= criteria.shared_load_rate_ratio * new_rate
            )
            if both_worsened and (shared_saturated or volume_doubled_or_surged):
                verdict = ComparisonVerdict.CONTRADICTS_DEPLOYMENT
                summary_parts.append(
                    f"Shared degradation across cohorts after deploy: stable cohort reached "
                    f"{_format_pct(max_stable_rate)} and {release_id} reached {_format_pct(new_rate)} "
                    f"(pre-deploy {_format_pct(baseline_rate)}; volume {pre_total_requests} -> {post_total_requests})"
                    + (" with shared resource saturation evidence." if shared_saturated else ".")
                )
            elif both_worsened:
                verdict = ComparisonVerdict.INDETERMINATE
                missing_data.append(
                    "Both cohorts worsened after deploy, but shared load/saturation evidence is incomplete."
                )
            elif (
                new_rate >= criteria.error_rate_tolerance
                and new_rate > max(max_stable_rate * 2.0, baseline_rate * 2.0)
            ):
                verdict = ComparisonVerdict.SUPPORTS_CAUSAL_CHANGE
                summary_parts.append(
                    f"Differential cohort impact supports deployment '{release_id}': "
                    f"pre-deploy {_format_pct(baseline_rate)}, stable post-deploy {_format_pct(max_stable_rate)}, "
                    f"{release_id} post-deploy {_format_pct(new_rate)}."
                )
            else:
                verdict = ComparisonVerdict.INDETERMINATE
                missing_data.append(
                    "Observed cohort rates do not isolate the active deployment from baseline noise."
                )
        else:
            limitations.append("Only the active cohort had post-deploy observations; no concurrent control cohort.")
            if new_rate >= criteria.error_rate_tolerance and baseline_rate < criteria.error_rate_tolerance:
                verdict = ComparisonVerdict.SUPPORTS_CAUSAL_CHANGE
                summary_parts.append(
                    f"Errors jumped after '{release_id}' from {_format_pct(baseline_rate)} to {_format_pct(new_rate)} "
                    "(without a separate stable post-deploy cohort)."
                )
            else:
                verdict = ComparisonVerdict.INDETERMINATE

    if verdict == ComparisonVerdict.INDETERMINATE and not summary_parts:
        detail = "; ".join(missing_data) if missing_data else "Insufficient cohort coverage to decide."
        summary_parts.append(f"Indeterminate cohort comparison: {detail}")

    return CohortComparisonResult(
        service=service,
        environment=environment,
        release_id=release_id,
        deployed_at=deployed_at,
        cutoff_at=cutoff_at,
        pre_window_start=pre_start,
        pre_window_end=pre_end,
        post_window_start=post_start,
        post_window_end=post_end,
        windows=all_windows,
        pre_total_requests=pre_total_requests,
        post_total_requests=post_total_requests,
        shared_resource_saturated=shared_saturated,
        verdict=verdict,
        missing_data=missing_data,
        limitations=limitations,
        summary=" ".join(summary_parts),
    )

