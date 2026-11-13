"""Proposal registration, backend risk evaluation, and idempotent decision service."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from vigia.application.evidence_registry import EvidenceRegistry
from vigia.domain.contracts import (
    ActionProposal,
    ActionType,
    CaseRecord,
    CaseStatus,
    DecisionChoice,
    ExecutionResult,
    Finding,
    HumanDecision,
    ProposalStatus,
    RiskLevel,
)
from vigia.domain.ports import ActionExecutorPort, ReleaseSourcePort

ACTION_RISK_TABLE: dict[ActionType, RiskLevel] = {
    ActionType.CREATE_PREVENTIVE_TICKET: RiskLevel.LOW,
    ActionType.NOTIFY_TEAM: RiskLevel.LOW,
    ActionType.SCALE_CAPACITY: RiskLevel.MEDIUM,
    ActionType.ROLLBACK_CHANGE: RiskLevel.HIGH,
}

_PLACEHOLDER_RELEASE_IDS = {"", "none", "n/a", "null", "unknown"}


class ApprovalError(Exception):
    """Base error for approval workflow violations."""


class ProposalNotFoundError(ApprovalError):
    """Raised when the referenced proposal does not exist on the case."""


class ProposalAlreadyResolvedError(ApprovalError):
    """Raised when a conflicting decision arrives for an already resolved proposal."""


class ProposalConflictError(ApprovalError):
    """Raised when attempting to replace an existing proposal on a case."""


class InactiveReleaseError(ApprovalError):
    """Raised when proposing or approving a rollback for an inactive or missing release."""


def compute_risk_level(action: ActionType) -> RiskLevel:
    """Authoritative backend risk computation."""
    return ACTION_RISK_TABLE[action]


def build_proposal_idempotency_key(
    case_id: str,
    action: ActionType,
    target_service: str,
    target_release_id: str,
) -> str:
    """Create a deterministic idempotency key for a proposal and target release."""
    raw = f"{case_id}:{action.value}:{target_service}:{target_release_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ApprovalService:
    """Manages proposal persistence, risk gating, and idempotent decisions."""

    def __init__(
        self,
        release_source: ReleaseSourcePort,
        executor: ActionExecutorPort,
    ) -> None:
        self._release_source = release_source
        self._executor = executor

    def register_proposal(
        self,
        *,
        case: CaseRecord,
        action: ActionType,
        target_service: str,
        target_release_id: str,
        finding: Finding,
        justification: str,
        evidence_ids: list[str],
        verification_plan: str,
        now: datetime | None = None,
    ) -> tuple[ActionProposal, bool]:
        """Validate citations and release target, compute risk, and store proposal.

        Returns (ActionProposal, was_duplicate). Existing pending/resolved proposals are immutable;
        equivalent re-registrations deduplicate while differing ones raise ProposalConflictError.
        """
        idempotency_key = build_proposal_idempotency_key(
            case.case_id, action, target_service, target_release_id
        )
        if case.proposal is not None:
            if case.proposal.idempotency_key == idempotency_key:
                return case.proposal, True
            raise ProposalConflictError(
                f"Case '{case.case_id}' already has proposal '{case.proposal.proposal_id}' "
                f"({case.proposal.action.value} on {case.proposal.target_service}/{case.proposal.target_release_id}) "
                f"and cannot replace it while status is '{case.proposal.status.value}'."
            )

        registry = EvidenceRegistry(case)
        all_cited = list(dict.fromkeys([*evidence_ids, *finding.supporting_evidence_ids]))
        registry.validate_citations(all_cited)

        if finding.contradicting_evidence_ids:
            registry.validate_citations(finding.contradicting_evidence_ids)

        if action == ActionType.ROLLBACK_CHANGE:
            self._ensure_concrete_active_release(
                target_service=target_service,
                environment=case.alert.environment,
                target_release_id=target_release_id,
                at_time=case.alert.effective_cutoff,
            )

        risk_level = compute_risk_level(action)
        timestamp = now or datetime.now(timezone.utc)
        proposal = ActionProposal(
            proposal_id=f"PROP-{case.case_id}-{idempotency_key[:6]}",
            case_id=case.case_id,
            finding=finding,
            target_release_id=target_release_id,
            target_service=target_service,
            action=action,
            risk_level=risk_level,
            justification=justification,
            evidence_ids=all_cited,
            verification_plan=verification_plan,
            idempotency_key=idempotency_key,
            status=ProposalStatus.PENDING,
            created_at=timestamp,
        )
        case.proposal = proposal
        case.updated_at = timestamp
        if risk_level == RiskLevel.LOW:
            execution = self._executor.execute_action(proposal)
            proposal.status = execution.status
            case.execution = execution
            case.status = (
                CaseStatus.MITIGATED
                if execution.status == ProposalStatus.EXECUTED
                else CaseStatus.OPEN
            )
        else:
            case.status = CaseStatus.AWAITING_APPROVAL
        return proposal, False

    def resolve_decision(
        self,
        *,
        case: CaseRecord,
        proposal_id: str,
        actor: str,
        decision: DecisionChoice,
        comment: str = "",
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> tuple[HumanDecision, ExecutionResult, bool]:
        """Apply an authenticated approve/reject decision idempotently.

        Returns (HumanDecision, ExecutionResult, was_duplicate).
        """
        proposal = case.proposal
        if proposal is None or proposal.proposal_id != proposal_id:
            raise ProposalNotFoundError(
                f"Proposal '{proposal_id}' not found on case '{case.case_id}'."
            )

        effective_key = idempotency_key or f"{proposal.idempotency_key}:{decision.value}:{actor}"

        # Idempotent replay requires identical operation details, not just a matching key.
        if case.decision is not None:
            same_operation = (
                case.decision.decision == decision
                and case.decision.actor == actor
                and case.decision.comment == comment
            )
            same_key = case.decision.idempotency_key == effective_key
            if same_operation and same_key and case.execution is not None:
                return case.decision, case.execution, True
            raise ProposalAlreadyResolvedError(
                f"Proposal '{proposal_id}' was already resolved as '{case.decision.decision.value}' "
                f"by '{case.decision.actor}'."
            )

        if proposal.status != ProposalStatus.PENDING:
            raise ProposalAlreadyResolvedError(
                f"Proposal '{proposal_id}' is not pending (status={proposal.status.value})."
            )

        timestamp = now or datetime.now(timezone.utc)

        if decision == DecisionChoice.APPROVE:
            if proposal.action == ActionType.ROLLBACK_CHANGE or (
                proposal.target_release_id.strip().lower() not in _PLACEHOLDER_RELEASE_IDS
            ):
                self._ensure_concrete_active_release(
                    target_service=proposal.target_service,
                    environment=case.alert.environment,
                    target_release_id=proposal.target_release_id,
                    at_time=timestamp,
                )
            proposal.status = ProposalStatus.APPROVED
            execution = self._executor.execute_action(proposal)
            proposal.status = execution.status
            case.status = CaseStatus.MITIGATED
        else:
            proposal.status = ProposalStatus.REJECTED
            execution = self._executor.create_preventive_ticket(
                case_id=case.case_id,
                proposal=proposal,
                actor=actor,
                reason=comment or "Human rejected proposed action; preventive ticket created.",
            )
            case.status = CaseStatus.OPEN

        human_decision = HumanDecision(
            case_id=case.case_id,
            proposal_id=proposal_id,
            actor=actor,
            decision=decision,
            comment=comment,
            decided_at=timestamp,
            idempotency_key=effective_key,
        )
        case.decision = human_decision
        case.execution = execution
        case.updated_at = timestamp
        return human_decision, execution, False

    def _ensure_concrete_active_release(
        self,
        *,
        target_service: str,
        environment: str,
        target_release_id: str,
        at_time: datetime,
    ) -> None:
        normalized = target_release_id.strip().lower()
        if normalized in _PLACEHOLDER_RELEASE_IDS:
            raise InactiveReleaseError(
                f"Action requires a concrete active release ID, got '{target_release_id}'."
            )
        resolution = self._release_source.resolve_active_releases(
            service=target_service,
            environment=environment,
            at_time=at_time,
        )
        active_ids = {
            rel.release_id for rel in resolution.releases if rel.status in {"active", "canary"}
        }
        if target_release_id not in active_ids:
            raise InactiveReleaseError(
                f"Target release '{target_release_id}' is not active for "
                f"'{target_service}' in '{environment}' at {at_time.isoformat()}."
            )

