"""CSV metrics, statistical anomaly detection, and redacted log query adapter."""

from __future__ import annotations

import csv
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

from adapters.outbound.executor import SimulationState
from domain.contracts import (
    AnomalyReport,
    ConnectorStatus,
    LogQueryResult,
    MetricPoint,
    MetricSeries,
)
from domain.ports import ServiceCatalogPort

_SECRET_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE), r"\1[REDACTED]"),
    (
        re.compile(r"((?:password|secret|token|api_key)\s*[=:]\s*)[^\s,;]+", re.IGNORECASE),
        r"\1[REDACTED]",
    ),
]


def _redact(text: str) -> str:
    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _linear_slope(points: list[MetricPoint]) -> float:
    """Compute ordinary least-squares slope in value units per minute."""
    if len(points) < 2:
        return 0.0
    t0 = points[0].timestamp
    xs = [(p.timestamp - t0).total_seconds() / 60.0 for p in points]
    ys = [p.value for p in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0.0:
        return 0.0
    numer = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return numer / denom


class CsvTelemetryAdapter:
    """Reads local synthetic CSV metrics and structured logs with strict cutoff enforcement."""

    def __init__(
        self,
        metrics_path: Path,
        logs_path: Path,
        catalog: ServiceCatalogPort,
        *,
        simulation_state: SimulationState | None = None,
    ) -> None:
        self._metrics_path = metrics_path
        self._logs_path = logs_path
        self._catalog = catalog
        self._sim_state = simulation_state or SimulationState()

    def query_metrics(
        self,
        service: str,
        environment: str,
        metric: str,
        window_minutes: int,
        cutoff_at: datetime,
        allow_post_action: bool = False,
    ) -> MetricSeries:
        canonical = self._catalog.resolve_canonical_name(service)
        window_start = cutoff_at - timedelta(minutes=window_minutes)
        query_ref = (
            f"metrics.csv?service={canonical}&env={environment}&metric={metric}"
            f"&from={window_start.isoformat()}&to={cutoff_at.isoformat()}"
        )
        if allow_post_action and self._sim_state.suppress_post_action_metrics:
            return MetricSeries(
                status=ConnectorStatus.NO_RESULTS,
                service=canonical,
                environment=environment,
                metric=metric,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=query_ref,
                note="Post-action metric unavailable.",
            )

        if not self._metrics_path.exists():
            return MetricSeries(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                service=canonical,
                environment=environment,
                metric=metric,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=query_ref,
                note=f"Metrics file not found: {self._metrics_path}",
            )

        points: list[MetricPoint] = []
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
                    points.append(
                        MetricPoint(
                            timestamp=ts,
                            value=float(row["value"]),
                            cohort=row.get("cohort") or None,
                        )
                    )

        points.sort(key=lambda p: p.timestamp)
        if not points:
            return MetricSeries(
                status=ConnectorStatus.NO_RESULTS,
                service=canonical,
                environment=environment,
                metric=metric,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=query_ref,
                note="No metric points found in window.",
            )

        current_val = points[-1].value
        ref_target = cutoff_at - timedelta(minutes=15)
        closest_15m = min(points, key=lambda p: abs((p.timestamp - ref_target).total_seconds()))
        return MetricSeries(
            status=ConnectorStatus.OK,
            service=canonical,
            environment=environment,
            metric=metric,
            window_start=window_start,
            window_end=cutoff_at,
            reproducible_query=query_ref,
            points=points,
            current_value=current_val,
            value_15m_ago=closest_15m.value,
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
        if series.status != ConnectorStatus.OK or len(series.points) < 3:
            return AnomalyReport(
                status=series.status,
                service=canonical,
                environment=environment,
                metric=metric,
                window_start=series.window_start,
                window_end=cutoff_at,
                reproducible_query=series.reproducible_query,
                threshold=threshold,
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
        slope = _linear_slope(rising_window)

        projected_minutes: float | None = None
        if slope > 0.0 and current_val < threshold:
            projected_minutes = round((threshold - current_val) / slope, 1)

        return AnomalyReport(
            status=ConnectorStatus.OK,
            service=canonical,
            environment=environment,
            metric=metric,
            window_start=series.window_start,
            window_end=cutoff_at,
            reproducible_query=series.reproducible_query,
            is_anomalous=z_score >= 3.0 or current_val >= threshold,
            z_score=round(z_score, 2),
            baseline_mean=round(mean_val, 2),
            current_value=current_val,
            threshold=threshold,
            slope_per_minute=round(slope, 2),
            projected_minutes_to_threshold=projected_minutes,
        )

    def search_logs(
        self,
        service: str,
        environment: str,
        pattern: str,
        window_minutes: int,
        cutoff_at: datetime,
        cohort: str | None = None,
    ) -> LogQueryResult:
        canonical = self._catalog.resolve_canonical_name(service)
        window_start = cutoff_at - timedelta(minutes=window_minutes)
        query_ref = (
            f"logs.txt?service={canonical}&env={environment}&pattern={pattern}"
            f"&from={window_start.isoformat()}&to={cutoff_at.isoformat()}"
        )
        if not self._logs_path.exists():
            return LogQueryResult(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                service=canonical,
                environment=environment,
                pattern=pattern,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=query_ref,
                note=f"Logs file not found: {self._logs_path}",
            )

        lowered_pattern = pattern.lower()
        total = 0
        first_seen: datetime | None = None
        examples: list[str] = []
        cohort_counts: dict[str, int] = {}

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
                if not (window_start <= ts <= cutoff_at):
                    continue
                entry_cohort = entry.get("cohort", "unknown")
                if cohort is not None and entry_cohort != cohort:
                    continue
                message = str(entry["message"])
                if lowered_pattern not in message.lower():
                    continue

                count = int(entry.get("count", 1))
                total += count
                cohort_counts[entry_cohort] = cohort_counts.get(entry_cohort, 0) + count
                if first_seen is None or ts < first_seen:
                    first_seen = ts
                if len(examples) < 3:
                    examples.append(
                        _redact(
                            f"{ts.isoformat()} [{entry_cohort}] {entry.get('level', 'ERROR')}: {message}"
                        )
                    )

        status = ConnectorStatus.OK if total > 0 else ConnectorStatus.NO_RESULTS
        return LogQueryResult(
            status=status,
            service=canonical,
            environment=environment,
            pattern=pattern,
            window_start=window_start,
            window_end=cutoff_at,
            reproducible_query=query_ref,
            total=total,
            first_occurrence=first_seen,
            examples=examples,
            cohort_counts=cohort_counts,
        )

