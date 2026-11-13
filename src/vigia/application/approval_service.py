"""Proposal registration, backend risk evaluation, shadow review, and idempotent decision service."""

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
    Evidence,
    ExecutionResult,
    ExternalOperationRecord,
    Finding,
    HumanDecision,
    OperationalMode,
    OperationStatus,
    ProposalHistoryEntry,
    ProposalReview,
    ProposalStatus,
    ReviewVerdict,
    RiskLevel,
)
from vigia.domain.ports import ActionExecutorPort, ReleaseSourcePort, ServiceCatalogPort

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


class ProposalUnderReviewError(ProposalAlreadyResolvedError):
    """Raised when attempting to approve a proposal currently under conversational reevaluation."""


class ProposalWithdrawnError(ProposalAlreadyResolvedError):
    """Raised when attempting to approve a proposal that was withdrawn in a newer revision."""


class ProposalConflictError(ApprovalError):
    """Raised when attempting to replace an existing proposal on the same case revision."""


class InactiveReleaseError(ApprovalError):
    """Raised when proposing or approving a rollback for an inactive or replaced release."""


class InsufficientRollbackEvidenceError(ApprovalError):
    """Raised when a rollback is proposed despite pre-deploy errors, telemetry gaps, or missing refutation checks."""


class ShadowModeOperationError(ApprovalError):
    """Raised when attempting to approve or execute an action while operating in shadow mode."""


class UnauthorizedApproverError(ApprovalError):
    """Raised when the actor lacks permission for the target service."""


class StaleProposalVersionError(ApprovalError):
    """Raised when a decision references an outdated proposal version."""


def ensure_proposal_history(case: CaseRecord) -> None:
    """Backfill immutable proposal history from an older CaseRecord that lacks history entries."""
    if case.proposal is not None and not case.proposal_history:
        case.proposal_history.append(
            ProposalHistoryEntry(
                proposal_id=case.proposal.proposal_id,
                revision_number=case.proposal.revision_number,
                version=case.proposal.version,
                action=case.proposal.action,
                target_service=case.proposal.target_service,
                target_release_id=case.proposal.target_release_id,
                status=case.proposal.status,
                hypothesis=case.proposal.finding.hypothesis,
                justification=case.proposal.justification,
                evidence_ids=list(case.proposal.evidence_ids),
                outcome="initial",
                created_at=case.proposal.created_at,
                updated_at=case.updated_at,
            )
        )


def upsert_proposal_history(
    case: CaseRecord,
    proposal: ActionProposal,
    *,
    outcome: str = "initial",
    turn_id: str | None = None,
    updated_at: datetime | None = None,
) -> None:
    """Record or update an immutable snapshot for a proposal revision/version."""
    ts = updated_at or datetime.now(timezone.utc)
    for entry in case.proposal_history:
        if (
            entry.proposal_id == proposal.proposal_id
            and entry.revision_number == proposal.revision_number
            and entry.version == proposal.version
        ):
            entry.status = proposal.status
            entry.outcome = outcome
            entry.turn_id = turn_id or entry.turn_id
            entry.updated_at = ts
            return
    case.proposal_history.append(
        ProposalHistoryEntry(
            proposal_id=proposal.proposal_id,
            revision_number=proposal.revision_number,
            version=proposal.version,
            action=proposal.action,
            target_service=proposal.target_service,
            target_release_id=proposal.target_release_id,
            status=proposal.status,
            hypothesis=proposal.finding.hypothesis,
            justification=proposal.justification,
            evidence_ids=list(proposal.evidence_ids),
            outcome=outcome,
            turn_id=turn_id,
            created_at=proposal.created_at,
            updated_at=ts,
        )
    )


def compute_risk_level(action: ActionType) -> RiskLevel:
    """Authoritative backend risk computation."""
    return ACTION_RISK_TABLE[action]


