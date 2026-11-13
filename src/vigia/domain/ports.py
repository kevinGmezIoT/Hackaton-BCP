"""Domain ports (typed Protocols) defining capabilities required by core logic."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from vigia.domain.contracts import (
    ActionProposal,
    AnomalyReport,
    AuditRecord,
    CaseEvent,
    CodeDiff,
    CodeSnippet,
    DependencyQueryResult,
    ExecutionResult,
    IncidentPrecedent,
    LogQueryResult,
    MetricSeries,
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
    ) -> LogQueryResult: ...


class KnowledgePort(Protocol):
    """Searches historical incidents and operational runbooks."""

    def search_similar_incidents(
        self,
        description: str,
        k: int = 3,
    ) -> list[IncidentPrecedent]: ...

    def search_runbook(self, query: str) -> list[RunbookMatch]: ...


class ActionExecutorPort(Protocol):
    """Executes approved or low-risk preventive actions."""

    def execute_action(self, proposal: ActionProposal) -> ExecutionResult: ...

    def create_preventive_ticket(
        self,
        case_id: str,
        proposal: ActionProposal,
        actor: str,
        reason: str,
    ) -> ExecutionResult: ...


class AuditSinkPort(Protocol):
    """Persists audit records and emits timeline events."""

    def record_audit(self, record: AuditRecord) -> None: ...

    def emit_event(self, event: CaseEvent) -> None: ...

    def list_audit_records(self, case_id: str | None = None) -> list[AuditRecord]: ...

