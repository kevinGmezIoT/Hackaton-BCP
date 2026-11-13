"""Prometheus metrics and Loki logs adapter with bounded retries and anomaly reuse."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from vigia.adapters.outbound.telemetry import (
    compute_anomaly_and_projection,
    redact_sensitive_text,
)
from vigia.domain.contracts import (
    AnomalyReport,
    ConnectorStatus,
    LogQueryResult,
    MetricPoint,
    MetricSeries,
)
from vigia.domain.ports import ServiceCatalogPort

HttpGetFn = Callable[[str, dict[str, str], dict[str, str]], tuple[int, dict[str, Any]]]


def _parse_telemetry_timestamp(raw_ts: Any, target_tz: Any = timezone.utc) -> datetime:
    """Parse Prometheus epoch (s/ms/us/ns), Loki nanosecond strings, or ISO-8601 timestamps."""
    tz = target_tz or timezone.utc
    if isinstance(raw_ts, (int, float)):
        numeric = float(raw_ts)
        abs_val = abs(numeric)
        if abs_val >= 1e18:
            sec, rem_ns = divmod(int(raw_ts), 1_000_000_000)
            dt = datetime.fromtimestamp(sec, tz=timezone.utc) + timedelta(microseconds=rem_ns // 1000)
        elif abs_val >= 1e15:
            sec, rem_us = divmod(int(raw_ts), 1_000_000)
            dt = datetime.fromtimestamp(sec, tz=timezone.utc) + timedelta(microseconds=rem_us)
        elif abs_val >= 1e12:
            sec, rem_ms = divmod(int(raw_ts), 1_000)
            dt = datetime.fromtimestamp(sec, tz=timezone.utc) + timedelta(milliseconds=rem_ms)
        else:
            dt = datetime.fromtimestamp(numeric, tz=timezone.utc)
        return dt.astimezone(tz)

    text = str(raw_ts).strip()
    if text.isdigit() and len(text) >= 9:
        ival = int(text)
        if len(text) >= 18:
            sec, rem_ns = divmod(ival, 1_000_000_000)
            dt = datetime.fromtimestamp(sec, tz=timezone.utc) + timedelta(microseconds=rem_ns // 1000)
        elif len(text) >= 15:
            sec, rem_us = divmod(ival, 1_000_000)
            dt = datetime.fromtimestamp(sec, tz=timezone.utc) + timedelta(microseconds=rem_us)
        elif len(text) >= 12:
            sec, rem_ms = divmod(ival, 1_000)
            dt = datetime.fromtimestamp(sec, tz=timezone.utc) + timedelta(milliseconds=rem_ms)
        else:
            dt = datetime.fromtimestamp(float(ival), tz=timezone.utc)
        return dt.astimezone(tz)

    try:
        numeric = float(text)
    except ValueError:
        cleaned = text[:-1] + "+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz)
    return _parse_telemetry_timestamp(numeric, tz)


def _default_http_get(
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
) -> tuple[int, dict[str, Any]]:
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}" if query else url
    req = urllib.request.Request(full_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": str(exc)}


class PrometheusLokiTelemetryAdapter:
    """Queries Prometheus HTTP API for metrics and Loki HTTP API for logs."""

    def __init__(
        self,
        catalog: ServiceCatalogPort,
        *,
        prometheus_base_url: str = "http://prometheus.internal",
        loki_base_url: str = "http://loki.internal",
        auth_token: str | None = None,
        http_get: HttpGetFn | None = None,
        max_retries_per_case: int = 2,
    ) -> None:
        self._catalog = catalog
        self._prom_url = prometheus_base_url.rstrip("/")
        self._loki_url = loki_base_url.rstrip("/")
        self._auth_token = auth_token
        self._http_get = http_get or _default_http_get
        self._max_retries = max_retries_per_case
        self._retries_used = 0

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def _call_with_retry(
        self,
        url: str,
        params: dict[str, str],
    ) -> tuple[int, dict[str, Any]]:
        while True:
            try:
                status_code, payload = self._http_get(url, params, self._headers())
            except (TimeoutError, ConnectionError, OSError) as exc:
                status_code, payload = 503, {"error": str(exc)}

            if status_code in {502, 503, 504} and self._retries_used < self._max_retries:
                self._retries_used += 1
                continue
            return status_code, payload

    def _query_counter_sum(
        self,
        promql: str,
        window_start: datetime,
        cutoff_at: datetime,
        step_seconds: int = 60,
    ) -> int | None:
        """Query a companion Prometheus counter series; return None when unavailable or absent."""
        params = {
            "query": promql,
            "start": window_start.isoformat(),
            "end": cutoff_at.isoformat(),
            "step": f"{step_seconds}s",
        }
        code, payload = self._call_with_retry(f"{self._prom_url}/api/v1/query_range", params)
        if code != 200 or not isinstance(payload, dict):
            return None
        results = payload.get("data", {}).get("result", [])
        if not results:
            return None
        total = 0.0
        saw_sample = False
        for series_item in results:
            samples = series_item.get("values", [])
            if not samples and "value" in series_item:
                samples = [series_item["value"]]
            for raw_point in samples:
                if len(raw_point) < 2:
                    continue
                ts = _parse_telemetry_timestamp(raw_point[0], cutoff_at.tzinfo)
                if not (window_start <= ts <= cutoff_at):
                    continue
                try:
                    total += max(0.0, float(raw_point[1]))
                    saw_sample = True
                except (TypeError, ValueError):
                    continue
        return int(round(total)) if saw_sample else None

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
        window_start = cutoff_at - timedelta(minutes=window_minutes)
        if svc_cfg is None or svc_cfg.environment.lower() != environment.lower():
            return MetricSeries(
                status=ConnectorStatus.UNAUTHORIZED,
                service=canonical,
                environment=environment,
                metric=metric,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=f"prometheus:{canonical}/{environment}",
                coverage=0.0,
                note=f"Service '{canonical}' in '{environment}' is not authorized in catalog.",
            )

        selector = svc_cfg.telemetry.service_selector
        env_sel = svc_cfg.telemetry.environment_selector
        unit = svc_cfg.telemetry.metric_unit
        step_seconds = 60
        promql = f'{metric}{{service="{selector}",env="{env_sel}"}}'
        params = {
            "query": promql,
            "start": window_start.isoformat(),
            "end": cutoff_at.isoformat(),
            "step": f"{step_seconds}s",
        }
        query_ref = (
            f"{self._prom_url}/api/v1/query_range?query={promql}"
            f"&start={window_start.isoformat()}&end={cutoff_at.isoformat()}"
        )
        code, payload = self._call_with_retry(f"{self._prom_url}/api/v1/query_range", params)
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        if code in {401, 403}:
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
                duration_ms=duration_ms,
                note="Prometheus returned unauthorized/forbidden.",
            )
        if code != 200:
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
                duration_ms=duration_ms,
                note=str(payload.get("error", f"HTTP {code}")),
            )

        results = payload.get("data", {}).get("result", [])
        points: list[MetricPoint] = []
        inline_requests = 0
        inline_errors = 0
        saw_inline_requests = False
        saw_inline_errors = False
        version_label = svc_cfg.telemetry.version_label

        for series_item in results:
            labels = series_item.get("metric", {})
            cohort = labels.get(version_label)
            samples = series_item.get("values", [])
            if not samples and "value" in series_item:
                samples = [series_item["value"]]
            for raw_point in samples:
                if len(raw_point) < 2:
                    continue
                raw_ts, raw_val = raw_point[0], raw_point[1]
                ts = _parse_telemetry_timestamp(raw_ts, cutoff_at.tzinfo)
                if not (window_start <= ts <= cutoff_at):
                    continue
                req_cnt: int | None = None
                err_cnt: int | None = None
                if len(raw_point) > 2 and raw_point[2] is not None:
                    req_cnt = int(raw_point[2])
                    inline_requests += req_cnt
                    saw_inline_requests = True
                if len(raw_point) > 3 and raw_point[3] is not None:
                    err_cnt = int(raw_point[3])
                    inline_errors += err_cnt
                    saw_inline_errors = True
                points.append(
                    MetricPoint(
                        timestamp=ts,
                        value=float(raw_val),
                        cohort=cohort,
                        request_count=req_cnt,
                        error_count=err_cnt,
                    )
                )

        points.sort(key=lambda p: p.timestamp)
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
                note="No Prometheus samples in requested window.",
            )

        total_requests: int | None = inline_requests if saw_inline_requests else None
        total_errors: int | None = inline_errors if saw_inline_errors else None
        if total_requests is None:
            total_requests = self._query_counter_sum(
                f'increase(http_requests_total{{service="{selector}",env="{env_sel}"}}[1m])',
                window_start,
                cutoff_at,
                step_seconds=step_seconds,
            )
        if total_errors is None:
            total_errors = self._query_counter_sum(
                f'increase(http_request_errors_total{{service="{selector}",env="{env_sel}"}}[1m])',
                window_start,
                cutoff_at,
                step_seconds=step_seconds,
            )

        expected_buckets = max(1, int((cutoff_at - window_start).total_seconds() // step_seconds) + 1)
        observed_buckets = len({int(p.timestamp.timestamp() // step_seconds) for p in points})
        temporal_coverage = min(1.0, round(observed_buckets / expected_buckets, 4))

        notes: list[str] = []
        if total_requests is None:
            notes.append("request_volume unknown (http_requests_total series unavailable)")
        if total_errors is None:
            notes.append("error_total unknown (http_request_errors_total series unavailable)")

        current_val = points[-1].value
        ref_target = cutoff_at - timedelta(minutes=15)
        closest_15m = min(points, key=lambda p: abs((p.timestamp - ref_target).total_seconds()))
        return MetricSeries(
            status=ConnectorStatus.OK,
            service=canonical,
            environment=environment,
            metric=metric,
            unit=unit,
            window_start=window_start,
            window_end=cutoff_at,
            reproducible_query=query_ref,
            coverage=temporal_coverage,
            points=points,
            current_value=current_val,
            value_15m_ago=closest_15m.value,
            request_volume=total_requests,
            error_total=total_errors,
            duration_ms=duration_ms,
            note="; ".join(notes),
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
        svc_cfg = self._catalog.get_service(canonical)
        effective_end = min(window_end, cutoff_at) if window_end is not None else cutoff_at
        effective_start = (
            window_start
            if window_start is not None
            else (effective_end - timedelta(minutes=window_minutes))
        )
        if effective_start > effective_end:
            effective_start = effective_end

        if svc_cfg is None or svc_cfg.environment.lower() != environment.lower():
            return LogQueryResult(
                status=ConnectorStatus.UNAUTHORIZED,
                service=canonical,
                environment=environment,
                pattern=pattern,
                window_start=effective_start,
                window_end=effective_end,
                reproducible_query=f"loki:{canonical}/{environment}",
                coverage=0.0,
                note=f"Service '{canonical}' in '{environment}' is not authorized in catalog.",
            )

        selector = svc_cfg.telemetry.service_selector
        env_sel = svc_cfg.telemetry.environment_selector
        version_label = svc_cfg.telemetry.version_label
        logql = f'{{service="{selector}",env="{env_sel}"}}'
        params = {
            "query": logql,
            "pattern": pattern,
            "start": effective_start.isoformat(),
            "end": effective_end.isoformat(),
        }
        if cohort is not None:
            params["cohort"] = cohort
        query_ref = (
            f"{self._loki_url}/loki/api/v1/query_range?query={logql}"
            f"&pattern={pattern}&start={effective_start.isoformat()}&end={effective_end.isoformat()}"
        )
        code, payload = self._call_with_retry(
            f"{self._loki_url}/loki/api/v1/query_range", params
        )
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        if code in {401, 403}:
            return LogQueryResult(
                status=ConnectorStatus.UNAUTHORIZED,
                service=canonical,
                environment=environment,
                pattern=pattern,
                window_start=effective_start,
                window_end=effective_end,
                reproducible_query=query_ref,
                coverage=0.0,
                duration_ms=duration_ms,
                note="Loki returned unauthorized/forbidden.",
            )
        if code != 200:
            return LogQueryResult(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                service=canonical,
                environment=environment,
                pattern=pattern,
                window_start=effective_start,
                window_end=effective_end,
                reproducible_query=query_ref,
                coverage=0.0,
                duration_ms=duration_ms,
                note=str(payload.get("error", f"HTTP {code}")),
            )

        streams = payload.get("data", {}).get("result", [])
        total = 0
        first_seen: datetime | None = None
        examples: list[str] = []
        cohort_counts: dict[str, int] = {}
        cohort_requests: dict[str, int] = {}
        lowered_pattern = pattern.lower()

        for stream in streams:
            labels = stream.get("stream", {})
            entry_cohort = str(labels.get(version_label, labels.get("cohort", "unknown")))
            if cohort is not None and entry_cohort != cohort:
                continue
            stream_level = str(labels.get("level", "")).upper()
            if "requests" in stream:
                cohort_requests[entry_cohort] = cohort_requests.get(entry_cohort, 0) + int(
                    stream["requests"]
                )
            for item in stream.get("values", []):
                raw_ts, message = item[0], str(item[1])
                count = int(item[2]) if len(item) > 2 else 1
                ts = _parse_telemetry_timestamp(raw_ts, effective_end.tzinfo)
                if not (effective_start <= ts <= effective_end):
                    continue
                if "requests" in message.lower() or "req/min" in message.lower():
                    cohort_requests[entry_cohort] = cohort_requests.get(entry_cohort, 0) + count
                if lowered_pattern == "*":
                    is_err = (
                        stream_level == "ERROR"
                        or '"level":"error"' in message.lower()
                        or '"level": "error"' in message.lower()
                        or "error" in message.lower()
                        or "exhausted" in message.lower()
                        or "timeout" in message.lower()
                        or "saturat" in message.lower()
                    )
                    if not is_err:
                        continue
                elif lowered_pattern not in message.lower():
                    continue
                total += count
                cohort_counts[entry_cohort] = cohort_counts.get(entry_cohort, 0) + count
                if first_seen is None or ts < first_seen:
                    first_seen = ts
                if len(examples) < 3:
                    examples.append(
                        redact_sensitive_text(f"{ts.isoformat()} [{entry_cohort}] {message}")
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
            numerator_kind="log_events_per_request",
            error_definition=pattern,
        )

