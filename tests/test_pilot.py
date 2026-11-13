"""Acceptance tests for the Vigía Pilot Implementation (Deliveries A, B, C, and D)."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import ScriptedChatModel, TransfersWorkspace
from langchain_core.messages import AIMessage

from vigia.adapters.inbound.http_api import VigiaHttpApp
from vigia.adapters.outbound.audit import JsonlAuditSinkAdapter
from vigia.adapters.outbound.case_store import SqliteCaseStoreAdapter
from vigia.adapters.outbound.drawio import DrawioArchitectureAdapter
from vigia.adapters.outbound.executor import SimulatedActionExecutor, SimulationState
from vigia.adapters.outbound.github import GitHubReleaseSourceAdapter, GitHubRepositoryAdapter
from vigia.adapters.outbound.http_telemetry import PrometheusLokiTelemetryAdapter
from vigia.adapters.outbound.knowledge import LocalKnowledgeAdapter
from vigia.adapters.outbound.local_git import JsonReleaseSourceAdapter, LocalGitRepositoryAdapter
from vigia.adapters.outbound.service_catalog import YamlServiceCatalogAdapter
from vigia.adapters.outbound.telemetry import CsvTelemetryAdapter
from vigia.application.approval_service import ApprovalService, ShadowModeOperationError
from vigia.application.case_service import CaseService
from vigia.application.investigator import InvestigatorAgent
from vigia.application.tools import CaseDependencies
from vigia.domain.contracts import (
    ActionType,
    Alert,
    AlertEnvelope,
    AlertIngestOutcome,
    AlertState,
    CaseStatus,
    ConnectorStatus,
    DecisionChoice,
    OperationalMode,
    OperationStatus,
    ProposalStatus,
    ReviewVerdict,
    RuntimeProfile,
    ServiceConfig,
)
from vigia.runtime import PilotRuntimeConfig, build_pilot_case_service


def _build_full_scripted_model(ws: TransfersWorkspace, *, low_risk: bool = False) -> ScriptedChatModel:
    if low_risk:
        action_call = {
            "id": "tc_prop",
            "name": "propose_action",
            "args": {
                "action": "create_preventive_ticket",
                "target_service": "pagos-core",
                "target_release_id": "CHG-4821",
                "hypothesis": "Pool exhaustion in pagos-core CHG-4821.",
                "justification": "312 errors in CHG-4821.",
                "evidence_ids": ["EV-001", "EV-002", "EV-003", "EV-005"],
                "verification_plan": "Monitor transferencias p95 below 220 ms.",
            },
        }
    else:
        action_call = {
            "id": "tc_prop",
            "name": "propose_action",
            "args": {
                "action": "rollback_change",
                "target_service": "pagos-core",
                "target_release_id": "CHG-4821",
                "hypothesis": "CHG-4821 reduced pool_size 80 -> 50 causing exhaustion.",
                "justification": "Pre-deploy clean; 312 errors post-deploy in CHG-4821.",
                "evidence_ids": ["EV-001", "EV-002", "EV-003", "EV-004", "EV-005", "EV-006"],
                "verification_plan": "Confirm transferencias p95 stabilizes near 184 ms.",
                "exposed_services": ["transferencias", "pagos-core"],
            },
        }
    return ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc_1",
                        "name": "detect_anomaly",
                        "args": {"service": "transferencias", "metric": "latency_p95_ms"},
                    },
                    {
                        "id": "tc_2",
                        "name": "query_dependencies",
                        "args": {"service": "transferencias", "direction": "outbound"},
                    },
                    {
                        "id": "tc_3",
                        "name": "resolve_release",
                        "args": {"service": "pagos-core", "environment": "production"},
                    },
                    {
                        "id": "tc_4",
                        "name": "compare_revisions",
                        "args": {
                            "repository": "applications/pagos-core",
                            "base_sha": ws.base_sha,
                            "target_sha": ws.deployed_sha,
                        },
                    },
                    {
                        "id": "tc_5",
                        "name": "search_logs",
                        "args": {
                            "service": "pagos-core",
                            "pattern": "Connection pool exhausted",
                            "window_start": "2026-11-12T08:40:00-05:00",
                            "window_end": "2026-11-12T08:55:00-05:00",
                        },
                    },
                    {
                        "id": "tc_6",
                        "name": "search_logs",
                        "args": {
                            "service": "pagos-core",
                            "pattern": "Connection pool exhausted",
                            "window_start": "2026-11-12T08:55:01-05:00",
                            "window_end": "2026-11-12T09:12:00-05:00",
                        },
                    },
                ],
            ),
            AIMessage(content="", tool_calls=[action_call]),
            AIMessage(content="Symptom in transferencias traced to CHG-4821 in pagos-core."),
        ]
    )


# ---------------------------------------------------------------------------
# Delivery A: Service Onboarding, Catalog Compatibility & External Adapters
# ---------------------------------------------------------------------------


def test_delivery_a_catalog_backward_compatibility_and_onboarding_probe_with_partial_access(
    transfers_workspace: TransfersWorkspace,
) -> None:
    minimal_cfg = ServiceConfig.model_validate(
        {
            "service": "legacy-svc",
            "repository": "applications/legacy",
            "release_source": "releases.json",
            "architecture": {"path": "docs/transfers.drawio"},
            "telemetry": {
                "service_selector": "legacy-api",
                "environment_selector": "production",
            },
        }
    )
    assert minimal_cfg.environment == "production"
    assert minimal_cfg.recovery_criteria.healthy_band_max_ms == 220.0

    # Deny access to pagos-core logs only; metrics, architecture, and releases remain accessible.
    cfg = PilotRuntimeConfig(
        profile=RuntimeProfile.PILOT,
        mode=OperationalMode.SHADOW,
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
        unauthorized_services={"logs:pagos-core"},
    )
    service, _, _ = build_pilot_case_service(
        cfg,
        model=ScriptedChatModel(responses=[AIMessage(content="ok")]),
    )
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    report = service.probe_service_onboarding("Transferencias", at_time=cutoff)

    assert report.canonical_service == "transferencias"
    assert report.declared_dependencies == ["pagos-core"]
    assert report.probes["architecture"].status == ConnectorStatus.OK
    assert report.probes["metrics"].status == ConnectorStatus.OK
    assert report.probes["releases"].status == ConnectorStatus.OK
    assert report.probes["releases"].revision == transfers_workspace.deployed_sha
    assert report.probes["logs"].status == ConnectorStatus.UNAUTHORIZED
    assert report.ready is False
    assert any("unauthorized" in item.lower() for item in report.pending_conclusions)


def test_delivery_a_prometheus_loki_github_and_knowledge_cutoff(
    transfers_workspace: TransfersWorkspace,
) -> None:
    catalog = YamlServiceCatalogAdapter.from_yaml(
        transfers_workspace.fixtures_dir / "services.yaml"
    )
    calls = {"prom": 0}

    def fake_http_get(
        url: str, params: dict[str, str], headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        if "query_range" in url and "prometheus" in url:
            calls["prom"] += 1
            if calls["prom"] == 1:
                return 503, {"error": "transient timeout"}
            # Oscillating unstable values -> projection must become 'indeterminate'
            return 200, {
                "data": {
                    "result": [
                        {
                            "metric": {"cohort": "v1"},
                            "values": [
                                ["2026-11-12T08:45:00-05:00", "180"],
                                ["2026-11-12T08:50:00-05:00", "182"],
                                ["2026-11-12T08:55:00-05:00", "181"],
                                ["2026-11-12T09:05:00-05:00", "390"],
                                ["2026-11-12T09:08:00-05:00", "250"],
                                ["2026-11-12T09:12:00-05:00", "410"],
                            ],
                        }
                    ]
                }
            }
        if "loki" in url:
            return 200, {
                "data": {
                    "result": [
                        {
                            "stream": {"cohort": "CHG-4821"},
                            "requests": 1000,
                            "values": [
                                [
                                    "2026-11-12T09:05:00-05:00",
                                    "Connection pool exhausted Bearer secret-tok-123",
                                    25,
                                ]
                            ],
                        },
                        {
                            "stream": {"cohort": "CHG-CANARY-ZERO"},
                            "requests": 0,
                            "values": [],
                        },
                    ]
                }
            }
        if "/deployments" in url:
            return 200, {
                "deployments": [
                    {
                        "release_id": "CHG-4821",
                        "environment": "production",
                        "deployed_at": "2026-11-12T08:55:00-05:00",
                        "previous_sha": transfers_workspace.base_sha,
                        "sha": transfers_workspace.deployed_sha,
                        "status": "active",
                    },
                    {
                        "release_id": "CHG-9999",
                        "environment": "production",
                        "deployed_at": "2026-11-12T09:20:00-05:00",
                        "previous_sha": transfers_workspace.deployed_sha,
                        "sha": transfers_workspace.undeployed_sha,
                        "status": "active",
                    },
                ]
            }
        if "/compare/" in url:
            return 200, {
                "files": [{"filename": "src/pool.py", "patch": "@@ -1 +1 @@\n-80\n+50"}]
            }
        return 404, {"error": "not found"}

    telemetry = PrometheusLokiTelemetryAdapter(catalog, http_get=fake_http_get)
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    anomaly = telemetry.detect_anomaly("transferencias", "production", "latency_p95_ms", cutoff)
    assert calls["prom"] == 2
    assert anomaly.status == ConnectorStatus.OK
    assert anomaly.is_anomalous is True
    assert anomaly.projection_status == "indeterminate"
    assert anomaly.projected_minutes_to_threshold is None

    logs = telemetry.search_logs("pagos-core", "production", "Connection pool exhausted", 30, cutoff)
    assert logs.total == 25
    assert logs.cohort_error_rates["CHG-4821"] == 0.025
    assert logs.cohort_error_rates["CHG-CANARY-ZERO"] is None
    assert "CHG-CANARY-ZERO" in logs.uncovered_cohorts
    assert "[REDACTED]" in logs.examples[0]

    gh_rel = GitHubReleaseSourceAdapter(
        {"pagos-core": "bank/pagos-core"},
        http_get=fake_http_get,
    )
    res = gh_rel.resolve_active_releases("pagos-core", "production", cutoff)
    assert [r.release_id for r in res.releases] == ["CHG-4821"]

    gh_repo = GitHubRepositoryAdapter({"bank/pagos-core"}, http_get=fake_http_get)
    diff = gh_repo.compare_revisions(
        "bank/pagos-core", transfers_workspace.base_sha, transfers_workspace.deployed_sha
    )
    assert diff.status == ConnectorStatus.OK and diff.files[0].path == "src/pool.py"

    # Historical incidents occurring after cutoff are excluded.
    inc_path = transfers_workspace.fixtures_dir / "incidents.json"
    inc_data = json.loads(inc_path.read_text(encoding="utf-8"))
    inc_data["incidents"][0]["occurred_at"] = "2026-11-12T10:00:00-05:00"
    inc_path.write_text(json.dumps(inc_data), encoding="utf-8")
    knowledge = LocalKnowledgeAdapter(inc_path, transfers_workspace.fixtures_dir / "runbooks")
    matches = knowledge.search_similar_incidents("Connection pool exhausted", cutoff_at=cutoff)
    assert all(m.incident_id != inc_data["incidents"][0]["id"] for m in matches)


# ---------------------------------------------------------------------------
# Delivery B: Webhook Ingestion, Deduplication, Revisions & Crash Recovery
# ---------------------------------------------------------------------------


def test_delivery_b_concurrent_duplicate_webhooks_episode_merge_and_worker_restart(
    transfers_workspace: TransfersWorkspace,
) -> None:
    db_path = transfers_workspace.root / "pilot.db"
    cfg = PilotRuntimeConfig(
        profile=RuntimeProfile.PILOT,
        mode=OperationalMode.SHADOW,
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
        db_path=db_path,
    )
    model = _build_full_scripted_model(transfers_workspace)
    service, _, _ = build_pilot_case_service(cfg, model=model)
    app = VigiaHttpApp(service, webhook_token="secret-hook")

    unauth = app.handle_request("POST", "/api/alerta", body="{}")
    assert unauth.status_code == 401

    webhook_payload = json.dumps(
        {
            "receiver": "grafana-transferencias",
            "alerts": [
                {
                    "fingerprint": "fp-lat-1",
                    "status": "firing",
                    "startsAt": "2026-11-12T09:12:00-05:00",
                    "labels": {
                        "service": "Transferencias",
                        "environment": "production",
                        "metric": "latency_p95_ms",
                        "episode_id": "ep-0912",
                    },
                    "annotations": {
                        "current_value": 420,
                        "threshold_value": 800,
                    },
                }
            ],
        }
    )

    responses: list[Any] = []
    lock = threading.Lock()

    def _send() -> None:
        r = app.handle_request(
            "POST",
            "/api/alerta",
            headers={"Authorization": "Bearer secret-hook"},
            body=webhook_payload,
        )
        with lock:
            responses.append(r)

    t1 = threading.Thread(target=_send)
    t2 = threading.Thread(target=_send)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert [r.status_code for r in responses] == [202, 202]
    outcomes = {r.json()["outcome"] for r in responses}
    case_ids = {r.json()["case_id"] for r in responses}
    assert outcomes == {AlertIngestOutcome.CREATED.value, AlertIngestOutcome.DUPLICATE.value}
    assert len(case_ids) == 1
    case_id = next(iter(case_ids))

    # Simulate partial progress before worker crash (claim work -> leave in_progress).
    work = service._store.claim_next_work(case_id=case_id)
    assert work is not None

    # Restart: new service instance connected to the same SQLite database resumes work cleanly.
    resumed_service, _, _ = build_pilot_case_service(
        cfg, model=_build_full_scripted_model(transfers_workspace)
    )
    resumed_case = resumed_service.process_pending_work(case_id=case_id)
    assert resumed_case is not None
    assert resumed_case.status == CaseStatus.AWAITING_REVIEW
    assert len(resumed_case.evidence) >= 5
    rev1_evidence_count = len(resumed_case.evidence)

    # Second firing sample for same episode merges and creates revision 2 without wiping revision 1.
    second_resp = VigiaHttpApp(resumed_service).handle_request(
        "POST",
        "/api/alerta",
        body=json.dumps(
            {
                "source": "grafana-transferencias",
                "event_id": "evt-0914",
                "episode_id": "ep-0912",
                "state": "firing",
                "alert": {
                    "service": "Transferencias",
                    "environment": "production",
                    "metric": "latency_p95_ms",
                    "observed_at": "2026-11-12T09:14:00-05:00",
                    "current_value": 452,
                    "threshold_value": 800,
                },
            }
        ),
    )
    assert second_resp.status_code == 202
    assert second_resp.json()["outcome"] == AlertIngestOutcome.MERGED.value
    assert second_resp.json()["revision_number"] == 2
    merged = resumed_service.get_case(case_id)
    assert len(merged.revisions) == 2
    assert len(merged.revisions[0].evidence_ids) == rev1_evidence_count

    # Monitor resolved signal is recorded in timeline.
    resolved_resp = VigiaHttpApp(resumed_service).handle_request(
        "POST",
        "/api/alerta",
        body=json.dumps(
            {
                "source": "grafana-transferencias",
                "event_id": "evt-0920-res",
                "episode_id": "ep-0912",
                "state": "resolved",
                "alert": {
                    "service": "Transferencias",
                    "environment": "production",
                    "metric": "latency_p95_ms",
                    "observed_at": "2026-11-12T09:20:00-05:00",
                    "current_value": 184,
                },
            }
        ),
    )
    assert resolved_resp.json()["outcome"] == AlertIngestOutcome.RESOLVED_RECORDED.value


# ---------------------------------------------------------------------------
# Delivery C: Shadow Mode, Pre-Deploy Refutation, SSE & Review API
# ---------------------------------------------------------------------------


def test_delivery_c_innocent_release_with_predeploy_errors_blocks_rollback(
    transfers_workspace: TransfersWorkspace,
) -> None:
    logs_path = transfers_workspace.fixtures_dir / "logs.txt"
    pre_deploy_line = json.dumps(
        {
            "timestamp": "2026-11-12T08:50:00-05:00",
            "service": "pagos-core",
            "environment": "production",
            "cohort": "CHG-4810",
            "level": "ERROR",
            "count": 40,
            "message": "Connection pool exhausted waiting for pagos-db connection",
        }
    )
    logs_path.write_text(pre_deploy_line + "\n" + logs_path.read_text(encoding="utf-8"))

    cfg = PilotRuntimeConfig(
        profile=RuntimeProfile.PILOT,
        mode=OperationalMode.SHADOW,
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
    )
    model = _build_full_scripted_model(transfers_workspace, low_risk=False)
    service, sim_state, _ = build_pilot_case_service(cfg, model=model)
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    case = service.start_case(
        Alert(
            service="Transferencias",
            environment="production",
            metric="latency_p95_ms",
            observed_at=cutoff,
            cutoff_at=cutoff,
            current_value=420.0,
            threshold_value=800.0,
        )
    )

    assert case.proposal is None
    assert case.status == CaseStatus.INCONCLUSIVE
    assert case.partial_findings is not None
    assert case.suggested_next_check is not None
    assert sim_state.rolled_back_releases == set()


def test_delivery_c_shadow_mode_blocks_writes_and_serves_console_sse_and_review_metrics(
    transfers_workspace: TransfersWorkspace,
) -> None:
    cfg = PilotRuntimeConfig(
        profile=RuntimeProfile.PILOT,
        mode=OperationalMode.SHADOW,
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
    )
    # Even low-risk action must NOT execute in shadow mode; it becomes RECOMMENDED.
    service, sim_state, _ = build_pilot_case_service(
        cfg, model=_build_full_scripted_model(transfers_workspace, low_risk=True)
    )
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    case = service.start_case(
        Alert(
            service="Transferencias",
            environment="production",
            metric="latency_p95_ms",
            observed_at=cutoff,
            cutoff_at=cutoff,
            current_value=420.0,
            threshold_value=800.0,
        )
    )

    assert case.mode == OperationalMode.SHADOW
    assert case.status == CaseStatus.AWAITING_REVIEW
    assert case.proposal is not None
    assert case.proposal.status == ProposalStatus.RECOMMENDED
    assert sim_state.tickets_created == []
    assert sim_state.rolled_back_releases == set()

    app = VigiaHttpApp(service, require_user_auth=True)
    # Approval is blocked with 403 in shadow mode.
    forbidden_approve = app.handle_request(
        "POST",
        "/api/aprobacion",
        headers={"X-Vigia-User": "lead", "X-Vigia-Groups": "oncall-approvers"},
        body=json.dumps(
            {
                "case_id": case.case_id,
                "proposal_id": case.proposal.proposal_id,
                "decision": "approve",
            }
        ),
    )
    assert forbidden_approve.status_code == 403

    # Initial SSE fetch -> verify sequential ids, then reconnect with Last-Event-ID.
    sse_full = app.handle_request(
        "GET",
        f"/api/eventos?caso={case.case_id}",
        headers={"X-Vigia-User": "lead", "X-Vigia-Groups": "transfers-squad"},
    )
    assert sse_full.status_code == 200
    assert "text/event-stream" in sse_full.headers["Content-Type"]
    all_ids = [
        int(line.split(": ", 1)[1])
        for line in sse_full.body.splitlines()
        if line.startswith("id: ")
    ]
    assert len(all_ids) >= 5
    last_id = all_ids[-1]

    # Submit shadow review & verify incremental SSE event after Last-Event-ID.
    rev_resp = app.handle_request(
        "POST",
        "/api/revision",
        headers={"X-Vigia-User": "lead-transfers", "X-Vigia-Groups": "transfers-squad"},
        body=json.dumps(
            {
                "case_id": case.case_id,
                "proposal_id": case.proposal.proposal_id,
                "verdict": "useful",
                "comment": "Matched manual trace.",
                "intervened_externally": True,
                "confirmed_cause": "CHG-4821",
            }
        ),
    )
    assert rev_resp.status_code == 200
    dto = rev_resp.json()
    assert dto["symptom"]["service"] == "transferencias"
    assert dto["probable_cause"]["target_service"] == "pagos-core"
    assert len(dto["reviews"]) == 1

    sse_delta = app.handle_request(
        "GET",
        f"/api/eventos?caso={case.case_id}",
        headers={
            "X-Vigia-User": "lead",
            "X-Vigia-Groups": "transfers-squad",
            "Last-Event-ID": str(last_id),
        },
    )
    assert "event: review" in sse_delta.body

    metrics_resp = app.handle_request("GET", "/api/piloto/metricas")
    assert metrics_resp.status_code == 200
    metrics = metrics_resp.json()
    assert metrics["useful_recommendations"] == 1
    assert metrics["false_alarm_rate"] == 0.0
    assert metrics["confirmed_cause_rate"] == 1.0


# ---------------------------------------------------------------------------
# Delivery D: Assisted AuthZ, Concurrency, Timeout Reconciliation & Observation
# ---------------------------------------------------------------------------


def test_delivery_d_assisted_permissions_concurrency_timeout_and_sustained_verification(
    transfers_workspace: TransfersWorkspace,
) -> None:
    cfg = PilotRuntimeConfig(
        profile=RuntimeProfile.PILOT,
        mode=OperationalMode.ASSISTED,
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
    )
    service, sim_state, _ = build_pilot_case_service(
        cfg, model=_build_full_scripted_model(transfers_workspace)
    )
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    case = service.start_case(
        Alert(
            service="Transferencias",
            environment="production",
            metric="latency_p95_ms",
            observed_at=cutoff,
            cutoff_at=cutoff,
            current_value=420.0,
            threshold_value=800.0,
        )
    )
    assert case.proposal is not None
    app = VigiaHttpApp(service, require_user_auth=True)

    # Wrong team -> 403.
    unauth_resp = app.handle_request(
        "POST",
        "/api/aprobacion",
        headers={"X-Vigia-User": "bob", "X-Vigia-Groups": "billing-squad"},
        body=json.dumps(
            {
                "case_id": case.case_id,
                "proposal_id": case.proposal.proposal_id,
                "decision": "approve",
            }
        ),
    )
    assert unauth_resp.status_code == 403

    # Stale proposal_version -> 409.
    stale_resp = app.handle_request(
        "POST",
        "/api/aprobacion",
        headers={"X-Vigia-User": "lead", "X-Vigia-Groups": "oncall-approvers"},
        body=json.dumps(
            {
                "case_id": case.case_id,
                "proposal_id": case.proposal.proposal_id,
                "decision": "approve",
                "proposal_version": 99,
            }
        ),
    )
    assert stale_resp.status_code == 409

    # Pipeline times out -> case enters OBSERVING and holds 1 operation ID.
    sim_state.next_operation_outcome = OperationStatus.TIMEOUT
    timed_out = app.handle_request(
        "POST",
        "/api/aprobacion",
        headers={"X-Vigia-User": "lead", "X-Vigia-Groups": "oncall-approvers"},
        body=json.dumps(
            {
                "case_id": case.case_id,
                "proposal_id": case.proposal.proposal_id,
                "decision": "approve",
                "idempotency_key": "idem-approve-1",
            }
        ),
    )
    assert timed_out.status_code == 200
    assert timed_out.json()["status"] == CaseStatus.OBSERVING.value
    op_id = timed_out.json()["execution"]["operation_id"]
    assert len(sim_state.operations) == 1

    # Duplicate conflicting decision returns 409; idempotent replay returns same single operation.
    conflict = app.handle_request(
        "POST",
        "/api/aprobacion",
        headers={"X-Vigia-User": "lead", "X-Vigia-Groups": "oncall-approvers"},
        body=json.dumps(
            {
                "case_id": case.case_id,
                "proposal_id": case.proposal.proposal_id,
                "decision": "reject",
                "idempotency_key": "idem-approve-1",
            }
        ),
    )
    assert conflict.status_code == 409
    assert len(sim_state.operations) == 1

    # Reconcile pipeline operation completing at 09:15; isolated drop 420 -> 410 ms stays OBSERVING.
    executor = SimulatedActionExecutor(sim_state)
    completed_at = datetime.fromisoformat("2026-11-12T09:15:00-05:00")
    executor.complete_timed_out_operation(
        op_id, status=OperationStatus.EXECUTED, completed_at=completed_at
    )

    metrics_path = transfers_workspace.fixtures_dir / "metrics.csv"
    base_metrics = "\n".join(
        line
        for line in metrics_path.read_text(encoding="utf-8").splitlines()
        if "post_rollback" not in line
    )
    metrics_path.write_text(
        base_metrics
        + "\n2026-11-12T09:16:00-05:00,transferencias,production,latency_p95_ms,410.0,v1,post_rollback\n",
        encoding="utf-8",
    )

    reconciled = service.reconcile_operation(
        case_id=case.case_id,
        operation_id=op_id,
        verification_at=datetime.fromisoformat("2026-11-12T09:16:30-05:00"),
    )
    assert reconciled.status == CaseStatus.OBSERVING
    assert reconciled.verification is not None
    assert reconciled.verification.recovered is False

    # Sustained recovery near 184 ms closes the case cleanly.
    metrics_path.write_text(
        base_metrics
        + "\n2026-11-12T09:16:00-05:00,transferencias,production,latency_p95_ms,410.0,v1,post_rollback"
        + "\n2026-11-12T09:18:00-05:00,transferencias,production,latency_p95_ms,190.0,v1,post_rollback"
        + "\n2026-11-12T09:19:00-05:00,transferencias,production,latency_p95_ms,184.0,v1,post_rollback\n",
        encoding="utf-8",
    )
    closed = service.verify_case_recovery(
        case.case_id,
        verification_at=datetime.fromisoformat("2026-11-12T09:19:00-05:00"),
    )
    assert closed.status == CaseStatus.CLOSED
    assert closed.verification is not None
    assert closed.verification.recovered is True
    assert closed.verification.metric_value == 184.0

