"""Domain contracts, value objects, and entities for production failure prevention."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConnectorStatus(str, Enum):
    """Distinguishes empty query results from connector outages or authorization blocks."""

    OK = "ok"
    NO_RESULTS = "no_results"
    CONNECTOR_UNAVAILABLE = "connector_unavailable"
    UNAUTHORIZED = "unauthorized"


class ActionType(str, Enum):
    """Preventive actions that may be proposed or executed."""

    CREATE_PREVENTIVE_TICKET = "create_preventive_ticket"
    NOTIFY_TEAM = "notify_team"
    SCALE_CAPACITY = "scale_capacity"
    ROLLBACK_CHANGE = "rollback_change"


class RiskLevel(str, Enum):
    """Risk tier computed authoritatively by the backend policy table."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ProposalStatus(str, Enum):
    """Lifecycle state of an action proposal."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


class DecisionChoice(str, Enum):
    """Human approval choices."""

    APPROVE = "approve"
    REJECT = "reject"


class CaseStatus(str, Enum):
    """Lifecycle state of an investigation case."""

    OPEN = "open"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    MITIGATED = "mitigated"
    CLOSED = "closed"
    INCONCLUSIVE = "inconclusive"


class EventType(str, Enum):
    """Timeline event classifications emitted during investigation and resolution."""

    STEP = "step"
    PROPOSAL = "proposal"
    DECISION = "decision"
    MESSAGE = "message"
    VERIFICATION = "verification"


class RelationType(str, Enum):
    """Architectural dependency relationship between components."""

    CALLS = "calls"
    PROVISIONS = "provisions"
    CONFIGURES = "configures"
    UNKNOWN = "unknown"


class ServiceArchitectureConfig(BaseModel):
    """Architecture source specification for a registered service."""

    model_config = ConfigDict(extra="forbid")

    path: str
    revision_mode: str = "from_release"


class ServiceTelemetryConfig(BaseModel):
    """Selectors used to query observability backends."""

    model_config = ConfigDict(extra="forbid")

    service_selector: str
    environment_selector: str


class ServiceConfig(BaseModel):
    """Registered service metadata and authorization boundaries."""

    model_config = ConfigDict(extra="forbid")

    service: str
    aliases: list[str] = Field(default_factory=list)
    repository: str
    release_source: str
    architecture: ServiceArchitectureConfig
    telemetry: ServiceTelemetryConfig
    threshold_ms: float = 800.0
    owner_team: str = "platform"


class Alert(BaseModel):
    """Early degradation signal opening an investigation case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str | None = None
    service: str
    environment: str = "production"
    metric: str = "latency_p95_ms"
    observed_at: datetime
    cutoff_at: datetime | None = None
    symptom: str = ""
    baseline_value: float | None = None
    current_value: float | None = None
    threshold_value: float | None = None
    projected_minutes_to_threshold: float | None = None

    @property
    def effective_cutoff(self) -> datetime:
        """Return the strict cutoff timestamp bounding all investigation queries."""
        return self.cutoff_at or self.observed_at


class Release(BaseModel):
    """Deployment record in an environment."""

    model_config = ConfigDict(extra="forbid")

    release_id: str
    service: str
    environment: str
    deployed_at: datetime
    previous_sha: str
    deployed_sha: str
    repository: str
    artifact_digest: str | None = None
    config_revision: str | None = None
    status: str = "active"
    cohort: str = "100%"
    summary: str = ""


class ReleaseResolution(BaseModel):
    """Active releases resolved at a point in time."""

    model_config = ConfigDict(extra="forbid")

    status: ConnectorStatus = ConnectorStatus.OK
    service: str
    environment: str
    resolved_at: datetime
    releases: list[Release] = Field(default_factory=list)
    note: str = ""


class DiffFile(BaseModel):
    """Individual file diff in a commit range."""

    model_config = ConfigDict(extra="forbid")

    path: str
    patch: str
    truncated: bool = False


class CodeDiff(BaseModel):
    """Bounded git diff output between two commits in an authorized repository."""

    model_config = ConfigDict(extra="forbid")

    status: ConnectorStatus = ConnectorStatus.OK
    repository: str
    base_sha: str
    target_sha: str
    files: list[DiffFile] = Field(default_factory=list)
    note: str = ""


class CodeSnippet(BaseModel):
    """Citable file slice at an exact commit SHA."""

    model_config = ConfigDict(extra="forbid")

    status: ConnectorStatus = ConnectorStatus.OK
    repository: str
    sha: str
    path: str
    start_line: int
    end_line: int
    content: str
    note: str = ""


class DependencyRelation(BaseModel):
    """Directed or ambiguous architectural edge with provenance."""

    model_config = ConfigDict(extra="forbid")

    source_service: str
    target_service: str
    relation_type: RelationType = RelationType.CALLS
    direction: str = "outbound"
    source_kind: str = "drawio_xml"
    reference: str
    revision: str | None = None
    corroborated: bool = False
    ambiguous: bool = False


class DependencyQueryResult(BaseModel):
    """Result of querying service dependencies."""

    model_config = ConfigDict(extra="forbid")

    status: ConnectorStatus = ConnectorStatus.OK
    service: str
    environment: str
    relations: list[DependencyRelation] = Field(default_factory=list)
    note: str = ""


