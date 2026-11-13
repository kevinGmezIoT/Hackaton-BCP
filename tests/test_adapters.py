"""Unit and integration tests for outbound adapters."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from conftest import TransfersWorkspace

from vigia.adapters.outbound.drawio import DrawioArchitectureAdapter
from vigia.adapters.outbound.executor import SimulationState
from vigia.adapters.outbound.http_telemetry import RestTelemetryApiAdapter
from vigia.adapters.outbound.knowledge import LocalKnowledgeAdapter
from vigia.adapters.outbound.local_git import JsonReleaseSourceAdapter, LocalGitRepositoryAdapter
from vigia.adapters.outbound.service_catalog import YamlServiceCatalogAdapter
from vigia.adapters.outbound.telemetry import CsvTelemetryAdapter
from vigia.domain.contracts import ConnectorStatus, RelationType


def test_account_statement_is_canonical_in_pilot_catalog():
    root = Path(__file__).resolve().parents[1]
    catalog = YamlServiceCatalogAdapter.from_yaml(root / "config/services.yaml")
    canonical = catalog.get_service("consulta_estado_cuenta")
    assert canonical.service == "consulta_estado_cuenta"
    assert catalog.get_service("transferencias").service == "consulta_estado_cuenta"
    assert canonical.telemetry.service_selector == "consulta_estado_cuenta"
    assert [svc.service for svc in catalog.list_services()] == ["consulta_estado_cuenta"]
    assert catalog.get_service("pagos-core") is None
    seen = []
    def http_get(url, params, headers):
        seen.append(params)
        return 200, {"servicios": [{"servicio": "consulta_estado_cuenta", "puntos": []}]}
    adapter = RestTelemetryApiAdapter(
        catalog, metrics_url="http://metrics/api/v1/metricas", http_get=http_get)
    series = adapter.query_metrics("transferencias", "production", "latency_p95_ms", 1,
                                   datetime.fromisoformat("2026-11-13T12:00:00-05:00"))
    assert seen == [{"servicio": "consulta_estado_cuenta", "minutos": "1"}]
    assert series.service == "consulta_estado_cuenta"


def test_pilot_runtime_uses_its_own_architecture_and_rejects_legacy_service():
    from vigia.runtime import PilotRuntimeConfig, build_pilot_case_service

    root = Path(__file__).resolve().parents[1]
    calls = []

    def http_get(url, params, headers):
        calls.append((url, params))
        return 200, []

    service, _, _ = build_pilot_case_service(
        PilotRuntimeConfig(
            catalog_path=root / "config/services.yaml", fixtures_dir=root / "src/vigia/demo",
            incidents_path=root / "config/incidents.json", runbooks_dir=root / "config/runbooks",
            logs_api_url="http://logs/api/logs", http_get=http_get,
        ), model="unused",
    )
    try:
        dependencies = service._deps.architecture.query_dependencies("consulta_estado_cuenta", "production")
        assert dependencies.status == ConnectorStatus.NO_RESULTS
        assert dependencies.relations == []
        assert "consulta_estado_cuenta.drawio" in dependencies.note
        assert service._deps.knowledge.search_similar_incidents("pagos-core pool exhausted") == []
        assert service._deps.knowledge.search_runbook("pool rollback") == []
        cutoff = datetime.fromisoformat("2026-11-13T20:14:00+00:00")
        result = service._deps.telemetry.search_logs("pagos-core", "production", "*", 60, cutoff)
        assert result.status == ConnectorStatus.UNAUTHORIZED
        assert calls == []
        service._deps.telemetry.search_logs("consulta_estado_cuenta", "production", "*", 60, cutoff)
        assert calls == [("http://logs/api/logs", {"servicio": "consulta_estado_cuenta", "limite": "500"})]
    finally:
        service.close()


def test_v1_metrics_selects_mapped_service_and_keeps_domain_identity(transfers_workspace):
    catalog = YamlServiceCatalogAdapter.from_yaml(transfers_workspace.fixtures_dir / "services.yaml")
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    requests = []
    def http_get(url, params, headers):
        requests.append((url, params))
        return 200, {"version": 1, "servicios": [
            {"servicio": "otro_servicio", "puntos": [[cutoff.isoformat(), 999]]},
            {"servicio": "consulta_estado_cuenta", "unidad": "ms",
             "puntos": [[cutoff.isoformat(), 211]]},
        ]}
    adapter = RestTelemetryApiAdapter(
        catalog, metrics_url="http://server:8009/api/v1/metricas",
        metrics_service_map={"transferencias": "consulta_estado_cuenta"}, http_get=http_get)
    series = adapter.query_metrics("Transferencias", "production", "latency_p95_ms", 1, cutoff)
    assert series.status == ConnectorStatus.OK
    assert series.service == "transferencias" and series.current_value == 211
    assert requests[0] == ("http://server:8009/api/v1/metricas",
                            {"servicio": "consulta_estado_cuenta", "minutos": "1"})
    unmapped = adapter.query_metrics("pagos-core", "production", "latency_p95_ms", 1, cutoff)
    assert unmapped.status == ConnectorStatus.NO_RESULTS
    assert not unmapped.points


def test_git_adapter_reads_deployed_diff_and_enforces_boundaries(
    transfers_workspace: TransfersWorkspace,
) -> None:
    adapter = LocalGitRepositoryAdapter(
        {"applications/pagos-core": transfers_workspace.repo_dir},
        max_patch_lines=5,
    )

    diff = adapter.compare_revisions(
        "applications/pagos-core",
        transfers_workspace.base_sha,
        transfers_workspace.deployed_sha,
    )
    assert diff.status == ConnectorStatus.OK
    assert len(diff.files) == 1
    assert diff.files[0].path == "src/pool.py"
    assert diff.files[0].truncated is True

    snippet = adapter.read_snippet(
        "applications/pagos-core",
        transfers_workspace.deployed_sha,
        "src/pool.py",
        start_line=1,
        end_line=10,
    )
    assert snippet.status == ConnectorStatus.OK
    assert '"pool_size": 50' in snippet.content

    unauthorized_repo = adapter.compare_revisions(
        "applications/unknown-repo",
        transfers_workspace.base_sha,
        transfers_workspace.deployed_sha,
    )
    assert unauthorized_repo.status == ConnectorStatus.UNAUTHORIZED

    traversal = adapter.read_snippet(
        "applications/pagos-core",
        transfers_workspace.deployed_sha,
        "../secrets.env",
    )
    assert traversal.status == ConnectorStatus.UNAUTHORIZED


def test_release_source_excludes_staging_commit_from_production(
    transfers_workspace: TransfersWorkspace,
) -> None:
    adapter = JsonReleaseSourceAdapter(transfers_workspace.fixtures_dir / "releases.json")
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")

    prod = adapter.resolve_active_releases("pagos-core", "production", cutoff)
    assert prod.status == ConnectorStatus.OK
    assert [r.release_id for r in prod.releases] == ["CHG-4821"]
    assert prod.releases[0].deployed_sha == transfers_workspace.deployed_sha


def test_drawio_adapter_extracts_directed_ambiguous_and_rejects_missing_revision(
    transfers_workspace: TransfersWorkspace,
) -> None:
    catalog = YamlServiceCatalogAdapter.from_yaml(
        transfers_workspace.fixtures_dir / "services.yaml"
    )
    adapter = DrawioArchitectureAdapter(
        {"transferencias": transfers_workspace.fixtures_dir / "docs" / "transfers.drawio"},
        catalog,
    )

    result = adapter.query_dependencies("Transferencias", "production", direction="outbound")
    assert result.status == ConnectorStatus.OK
    assert len(result.relations) == 1
    rel = result.relations[0]
    assert rel.source_service == "transferencias"
    assert rel.target_service == "pagos-core"
    assert rel.relation_type == RelationType.CALLS
    assert rel.ambiguous is False
    assert rel.revision is not None and rel.revision.startswith("sha256:")

    missing_rev = adapter.query_dependencies(
        "Transferencias",
        "production",
        version="nonexistent-rev-999",
        direction="outbound",
    )
    assert missing_rev.status == ConnectorStatus.NO_RESULTS
    assert missing_rev.relations == []

    ambiguous_xml = transfers_workspace.root / "ambiguous.drawio"
    ambiguous_xml.write_text(
        '<mxfile><diagram name="p1"><mxGraphModel><root>'
        '<mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="a" value="Transferencias" vertex="1" parent="1"/>'
        '<mxCell id="b" value="pagos-core" vertex="1" parent="1"/>'
        '<mxCell id="e1" value="" style="endArrow=none;" edge="1" parent="1" source="a" target="b"/>'
        "</root></mxGraphModel></diagram></mxfile>",
        encoding="utf-8",
    )
    ambiguous_adapter = DrawioArchitectureAdapter({"transferencias": ambiguous_xml}, catalog)
    amb_res = ambiguous_adapter.query_dependencies("transferencias", "production")
    assert amb_res.status == ConnectorStatus.OK
    assert amb_res.relations[0].ambiguous is True


def test_telemetry_adapter_enforces_cutoff_detects_anomaly_and_redacts_logs(
    transfers_workspace: TransfersWorkspace,
) -> None:
    catalog = YamlServiceCatalogAdapter.from_yaml(
        transfers_workspace.fixtures_dir / "services.yaml"
    )
    sim_state = SimulationState()
    telemetry = CsvTelemetryAdapter(
        transfers_workspace.fixtures_dir / "metrics.csv",
        transfers_workspace.fixtures_dir / "logs.txt",
        catalog,
        simulation_state=sim_state,
    )
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")

    series = telemetry.query_metrics("Transferencias", "production", "latency_p95_ms", 40, cutoff)
    assert series.status == ConnectorStatus.OK
    assert series.current_value == 420.0

    anomaly = telemetry.detect_anomaly("Transferencias", "production", "latency_p95_ms", cutoff)
    assert anomaly.status == ConnectorStatus.OK
    assert anomaly.is_anomalous is True
    assert anomaly.projected_minutes_to_threshold is not None
    assert 20.0 <= anomaly.projected_minutes_to_threshold <= 26.0

    logs = telemetry.search_logs("pagos-core", "production", "Connection pool exhausted", 30, cutoff)
    assert logs.status == ConnectorStatus.OK
    assert logs.total == 312
    assert logs.cohort_counts == {"CHG-4821": 312}

    before_cutoff = datetime.fromisoformat("2026-11-12T08:55:00-05:00")
    before_logs = telemetry.search_logs(
        "pagos-core", "production", "Connection pool exhausted", 30, before_cutoff
    )
    assert before_logs.status == ConnectorStatus.NO_RESULTS
    assert before_logs.total == 0


def test_knowledge_adapter_discriminates_relevant_incidents_and_runbook(
    transfers_workspace: TransfersWorkspace,
) -> None:
    knowledge = LocalKnowledgeAdapter(
        transfers_workspace.fixtures_dir / "incidents.json",
        transfers_workspace.fixtures_dir / "runbooks",
    )

    incidents = knowledge.search_similar_incidents(
        "Connection pool exhausted in pagos-core after pool_size reduction causing Transferencias latency",
        k=3,
    )
    top_ids = {inc.incident_id for inc in incidents[:2]}
    assert top_ids == {"INC-3512", "INC-3307"}

    runbooks = knowledge.search_runbook("Connection pool exhausted rollback")
    assert runbooks
    assert runbooks[0].runbook_id == "RB-087"


def test_rest_telemetry_adapter_queries_metrics_detects_anomaly_and_searches_logs(
    transfers_workspace: TransfersWorkspace,
) -> None:
    catalog = YamlServiceCatalogAdapter.from_yaml(
        transfers_workspace.fixtures_dir / "services.yaml"
    )
    seen_calls: list[tuple[str, dict[str, str]]] = []

    def fake_http_get(url: str, params: dict[str, str], headers: dict[str, str]):
        seen_calls.append((url, dict(params)))
        if url.endswith("/api/metricas"):
            return 200, {
                "servicio": "transferencias",
                "unidad": "ms",
                "consultas_sesion": 3000,
                "errores": 12,
                "puntos": [
                    ["2026-11-13T17:30:00+00:00", 180],
                    ["2026-11-13T17:31:00+00:00", 182],
                    ["2026-11-13T17:32:00+00:00", 179],
                    ["2026-11-13T17:54:00+00:00", 520],
                    ["2026-11-13T17:55:00+00:00", 590],
                    ["2026-11-13T17:56:00+00:00", 660],
                    ["2026-11-13T18:10:00+00:00", 999],
                ],
            }
        if url.endswith("/api/logs"):
            return 200, [
                {
                    "fecha": "2026-11-13T17:55:30+00:00",
                    "servicio": "transferencias",
                    "endpoint": "/api/operaciones/transferencia",
                    "metodo": "POST",
                    "status": 504,
                    "latencia_ms": 1200,
                    "fase": "degradacion",
                    "mensaje": "Upstream timeout token=secret-abc",
                },
                {
                    "fecha": "2026-11-13T17:55:45+00:00",
                    "servicio": "transferencias",
                    "endpoint": "/api/operaciones/transferencia",
                    "metodo": "POST",
                    "status": 201,
                    "latencia_ms": 188,
                    "fase": "degradacion",
                },
                {
                    "fecha": "2026-11-13T18:05:00+00:00",
                    "servicio": "transferencias",
                    "endpoint": "/api/operaciones/transferencia",
                    "metodo": "POST",
                    "status": 500,
                    "latencia_ms": 1500,
                    "fase": "degradacion",
                },
            ]
        return 404, {"error": "not found"}

    adapter = RestTelemetryApiAdapter(
        catalog,
        metrics_url="http://10.233.43.117:8010/api/metricas?servicio=transferencias&minutos=1",
        logs_url="http://10.233.43.117:8010/api/logs?servicio=transferencias&limite=10",
        http_get=fake_http_get,
    )
    cutoff = datetime.fromisoformat("2026-11-13T17:56:00+00:00")

    series = adapter.query_metrics("Transferencias", "production", "latency_p95_ms", 90, cutoff)
    assert series.status == ConnectorStatus.OK
    assert series.current_value == 660.0
    assert series.request_volume == 3000
    assert series.error_total == 12
    assert seen_calls[0] == (
        "http://10.233.43.117:8010/api/metricas",
        {"servicio": "transferencias", "minutos": "60"},
    )

    anomaly = adapter.detect_anomaly("Transferencias", "production", "latency_p95_ms", cutoff)
    assert anomaly.status == ConnectorStatus.OK
    assert anomaly.is_anomalous is True

    logs = adapter.search_logs("Transferencias", "production", "*", 30, cutoff)
    assert logs.status == ConnectorStatus.OK
    assert logs.total == 1
    assert logs.cohort_requests == {"degradacion": 2}
    assert logs.cohort_error_rates == {"degradacion": 0.5}
    assert "[REDACTED]" in logs.examples[0]
    assert seen_calls[-1][0] == "http://10.233.43.117:8010/api/logs"

    unauthorized = adapter.query_metrics("unknown-svc", "production", "latency_p95_ms", 15, cutoff)
    assert unauthorized.status == ConnectorStatus.UNAUTHORIZED


def test_runtime_wires_rest_telemetry_api_from_env(
    transfers_workspace: TransfersWorkspace,
    monkeypatch,
) -> None:
    import argparse
    from vigia.domain.contracts import OperationalMode, RuntimeProfile
    from vigia.entrypoints.cli import _build_runtime_config
    from vigia.runtime import build_pilot_case_service

    monkeypatch.setenv("VIGIA_METRICS_API_URL", "http://10.233.43.117:8010/api/metricas")
    monkeypatch.setenv("VIGIA_LOGS_API_URL", "http://10.233.43.117:8010/api/logs")
    args = argparse.Namespace(
        fixtures_dir=str(transfers_workspace.fixtures_dir),
        catalog=None,
        metrics_path=None,
        logs_path=None,
        db_path=None,
        audit_path=None,
        prometheus_url=None,
        loki_url=None,
        github_api_url=None,
        github_repo=None,
        local_repo=None,
    )
    cfg, _, _ = _build_runtime_config(
        args,
        transfers_workspace.root / "tmp",
        RuntimeProfile.PILOT,
        OperationalMode.SHADOW,
    )
    assert cfg.metrics_api_url == "http://10.233.43.117:8010/api/metricas"
    assert cfg.logs_api_url == "http://10.233.43.117:8010/api/logs"
    service, _, _ = build_pilot_case_service(cfg, model="vigia-unused")
    try:
        assert isinstance(service._deps.telemetry, RestTelemetryApiAdapter)
    finally:
        service.close()


