"""Unit and integration tests for outbound adapters."""

from __future__ import annotations

from datetime import datetime

from conftest import TransfersWorkspace

from vigia.adapters.outbound.drawio import DrawioArchitectureAdapter
from vigia.adapters.outbound.executor import SimulationState
from vigia.adapters.outbound.knowledge import LocalKnowledgeAdapter
from vigia.adapters.outbound.local_git import JsonReleaseSourceAdapter, LocalGitRepositoryAdapter
from vigia.adapters.outbound.service_catalog import YamlServiceCatalogAdapter
from vigia.adapters.outbound.telemetry import CsvTelemetryAdapter
from vigia.domain.contracts import ConnectorStatus, RelationType


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

