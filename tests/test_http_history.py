"""Historical export contract: service isolation, interval boundaries and missing data."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vigia.adapters.outbound.http_telemetry import RestTelemetryApiAdapter
from vigia.adapters.outbound.service_catalog import YamlServiceCatalogAdapter
from vigia.domain.contracts import ConnectorStatus


START = datetime(2026, 11, 13, 19, 44, 21, 416540, tzinfo=timezone.utc)


def bucket(start, end, value=19.468, requests=300, errors=0):
    return {
        "desde_utc": (START + timedelta(minutes=start)).isoformat(),
        "hasta_utc": (START + timedelta(minutes=end)).isoformat(),
        "peticiones": requests, "errores": errors,
        "tasa_error": errors / requests if requests else 0,
        "rps": requests / ((end - start) * 60), "latencia_p95_ms": value,
    }


def query(points, *, minutes=5, service="consulta_estado_cuenta", metric="latency_p95_ms"):
    catalog = YamlServiceCatalogAdapter.from_yaml(Path(__file__).resolve().parents[1] / "config/services.yaml")
    calls = []

    def get(url, params, headers):
        calls.append((url, params))
        return 200, {
            "version": 1, "fuente": "banco", "intervalo_minutos": 1,
            "desde_utc": START.isoformat(),
            "hasta_utc": (START + timedelta(minutes=minutes)).isoformat(),
            "servicios": [
                {"servicio": "transferencias", "puntos": [bucket(0, 1, 9999)]},
                {"servicio": "consulta_estado_cuenta", "puntos": points},
                {"servicio": "depositos", "puntos": []},
            ],
        }

    adapter = RestTelemetryApiAdapter(
        catalog, metrics_url="http://metrics:8009/api/v1/metricas/historico.txt", http_get=get,
    )
    # Same instants expressed in Lima time must give the same bounded series.
    cutoff = (START + timedelta(minutes=minutes)).astimezone(timezone(timedelta(hours=-5)))
    return adapter.query_metrics(service, "production", metric, minutes, cutoff), calls


def test_historical_export_bounds_deduplicates_and_sums_only_selected_service():
    series, calls = query([
        bucket(3, 4, 67.44, 25), bucket(0, 1), bucket(1, 2, 17.123),
        bucket(0, 1), bucket(-1, 0, 999), bucket(-0.5, 0.5, 999),
        bucket(5, 6, 999), bucket(4, 5, None, 100, 100),
    ])
    assert calls == [("http://metrics:8009/api/v1/metricas/historico.txt", {})]
    assert series.status == ConnectorStatus.OK
    assert series.service == "consulta_estado_cuenta"
    assert [p.value for p in series.points] == [19.468, 17.123, 67.44]
    assert series.points[-1].timestamp == START + timedelta(minutes=4)
    assert series.points[-1].window_start == START + timedelta(minutes=3)
    assert series.points[-1].window_end == series.points[-1].timestamp
    assert series.points[-1].request_count == 25
    assert series.current_value == 67.44
    assert series.request_volume == 725
    assert series.error_total == 100  # Errors without a measured p95 must remain visible.
    assert series.coverage == 0.6
    assert series.value_15m_ago is None


@pytest.mark.parametrize("points,volume,errors", [
    ([], None, None),
    ([bucket(0, 1, None, 100, 100)], 100, 100),
    ([bucket(0, 1, 0, 0, 0)], 0, 0),
])
def test_empty_or_unmeasured_history_never_creates_zero_latency(points, volume, errors):
    series, _ = query(points)
    assert series.status == ConnectorStatus.NO_RESULTS
    assert series.points == [] and series.current_value is None
    assert series.request_volume == volume and series.error_total == errors
    assert series.coverage == 0


def test_invalid_history_does_not_discard_valid_intervals_or_invent_unknown_counters():
    unknown = bucket(1, 2, 17)
    unknown.pop("peticiones")
    unknown.pop("errores")
    series, _ = query([bucket(0, 1), {"desde_utc": "invalid"}, bucket(2, 3, float("nan")), unknown])
    assert series.status == ConnectorStatus.OK
    assert len(series.points) == 2
    assert series.request_volume is None and series.error_total is None
    assert "Ignored 2 invalid interval(s)" in series.note


def test_fifteen_minute_reference_requires_an_interval_at_the_target():
    series, _ = query([bucket(0, 1, 10), bucket(29, 30, 20)], minutes=30)
    assert series.value_15m_ago is None
    series, _ = query([bucket(14, 15, 17), bucket(29, 30, 20)], minutes=30)
    assert series.value_15m_ago == 17


def test_p95_is_not_reused_for_an_unavailable_metric():
    series, calls = query([bucket(0, 1)], metric="db_pool_utilization")
    assert series.status == ConnectorStatus.NO_RESULTS
    assert series.current_value is None and calls == []
