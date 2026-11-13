"""CSV metrics, statistical anomaly detection, and redacted log query adapter."""

from __future__ import annotations

import csv
import json
import math
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from vigia.adapters.outbound.executor import SimulationState
from vigia.domain.contracts import (
    AnomalyReport,
    ConnectorStatus,
    LogQueryResult,
    MetricPoint,
    MetricSeries,
)
from vigia.domain.ports import ServiceCatalogPort

_SECRET_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), r"\1[REDACTED]"),
    (
        re.compile(r"((?:password|secret|token|api_key)\s*[=:]\s*)[^\s,;]+", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
]


def redact_sensitive_text(text: str) -> str:
    """Redact bearer tokens and key=value secrets before exposing logs to the model."""
    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _linear_slope_and_r2(points: list[MetricPoint]) -> tuple[float, float]:
    """Compute ordinary least-squares slope (units/min) and R^2 goodness of fit."""
    if len(points) < 2:
        return 0.0, 0.0
    t0 = points[0].timestamp
    xs = [(p.timestamp - t0).total_seconds() / 60.0 for p in points]
    ys = [p.value for p in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denom_x = sum((x - mean_x) ** 2 for x in xs)
    denom_y = sum((y - mean_y) ** 2 for y in ys)
    if denom_x == 0.0:
        return 0.0, 0.0
    numer = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = numer / denom_x
    if denom_y == 0.0:
        return slope, 1.0
    r2 = (numer**2) / (denom_x * denom_y)
    return slope, max(0.0, min(1.0, r2))


def compute_anomaly_and_projection(
    series: MetricSeries,
    *,
    threshold: float,
    cutoff_at: datetime,
) -> AnomalyReport:
    """Reusable anomaly detection and threshold projection across CSV and HTTP series."""
    if series.status != ConnectorStatus.OK or len(series.points) < 3:
        return AnomalyReport(
            status=series.status,
            service=series.service,
            environment=series.environment,
            metric=series.metric,
            window_start=series.window_start,
            window_end=cutoff_at,
            reproducible_query=series.reproducible_query,
            threshold=threshold,
            projection_status="indeterminate",
            request_volume=series.request_volume,
            coverage=series.coverage,
            note=series.note or "Insufficient points for anomaly calculation.",
        )

    recent_cutoff = cutoff_at - timedelta(minutes=15)
    baseline_pts = [p.value for p in series.points if p.timestamp <= recent_cutoff]
    if len(baseline_pts) < 3:
        baseline_pts = [p.value for p in series.points[:-1]]

    mean_val = sum(baseline_pts) / len(baseline_pts)
    variance = sum((v - mean_val) ** 2 for v in baseline_pts) / max(len(baseline_pts), 1)
    std_dev = max(math.sqrt(variance), 1.0)
    current_val = series.points[-1].value
    z_score = (current_val - mean_val) / std_dev

    rising_window = [p for p in series.points if p.timestamp >= recent_cutoff]
    if len(rising_window) < 2:
        rising_window = series.points[-5:]
    slope, r2 = _linear_slope_and_r2(rising_window)

    is_anomalous = z_score >= 3.0 or current_val >= threshold
    deltas = [
        rising_window[i + 1].value - rising_window[i].value
        for i in range(len(rising_window) - 1)
    ]
    has_sharp_reversal = any(d < -10.0 for d in deltas) and any(d > 10.0 for d in deltas)
    trend_unstable = len(rising_window) >= 3 and (r2 < 0.65 or has_sharp_reversal)

    projected_minutes: float | None = None
    projection_status = "not_applicable"
    note = series.note

    if is_anomalous:
        if trend_unstable or slope <= 0.0:
            projection_status = "indeterminate"
            if not note:
                note = "Latency elevation confirmed, but trend is unstable for linear projection."
        elif current_val < threshold:
            projected_minutes = round((threshold - current_val) / slope, 1)
            projection_status = "projected"
        else:
            projected_minutes = 0.0
            projection_status = "projected"

    return AnomalyReport(
        status=ConnectorStatus.OK,
        service=series.service,
        environment=series.environment,
        metric=series.metric,
        window_start=series.window_start,
        window_end=cutoff_at,
        reproducible_query=series.reproducible_query,
        is_anomalous=is_anomalous,
        z_score=round(z_score, 2),
        baseline_mean=round(mean_val, 2),
        current_value=current_val,
        threshold=threshold,
        slope_per_minute=round(slope, 2),
        projected_minutes_to_threshold=projected_minutes,
        projection_status=projection_status,
        request_volume=series.request_volume,
        coverage=series.coverage,
        note=note,
    )


class CsvTelemetryAdapter:
    """Reads local synthetic CSV metrics and structured logs with strict cutoff enforcement."""

    def __init__(
        self,
        metrics_path: Path,
        logs_path: Path,
        catalog: ServiceCatalogPort,
        *,
        simulation_state: SimulationState | None = None,
        unauthorized_services: set[str] | None = None,
        max_retries: int = 2,
    ) -> None:
        self._metrics_path = metrics_path
        self._logs_path = logs_path
        self._catalog = catalog
        self._sim_state = simulation_state or SimulationState()
        self._unauthorized = {s.lower() for s in (unauthorized_services or set())}
        self._max_retries = max_retries

    def query_metrics(
        self,
        service: str,
        environment: str,
        metric: str,
        window_minutes: int,
        cutoff_at: datetime,
        allow_post_action: bool = False,
    ) -> MetricSeries:
        t0 = time.perf_counter()
        canonical = self._catalog.resolve_canonical_name(service)
        svc_cfg = self._catalog.get_service(canonical)
        unit = svc_cfg.telemetry.metric_unit if svc_cfg else "ms"
        window_start = cutoff_at - timedelta(minutes=window_minutes)
        query_ref = (
            f"metrics.csv?service={canonical}&env={environment}&metric={metric}"
            f"&from={window_start.isoformat()}&to={cutoff_at.isoformat()}"
        )
        if canonical.lower() in self._unauthorized or f"metrics:{canonical.lower()}" in self._unauthorized:
            return MetricSeries(
                status=ConnectorStatus.UNAUTHORIZED,
                service=canonical,
                environment=environment,
                metric=metric,
                unit=unit,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=query_ref,
                coverage=0.0,
                note=f"Permission denied reading metrics for '{canonical}'.",
            )

        if allow_post_action and self._sim_state.suppress_post_action_metrics:
            return MetricSeries(
                status=ConnectorStatus.NO_RESULTS,
                service=canonical,
                environment=environment,
                metric=metric,
                unit=unit,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=query_ref,
                coverage=0.0,
                note="Post-action metric unavailable.",
            )

        if not self._metrics_path.exists():
            return MetricSeries(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                service=canonical,
                environment=environment,
                metric=metric,
                unit=unit,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=query_ref,
                coverage=0.0,
                note=f"Metrics file not found: {self._metrics_path}",
            )

        points: list[MetricPoint] = []
        total_requests = 0
        total_errors = 0
        saw_requests = False
        saw_errors = False
        with self._metrics_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                row_service = self._catalog.resolve_canonical_name(row["service"])
                if row_service.lower() != canonical.lower():
                    continue
                if row["environment"].lower() != environment.lower():
                    continue
                if row["metric"] != metric:
                    continue
                phase = row.get("phase", "observed").strip().lower()
                if phase == "post_rollback" and not (
                    allow_post_action and bool(self._sim_state.rolled_back_releases)
                ):
                    continue
                ts = datetime.fromisoformat(row["timestamp"])
                if window_start <= ts <= cutoff_at:
                    req_raw = row.get("requests")
                    err_raw = row.get("errors")
                    req_count = int(req_raw) if req_raw is not None and req_raw != "" else None
                    err_count = int(err_raw) if err_raw is not None and err_raw != "" else None
                    if req_count is not None:
                        total_requests += req_count
                        saw_requests = True
                    if err_count is not None:
                        total_errors += err_count
                        saw_errors = True
                    points.append(
                        MetricPoint(
                            timestamp=ts,
                            value=float(row["value"]),
                            cohort=row.get("cohort") or None,
                            request_count=req_count,
                            error_count=err_count,
                        )
                    )

        points.sort(key=lambda p: p.timestamp)
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        if not points:
            return MetricSeries(
                status=ConnectorStatus.NO_RESULTS,
                service=canonical,
                environment=environment,
                metric=metric,
                unit=unit,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=query_ref,
                coverage=0.0,
                duration_ms=duration_ms,
                note="No metric points found in window.",
            )

        current_val = points[-1].value
        ref_target = cutoff_at - timedelta(minutes=15)
        closest_15m = min(points, key=lambda p: abs((p.timestamp - ref_target).total_seconds()))
        req_vol: int | None = total_requests if saw_requests else None
        err_vol: int | None = total_errors if saw_errors else None
        coverage = 1.0 if (req_vol is not None and req_vol > 0) else 0.0
        return MetricSeries(
            status=ConnectorStatus.OK,
            service=canonical,
            environment=environment,
            metric=metric,
            unit=unit,
            window_start=window_start,
            window_end=cutoff_at,
            reproducible_query=query_ref,
            coverage=coverage,
            points=points,
            current_value=current_val,
            value_15m_ago=closest_15m.value,
            request_volume=req_vol,
            error_total=err_vol,
            duration_ms=duration_ms,
        )

    def detect_anomaly(
        self,
        service: str,
        environment: str,
        metric: str,
        cutoff_at: datetime,
    ) -> AnomalyReport:
        canonical = self._catalog.resolve_canonical_name(service)
        svc_cfg = self._catalog.get_service(canonical)
        threshold = svc_cfg.threshold_ms if svc_cfg else 800.0
        series = self.query_metrics(canonical, environment, metric, 45, cutoff_at)
        return compute_anomaly_and_projection(
            series,
            threshold=threshold,
            cutoff_at=cutoff_at,
        )

    def search_logs(
        self,
        service: str,
        environment: str,
        pattern: str,
        window_minutes: int,
        cutoff_at: datetime,
        cohort: str | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> LogQueryResult:
        t0 = time.perf_counter()
        canonical = self._catalog.resolve_canonical_name(service)
        effective_end = min(window_end, cutoff_at) if window_end is not None else cutoff_at
        effective_start = (
            window_start
            if window_start is not None
            else (effective_end - timedelta(minutes=window_minutes))
        )
        if effective_start > effective_end:
            effective_start = effective_end

        query_ref = (
            f"logs.txt?service={canonical}&env={environment}&pattern={pattern}"
            f"&from={effective_start.isoformat()}&to={effective_end.isoformat()}"
        )
        if canonical.lower() in self._unauthorized or f"logs:{canonical.lower()}" in self._unauthorized:
            return LogQueryResult(
                status=ConnectorStatus.UNAUTHORIZED,
                service=canonical,
                environment=environment,
                pattern=pattern,
                window_start=effective_start,
                window_end=effective_end,
                reproducible_query=query_ref,
                coverage=0.0,
                note=f"Permission denied reading logs for '{canonical}'.",
            )

        if not self._logs_path.exists():
            return LogQueryResult(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                service=canonical,
                environment=environment,
                pattern=pattern,
                window_start=effective_start,
                window_end=effective_end,
                reproducible_query=query_ref,
                coverage=0.0,
                note=f"Logs file not found: {self._logs_path}",
            )

        lowered_pattern = pattern.lower()
        total = 0
        first_seen: datetime | None = None
        examples: list[str] = []
        cohort_counts: dict[str, int] = {}
        cohort_requests: dict[str, int] = {}
        saw_failed_requests_field = False
        saw_log_event_count = False

        with self._logs_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                entry_svc = self._catalog.resolve_canonical_name(entry["service"])
                if entry_svc.lower() != canonical.lower():
                    continue
                if entry["environment"].lower() != environment.lower():
                    continue
                ts = datetime.fromisoformat(entry["timestamp"])
                if not (effective_start <= ts <= effective_end):
                    continue
                entry_cohort = entry.get("cohort", "unknown")
                if cohort is not None and entry_cohort != cohort:
                    continue

                message = str(entry["message"])
                level = str(entry.get("level", "INFO")).upper()
                count = int(entry.get("count", 1))

                if "requests" in entry:
                    cohort_requests[entry_cohort] = cohort_requests.get(entry_cohort, 0) + int(
                        entry["requests"]
                    )
                elif level == "INFO" and (
                    "requests" in message.lower() or "req/min" in message.lower()
                ):
                    cohort_requests[entry_cohort] = cohort_requests.get(entry_cohort, 0) + count

                matches = (
                    level == "ERROR"
                    if lowered_pattern == "*"
                    else lowered_pattern in message.lower()
                )
                if not matches:
                    continue

                if "failed_requests" in entry:
                    saw_failed_requests_field = True
                    err_increment = int(entry["failed_requests"])
                else:
                    saw_log_event_count = True
                    err_increment = count

                total += err_increment
                cohort_counts[entry_cohort] = cohort_counts.get(entry_cohort, 0) + err_increment
                if first_seen is None or ts < first_seen:
                    first_seen = ts
                if len(examples) < 3:
                    examples.append(
                        redact_sensitive_text(
                            f"{ts.isoformat()} [{entry_cohort}] {level}: {message}"
                        )
                    )

        if cohort is not None and cohort not in cohort_requests and cohort not in cohort_counts:
            cohort_requests[cohort] = 0

        all_cohorts = set(cohort_requests.keys()) | set(cohort_counts.keys())
        cohort_error_rates: dict[str, float | None] = {}
        uncovered_cohorts: list[str] = []
        for coh in sorted(all_cohorts):
            reqs = cohort_requests.get(coh, 0)
            errs = cohort_counts.get(coh, 0)
            if reqs <= 0 or coh == "unknown":
                cohort_requests.setdefault(coh, 0)
                cohort_error_rates[coh] = None
                uncovered_cohorts.append(coh)
            else:
                cohort_error_rates[coh] = round(errs / reqs, 4)

        coverage = 1.0
        if cohort is not None and cohort in uncovered_cohorts and total == 0:
            coverage = 0.0
        elif all_cohorts and len(uncovered_cohorts) == len(all_cohorts):
            coverage = 0.0

        numerator_kind = (
            "log_events_per_request"
            if saw_log_event_count and not saw_failed_requests_field
            else "failed_requests"
        )
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        status = ConnectorStatus.OK if total > 0 else ConnectorStatus.NO_RESULTS
        return LogQueryResult(
            status=status,
            service=canonical,
            environment=environment,
            pattern=pattern,
            window_start=effective_start,
            window_end=effective_end,
            reproducible_query=query_ref,
            total=total,
            first_occurrence=first_seen,
            examples=examples,
            cohort_counts=cohort_counts,
            cohort_requests=cohort_requests,
            cohort_error_rates=cohort_error_rates,
            uncovered_cohorts=uncovered_cohorts,
            coverage=coverage,
            duration_ms=duration_ms,
            numerator_kind=numerator_kind,
            error_definition=pattern,
        )
