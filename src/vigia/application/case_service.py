"""Application service orchestrating onboarding probes, alert ingestion, worker runs, reviews, approvals, and recovery verification."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from vigia.adapters.outbound.case_store import SqliteCaseStoreAdapter
from vigia.application.conversation_service import ConversationService
from vigia.application.investigator import InvestigatorAgent
from vigia.application.tools import CaseDependencies
from vigia.domain.contracts import (
    Alert,
    AlertEnvelope,
    AlertIngestionResult,
    AuditRecord,
    CaseEvent,
    CaseRecord,
    CaseRevision,
    CaseStatus,
    ChatSubmissionResult,
    ConnectorStatus,
    ConversationTurn,
    DecisionChoice,
    EventType,
    OperationalMode,
    OperationStatus,
    PilotMetricsSummary,
    ProposalStatus,
    RecoveryCriteriaConfig,
    ReviewVerdict,
    ServiceOnboardingReport,
    SourceProbeReport,
    VerificationResult,
)
from vigia.domain.ports import CaseStorePort


class CaseNotFoundError(KeyError):
    """Raised when a requested case_id does not exist."""


class UnknownServiceError(ValueError):
    """Raised when an incoming alert references an unregistered service or environment."""


class OperationNotFoundError(KeyError):
    """Raised when a referenced external operation ID is unknown."""


class CaseService:
    """Coordinates case creation, alert ingestion, worker execution, shadow reviews, and assisted approvals."""

    def __init__(
        self,
        deps: CaseDependencies,
        investigator: InvestigatorAgent,
        *,
        clock: Callable[[], datetime] | None = None,
        default_mode: OperationalMode = OperationalMode.ASSISTED,
        re_evaluate_on_update: bool = True,
    ) -> None:
        self._deps = deps
        self._investigator = investigator
        self._clock = clock
        self._default_mode = default_mode
        self._re_evaluate_on_update = re_evaluate_on_update
        self._store: CaseStorePort = deps.case_store or SqliteCaseStoreAdapter(":memory:")
        if self._deps.case_store is None:
            self._deps.case_store = self._store
        self._conversation = ConversationService(
            store=self._store,
            deps=self._deps,
            investigator=self._investigator,
        )
        self._sequence = 1041

    def _now(self) -> datetime:
        return self._clock() if self._clock is not None else datetime.now(timezone.utc)

    def close(self) -> None:
        """Release underlying case store resources if supported."""
        close_fn = getattr(self._store, "close", None)
        if callable(close_fn):
            close_fn()

    def _decision_clock(self, case: CaseRecord, decided_at: datetime | None) -> datetime:
        """Align decision timestamps with the simulation cutoff clock when replaying scenarios."""
        if decided_at is not None:
            return decided_at
        if self._clock is not None:
            return self._clock()
        if case.alert.cutoff_at is not None:
            return case.alert.cutoff_at
        return datetime.now(timezone.utc)

    def _validate_alert_target(self, alert: Alert) -> str:
        svc_cfg = self._deps.catalog.get_service(alert.service)
        if svc_cfg is None:
            raise UnknownServiceError(
                f"Service '{alert.service}' is not registered in the Vigía service catalog."
            )
        if svc_cfg.environment.lower() != alert.environment.lower():
            raise UnknownServiceError(
                f"Environment '{alert.environment}' is not enabled for service '{svc_cfg.service}' "
                f"(expected '{svc_cfg.environment}')."
            )
        return svc_cfg.service

    def probe_service_onboarding(
        self,
        service_or_alias: str,
        *,
        environment: str = "production",
        at_time: datetime | None = None,
    ) -> ServiceOnboardingReport:
        """Verify service registration, dependencies, telemetry, and release connectivity."""
        probe_time = at_time or self._now()
        svc_cfg = self._deps.catalog.get_service(service_or_alias)
        if svc_cfg is None:
            return ServiceOnboardingReport(
                service=service_or_alias,
                canonical_service=service_or_alias,
                environment=environment,
                ready=False,
                pending_conclusions=[
                    f"Service '{service_or_alias}' is missing from the service catalog."
                ],
            )

        canonical = svc_cfg.service
        probes: dict[str, SourceProbeReport] = {}
        pending_conclusions: list[str] = []

        arch_res = self._deps.architecture.query_dependencies(
            canonical, environment, direction="outbound"
        )
        declared_deps = [rel.target_service for rel in arch_res.relations]
        arch_rev = arch_res.relations[0].revision if arch_res.relations else None
        arch_ref = arch_res.relations[0].reference if arch_res.relations else svc_cfg.architecture.path
        probes["architecture"] = SourceProbeReport(
            source="architecture",
            status=arch_res.status,
            summary=(
                f"Dependencies: {', '.join(declared_deps)}"
                if declared_deps
                else (arch_res.note or "No outbound dependencies declared.")
            ),
            revision=arch_rev,
            reference=arch_ref,
        )
        if arch_res.status != ConnectorStatus.OK:
            pending_conclusions.append(
                f"Architecture check incomplete ({arch_res.status.value}): {arch_res.note}"
            )

        metric_res = self._deps.telemetry.query_metrics(
            canonical,
            environment,
            svc_cfg.telemetry.impact_metric,
            30,
            probe_time,
        )
        latest_ts = metric_res.points[-1].timestamp if metric_res.points else None
        probes["metrics"] = SourceProbeReport(
            source="metrics",
            status=metric_res.status,
            summary=(
                f"Latest {svc_cfg.telemetry.impact_metric}={metric_res.current_value} "
                f"{metric_res.unit} at {latest_ts.isoformat() if latest_ts else 'n/a'}"
                if metric_res.status == ConnectorStatus.OK
                else (metric_res.note or metric_res.status.value)
            ),
            latest_timestamp=latest_ts,
            reference=metric_res.reproducible_query,
        )
        if metric_res.status != ConnectorStatus.OK:
            pending_conclusions.append(
                f"Metrics check incomplete ({metric_res.status.value}): {metric_res.note}"
            )

        release_target = declared_deps[0] if declared_deps else canonical
        rel_res = self._deps.releases.resolve_active_releases(
            release_target, environment, probe_time
        )
        active_rel = rel_res.releases[0] if rel_res.releases else None
        probes["releases"] = SourceProbeReport(
            source="releases",
            status=rel_res.status,
            summary=(
                f"Active {release_target} release={active_rel.release_id} "
                f"(sha={active_rel.deployed_sha})"
                if active_rel is not None
                else (rel_res.note or rel_res.status.value)
            ),
            latest_timestamp=active_rel.deployed_at if active_rel else None,
            revision=active_rel.deployed_sha if active_rel else None,
            reference=f"{release_target}/{environment}",
        )
        if rel_res.status != ConnectorStatus.OK:
            pending_conclusions.append(
                f"Release resolution incomplete ({rel_res.status.value}): {rel_res.note}"
            )

        logs_res = self._deps.telemetry.search_logs(
            release_target, environment, "error", 30, probe_time
        )
        probes["logs"] = SourceProbeReport(
            source="logs",
            status=logs_res.status,
            summary=(
                f"Logs query {logs_res.status.value} on {release_target}: "
                f"total={logs_res.total}, coverage={logs_res.coverage:.2f}"
                if logs_res.status in {ConnectorStatus.OK, ConnectorStatus.NO_RESULTS}
                else (logs_res.note or logs_res.status.value)
            ),
            latest_timestamp=logs_res.first_occurrence,
            reference=logs_res.reproducible_query,
        )
        if logs_res.status in {ConnectorStatus.UNAUTHORIZED, ConnectorStatus.CONNECTOR_UNAVAILABLE}:
            pending_conclusions.append(
                f"Logs probe incomplete ({logs_res.status.value}): {logs_res.note}"
            )

        ready = (
            probes["metrics"].status == ConnectorStatus.OK
            and probes["releases"].status == ConnectorStatus.OK
            and probes["architecture"].status == ConnectorStatus.OK
            and probes["logs"].status in {ConnectorStatus.OK, ConnectorStatus.NO_RESULTS}
        )
        return ServiceOnboardingReport(
            service=service_or_alias,
            canonical_service=canonical,
            environment=environment,
            ready=ready,
            declared_dependencies=declared_deps,
            probes=probes,
            pending_conclusions=pending_conclusions,
        )

    def ingest_alert(
        self,
        envelope: AlertEnvelope,
        *,
        mode: OperationalMode | None = None,
    ) -> AlertIngestionResult:
        """Normalize, validate, and transactionally persist an alert envelope before background work."""
        canonical_service = self._validate_alert_target(envelope.alert)
        svc_cfg = self._deps.catalog.get_service(canonical_service)
        config_ver = svc_cfg.config_version if svc_cfg else "v1"
        effective_mode = mode or self._default_mode

        case, result, _ = self._store.ingest_alert_transactional(
            envelope=envelope,
            canonical_service=canonical_service,
            service_config_version=config_ver,
            mode=effective_mode,
            now=self._now(),
            re_evaluate_on_update=self._re_evaluate_on_update,
        )
        for ev in case.events:
            if ev not in self._deps.audit.list_events(case.case_id):
                self._deps.audit.emit_event(ev)
        return result

    def process_pending_work(self, case_id: str | None = None) -> CaseRecord | None:
        """Claim the next pending or interrupted investigation or follow-up task and run it durably."""
        work = self._store.claim_next_work(case_id=case_id)
        if work is None:
            return None

        case = self._store.get_case(work.case_id)
        if case is None:
            self._store.complete_work(work.work_id, status="failed")
            return None

        if work.work_type == "follow_up":
            turn_id = str(work.payload.get("turn_id") or "")
            try:
                updated, _turn = self._conversation.execute_follow_up_turn(
                    case=case,
                    turn_id=turn_id,
                    now=self._now(),
                )
            except Exception:
                self._store.complete_work(work.work_id, status="failed")
                raise
            self._store.complete_work(
                work.work_id,
                last_step_index=len(updated.evidence),
                status="completed",
            )
            return updated

        svc_cfg = self._deps.catalog.get_service(case.alert.service)
        updated = self._investigator.run(case, self._deps, svc_cfg)
        self._store.complete_work(
            work.work_id,
            last_step_index=len(updated.evidence),
            status="completed",
        )
        return updated

    def submit_chat_message(
        self,
        *,
        case_id: str,
        client_message_id: str,
        actor: str,
        text: str,
        base_revision: int | None = None,
        proposal_id: str | None = None,
    ) -> ChatSubmissionResult:
        """Persist a conversational turn on an existing case and enqueue follow-up work if required."""
        _ = self.get_case(case_id)
        return self._conversation.submit_message(
            case_id=case_id,
            client_message_id=client_message_id,
            actor=actor,
            text=text,
            base_revision=base_revision,
            proposal_id=proposal_id,
            now=self._now(),
        )

    def list_messages(self, case_id: str) -> list[ConversationTurn]:
        """Return ordered conversation turns for a case."""
        case = self.get_case(case_id)
        turns = self._store.list_turns(case_id)
        return turns or list(case.conversation_turns)

    def start_case(
        self,
        alert: Alert,
        *,
        mode: OperationalMode | None = None,
    ) -> CaseRecord:
        """Open a new investigation case from an early anomaly alert and run Alternative A."""
        self._sequence += 1
        canonical_service = self._deps.catalog.resolve_canonical_name(alert.service)
        svc_cfg = self._deps.catalog.get_service(canonical_service)
        case_id = alert.case_id or f"CASE-{self._sequence}"
        normalized_alert = alert.model_copy(
            update={"case_id": case_id, "service": canonical_service}
        )
        now = self._now()
        effective_mode = mode or self._default_mode
        threshold = normalized_alert.threshold_value or (
            svc_cfg.threshold_ms if svc_cfg else 800.0
        )
        crossed = (
            normalized_alert.current_value is not None
            and normalized_alert.current_value >= threshold
        )
        case = CaseRecord(
            case_id=case_id,
            version=1,
            mode=effective_mode,
            service_config_version=svc_cfg.config_version if svc_cfg else "v1",
            episode_id=f"{canonical_service}-{uuid.uuid4().hex[:8]}",
            alert=normalized_alert,
            status=CaseStatus.OPEN,
            created_at=now,
            updated_at=now,
            revisions=[
                CaseRevision(
                    revision_number=1,
                    cutoff_at=normalized_alert.effective_cutoff,
                    created_at=now,
                    reason="direct_start_case",
                )
            ],
            crossed_threshold=crossed,
            threshold_crossed_at=normalized_alert.observed_at if crossed else None,
        )
        self._store.save_case(case)
        return self._investigator.run(case, self._deps, svc_cfg)

    def get_case(self, case_id: str) -> CaseRecord:
        """Retrieve a CaseRecord by ID."""
        case = self._store.get_case(case_id)
        if case is None:
            raise CaseNotFoundError(f"Case '{case_id}' was not found.")
        return case

    def list_cases(self) -> list[CaseRecord]:
        """List all persisted investigation cases."""
        return self._store.list_cases()

    def list_events(self, case_id: str, after_sequence_id: int = 0) -> list[CaseEvent]:
        """Return the ordered timeline events for a case after an optional sequence ID."""
        _ = self.get_case(case_id)
        events = self._store.list_events(case_id, after_sequence_id=after_sequence_id)
        if events or after_sequence_id > 0:
            return events
        return [
            ev
            for ev in self.get_case(case_id).events
            if ev.sequence_id > after_sequence_id
        ]

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
            f"Case {case.case_id} (status={case.status.value}, mode={case.mode.value}, service={case.alert.service}):"
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

    def submit_review(
        self,
        *,
        case_id: str,
        proposal_id: str,
        reviewer: str,
        verdict: ReviewVerdict,
        comment: str = "",
        proposal_revision: int | None = None,
        intervened_externally: bool = False,
        confirmed_cause: str | None = None,
        reviewed_at: datetime | None = None,
    ) -> CaseRecord:
        """Record human evaluation of a recommendation in shadow mode."""
        case = self.get_case(case_id)
        review_time = reviewed_at or self._now()
        review = self._deps.approvals.record_review(
            case=case,
            proposal_id=proposal_id,
            reviewer=reviewer,
            verdict=verdict,
            comment=comment,
            proposal_revision=proposal_revision,
            intervened_externally=intervened_externally,
            confirmed_cause=confirmed_cause,
            now=review_time,
        )
        if intervened_externally:
            case.status = CaseStatus.OBSERVING

        review_event = CaseEvent(
            event_type=EventType.REVIEW,
            case_id=case.case_id,
            timestamp=review.reviewed_at,
            role="HumanReview",
            summary=(
                f"Shadow review by {reviewer} on {proposal_id} (rev={review.proposal_revision}): "
                f"verdict={verdict.value}"
            ),
            references=[proposal_id],
            payload={
                "review_id": review.review_id,
                "verdict": verdict.value,
                "intervened_externally": intervened_externally,
                "confirmed_cause": confirmed_cause,
            },
        )
        case.events.append(review_event)
        audit_rec = AuditRecord(
            case_id=case.case_id,
            timestamp=review.reviewed_at,
            actor=reviewer,
            action_or_tool=f"review:{verdict.value}",
            sanitized_args={
                "proposal_id": proposal_id,
                "proposal_revision": review.proposal_revision,
                "intervened_externally": intervened_externally,
            },
            status=verdict.value,
            summary=comment or review_event.summary,
            references=[proposal_id],
        )
        self._deps.audit.record_audit(audit_rec)
        self._store.save_case(case, audit_records=[audit_rec])
        self._deps.audit.emit_event(case.events[-1])
        return case

    def resolve_approval(
        self,
        *,
        case_id: str,
        proposal_id: str,
        actor: str,
        decision: DecisionChoice,
        comment: str = "",
        idempotency_key: str | None = None,
        actor_groups: list[str] | None = None,
        proposal_version: int | None = None,
        decided_at: datetime | None = None,
        verification_at: datetime | None = None,
    ) -> CaseRecord:
        """Atomically reserve an authenticated approve/reject decision, dispatch it, and verify recovery."""
        initial_case = self.get_case(case_id)
        action_time = self._decision_clock(initial_case, decided_at)

        def _validate_in_tx(current_case: CaseRecord):
            return self._deps.approvals.prepare_decision_reservation(
                case=current_case,
                proposal_id=proposal_id,
                actor=actor,
                decision=decision,
                comment=comment,
                idempotency_key=idempotency_key,
                actor_groups=actor_groups,
                proposal_version=proposal_version,
                now=action_time,
            )

        reserved_case, human_decision, cached_exec, op_key, was_duplicate = (
            self._store.reserve_approval_transactional(
                case_id=case_id,
                proposal_id=proposal_id,
                actor=actor,
                decision=decision,
                comment=comment,
                idempotency_key=idempotency_key,
                actor_groups=actor_groups,
                proposal_version=proposal_version,
                now=action_time,
                validate_fn=_validate_in_tx,
            )
        )

        if was_duplicate:
            return reserved_case

        assert op_key is not None
        execution, operation = self._deps.approvals.dispatch_reserved_execution(
            case=reserved_case,
            decision=decision,
            actor=actor,
            comment=comment,
            op_key=op_key,
            timestamp=action_time,
        )

        decision_event = CaseEvent(
            event_type=EventType.DECISION,
            case_id=reserved_case.case_id,
            timestamp=human_decision.decided_at,
            role="Human",
            summary=(
                f"Decision '{decision.value}' by {actor} on {proposal_id}: "
                f"{execution.detail}"
            ),
            references=reserved_case.proposal.evidence_ids if reserved_case.proposal else [],
            payload={
                "proposal_id": proposal_id,
                "decision": decision.value,
                "execution_status": execution.status.value,
                "operation_id": execution.operation_id,
                "preventive_ticket_id": execution.preventive_ticket_id,
            },
        )
        audit_rec = AuditRecord(
            case_id=reserved_case.case_id,
            timestamp=human_decision.decided_at,
            actor=actor,
            action_or_tool=f"decision:{decision.value}",
            sanitized_args={"proposal_id": proposal_id, "comment": comment},
            status=execution.status.value,
            summary=execution.detail,
            references=reserved_case.proposal.evidence_ids if reserved_case.proposal else [],
        )
        self._deps.audit.record_audit(audit_rec)
        final_case = self._store.finalize_operation_transactional(
            case_id=reserved_case.case_id,
            operation_key=op_key,
            decision=human_decision,
            execution=execution,
            operation=operation,
            audit_record=audit_rec,
            event=decision_event,
        )
        self._deps.audit.emit_event(final_case.events[-1])

        if (
            decision == DecisionChoice.APPROVE
            and final_case.proposal is not None
            and execution.status == ProposalStatus.EXECUTED
        ):
            self._verify_recovery(
                final_case,
                action_at=execution.executed_at,
                verification_at=verification_at,
            )

        return final_case

    def reconcile_operation(
        self,
        *,
        case_id: str,
        operation_id: str,
        verification_at: datetime | None = None,
    ) -> CaseRecord:
        """Query an external timed-out operation before retrying and verify from its effective completion time."""
        case = self.get_case(case_id)
        op = self._deps.approvals.get_operation_status(operation_id)
        if op is None:
            raise OperationNotFoundError(f"Operation '{operation_id}' was not found.")

        case.external_operations = [
            existing
            for existing in case.external_operations
            if existing.operation_id != op.operation_id
        ] + [op]

        if op.status == OperationStatus.TIMEOUT:
            self._store.save_case(case)
            return case

        now = op.completed_at or self._now()
        if case.proposal is not None:
            if op.status == OperationStatus.EXECUTED:
                case.proposal.status = ProposalStatus.EXECUTED
                case.status = CaseStatus.MITIGATED
            else:
                case.proposal.status = ProposalStatus.FAILED
                case.status = CaseStatus.OPEN

        exec_result = case.execution
        if case.proposal is not None:
            from vigia.domain.contracts import ExecutionResult

            exec_result = ExecutionResult(
                proposal_id=case.proposal.proposal_id,
                action=case.proposal.action,
                status=case.proposal.status,
                detail=op.detail,
                executed_at=now,
                preventive_ticket_id=op.external_ticket_id,
                operation_id=op.operation_id,
            )
            case.execution = exec_result

        event = CaseEvent(
            event_type=EventType.EXECUTION,
            case_id=case.case_id,
            timestamp=now,
            role="Pipeline",
            summary=op.detail,
            references=[op.operation_id],
            payload={"operation_id": op.operation_id, "status": op.status.value},
        )
        case.events.append(event)
        audit_rec = AuditRecord(
            case_id=case.case_id,
            timestamp=now,
            actor="operation_reconciler",
            action_or_tool="reconcile_operation",
            sanitized_args={"operation_id": operation_id},
            status=op.status.value,
            summary=op.detail,
            references=[op.operation_id],
        )
        self._deps.audit.record_audit(audit_rec)
        self._store.save_case(case, audit_records=[audit_rec])
        self._deps.audit.emit_event(case.events[-1])

        if op.status == OperationStatus.EXECUTED and case.proposal is not None:
            self._verify_recovery(
                case,
                action_at=now,
                verification_at=verification_at,
            )
        return case

    def verify_case_recovery(
        self,
        case_id: str,
        *,
        verification_at: datetime | None = None,
    ) -> CaseRecord:
        """Resume verification when late post-action telemetry samples arrive."""
        case = self.get_case(case_id)
        if case.proposal is None or case.execution is None:
            raise ValueError(f"Case '{case_id}' has no executed action to verify.")
        self._verify_recovery(
            case,
            action_at=case.execution.executed_at,
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
        """Require sustained post-execution recovery, traffic coverage, and clean dependency logs."""
        assert case.proposal is not None
        verify_time = verification_at or (action_at + timedelta(minutes=10))
        service_cfg = self._deps.catalog.get_service(case.alert.service)
        threshold = (
            case.alert.threshold_value
            if case.alert.threshold_value is not None
            else (service_cfg.threshold_ms if service_cfg else 800.0)
        )
        criteria = (
            service_cfg.recovery_criteria
            if service_cfg is not None
            else RecoveryCriteriaConfig()
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

        dep_logs = self._deps.telemetry.search_logs(
            service=case.proposal.target_service,
            environment=case.alert.environment,
            pattern="exhausted",
            window_minutes=window_minutes,
            cutoff_at=verify_time,
            window_start=action_at + timedelta(seconds=1),
            window_end=verify_time,
        )
        dep_errors_after = dep_logs.total if dep_logs.status == ConnectorStatus.OK else 0

        post_value: float | None = None
        post_volume = sum(
            p.request_count if p.request_count is not None else 100
            for p in post_points
        )
        consecutive_healthy = 0
        for pt in post_points:
            improved_pt = pre_action_value is None or pt.value < pre_action_value
            if pt.value < threshold and pt.value <= criteria.healthy_band_max_ms and improved_pt:
                consecutive_healthy += 1
            else:
                consecutive_healthy = 0

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
            meets_band = post_value <= criteria.healthy_band_max_ms
            meets_consecutive = consecutive_healthy >= criteria.consecutive_healthy_windows
            meets_traffic = (
                post_volume >= criteria.min_request_volume
                and series.coverage >= criteria.min_coverage
            )
            meets_dep_errors = dep_errors_after == 0

            if (
                post_value < threshold
                and improved
                and meets_band
                and meets_consecutive
                and meets_traffic
                and meets_dep_errors
            ):
                recovered = True
                case.status = CaseStatus.CLOSED
                detail = (
                    f"Verified recovery on {case.alert.service} at {latest_post.timestamp.isoformat()}: "
                    f"{case.alert.metric} dropped to {post_value} {series.unit} "
                    f"(threshold={threshold} {series.unit}, healthy_band<={criteria.healthy_band_max_ms}, "
                    f"consecutive={consecutive_healthy}, dep_errors={dep_errors_after})."
                )
            elif post_value < threshold:
                recovered = False
                case.status = CaseStatus.OBSERVING
                reasons: list[str] = []
                if not meets_band:
                    reasons.append(
                        f"value {post_value} {series.unit} exceeds healthy band <= {criteria.healthy_band_max_ms}"
                    )
                if not meets_consecutive:
                    reasons.append(
                        f"consecutive healthy windows {consecutive_healthy} < {criteria.consecutive_healthy_windows}"
                    )
                if not meets_traffic:
                    reasons.append(
                        f"insufficient traffic/coverage (volume={post_volume}/{criteria.min_request_volume}, coverage={series.coverage:.2f})"
                    )
                if not meets_dep_errors:
                    reasons.append(f"dependency still reports {dep_errors_after} post-action error(s)")
                detail = (
                    f"Case remains in observation at {latest_post.timestamp.isoformat()}: "
                    + ("; ".join(reasons) if reasons else "awaiting sustained confirmation.")
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
            consecutive_healthy_points=consecutive_healthy,
            dependency_errors_after=dep_errors_after,
            request_volume=post_volume,
            coverage=series.coverage,
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
                "consecutive_healthy_points": consecutive_healthy,
            },
        )
        case.events.append(event)
        audit_rec = AuditRecord(
            case_id=case.case_id,
            timestamp=verify_time,
            actor="system_verifier",
            action_or_tool="verify_recovery",
            sanitized_args={"service": case.alert.service, "metric": case.alert.metric},
            status="recovered" if recovered else case.status.value,
            summary=detail,
            references=[series.reproducible_query],
        )
        self._deps.audit.record_audit(audit_rec)
        self._store.save_case(case, audit_records=[audit_rec])
        self._deps.audit.emit_event(case.events[-1])
        return verification

    def get_pilot_metrics(self) -> PilotMetricsSummary:
        """Compute evaluation KPIs across persisted episodes (shadow and assisted)."""
        cases = self.list_cases()
        total = len(cases)
        evaluated = 0
        pending = 0
        useful = 0
        incorrect = 0
        confirmed_cause = 0
        diag_times: list[float] = []
        margins: list[float] = []

        for case in cases:
            if case.proposal is not None and case.proposal.created_at >= case.created_at:
                delta_sec = (case.proposal.created_at - case.created_at).total_seconds()
            else:
                delta_sec = None

            if case.reviews:
                evaluated += 1
                latest = case.reviews[-1]
                if latest.verdict == ReviewVerdict.USEFUL:
                    useful += 1
                    if delta_sec is not None:
                        diag_times.append(delta_sec)
                elif latest.verdict == ReviewVerdict.INCORRECT:
                    incorrect += 1
                if latest.confirmed_cause:
                    confirmed_cause += 1
            elif case.proposal is not None:
                pending += 1

            if case.crossed_threshold and case.threshold_crossed_at is not None:
                margin_min = (
                    case.threshold_crossed_at - case.alert.observed_at
                ).total_seconds() / 60.0
                margins.append(max(0.0, round(margin_min, 2)))

        false_alarm_rate = round(incorrect / evaluated, 4) if evaluated > 0 else 0.0
        confirmed_rate = round(confirmed_cause / evaluated, 4) if evaluated > 0 else 0.0
        avg_diag = round(sum(diag_times) / len(diag_times), 2) if diag_times else None
        avg_margin = round(sum(margins) / len(margins), 2) if margins else None

        return PilotMetricsSummary(
            total_episodes=total,
            evaluated_episodes=evaluated,
            pending_review_episodes=pending,
            useful_recommendations=useful,
            false_alarm_rate=false_alarm_rate,
            confirmed_cause_rate=confirmed_rate,
            avg_seconds_to_useful_recommendation=avg_diag,
            avg_observed_margin_minutes_when_crossed=avg_margin,
        )
