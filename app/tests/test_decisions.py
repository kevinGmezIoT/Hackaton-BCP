"""Decision workflow tests: approval, rejection, idempotency, risk levels, verification."""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import ScriptedChatModel, TransfersWorkspace
from langchain_core.messages import AIMessage

from application.approval_service import InactiveReleaseError, ProposalAlreadyResolvedError
from domain.contracts import (
    Alert,
    CaseRecord,
    CaseStatus,
    DecisionChoice,
    ProposalStatus,
    RiskLevel,
)
from runtime import build_case_service


def _run_investigator_to_proposal(
    transfers_workspace: TransfersWorkspace,
    *,
    action: str = "rollback_change",
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
                            "target_release_id": "CHG-4821",
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
        )
    )
    return case_service, case, sim_state


def test_approve_executes_rollback_once_verifies_recovery_and_handles_replay(
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

    with pytest.raises(ProposalAlreadyResolvedError):
        case_service.resolve_approval(
            case_id=case.case_id,
            proposal_id=case.proposal.proposal_id,
            actor="other-user",
            decision=DecisionChoice.REJECT,
        )


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


def test_low_risk_action_executes_without_pausing_for_approval(
    transfers_workspace: TransfersWorkspace,
) -> None:
    case_service, case, sim_state = _run_investigator_to_proposal(
        transfers_workspace, action="create_preventive_ticket"
    )
    assert case.proposal is not None
    assert case.proposal.risk_level == RiskLevel.LOW
    assert case.proposal.status == ProposalStatus.EXECUTED
    assert len(sim_state.tickets_created) == 1
