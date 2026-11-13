"""Application service orchestrating case lifecycle, queries, decisions, and verification."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from application.investigator import InvestigatorAgent
from application.tools import CaseDependencies
from domain.contracts import (
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
    ) -> None:
        self._deps = deps
        self._investigator = investigator
        self._cases: dict[str, CaseRecord] = {}
        self._sequence = 1041

    def start_case(self, alert: Alert) -> CaseRecord:
        """Open a new investigation case from an early anomaly alert and run Alternative A."""
        self._sequence += 1
        case_id = alert.case_id or f"CASE-{self._sequence}"
        canonical_service = self._deps.catalog.resolve_canonical_name(alert.service)
        normalized_alert = alert.model_copy(
            update={"case_id": case_id, "service": canonical_service}
        )
        service_config = self._deps.catalog.get_service(canonical_service)
        now = datetime.now(timezone.utc)
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
        verification_at: datetime | None = None,
    ) -> CaseRecord:
        """Process an authenticated approval or rejection and verify post-action metrics."""
        case = self.get_case(case_id)
        human_decision, execution, was_duplicate = self._deps.approvals.resolve_decision(
            case=case,
            proposal_id=proposal_id,
            actor=actor,
            decision=decision,
            comment=comment,
            idempotency_key=idempotency_key,
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
            self._verify_recovery(case, verification_at=verification_at)

        return case

    def _verify_recovery(
        self,
        case: CaseRecord,
        *,
        verification_at: datetime | None = None,
    ) -> VerificationResult:
        """Consult post-execution telemetry to confirm recovery before closing the case."""
        assert case.proposal is not None
        verify_time = verification_at or (case.alert.effective_cutoff + timedelta(minutes=10))
        service_cfg = self._deps.catalog.get_service(case.alert.service)
        threshold = (
            case.alert.threshold_value
            if case.alert.threshold_value is not None
            else (service_cfg.threshold_ms if service_cfg else 800.0)
        )

        series = self._deps.telemetry.query_metrics(
            service=case.alert.service,
            environment=case.alert.environment,
            metric=case.alert.metric,
            window_minutes=10,
            cutoff_at=verify_time,
            allow_post_action=True,
        )

        if series.status != ConnectorStatus.OK or series.current_value is None:
            recovered = False
            case.status = CaseStatus.OPEN
            detail = (
                f"Post-action metric unavailable ({series.status.value}: {series.note}); "
                "case remains open pending verification."
            )
        elif series.current_value < threshold and (
            case.alert.current_value is None or series.current_value < case.alert.current_value
        ):
            recovered = True
            case.status = CaseStatus.CLOSED
            detail = (
                f"Verified recovery on {case.alert.service}: {case.alert.metric} dropped to "
                f"{series.current_value} {series.unit} (threshold={threshold} {series.unit})."
            )
        else:
            recovered = False
            case.status = CaseStatus.OPEN
            detail = (
                f"Metric remains degraded at {series.current_value} {series.unit} "
                f"(threshold={threshold} {series.unit}); case remains open."
            )

        verification = VerificationResult(
            case_id=case.case_id,
            proposal_id=case.proposal.proposal_id,
            verified_at=verify_time,
            metric_value=series.current_value,
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
                "metric_value": series.current_value,
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

