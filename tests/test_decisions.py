"""Decision workflow tests: approval, rejection, idempotency, risk levels, verification."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from conftest import ScriptedChatModel, TransfersWorkspace
from langchain_core.messages import AIMessage

from vigia.application.approval_service import (
    InactiveReleaseError,
    ProposalAlreadyResolvedError,
)
from vigia.domain.contracts import (
    Alert,
    CaseRecord,
    CaseStatus,
    DecisionChoice,
    MetricPoint,
    MetricSeries,
    ProposalStatus,
    RiskLevel,
)
from vigia.runtime import build_case_service


@pytest.mark.parametrize("intervals,errors,expected_healthy,recovered", [
    ([(-0.5, 0.5), (0.5, 1.5)], 0, 1, False),
    ([(0, 1), (2, 3)], 0, 1, False),
    ([(0, 1), (1, 2)], 0, 2, True),
    ([(0, 1), (1, 2)], 100, 2, False),
])
def test_historical_recovery_requires_wholly_post_action_contiguous_clean_intervals(
    transfers_workspace, monkeypatch, intervals, errors, expected_healthy, recovered,
):
    service, case, _ = _run_investigator_to_proposal(transfers_workspace)
    action_at = case.alert.cutoff_at
    service._deps.catalog.get_service(case.alert.service).recovery_criteria.consecutive_healthy_windows = 2
    points = [MetricPoint(
        timestamp=action_at + timedelta(minutes=end), value=184,
        window_start=action_at + timedelta(minutes=start),
        window_end=action_at + timedelta(minutes=end), request_count=100, error_count=0,
    ) for start, end in intervals]
    series = MetricSeries(
        service=case.alert.service, environment="production", metric="latency_p95_ms",
        window_start=action_at - timedelta(minutes=1), window_end=action_at + timedelta(minutes=4),
        reproducible_query="http://metrics/api/v1/metricas/historico.txt", points=points,
        request_volume=200, error_total=errors, current_value=184, coverage=1,
    )
    monkeypatch.setattr(service._deps.telemetry, "query_metrics", lambda **kwargs: series)
    result = service._verify_recovery(case, action_at=action_at,
                                      verification_at=action_at + timedelta(minutes=4))
    assert result.consecutive_healthy_points == expected_healthy
    assert result.recovered is recovered


def _run_investigator_to_proposal(
    transfers_workspace: TransfersWorkspace,
    *,
    action: str = "rollback_change",
    target_release_id: str = "CHG-4821",
    alert_current_value: float | None = 420.0,
) -> tuple[object, CaseRecord, object]:
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
                        "name": "resolve_release",
                        "args": {"service": "pagos-core", "environment": "production"},
                    },
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc_3",
                        "name": "propose_action",
                        "args": {
                            "action": action,
                            "target_service": "pagos-core",
                            "target_release_id": target_release_id,
                            "hypothesis": "Pool reduction in CHG-4821 caused exhaustion.",
                            "justification": "Latency anomaly correlates with CHG-4821.",
                            "evidence_ids": ["EV-001", "EV-002"],
                            "verification_plan": "Verify transferencias latency drops below 800 ms.",
                        },
                    }
                ],
            ),
            AIMessage(content="Proposal submitted."),
        ]
    )
    case_service, sim_state, _ = build_case_service(
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
        model=model,
    )
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    case = case_service.start_case(
        Alert(
            service="Transferencias",
            environment="production",
            metric="latency_p95_ms",
            observed_at=cutoff,
            cutoff_at=cutoff,
            current_value=alert_current_value,
        )
    )
    return case_service, case, sim_state


def test_approve_executes_rollback_once_verifies_recovery_and_rejects_conflicting_key_reuse(
    transfers_workspace: TransfersWorkspace,
) -> None:
    case_service, case, sim_state = _run_investigator_to_proposal(transfers_workspace)
    assert case.proposal is not None
    assert case.status == CaseStatus.AWAITING_APPROVAL
    assert case.proposal.risk_level == RiskLevel.HIGH

    verify_at = datetime.fromisoformat("2026-11-12T09:19:00-05:00")
    updated = case_service.resolve_approval(
        case_id=case.case_id,
        proposal_id=case.proposal.proposal_id,
        actor="lead-engineer",
        decision=DecisionChoice.APPROVE,
        idempotency_key="dec-001",
        verification_at=verify_at,
    )

    assert updated.proposal.status == ProposalStatus.EXECUTED
    assert sim_state.rolled_back_releases == {"CHG-4821"}
    assert updated.verification is not None
    assert updated.verification.recovered is True
    assert updated.verification.metric_value == 184.0
    assert updated.status == CaseStatus.CLOSED

    replayed = case_service.resolve_approval(
        case_id=case.case_id,
        proposal_id=case.proposal.proposal_id,
        actor="lead-engineer",
        decision=DecisionChoice.APPROVE,
        idempotency_key="dec-001",
        verification_at=verify_at,
    )
    assert replayed.status == CaseStatus.CLOSED
    assert sim_state.tickets_created == []

    # Reusing the same idempotency_key for a contradicting decision must fail.
    with pytest.raises(ProposalAlreadyResolvedError):
        case_service.resolve_approval(
            case_id=case.case_id,
            proposal_id=case.proposal.proposal_id,
            actor="lead-engineer",
            decision=DecisionChoice.REJECT,
            idempotency_key="dec-001",
        )
    assert sim_state.tickets_created == []


def test_verification_requires_post_execution_sample_and_keeps_case_open_without_it(
    transfers_workspace: TransfersWorkspace,
) -> None:
    # Remove the post_rollback sample (184 ms at 09:19) and leave alert.current_value=None.
    metrics_path = transfers_workspace.fixtures_dir / "metrics.csv"
    kept_lines = [
        line
        for line in metrics_path.read_text(encoding="utf-8").splitlines()
        if "post_rollback" not in line
    ]
    metrics_path.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")

    case_service, case, sim_state = _run_investigator_to_proposal(
        transfers_workspace, alert_current_value=None
    )
    assert case.proposal is not None

    updated = case_service.resolve_approval(
        case_id=case.case_id,
        proposal_id=case.proposal.proposal_id,
        actor="lead-engineer",
        decision=DecisionChoice.APPROVE,
    )

    assert sim_state.rolled_back_releases == {"CHG-4821"}
    assert updated.verification is not None
    assert updated.verification.recovered is False
    assert updated.verification.metric_value is None
    assert updated.status == CaseStatus.OPEN


def test_pending_proposal_cannot_be_replaced_by_second_propose_action(
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
                        "name": "resolve_release",
                        "args": {"service": "pagos-core", "environment": "production"},
                    },
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc_3",
                        "name": "propose_action",
                        "args": {
                            "action": "rollback_change",
                            "target_service": "pagos-core",
                            "target_release_id": "CHG-4821",
                            "hypothesis": "CHG-4821 pool reduction.",
                            "justification": "Matches errors.",
                            "evidence_ids": ["EV-001", "EV-002"],
                            "verification_plan": "Check latency.",
                        },
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "tc_4",
                        "name": "propose_action",
                        "args": {
                            "action": "scale_capacity",
                            "target_service": "transferencias",
                            "target_release_id": "none",
                            "hypothesis": "Attempted override.",
                            "justification": "Should be blocked.",
                            "evidence_ids": ["EV-001"],
                            "verification_plan": "Check latency.",
                        },
                    }
                ],
            ),
            AIMessage(content="Waiting for approval on original proposal."),
        ]
    )
    case_service, _, audit = build_case_service(
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
        model=model,
    )
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    case = case_service.start_case(
        Alert(
            service="Transferencias",
            environment="production",
            metric="latency_p95_ms",
            observed_at=cutoff,
            cutoff_at=cutoff,
        )
    )

    assert case.proposal is not None
    assert case.proposal.target_service == "pagos-core"
    assert case.proposal.target_release_id == "CHG-4821"
    assert any(r.status == "proposal_locked" for r in audit.list_audit_records(case.case_id))


def test_rollback_rejects_placeholder_release_id(
    transfers_workspace: TransfersWorkspace,
) -> None:
    case_service, case, sim_state = _run_investigator_to_proposal(
        transfers_workspace,
        action="rollback_change",
        target_release_id="none",
    )
    assert case.proposal is None
    assert case.status == CaseStatus.INCONCLUSIVE
    assert sim_state.rolled_back_releases == set()


def test_reject_creates_single_preventive_ticket_without_rollback_and_keeps_case_open(
    transfers_workspace: TransfersWorkspace,
) -> None:
    case_service, case, sim_state = _run_investigator_to_proposal(transfers_workspace)
    assert case.proposal is not None

    updated = case_service.resolve_approval(
        case_id=case.case_id,
        proposal_id=case.proposal.proposal_id,
        actor="lead-engineer",
        decision=DecisionChoice.REJECT,
        comment="Need traffic check before rollback.",
    )

    assert sim_state.rolled_back_releases == set()
    assert len(sim_state.tickets_created) == 1
    assert updated.proposal.status == ProposalStatus.REJECTED
    assert updated.status == CaseStatus.OPEN

    case_service.resolve_approval(
        case_id=case.case_id,
        proposal_id=case.proposal.proposal_id,
        actor="lead-engineer",
        decision=DecisionChoice.REJECT,
        comment="Need traffic check before rollback.",
    )
    assert len(sim_state.tickets_created) == 1


def test_missing_post_action_metric_keeps_case_open(
    transfers_workspace: TransfersWorkspace,
) -> None:
    case_service, case, sim_state = _run_investigator_to_proposal(transfers_workspace)
    assert case.proposal is not None
    sim_state.suppress_post_action_metrics = True

    updated = case_service.resolve_approval(
        case_id=case.case_id,
        proposal_id=case.proposal.proposal_id,
        actor="lead-engineer",
        decision=DecisionChoice.APPROVE,
        verification_at=datetime.fromisoformat("2026-11-12T09:19:00-05:00"),
    )

    assert updated.verification is not None
    assert updated.verification.recovered is False
    assert updated.status == CaseStatus.OPEN


def test_inactive_release_cannot_be_approved(
    transfers_workspace: TransfersWorkspace,
) -> None:
    case_service, case, sim_state = _run_investigator_to_proposal(transfers_workspace)
    assert case.proposal is not None
    sim_state.rolled_back_releases.add("CHG-4821")

    with pytest.raises(InactiveReleaseError):
        case_service.resolve_approval(
            case_id=case.case_id,
            proposal_id=case.proposal.proposal_id,
            actor="lead-engineer",
            decision=DecisionChoice.APPROVE,
        )


def test_low_risk_action_executes_without_pausing_and_sets_coherent_case_status(
    transfers_workspace: TransfersWorkspace,
) -> None:
    case_service, case, sim_state = _run_investigator_to_proposal(
        transfers_workspace, action="create_preventive_ticket"
    )
    assert case.proposal is not None
    assert case.proposal.risk_level == RiskLevel.LOW
    assert case.proposal.status == ProposalStatus.EXECUTED
    assert case.status == CaseStatus.MITIGATED
    assert len(sim_state.tickets_created) == 1