class MetricPoint(BaseModel):
    """Single metric observation."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    value: float
    cohort: str | None = None


class MetricSeries(BaseModel):
    """Bounded metric time-series response."""

    model_config = ConfigDict(extra="forbid")

    status: ConnectorStatus = ConnectorStatus.OK
    service: str
    environment: str
    metric: str
    unit: str = "ms"
    window_start: datetime
    window_end: datetime
    reproducible_query: str
    coverage: float = 1.0
    points: list[MetricPoint] = Field(default_factory=list)
    current_value: float | None = None
    value_15m_ago: float | None = None
    note: str = ""


class AnomalyReport(BaseModel):
    """Statistical anomaly and threshold projection readout."""

    model_config = ConfigDict(extra="forbid")

    status: ConnectorStatus = ConnectorStatus.OK
    service: str
    environment: str
    metric: str
    window_start: datetime
    window_end: datetime
    reproducible_query: str
    is_anomalous: bool = False
    z_score: float = 0.0
    baseline_mean: float = 0.0
    current_value: float = 0.0
    threshold: float = 800.0
    slope_per_minute: float = 0.0
    projected_minutes_to_threshold: float | None = None
    note: str = ""


class LogQueryResult(BaseModel):
    """Aggregated, redacted error log count and sample output."""

    model_config = ConfigDict(extra="forbid")

    status: ConnectorStatus = ConnectorStatus.OK
    service: str
    environment: str
    pattern: str
    window_start: datetime
    window_end: datetime
    reproducible_query: str
    total: int = 0
    before_cutoff_count: int = 0
    first_occurrence: datetime | None = None
    examples: list[str] = Field(default_factory=list)
    cohort_counts: dict[str, int] = Field(default_factory=dict)
    coverage: float = 1.0
    note: str = ""


class IncidentPrecedent(BaseModel):
    """Historical incident precedent retrieved by similarity search."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    similarity: float
    title: str
    root_cause: str
    resolution: str
    source: str


class RunbookMatch(BaseModel):
    """Standard operating procedure retrieved by keyword search."""

    model_config = ConfigDict(extra="forbid")

    runbook_id: str
    title: str
    applicable_conditions: str
    steps: list[str]
    source: str


class Evidence(BaseModel):
    """Verifiable fact stored in the case dossier with citable ID."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    case_id: str
    service: str
    environment: str
    observed_at: datetime
    source: str
    reproducible_reference: str
    observed_fact: str
    revision: str | None = None
    window: str | None = None


class Finding(BaseModel):
    """Diagnostic hypothesis supported and challenged by evidence."""

    model_config = ConfigDict(extra="forbid")

    hypothesis: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    exposed_services: list[str] = Field(default_factory=list)
    inferred_claims: list[str] = Field(default_factory=list)


class ActionProposal(BaseModel):
    """Proposed preventive action awaiting or resolved by human decision."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    case_id: str
    finding: Finding
    target_release_id: str
    target_service: str
    action: ActionType
    risk_level: RiskLevel
    justification: str
    evidence_ids: list[str]
    verification_plan: str
    idempotency_key: str
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime


class HumanDecision(BaseModel):
    """Authenticated human approval or rejection of an action proposal."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    proposal_id: str
    actor: str
    decision: DecisionChoice
    comment: str = ""
    decided_at: datetime
    idempotency_key: str


class ExecutionResult(BaseModel):
    """Execution status of an approved or low-risk preventive action."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    action: ActionType
    status: ProposalStatus
    detail: str
    executed_at: datetime
    preventive_ticket_id: str | None = None


class VerificationResult(BaseModel):
    """Post-action metric readout verifying recovery before closing a case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    proposal_id: str
    verified_at: datetime
    metric_value: float | None = None
    threshold: float
    recovered: bool
    case_status: CaseStatus
    detail: str


class CaseEvent(BaseModel):
    """Timeline event emitted during case investigation, approval, and verification."""

    model_config = ConfigDict(extra="forbid")

    event_type: EventType
    case_id: str
    timestamp: datetime
    role: str
    summary: str
    tool_name: str | None = None
    sanitized_args: dict[str, Any] = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditRecord(BaseModel):
    """Immutable audit entry persisted to JSON Lines storage."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    timestamp: datetime
    actor: str
    action_or_tool: str
    sanitized_args: dict[str, Any] = Field(default_factory=dict)
    status: str
    summary: str
    references: list[str] = Field(default_factory=list)


class CaseRecord(BaseModel):
    """Complete state of an investigation case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    alert: Alert
    status: CaseStatus = CaseStatus.OPEN
    created_at: datetime
    updated_at: datetime
    evidence: list[Evidence] = Field(default_factory=list)
    connector_gaps: list[str] = Field(default_factory=list)
    partial_findings: Finding | None = None
    suggested_next_check: str | None = None
    proposal: ActionProposal | None = None
    decision: HumanDecision | None = None
    execution: ExecutionResult | None = None
    verification: VerificationResult | None = None
    events: list[CaseEvent] = Field(default_factory=list)
    final_summary: str = ""