def build_proposal_idempotency_key(
    case_id: str,
    action: ActionType,
    target_service: str,
    target_release_id: str,
    revision_number: int = 1,
) -> str:
    """Create a deterministic idempotency key for a proposal and target release."""
    suffix = f":{revision_number}" if revision_number > 1 else ""
    raw = f"{case_id}:{action.value}:{target_service}:{target_release_id}{suffix}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ApprovalService:
    """Manages proposal persistence, shadow reviews, risk gating, and idempotent decisions."""

    def __init__(
        self,
        release_source: ReleaseSourcePort,
        executor: ActionExecutorPort,
        *,
        catalog: ServiceCatalogPort | None = None,
        mode: OperationalMode = OperationalMode.ASSISTED,
        strict_rollback_validation: bool = False,
    ) -> None:
        self._release_source = release_source
        self._executor = executor
        self._catalog = catalog
        self._mode = mode
        self._strict_rollback_validation = strict_rollback_validation

    def _effective_mode(self, case: CaseRecord) -> OperationalMode:
        return case.mode or self._mode

    def get_operation_status(self, operation_id: str):
        """Query an external pipeline operation status via the executor port."""
        return self._executor.get_operation_status(operation_id)

    def _current_revision_number(self, case: CaseRecord) -> int:
        if case.revisions:
            return case.revisions[-1].revision_number
        return 1

    def _current_cutoff(self, case: CaseRecord) -> datetime:
        if case.revisions:
            return case.revisions[-1].cutoff_at
        return case.alert.effective_cutoff

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
        """Validate citations and release target, compute risk, and store proposal."""
        rev_num = self._current_revision_number(case)
        idempotency_key = build_proposal_idempotency_key(
            case.case_id, action, target_service, target_release_id, rev_num
        )
        next_version = 1
        if case.proposal is not None:
            if case.proposal.revision_number == rev_num:
                if case.proposal.idempotency_key == idempotency_key:
                    return case.proposal, True
                raise ProposalConflictError(
                    f"Case '{case.case_id}' already has proposal '{case.proposal.proposal_id}' "
                    f"({case.proposal.action.value} on {case.proposal.target_service}/{case.proposal.target_release_id}) "
                    f"and cannot replace it while status is '{case.proposal.status.value}'."
                )
            next_version = case.proposal.version + 1

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
                at_time=self._current_cutoff(case),
            )
            self._validate_rollback_evidence(
                case=case,
                cited_evidence=registry.get_many(all_cited),
                target_service=target_service,
                target_release_id=target_release_id,
            )

        risk_level = compute_risk_level(action)
        timestamp = now or datetime.now(timezone.utc)
        effective_mode = self._effective_mode(case)

        initial_status = (
            ProposalStatus.RECOMMENDED
            if effective_mode == OperationalMode.SHADOW
            else ProposalStatus.PENDING
        )
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
            status=initial_status,
            revision_number=rev_num,
            version=next_version,
            created_at=timestamp,
        )
        case.proposal = proposal
        if case.revisions:
            case.revisions[-1].proposal_id = proposal.proposal_id
            case.revisions[-1].action_outcome = "initial"
            case.revisions[-1].summary = finding.hypothesis
        case.updated_at = timestamp

        if effective_mode == OperationalMode.SHADOW:
            case.status = CaseStatus.AWAITING_REVIEW
            upsert_proposal_history(case, proposal, outcome="initial", updated_at=timestamp)
            return proposal, False

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
        upsert_proposal_history(case, proposal, outcome="initial", updated_at=timestamp)
        return proposal, False

    def _validate_rollback_evidence(
        self,
        *,
        case: CaseRecord,
        cited_evidence: list[Evidence],
        target_service: str,
        target_release_id: str,
    ) -> None:
        """Verify rollback preconditions: no pre-deploy errors, no decisive telemetry gaps, and refutation check."""
        for ev in case.evidence:
            if (
                ev.service.lower() == target_service.lower()
                and ev.comparison_result == "pre_deploy_errors_detected"
            ):
                raise InsufficientRollbackEvidenceError(
                    f"Cannot recommend rollback of '{target_release_id}' on '{target_service}': "
                    f"evidence {ev.evidence_id} shows errors already occurred before deployment."
                )

        for gap in case.connector_gaps:
            lowered = gap.lower()
            if (
                ("logs (" in lowered or "metrics (" in lowered)
                and (
                    "connector_unavailable" in lowered
                    or "unauthorized" in lowered
                )
            ):
                raise InsufficientRollbackEvidenceError(
                    f"Cannot recommend rollback of '{target_release_id}' due to decisive telemetry gap: {gap}"
                )

        if not self._strict_rollback_validation:
            return

        sources = {ev.source for ev in cited_evidence}
        obs_types = {ev.observation_type for ev in cited_evidence}

        has_symptom = bool(
            sources & {"metrics", "analytics_anomaly"}
            or obs_types & {"metrics", "anomaly"}
        )
        if not has_symptom:
            raise InsufficientRollbackEvidenceError(
                "Pilot rollback recommendation requires cited symptom telemetry evidence (metrics or anomaly)."
            )

        has_release_or_code = bool(
            sources & {"release_source", "change_log", "git_diff", "git_show"}
        )
        if not has_release_or_code:
            raise InsufficientRollbackEvidenceError(
                "Pilot rollback recommendation requires cited release and code evidence."
            )

        has_refutation = any(
            ev.source == "logs"
            and (
                ev.comparison_result == "post_deploy_errors_only"
                or (ev.cohort is not None and target_release_id.lower() in ev.cohort.lower())
                or target_release_id.lower() in ev.observed_fact.lower()
            )
            for ev in cited_evidence
        )
        if not has_refutation:
            raise InsufficientRollbackEvidenceError(
                "Pilot rollback recommendation requires a cited log/cohort refutation check "
                f"confirming errors started after '{target_release_id}'."
            )

    def record_review(
        self,
        *,
        case: CaseRecord,
        proposal_id: str,
        reviewer: str,
        verdict: ReviewVerdict,
        comment: str = "",
        proposal_revision: int | None = None,
        intervened_externally: bool = False,
        confirmed_cause: str | None = None,
        now: datetime | None = None,
    ) -> ProposalReview:
        """Record a human evaluation of a recommendation in shadow mode without executing external actions."""
        proposal = case.proposal
        if proposal is None or proposal.proposal_id != proposal_id:
            raise ProposalNotFoundError(
                f"Proposal '{proposal_id}' not found on case '{case.case_id}'."
            )
        target_rev = (
            proposal_revision
            if proposal_revision is not None
            else proposal.revision_number
        )
        if target_rev != proposal.revision_number:
            raise StaleProposalVersionError(
                f"Review targets proposal revision {target_rev}, but active proposal is revision {proposal.revision_number}."
            )

        timestamp = now or datetime.now(timezone.utc)
        review = ProposalReview(
            review_id=f"REV-{case.case_id}-{len(case.reviews) + 1:02d}",
            case_id=case.case_id,
            proposal_id=proposal_id,
            proposal_revision=target_rev,
            reviewer=reviewer,
            verdict=verdict,
            comment=comment,
            intervened_externally=intervened_externally,
            confirmed_cause=confirmed_cause,
            reviewed_at=timestamp,
        )
        case.reviews.append(review)
        case.updated_at = timestamp
        return review

    def prepare_decision_reservation(
        self,
        *,
        case: CaseRecord,
        proposal_id: str,
        actor: str,
        decision: DecisionChoice,
        comment: str = "",
        idempotency_key: str | None = None,
        actor_groups: list[str] | None = None,
        proposal_version: int | None = None,
        require_proposal_version: bool = False,
        now: datetime | None = None,
    ) -> tuple[HumanDecision, ExecutionResult | None, str | None, bool]:
        """Validate permissions, proposal currency, and reserve an approval/rejection before dispatch."""
        if self._effective_mode(case) == OperationalMode.SHADOW:
            raise ShadowModeOperationError(
                f"Case '{case.case_id}' is running in shadow mode; action execution is disabled."
            )

        ensure_proposal_history(case)
        proposal = case.proposal
        if proposal is None or proposal.proposal_id != proposal_id:
            historical = [
                entry for entry in case.proposal_history if entry.proposal_id == proposal_id
            ]
            if historical:
                latest_hist = historical[-1]
                if latest_hist.status == ProposalStatus.WITHDRAWN:
                    raise ProposalWithdrawnError(
                        f"Proposal '{proposal_id}' was withdrawn in revision {latest_hist.revision_number} "
                        "and cannot be approved."
                    )
                raise StaleProposalVersionError(
                    f"Proposal '{proposal_id}' has been superseded by revision {latest_hist.revision_number}."
                )
            raise ProposalNotFoundError(
                f"Proposal '{proposal_id}' not found on case '{case.case_id}'."
            )

        if require_proposal_version and proposal_version is None:
            raise StaleProposalVersionError(
                f"Proposal '{proposal_id}' approval requires explicit current proposal_version "
                f"(current={proposal.version})."
            )
        if proposal_version is not None and proposal.version != proposal_version:
            raise StaleProposalVersionError(
                f"Proposal '{proposal_id}' version mismatch: expected {proposal.version}, got {proposal_version}."
            )

        if actor_groups is not None and self._catalog is not None:
            svc_cfg = self._catalog.get_service(proposal.target_service)
            if svc_cfg is not None:
                allowed = {
                    svc_cfg.approver_group.lower(),
                    svc_cfg.owner_team.lower(),
                    "admin",
                }
                provided = {g.strip().lower() for g in actor_groups if g.strip()}
                if not (provided & allowed):
                    raise UnauthorizedApproverError(
                        f"Actor '{actor}' (groups={actor_groups}) is not authorized to approve actions "
                        f"for '{proposal.target_service}' (required='{svc_cfg.approver_group}')."
                    )

        effective_key = idempotency_key or f"{proposal.idempotency_key}:{decision.value}:{actor}"

        if case.decision is not None:
            same_operation = (
                case.decision.decision == decision
                and case.decision.actor == actor
                and case.decision.comment == comment
            )
            same_key = case.decision.idempotency_key == effective_key
            if same_operation and same_key and case.execution is not None:
                return case.decision, case.execution, None, True
            raise ProposalAlreadyResolvedError(
                f"Proposal '{proposal_id}' was already resolved as '{case.decision.decision.value}' "
                f"by '{case.decision.actor}'."
            )

        if proposal.status == ProposalStatus.UNDER_REVIEW:
            raise ProposalUnderReviewError(
                f"Proposal '{proposal_id}' is currently under reevaluation and cannot be approved until resolved."
            )
        if proposal.status == ProposalStatus.WITHDRAWN:
            raise ProposalWithdrawnError(
                f"Proposal '{proposal_id}' was withdrawn and cannot be approved."
            )
        if proposal.status != ProposalStatus.PENDING:
            raise ProposalAlreadyResolvedError(
                f"Proposal '{proposal_id}' is not pending (status={proposal.status.value})."
            )

        timestamp = now or datetime.now(timezone.utc)
        op_key: str | None = None
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
            case.status = CaseStatus.OBSERVING
            op_key = f"op:{case.case_id}:{proposal.proposal_id}:{proposal.target_release_id}"
        else:
            proposal.status = ProposalStatus.REJECTED
            case.status = CaseStatus.OPEN
            op_key = f"ticket:{case.case_id}:{proposal.proposal_id}:{effective_key}"

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
        case.updated_at = timestamp
        upsert_proposal_history(
            case,
            proposal,
            outcome=f"reserved_{decision.value}",
            updated_at=timestamp,
        )
        return human_decision, None, op_key, False

    def dispatch_reserved_execution(
        self,
        *,
        case: CaseRecord,
        decision: DecisionChoice,
        actor: str,
        comment: str,
        op_key: str,
        timestamp: datetime,
    ) -> tuple[ExecutionResult, ExternalOperationRecord | None]:
        """Dispatch an already reserved approve/reject decision to the external executor."""
        proposal = case.proposal
        assert proposal is not None
        op: ExternalOperationRecord | None = None
        if decision == DecisionChoice.APPROVE:
            self._ensure_concrete_active_release(
                target_service=proposal.target_service,
                environment=case.alert.environment,
                target_release_id=proposal.target_release_id,
                at_time=timestamp,
            ) if (
                proposal.action == ActionType.ROLLBACK_CHANGE
                or proposal.target_release_id.strip().lower() not in _PLACEHOLDER_RELEASE_IDS
            ) else None
            if hasattr(self._executor, "dispatch_operation"):
                op = self._executor.dispatch_operation(
                    case_id=case.case_id,
                    environment=case.alert.environment,
                    proposal=proposal,
                    operation_key=op_key,
                    now=timestamp,
                )
                case.external_operations = [
                    existing
                    for existing in case.external_operations
                    if existing.operation_id != op.operation_id
                ] + [op]
                if op.status == OperationStatus.EXECUTED:
                    proposal.status = ProposalStatus.EXECUTED
                    case.status = CaseStatus.MITIGATED
                elif op.status == OperationStatus.FAILED:
                    proposal.status = ProposalStatus.FAILED
                    case.status = CaseStatus.OPEN
                else:
                    proposal.status = ProposalStatus.APPROVED
                    case.status = CaseStatus.OBSERVING
                execution = ExecutionResult(
                    proposal_id=proposal.proposal_id,
                    action=proposal.action,
                    status=proposal.status,
                    detail=op.detail,
                    executed_at=op.completed_at or op.started_at,
                    preventive_ticket_id=op.external_ticket_id,
                    operation_id=op.operation_id,
                )
            else:
                execution = self._executor.execute_action(proposal)
                proposal.status = execution.status
                case.status = (
                    CaseStatus.MITIGATED
                    if execution.status == ProposalStatus.EXECUTED
                    else CaseStatus.OPEN
                )
        else:
            proposal.status = ProposalStatus.REJECTED
            execution = self._executor.create_preventive_ticket(
                case_id=case.case_id,
                proposal=proposal,
                actor=actor,
                reason=comment or "Human rejected proposed action; preventive ticket created.",
            )
            case.status = CaseStatus.OPEN

        case.execution = execution
        case.updated_at = execution.executed_at
        upsert_proposal_history(
            case,
            proposal,
            outcome=proposal.status.value,
            updated_at=execution.executed_at,
        )
        return execution, op

    def resolve_decision(
        self,
        *,
        case: CaseRecord,
        proposal_id: str,
        actor: str,
        decision: DecisionChoice,
        comment: str = "",
        idempotency_key: str | None = None,
        actor_groups: list[str] | None = None,
        proposal_version: int | None = None,
        require_proposal_version: bool = False,
        now: datetime | None = None,
    ) -> tuple[HumanDecision, ExecutionResult, bool]:
        """Apply an authenticated approve/reject decision idempotently in assisted mode."""
        human_decision, cached_exec, op_key, was_duplicate = self.prepare_decision_reservation(
            case=case,
            proposal_id=proposal_id,
            actor=actor,
            decision=decision,
            comment=comment,
            idempotency_key=idempotency_key,
            actor_groups=actor_groups,
            proposal_version=proposal_version,
            require_proposal_version=require_proposal_version,
            now=now,
        )
        if was_duplicate:
            assert cached_exec is not None
            return human_decision, cached_exec, True
        assert op_key is not None
        execution, _ = self.dispatch_reserved_execution(
            case=case,
            decision=decision,
            actor=actor,
            comment=comment,
            op_key=op_key,
            timestamp=human_decision.decided_at,
        )
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
