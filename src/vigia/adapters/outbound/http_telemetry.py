"""Prometheus metrics and Loki logs adapter with bounded retries and anomaly reuse."""

from __future__ import annotations

import json
import math
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

HttpGetFn = Callable[[str, dict[str, str], dict[str, str]], tuple[int, Any]]


def _build_auth_headers(auth_token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    return headers


def _normalize_rest_endpoint(raw_url: str | None, default_path: str) -> str | None:
    """Strip query parameters from a configured endpoint URL and ensure a path."""
    if not raw_url:
        return None
    cleaned = raw_url.strip()
    if not cleaned:
        return None
    parts = urllib.parse.urlsplit(cleaned)
    if parts.scheme and parts.netloc:
        path = parts.path.rstrip("/") or default_path
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return cleaned.rstrip("/")


def _is_error_log_entry(
    message_lower: str,
    *,
    level: str = "",
    http_status: int | None = None,
) -> bool:
    if http_status is not None and http_status >= 500:
        return True
    if level == "ERROR":
        return True
    return (
        '"level":"error"' in message_lower
        or '"level": "error"' in message_lower
        or "error" in message_lower
        or "exhausted" in message_lower
        or "timeout" in message_lower
        or "saturat" in message_lower
    )


def _finalize_metric_series(
    *,
    canonical: str,
    environment: str,
    metric: str,
    unit: str,
    window_start: datetime,
    cutoff_at: datetime,
    query_ref: str,
    points: list[MetricPoint],
    request_volume: int | None,
    error_total: int | None,
    duration_ms: float,
    step_seconds: int = 60,
    notes: list[str] | None = None,
    coverage: float | None = None,
) -> MetricSeries:
    expected_buckets = max(1, int((cutoff_at - window_start).total_seconds() // step_seconds) + 1)
    observed_buckets = len({int(p.timestamp.timestamp() // step_seconds) for p in points})
    temporal_coverage = min(1.0, round(observed_buckets / expected_buckets, 4))
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
        coverage=temporal_coverage if coverage is None else coverage,
        points=points,
        current_value=current_val,
        value_15m_ago=closest_15m.value,
        request_volume=request_volume,
        error_total=error_total,
        duration_ms=duration_ms,
        note="; ".join(notes or []),
    )


def _finalize_log_query_result(
    *,
    canonical: str,
    environment: str,
    pattern: str,
    effective_start: datetime,
    effective_end: datetime,
    query_ref: str,
    total: int,
    first_seen: datetime | None,
    examples: list[str],
    cohort_counts: dict[str, int],
    cohort_requests: dict[str, int],
    cohort: str | None,
    duration_ms: float,
) -> LogQueryResult:
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


class _RetryJsonClient:
    """Bounded-retry HTTP JSON client shared by outbound telemetry adapters."""

    def __init__(
        self,
        *,
        auth_token: str | None = None,
        http_get: HttpGetFn | None = None,
        max_retries_per_case: int = 2,
    ) -> None:
        self._auth_token = auth_token
        self._http_get = http_get or _default_http_get
        self._max_retries = max_retries_per_case
        self._retries_used = 0

    def headers(self) -> dict[str, str]:
        return _build_auth_headers(self._auth_token)

    def get_json(self, url: str, params: dict[str, str]) -> tuple[int, Any]:
        while True:
            try:
                status_code, payload = self._http_get(url, params, self.headers())
            except (TimeoutError, ConnectionError, OSError) as exc:
                status_code, payload = 503, {"error": str(exc)}

            if status_code in {502, 503, 504} and self._retries_used < self._max_retries:
                self._retries_used += 1
                continue
            return status_code, payload


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
        self._client = _RetryJsonClient(
            auth_token=auth_token,
            http_get=http_get,
            max_retries_per_case=max_retries_per_case,
        )

    def _headers(self) -> dict[str, str]:
        return self._client.headers()

    def _call_with_retry(
        self,
        url: str,
        params: dict[str, str],
    ) -> tuple[int, Any]:
        return self._client.get_json(url, params)

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

        notes: list[str] = []
        if total_requests is None:
            notes.append("request_volume unknown (http_requests_total series unavailable)")
        if total_errors is None:
            notes.append("error_total unknown (http_request_errors_total series unavailable)")

        return _finalize_metric_series(
            canonical=canonical,
            environment=environment,
            metric=metric,
            unit=unit,
            window_start=window_start,
            cutoff_at=cutoff_at,
            query_ref=query_ref,
            points=points,
            request_volume=total_requests,
            error_total=total_errors,
            duration_ms=duration_ms,
            step_seconds=step_seconds,
            notes=notes,
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
                    if not _is_error_log_entry(message.lower(), level=stream_level):
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

        return _finalize_log_query_result(
            canonical=canonical,
            environment=environment,
            pattern=pattern,
            effective_start=effective_start,
            effective_end=effective_end,
            query_ref=query_ref,
            total=total,
            first_seen=first_seen,
            examples=examples,
            cohort_counts=cohort_counts,
            cohort_requests=cohort_requests,
            cohort=cohort,
            duration_ms=duration_ms,
        )


class RestTelemetryApiAdapter:
    """Queries custom HTTP REST telemetry endpoints (/api/metricas and /api/logs)."""

    def __init__(
        self,
        catalog: ServiceCatalogPort,
        *,
        metrics_url: str | None = None,
        logs_url: str | None = None,
        auth_token: str | None = None,
        http_get: HttpGetFn | None = None,
        max_retries_per_case: int = 2,
        log_limit: int = 500,
        metrics_service_map: dict[str, str] | None = None,
    ) -> None:
        self._catalog = catalog
        self._metrics_url = _normalize_rest_endpoint(metrics_url, "/api/metricas")
        self._logs_url = _normalize_rest_endpoint(logs_url, "/api/logs")
        self._client = _RetryJsonClient(
            auth_token=auth_token,
            http_get=http_get,
            max_retries_per_case=max_retries_per_case,
        )
        self._log_limit = max(1, log_limit)
        self._metrics_service_map = {k.lower(): v for k, v in (metrics_service_map or {}).items()}

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
        unit = svc_cfg.telemetry.metric_unit if svc_cfg else "ms"

        if svc_cfg is None or svc_cfg.environment.lower() != environment.lower():
            return MetricSeries(
                status=ConnectorStatus.UNAUTHORIZED,
                service=canonical,
                environment=environment,
                metric=metric,
                unit=unit,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=f"rest-metrics:{canonical}/{environment}",
                coverage=0.0,
                note=f"Service '{canonical}' in '{environment}' is not authorized in catalog.",
            )

        if not self._metrics_url:
            return MetricSeries(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                service=canonical,
                environment=environment,
                metric=metric,
                unit=unit,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=f"rest-metrics:{canonical}/{environment}",
                coverage=0.0,
                note="Metrics API URL is not configured.",
            )
        if metric != svc_cfg.telemetry.impact_metric:
            return MetricSeries(
                status=ConnectorStatus.NO_RESULTS, service=canonical, environment=environment,
                metric=metric, unit=unit, window_start=window_start, window_end=cutoff_at,
                reproducible_query=self._metrics_url, coverage=0.0,
                note=f"The REST latency endpoint does not provide metric '{metric}'.",
            )

        requested_minutes = max(1, min(window_minutes, 60))
        api_service = self._metrics_service_map.get(canonical.lower(), canonical)
        # The historical export contains every service; selection and windowing are local.
        historical_export = urllib.parse.urlparse(self._metrics_url).path.endswith("/historico.txt")
        params = {} if historical_export else {"servicio": api_service, "minutos": str(requested_minutes)}
        query_ref = self._metrics_url + (f"?{urllib.parse.urlencode(params)}" if params else "")
        code, payload = self._client.get_json(self._metrics_url, params)
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
                note="Metrics API returned unauthorized/forbidden.",
            )
        if code != 200 or not isinstance(payload, dict):
            err_msg = payload.get("error", f"HTTP {code}") if isinstance(payload, dict) else f"HTTP {code}"
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
                note=str(err_msg),
            )

        if isinstance(payload.get("servicios"), list):
            payload = next(
                (item for item in payload["servicios"] if isinstance(item, dict)
                 and str(item.get("servicio", "")).lower() == api_service.lower()),
                {},
            )
        resolved_unit = str(payload.get("unidad") or unit)
        raw_points = payload.get("puntos", [])
        if isinstance(raw_points, list) and any(isinstance(item, dict) for item in raw_points):
            return self._historical_series(
                raw_points, canonical=canonical, environment=environment, metric=metric,
                unit=resolved_unit, window_start=window_start, cutoff_at=cutoff_at,
                query_ref=query_ref, duration_ms=duration_ms,
            )
        points: list[MetricPoint] = []
        if isinstance(raw_points, list):
            for item in raw_points:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                ts = _parse_telemetry_timestamp(item[0], cutoff_at.tzinfo)
                if window_start <= ts <= cutoff_at:
                    points.append(MetricPoint(timestamp=ts, value=float(item[1])))
        points.sort(key=lambda p: p.timestamp)

        if not points:
            return MetricSeries(
                status=ConnectorStatus.NO_RESULTS,
                service=canonical,
                environment=environment,
                metric=metric,
                unit=resolved_unit,
                window_start=window_start,
                window_end=cutoff_at,
                reproducible_query=query_ref,
                coverage=0.0,
                duration_ms=duration_ms,
                note="No metrics samples in requested window.",
            )

        raw_requests = payload.get("consultas_sesion")
        raw_errors = payload.get("errores")
        request_volume = int(raw_requests) if raw_requests is not None else None
        error_total = int(raw_errors) if raw_errors is not None else None
        return _finalize_metric_series(
            canonical=canonical,
            environment=environment,
            metric=metric,
            unit=resolved_unit,
            window_start=window_start,
            cutoff_at=cutoff_at,
            query_ref=query_ref,
            points=points,
            request_volume=request_volume,
            error_total=error_total,
            duration_ms=duration_ms,
        )

    def _historical_series(
        self, raw_points: list, *, canonical: str, environment: str, metric: str, unit: str,
        window_start: datetime, cutoff_at: datetime, query_ref: str, duration_ms: float,
    ) -> MetricSeries:
        """Read complete buckets available at the case cutoff, preserving their measurement interval."""
        points: list[MetricPoint] = []
        intervals: set[tuple[datetime, datetime]] = set()
        requests: list[int | None] = []
        errors: list[int | None] = []
        invalid = 0
        for item in raw_points:
            if not isinstance(item, dict):
                invalid += 1
                continue
            try:
                start = _parse_telemetry_timestamp(item["desde_utc"], cutoff_at.tzinfo)
                end = _parse_telemetry_timestamp(item["hasta_utc"], cutoff_at.tzinfo)
                if end <= start:
                    raise ValueError("Invalid measurement interval")
                if start < window_start or end > cutoff_at or (start, end) in intervals:
                    continue
                request_count = item.get("peticiones")
                error_count = item.get("errores")
                if any(value is not None and (type(value) is not int or value < 0)
                       for value in (request_count, error_count)):
                    raise ValueError("Invalid counters")
                if request_count is not None and error_count is not None and error_count > request_count:
                    raise ValueError("Errors exceed request count")
                value = item.get("latencia_p95_ms")
                if value is not None:
                    value = float(value)
                    if not math.isfinite(value) or value < 0:
                        raise ValueError("Invalid latency")
            except (KeyError, TypeError, ValueError, OverflowError):
                invalid += 1
                continue
            intervals.add((start, end))
            requests.append(request_count)
            errors.append(error_count)
            if value is not None and request_count != 0:
                points.append(MetricPoint(
                    timestamp=end, value=value, request_count=request_count, error_count=error_count,
                    window_start=start, window_end=end,
                ))
        points.sort(key=lambda point: point.timestamp)
        request_volume = sum(requests) if requests and all(v is not None for v in requests) else None
        error_total = sum(errors) if errors and all(v is not None for v in errors) else None
        # Coverage is the union of measured intervals. Gaps and overlaps retain their actual duration.
        measured_seconds = 0.0
        previous_end = window_start
        for point in sorted(points, key=lambda point: point.window_start):
            start = max(previous_end, point.window_start)
            measured_seconds += max(0, (point.window_end - start).total_seconds())
            previous_end = max(previous_end, point.window_end)
        coverage = min(1.0, measured_seconds / max(1, (cutoff_at - window_start).total_seconds()))
        notes = ["Historical p95 buckets are timestamped at hasta_utc. Only complete intervals are included."]
        if invalid:
            notes.append(f"Ignored {invalid} invalid interval(s).")
        if not points:
            return MetricSeries(
                status=ConnectorStatus.NO_RESULTS, service=canonical, environment=environment,
                metric=metric, unit=unit, window_start=window_start, window_end=cutoff_at,
                reproducible_query=query_ref, coverage=0.0, request_volume=request_volume,
                error_total=error_total, duration_ms=duration_ms,
                note="No measured latency in complete intervals of the requested window. " + " ".join(notes),
            )
        series = _finalize_metric_series(
            canonical=canonical, environment=environment, metric=metric, unit=unit,
            window_start=window_start, cutoff_at=cutoff_at, query_ref=query_ref, points=points,
            request_volume=request_volume, error_total=error_total, duration_ms=duration_ms,
            coverage=round(coverage, 4), notes=notes,
        )
        ref_target = cutoff_at - timedelta(minutes=15)
        reference = next((p.value for p in points if p.window_start < ref_target <= p.window_end), None)
        return series.model_copy(update={"value_15m_ago": reference})

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
                reproducible_query=f"rest-logs:{canonical}/{environment}",
                coverage=0.0,
                note=f"Service '{canonical}' in '{environment}' is not authorized in catalog.",
            )

        if not self._logs_url:
            return LogQueryResult(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                service=canonical,
                environment=environment,
                pattern=pattern,
                window_start=effective_start,
                window_end=effective_end,
                reproducible_query=f"rest-logs:{canonical}/{environment}",
                coverage=0.0,
                note="Logs API URL is not configured.",
            )

        params = {"servicio": canonical, "limite": str(self._log_limit)}
        query_ref = f"{self._logs_url}?{urllib.parse.urlencode(params)}"
        code, payload = self._client.get_json(self._logs_url, params)
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
                note="Logs API returned unauthorized/forbidden.",
            )
        if code != 200:
            err_msg = payload.get("error", f"HTTP {code}") if isinstance(payload, dict) else f"HTTP {code}"
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
                note=str(err_msg),
            )

        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict) and isinstance(payload.get("logs"), list):
            entries = payload["logs"]
        else:
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
                note="Unexpected Logs API response format.",
            )

        total = 0
        first_seen: datetime | None = None
        examples: list[str] = []
        cohort_counts: dict[str, int] = {}
        cohort_requests: dict[str, int] = {}
        lowered_pattern = pattern.lower()

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_service = entry.get("servicio") or entry.get("service")
            if entry_service is not None:
                resolved_entry_svc = self._catalog.resolve_canonical_name(str(entry_service))
                if resolved_entry_svc.lower() != canonical.lower():
                    continue
            raw_ts = entry.get("fecha") or entry.get("timestamp")
            if raw_ts is None:
                continue
            ts = _parse_telemetry_timestamp(raw_ts, effective_end.tzinfo)
            if not (effective_start <= ts <= effective_end):
                continue

            entry_cohort = str(
                entry.get("cohort") or entry.get("cohorte") or entry.get("fase") or "default"
            )
            if cohort is not None and entry_cohort != cohort:
                continue

            cohort_requests[entry_cohort] = cohort_requests.get(entry_cohort, 0) + 1

            raw_status = entry.get("status")
            http_status = int(raw_status) if isinstance(raw_status, (int, float)) else None
            level = str(entry.get("nivel") or entry.get("level") or "").upper()
            parts = [
                f"{entry.get('metodo', '')} {entry.get('endpoint', '')}".strip(),
                f"status={raw_status}" if raw_status is not None else "",
                f"latencia_ms={entry['latencia_ms']}" if entry.get("latencia_ms") is not None else "",
                f"fase={entry['fase']}" if entry.get("fase") is not None else "",
                str(entry.get("mensaje") or entry.get("message") or entry.get("error") or ""),
            ]
            message = " ".join(part for part in parts if part).strip()
            searchable_lower = message.lower()

            if lowered_pattern == "*":
                if not _is_error_log_entry(searchable_lower, level=level, http_status=http_status):
                    continue
            elif lowered_pattern not in searchable_lower:
                continue

            total += 1
            cohort_counts[entry_cohort] = cohort_counts.get(entry_cohort, 0) + 1
            if first_seen is None or ts < first_seen:
                first_seen = ts
            if len(examples) < 3:
                examples.append(
                    redact_sensitive_text(f"{ts.isoformat()} [{entry_cohort}] {message}")
                )

        return _finalize_log_query_result(
            canonical=canonical,
            environment=environment,
            pattern=pattern,
            effective_start=effective_start,
            effective_end=effective_end,
            query_ref=query_ref,
            total=total,
            first_seen=first_seen,
            examples=examples,
            cohort_counts=cohort_counts,
            cohort_requests=cohort_requests,
            cohort=cohort,
            duration_ms=duration_ms,
        )

