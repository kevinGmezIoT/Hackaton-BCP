"""Application service orchestrating case lifecycle, queries, decisions, and verification."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from vigia.application.investigator import InvestigatorAgent
from vigia.application.tools import CaseDependencies
from vigia.domain.contracts import (
    Alert,
    AuditRecord,
    CaseEvent,
    CaseRecord,
    CaseStatus,
    ConnectorStatus,
    DecisionChoice,
    EventType,
    VerificationResult,
)


class CaseNotFoundError(KeyError):
    """Raised when a requested case_id does not exist."""


class CaseService:
    """Coordinates case creation, investigator execution, queries, and human approvals."""

    def __init__(
        self,
        deps: CaseDependencies,
        investigator: InvestigatorAgent,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._deps = deps
        self._investigator = investigator
        self._clock = clock
        self._cases: dict[str, CaseRecord] = {}
        self._sequence = 1041

    def _decision_clock(self, case: CaseRecord, decided_at: datetime | None) -> datetime:
        """Align decision timestamps with the simulation cutoff clock when replaying scenarios."""
        if decided_at is not None:
            return decided_at
        if self._clock is not None:
            return self._clock()
        if case.alert.cutoff_at is not None:
            return case.alert.cutoff_at
        return datetime.now(timezone.utc)

    def start_case(self, alert: Alert) -> CaseRecord:
        """Open a new investigation case from an early anomaly alert and run Alternative A."""
        self._sequence += 1
        case_id = alert.case_id or f"CASE-{self._sequence}"
        canonical_service = self._deps.catalog.resolve_canonical_name(alert.service)
        normalized_alert = alert.model_copy(
            update={"case_id": case_id, "service": canonical_service}
        )
        service_config = self._deps.catalog.get_service(canonical_service)
        now = self._clock() if self._clock is not None else datetime.now(timezone.utc)
        case = CaseRecord(
            case_id=case_id,
            alert=normalized_alert,
            status=CaseStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
        self._cases[case_id] = case
        return self._investigator.run(case, self._deps, service_config)

    def get_case(self, case_id: str) -> CaseRecord:
        """Retrieve a CaseRecord by ID."""
        if case_id not in self._cases:
            raise CaseNotFoundError(f"Case '{case_id}' was not found.")
        return self._cases[case_id]

    def list_events(self, case_id: str) -> list[CaseEvent]:
        """Return the ordered timeline events for a case."""
        return list(self.get_case(case_id).events)

    def answer_query(self, case_id: str, question: str) -> dict[str, object]:
        """Answer an informational question strictly from recorded case evidence and gaps."""
        case = self.get_case(case_id)
        lowered = question.lower()

        matched = [
            ev
            for ev in case.evidence
            if any(
                token in ev.observed_fact.lower()
                or token in ev.source.lower()
                or token in ev.service.lower()
                for token in lowered.split()
                if len(token) > 2
            )
        ]
        if not matched:
            matched = case.evidence[:5]

        references = [ev.evidence_id for ev in matched]
        lines = [
            f"Case {case.case_id} (status={case.status.value}, service={case.alert.service}):"
        ]
        if case.proposal:
            lines.append(
                f"Proposal {case.proposal.proposal_id}: {case.proposal.action.value} on "
                f"{case.proposal.target_service} ({case.proposal.target_release_id}), "
                f"risk={case.proposal.risk_level.value}, status={case.proposal.status.value}."
            )
        for ev in matched:
            lines.append(
                f"- [{ev.evidence_id}] ({ev.source}, {ev.reproducible_reference}): {ev.observed_fact}"
            )
        if case.connector_gaps:
            lines.append("Gaps: " + "; ".join(case.connector_gaps))

        return {
            "case_id": case.case_id,
            "answer": "\n".join(lines),
            "references": references,
        }

    def resolve_approval(
        self,
        *,
        case_id: str,
        proposal_id: str,
        actor: str,
        decision: DecisionChoice,
        comment: str = "",
        idempotency_key: str | None = None,
        decided_at: datetime | None = None,
        verification_at: datetime | None = None,
    ) -> CaseRecord:
        """Process an authenticated approval or rejection and verify post-action metrics."""
        case = self.get_case(case_id)
        action_time = self._decision_clock(case, decided_at)
        human_decision, execution, was_duplicate = self._deps.approvals.resolve_decision(
            case=case,
            proposal_id=proposal_id,
            actor=actor,
            decision=decision,
            comment=comment,
            idempotency_key=idempotency_key,
            now=action_time,
        )

        if was_duplicate:
            return case

        decision_event = CaseEvent(
            event_type=EventType.DECISION,
            case_id=case.case_id,
            timestamp=human_decision.decided_at,
            role="Human",
            summary=(
                f"Decision '{decision.value}' by {actor} on {proposal_id}: "
                f"{execution.detail}"
            ),
            references=case.proposal.evidence_ids if case.proposal else [],
            payload={
                "proposal_id": proposal_id,
                "decision": decision.value,
                "execution_status": execution.status.value,
                "preventive_ticket_id": execution.preventive_ticket_id,
            },
        )
        case.events.append(decision_event)
        self._deps.audit.emit_event(decision_event)
        self._deps.audit.record_audit(
            AuditRecord(
                case_id=case.case_id,
                timestamp=human_decision.decided_at,
                actor=actor,
                action_or_tool=f"decision:{decision.value}",
                sanitized_args={"proposal_id": proposal_id, "comment": comment},
                status=execution.status.value,
                summary=execution.detail,
                references=case.proposal.evidence_ids if case.proposal else [],
            )
        )

        if decision == DecisionChoice.APPROVE and case.proposal is not None:
            self._verify_recovery(
                case,
                action_at=human_decision.decided_at,
                verification_at=verification_at,
            )

        return case

    def _verify_recovery(
        self,
        case: CaseRecord,
        *,
        action_at: datetime,
        verification_at: datetime | None = None,
    ) -> VerificationResult:
        """Require at least one post-execution reading confirming recovery before closing."""
        assert case.proposal is not None
        verify_time = verification_at or (action_at + timedelta(minutes=10))
        service_cfg = self._deps.catalog.get_service(case.alert.service)
        threshold = (
            case.alert.threshold_value
            if case.alert.threshold_value is not None
            else (service_cfg.threshold_ms if service_cfg else 800.0)
        )
        window_minutes = max(1, int((verify_time - action_at).total_seconds() // 60) + 1)

        series = self._deps.telemetry.query_metrics(
            service=case.alert.service,
            environment=case.alert.environment,
            metric=case.alert.metric,
            window_minutes=window_minutes,
            cutoff_at=verify_time,
            allow_post_action=True,
        )

        post_points = [p for p in series.points if p.timestamp > action_at]
        pre_points = [p for p in series.points if p.timestamp <= action_at]
        pre_action_value = (
            case.alert.current_value
            if case.alert.current_value is not None
            else (pre_points[-1].value if pre_points else None)
        )

        post_value: float | None = None
        if series.status != ConnectorStatus.OK or not post_points:
            recovered = False
            case.status = CaseStatus.OPEN
            detail = (
                f"No post-execution metric reading found after {action_at.isoformat()} "
                f"(status={series.status.value}); case remains open pending verification."
            )
        else:
            latest_post = post_points[-1]
            post_value = latest_post.value
            improved = pre_action_value is None or post_value < pre_action_value
            if post_value < threshold and improved:
                recovered = True
                case.status = CaseStatus.CLOSED
                detail = (
                    f"Verified recovery on {case.alert.service} at {latest_post.timestamp.isoformat()}: "
                    f"{case.alert.metric} dropped to {post_value} {series.unit} "
                    f"(threshold={threshold} {series.unit})."
                )
            else:
                recovered = False
                case.status = CaseStatus.OPEN
                detail = (
                    f"Post-execution metric at {latest_post.timestamp.isoformat()} remains degraded at "
                    f"{post_value} {series.unit} (threshold={threshold} {series.unit}); case remains open."
                )

        verification = VerificationResult(
            case_id=case.case_id,
            proposal_id=case.proposal.proposal_id,
            verified_at=verify_time,
            metric_value=post_value,
            threshold=threshold,
            recovered=recovered,
            case_status=case.status,
            detail=detail,
        )
        case.verification = verification
        case.updated_at = verify_time

        event = CaseEvent(
            event_type=EventType.VERIFICATION,
            case_id=case.case_id,
            timestamp=verify_time,
            role="Verification",
            summary=detail,
            references=[series.reproducible_query],
            payload={
                "recovered": recovered,
                "metric_value": post_value,
                "case_status": case.status.value,
            },
        )
        case.events.append(event)
        self._deps.audit.emit_event(event)
        self._deps.audit.record_audit(
            AuditRecord(
                case_id=case.case_id,
                timestamp=verify_time,
                actor="system_verifier",
                action_or_tool="verify_recovery",
                sanitized_args={"service": case.alert.service, "metric": case.alert.metric},
                status="recovered" if recovered else "open",
                summary=detail,
                references=[series.reproducible_query],
            )
        )
        return verification

