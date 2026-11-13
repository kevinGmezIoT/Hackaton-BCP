"""Investigation tests for Alternative A (citations, cutoff, undeployed commits, outages)."""

from __future__ import annotations

from datetime import datetime

from conftest import ScriptedChatModel, TransfersWorkspace
from langchain_core.messages import AIMessage

from vigia.domain.contracts import Alert, CaseStatus
from vigia.runtime import build_case_service


def _build_transfers_alert() -> Alert:
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    return Alert(
        service="Transferencias",
        environment="production",
        metric="latency_p95_ms",
        observed_at=cutoff,
        cutoff_at=cutoff,
        symptom="p95 latency jumped from 180 ms to 420 ms in 15 minutes",
        baseline_value=180.0,
        current_value=420.0,
        threshold_value=800.0,
        projected_minutes_to_threshold=24.0,
    )


def test_investigates_from_transfers_to_deployed_pagos_core_line_and_pauses(
    transfers_workspace: TransfersWorkspace,
) -> None:
    model = ScriptedChatModel(
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
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc_3",
                        "name": "resolve_release",
                        "args": {"service": "pagos-core", "environment": "production"},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc_4",
                        "name": "compare_revisions",
                        "args": {
                            "repository": "applications/pagos-core",
                            "base_sha": transfers_workspace.base_sha,
                            "target_sha": transfers_workspace.deployed_sha,
                        },
                    },
                    {
                        "id": "tc_5",
                        "name": "read_snippet",
                        "args": {
                            "repository": "applications/pagos-core",
                            "sha": transfers_workspace.deployed_sha,
                            "path": "src/pool.py",
                            "start_line": 1,
                            "end_line": 10,
                        },
                    },
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc_6",
                        "name": "search_logs",
                        "args": {
                            "service": "pagos-core",
                            "pattern": "Connection pool exhausted",
                            "window_minutes": 30,
                        },
                    },
                    {
                        "id": "tc_7",
                        "name": "search_similar_incidents",
                        "args": {
                            "description": "Connection pool exhausted pagos-core pool_size",
                            "k": 2,
                        },
                    },
                    {
                        "id": "tc_8",
                        "name": "search_runbook",
                        "args": {"query": "Connection pool exhausted rollback"},
                    },
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc_9",
                        "name": "propose_action",
                        "args": {
                            "action": "rollback_change",
                            "target_service": "pagos-core",
                            "target_release_id": "CHG-4821",
                            "hypothesis": "CHG-4821 reduced pool_size from 80 to 50.",
                            "justification": "312 errors in CHG-4821 cohort; matches INC-3512 and RB-087.",
                            "evidence_ids": [
                                "EV-001",
                                "EV-002",
                                "EV-003",
                                "EV-004",
                                "EV-005",
                                "EV-006",
                                "EV-007",
                                "EV-009",
                            ],
                            "verification_plan": "Check transferencias latency_p95_ms after rollback.",
                            "exposed_services": ["transferencias", "pagos-core"],
                        },
                    }
                ],
            ),
            AIMessage(content="Proposed rollback of CHG-4821 awaiting human approval."),
        ]
    )

    audit_file = transfers_workspace.root / "audit.jsonl"
    case_service, sim_state, audit = build_case_service(
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
        model=model,
        audit_path=audit_file,
    )

    case = case_service.start_case(_build_transfers_alert())

    assert case.status == CaseStatus.AWAITING_APPROVAL
    assert case.proposal is not None
    assert case.proposal.target_release_id == "CHG-4821"
    assert sim_state.rolled_back_releases == set()
    refs = " ".join(ev.reproducible_reference for ev in case.evidence)
    assert transfers_workspace.deployed_sha in refs
    assert transfers_workspace.undeployed_sha not in refs
    assert audit_file.exists()
    assert len(audit.list_audit_records(case.case_id)) >= 8

    query_reply = case_service.answer_query(case.case_id, "Why pagos-core pool_size?")
    assert query_reply["case_id"] == case.case_id
    assert query_reply["references"]
    assert "CHG-4821" in str(query_reply["answer"])


def test_undeployed_staging_sha_diff_is_tagged_as_undeployed_candidate(
    transfers_workspace: TransfersWorkspace,
) -> None:
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc_undeployed",
                        "name": "compare_revisions",
                        "args": {
                            "repository": "applications/pagos-core",
                            "base_sha": transfers_workspace.deployed_sha,
                            "target_sha": transfers_workspace.undeployed_sha,
                        },
                    }
                ],
            ),
            AIMessage(content="Only staging commit changed; not active in production."),
        ]
    )

    case_service, _, _ = build_case_service(
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
        model=model,
    )

    case = case_service.start_case(_build_transfers_alert())

    assert case.status == CaseStatus.INCONCLUSIVE
    assert len(case.evidence) == 1
    assert case.evidence[0].source == "git_diff_undeployed"
    assert "UNDEPLOYED CANDIDATE" in case.evidence[0].observed_fact
    assert any("not deployed in production" in gap for gap in case.connector_gaps)


def test_rejects_fabricated_evidence_citations_and_records_inconclusive_when_budget_ends(
    transfers_workspace: TransfersWorkspace,
) -> None:
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc_bad",
                        "name": "propose_action",
                        "args": {
                            "action": "rollback_change",
                            "target_service": "pagos-core",
                            "target_release_id": "CHG-4821",
                            "hypothesis": "Fabricated guess",
                            "justification": "Guessing without evidence",
                            "evidence_ids": ["EV-999"],
                            "verification_plan": "None",
                        },
                    }
                ],
            ),
            AIMessage(content="Unable to substantiate proposal; further checks needed."),
        ]
    )

    case_service, sim_state, _ = build_case_service(
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
        model=model,
    )

    case = case_service.start_case(_build_transfers_alert())

    assert case.proposal is None
    assert case.status == CaseStatus.INCONCLUSIVE
    assert case.partial_findings is not None
    assert case.suggested_next_check is not None
    assert sim_state.rolled_back_releases == set()


def test_connector_outage_is_recorded_as_gap_distinct_from_empty_query(
    transfers_workspace: TransfersWorkspace,
) -> None:
    (transfers_workspace.fixtures_dir / "logs.txt").unlink()

    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc_logs",
                        "name": "search_logs",
                        "args": {
                            "service": "pagos-core",
                            "pattern": "Connection pool exhausted",
                            "window_minutes": 30,
                        },
                    }
                ],
            ),
            AIMessage(content="Log connector unavailable; abstaining from rollback."),
        ]
    )

    case_service, _, _ = build_case_service(
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
        model=model,
    )

    case = case_service.start_case(_build_transfers_alert())

    assert case.status == CaseStatus.INCONCLUSIVE
    assert any("connector_unavailable" in gap for gap in case.connector_gaps)

