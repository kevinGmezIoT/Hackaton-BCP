"""Domain ports (typed Protocols) defining capabilities required by core logic."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from vigia.domain.contracts import (
    ActionProposal,
    AlertEnvelope,
    AlertIngestionResult,
    AnomalyReport,
    AuditRecord,
    CaseEvent,
    CaseRecord,
    CodeDiff,
    CodeSnippet,
    ConversationTurn,
    DecisionChoice,
    DependencyQueryResult,
    ExecutionResult,
    ExternalOperationRecord,
    FollowUpCandidate,
    HumanDecision,
    IncidentPrecedent,
    LogQueryResult,
    MetricSeries,
    OperationalMode,
    PendingWorkItem,
    ReleaseResolution,
    RunbookMatch,
    ServiceConfig,
)


class ServiceCatalogPort(Protocol):
    """Resolves service metadata, aliases, and access boundaries."""

    def get_service(self, service_or_alias: str) -> ServiceConfig | None: ...

    def resolve_canonical_name(self, service_or_alias: str) -> str: ...

    def list_services(self) -> list[ServiceConfig]: ...


class ReleaseSourcePort(Protocol):
    """Provides active releases and recent change history."""

    def resolve_active_releases(
        self,
        service: str,
        environment: str,
        at_time: datetime,
    ) -> ReleaseResolution: ...

    def list_recent_changes(
        self,
        service: str,
        environment: str,
        hours: int,
        at_time: datetime,
    ) -> ReleaseResolution: ...


class CodeRepositoryPort(Protocol):
    """Scoped, read-only access to authorized source repositories."""

    def compare_revisions(
        self,
        repository: str,
        base_sha: str,
        target_sha: str,
        path: str | None = None,
    ) -> CodeDiff: ...

    def read_snippet(
        self,
        repository: str,
        sha: str,
        path: str,
        start_line: int = 1,
        end_line: int = 80,
    ) -> CodeSnippet: ...


class ArchitecturePort(Protocol):
    """Reads declared or observed service dependencies."""

    def query_dependencies(
        self,
        service: str,
        environment: str,
        version: str | None = None,
        direction: str = "outbound",
    ) -> DependencyQueryResult: ...


class TelemetryPort(Protocol):
    """Queries metrics, anomaly projections, and logs bounded by cutoff time."""

    def query_metrics(
        self,
        service: str,
        environment: str,
        metric: str,
        window_minutes: int,
        cutoff_at: datetime,
        allow_post_action: bool = False,
    ) -> MetricSeries: ...

    def detect_anomaly(
        self,
        service: str,
        environment: str,
        metric: str,
        cutoff_at: datetime,
    ) -> AnomalyReport: ...

    def search_logs(
        self,
        service: str,
        environment: str,
        pattern: str,
        window_minutes: int,
        cutoff_at: datetime,
        cohort: str | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> LogQueryResult: ...


class KnowledgePort(Protocol):
    """Searches historical incidents and operational runbooks."""

    def search_similar_incidents(
        self,
        description: str,
        k: int = 3,
        cutoff_at: datetime | None = None,
    ) -> list[IncidentPrecedent]: ...

    def search_runbook(self, query: str) -> list[RunbookMatch]: ...


class ActionExecutorPort(Protocol):
    """Executes approved or low-risk preventive actions and queries pipeline operations."""

    def execute_action(self, proposal: ActionProposal) -> ExecutionResult: ...

    def create_preventive_ticket(
        self,
        case_id: str,
        proposal: ActionProposal,
        actor: str,
        reason: str,
    ) -> ExecutionResult: ...

    def dispatch_operation(
        self,
        *,
        case_id: str,
        environment: str,
        proposal: ActionProposal,
        operation_key: str,
        now: datetime | None = None,
    ) -> ExternalOperationRecord: ...

    def get_operation_status(self, operation_id: str) -> ExternalOperationRecord | None: ...


class AuditSinkPort(Protocol):
    """Persists audit records and emits timeline events."""

    def record_audit(self, record: AuditRecord) -> None: ...

    def emit_event(self, event: CaseEvent) -> None: ...

    def list_audit_records(self, case_id: str | None = None) -> list[AuditRecord]: ...


class CaseStorePort(Protocol):
    """Transactional persistence for cases, episodes, events, reviews, and pending work."""

    def ingest_alert_transactional(
        self,
        *,
        envelope: AlertEnvelope,
        canonical_service: str,
        service_config_version: str,
        mode: OperationalMode,
        now: datetime,
        re_evaluate_on_update: bool = True,
    ) -> tuple[CaseRecord, AlertIngestionResult, PendingWorkItem | None]: ...

    def save_case(
        self,
        case: CaseRecord,
        *,
        audit_records: list[AuditRecord] | None = None,
        new_events: list[CaseEvent] | None = None,
        pending_work: PendingWorkItem | None = None,
        expected_version: int | None = None,
    ) -> CaseRecord: ...

    def get_case(self, case_id: str) -> CaseRecord | None: ...

    def list_cases(self) -> list[CaseRecord]: ...

    def list_events(self, case_id: str, after_sequence_id: int = 0) -> list[CaseEvent]: ...

    def claim_next_work(self, case_id: str | None = None) -> PendingWorkItem | None: ...

    def complete_work(
        self,
        work_id: str,
        *,
        last_step_index: int = 0,
        status: str = "completed",
    ) -> None: ...

    def create_chat_turn_transactional(
        self,
        *,
        case_id: str,
        client_message_id: str,
        actor: str,
        question: str,
        base_revision: int | None,
        proposal_id: str | None,
        intent: str,
        re_evaluate: bool,
        cutoff_at: datetime,
        user_hypothesis: str | None,
        now: datetime,
        immediate_answer: str | None = None,
        immediate_outcome: str | None = None,
        immediate_evidence_ids: list[str] | None = None,
    ) -> tuple[CaseRecord, ConversationTurn, PendingWorkItem | None, bool]: ...

    def list_turns(self, case_id: str) -> list[ConversationTurn]: ...

    def publish_follow_up_transactional(
        self,
        *,
        case_id: str,
        turn_id: str,
        base_revision: int,
        candidate: FollowUpCandidate,
        updated_case_snapshot: CaseRecord,
        now: datetime,
    ) -> tuple[CaseRecord, ConversationTurn]: ...

    def reserve_approval_transactional(
        self,
        *,
        case_id: str,
        proposal_id: str,
        actor: str,
        decision: DecisionChoice,
        comment: str,
        idempotency_key: str | None,
        actor_groups: list[str] | None,
        proposal_version: int | None,
        now: datetime,
        validate_fn: object,
    ) -> tuple[CaseRecord, HumanDecision, ExecutionResult | None, str | None, bool]: ...

    def finalize_operation_transactional(
        self,
        *,
        case_id: str,
        operation_key: str | None,
        decision: HumanDecision,
        execution: ExecutionResult,
        operation: ExternalOperationRecord | None,
        audit_record: AuditRecord,
        event: CaseEvent,
    ) -> CaseRecord: ...
