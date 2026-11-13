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


class OperationalMode(str, Enum):
    """Execution mode controlling whether Vigía only recommends (shadow) or dispatches actions (assisted)."""

    SHADOW = "shadow"
    ASSISTED = "assisted"


class RuntimeProfile(str, Enum):
    """Runtime composition profile."""

    DEMO = "demo"
    PILOT = "pilot"


class AlertState(str, Enum):
    """Monitor alert state in an incoming envelope."""

    FIRING = "firing"
    RESOLVED = "resolved"


class AlertIngestOutcome(str, Enum):
    """Result of ingesting an alert envelope into the case store."""

    CREATED = "created"
    MERGED = "merged"
    DUPLICATE = "duplicate"
    RESOLVED_RECORDED = "resolved_recorded"


class ReviewVerdict(str, Enum):
    """Human evaluation of a shadow recommendation."""

    USEFUL = "useful"
    INCORRECT = "incorrect"
    INDETERMINATE = "indeterminate"


class OperationStatus(str, Enum):
    """Lifecycle status of an external pipeline or ticketing operation."""

    PENDING = "pending"
    EXECUTED = "executed"
    FAILED = "failed"
    TIMEOUT = "timeout"


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
    RECOMMENDED = "recommended"
    UNDER_REVIEW = "under_review"
    WITHDRAWN = "withdrawn"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


class ConversationIntent(str, Enum):
    """Structured intent classified from an operator chat message."""

    EXPLAIN_PROPOSAL = "explain_proposal"
    CHALLENGE_HYPOTHESIS = "challenge_hypothesis"
    CURRENT_STATUS = "current_status"
    APPROVAL_REQUEST = "approval_request"
    AMBIGUOUS = "ambiguous"
    GENERAL_QUESTION = "general_question"


class ComparisonVerdict(str, Enum):
    """Outcome of comparing pre/post-deployment error rates across cohorts."""

    SUPPORTS_CAUSAL_CHANGE = "supports_causal_change"
    CONTRADICTS_DEPLOYMENT = "contradicts_deployment"
    INDETERMINATE = "indeterminate"


class TurnStatus(str, Enum):
    """Execution lifecycle status of a conversational investigation turn."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class DecisionChoice(str, Enum):
    """Human approval choices."""

    APPROVE = "approve"
    REJECT = "reject"


class CaseStatus(str, Enum):
    """Lifecycle state of an investigation case."""

    OPEN = "open"
    INVESTIGATING = "investigating"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_APPROVAL = "awaiting_approval"
    OBSERVING = "observing"
    MITIGATED = "mitigated"
    CLOSED = "closed"
    INCONCLUSIVE = "inconclusive"


class EventType(str, Enum):
    """Timeline event classifications emitted during investigation and resolution."""

    ALERT = "alert"
    STEP = "step"
    PROPOSAL = "proposal"
    REVISION = "revision"
    CONVERSATION = "conversation"
    REVIEW = "review"
    DECISION = "decision"
    EXECUTION = "execution"
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


class RecoveryCriteriaConfig(BaseModel):
    """Agreed conditions required to confirm sustained service recovery after an intervention."""

    model_config = ConfigDict(extra="forbid")

    healthy_band_max_ms: float = 220.0
    min_request_volume: int = 1
    min_coverage: float = 0.8
    consecutive_healthy_windows: int = 1
    error_rate_tolerance: float = 0.01
    comparison_min_volume: int = 100
    shared_load_rate_ratio: float = 0.5


class ServiceTelemetryConfig(BaseModel):
    """Selectors and baseline metadata used to query observability backends."""

    model_config = ConfigDict(extra="forbid")

    service_selector: str
    environment_selector: str
    metric_unit: str = "ms"
    version_label: str = "cohort"
    impact_metric: str = "latency_p95_ms"
    baseline_ms: float = 180.0


class ServiceConfig(BaseModel):
    """Registered service metadata, recovery criteria, and authorization boundaries."""

    model_config = ConfigDict(extra="forbid")

    service: str
    aliases: list[str] = Field(default_factory=list)
    environment: str = "production"
    repository: str
    release_source: str
    architecture: ServiceArchitectureConfig
    telemetry: ServiceTelemetryConfig
    threshold_ms: float = 800.0
    owner_team: str = "platform"
    approver_group: str = "oncall-approvers"
    runbooks_path: str = "runbooks"
    config_version: str = "v1"
    allowed_actions: list[ActionType] = Field(default_factory=lambda: list(ActionType))
    recovery_criteria: RecoveryCriteriaConfig = Field(default_factory=RecoveryCriteriaConfig)


class SourceProbeReport(BaseModel):
    """Connection test result for an individual operational data source."""

    model_config = ConfigDict(extra="forbid")

    source: str
    status: ConnectorStatus
    summary: str
    latest_timestamp: datetime | None = None
    revision: str | None = None
    reference: str = ""


class ServiceOnboardingReport(BaseModel):
    """Connection and readiness report produced when registering a service."""

    model_config = ConfigDict(extra="forbid")

    service: str
    canonical_service: str
    environment: str
    ready: bool
    declared_dependencies: list[str] = Field(default_factory=list)
    probes: dict[str, SourceProbeReport] = Field(default_factory=dict)
    pending_conclusions: list[str] = Field(default_factory=list)


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


class AlertEnvelope(BaseModel):
    """Normalized incoming monitor alert envelope with external correlation identifiers."""

    model_config = ConfigDict(extra="forbid")

    source: str
    event_id: str
    episode_id: str | None = None
    state: AlertState = AlertState.FIRING
    alert: Alert


class AlertIngestionResult(BaseModel):
    """Outcome returned by alert reception before background investigation completes."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    episode_id: str
    event_id: str
    outcome: AlertIngestOutcome
    revision_number: int = 1
    status_code: int = 202


class CaseRevision(BaseModel):
    """Point-in-time investigation revision within a case preserving its cutoff and evidence."""

    model_config = ConfigDict(extra="forbid")

    revision_number: int
    cutoff_at: datetime
    created_at: datetime
    reason: str = "initial_alert"
    evidence_ids: list[str] = Field(default_factory=list)
    proposal_id: str | None = None
    summary: str = ""
    action_outcome: str | None = None
    suggested_next_check: str | None = None
    turn_id: str | None = None


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
    request_count: int | None = None
    error_count: int | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None


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
    request_volume: int | None = None
    error_total: int | None = None
    duration_ms: float = 0.0
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
    projection_status: str = "projected"
    request_volume: int | None = None
    coverage: float = 1.0
    note: str = ""


class LogQueryResult(BaseModel):
    """Aggregated, redacted error log count, per-cohort error rates, and sample output."""

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
    cohort_requests: dict[str, int] = Field(default_factory=dict)
    cohort_error_rates: dict[str, float | None] = Field(default_factory=dict)
    uncovered_cohorts: list[str] = Field(default_factory=list)
    coverage: float = 1.0
    duration_ms: float = 0.0
    numerator_kind: str = "failed_requests"
    error_definition: str = ""
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
    occurred_at: datetime | None = None


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
    observation_type: str = "general"
    cohort: str | None = None
    error_definition: str | None = None
    comparison_result: str | None = None
    revision_number: int = 1


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
    """Proposed preventive action awaiting review (shadow) or human decision (assisted)."""

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
    revision_number: int = 1
    version: int = 1
    created_at: datetime


class ProposalReview(BaseModel):
    """Human evaluation of a recommendation in shadow mode."""

    model_config = ConfigDict(extra="forbid")

    review_id: str
    case_id: str
    proposal_id: str
    proposal_revision: int = 1
    reviewer: str
    verdict: ReviewVerdict
    comment: str = ""
    intervened_externally: bool = False
    confirmed_cause: str | None = None
    reviewed_at: datetime


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


class ExternalOperationRecord(BaseModel):
    """Tracked execution operation dispatched to an external deployment pipeline or ticket system."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    case_id: str
    proposal_id: str
    service: str
    environment: str
    action: ActionType
    target_release_id: str
    operation_key: str
    status: OperationStatus
    detail: str = ""
    started_at: datetime
    completed_at: datetime | None = None
    external_ticket_id: str | None = None


class ExecutionResult(BaseModel):
    """Execution status of an approved or low-risk preventive action."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    action: ActionType
    status: ProposalStatus
    detail: str
    executed_at: datetime
    preventive_ticket_id: str | None = None
    operation_id: str | None = None


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
    consecutive_healthy_points: int = 0
    dependency_errors_after: int | None = None
    request_volume: int | None = None
    coverage: float = 1.0


class CaseEvent(BaseModel):
    """Timeline event emitted during case investigation, approval, and verification."""

    model_config = ConfigDict(extra="forbid")

    sequence_id: int = 0
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
    """Immutable audit entry persisted to JSON Lines or transactional storage."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    timestamp: datetime
    actor: str
    action_or_tool: str
    sanitized_args: dict[str, Any] = Field(default_factory=dict)
    status: str
    summary: str
    references: list[str] = Field(default_factory=list)


class CohortRateWindow(BaseModel):
    """Comparable error rate computed in code for a single window and cohort."""

    model_config = ConfigDict(extra="forbid")

    label: str
    window_start: datetime
    window_end: datetime
    cohort: str
    errors: int = 0
    requests: int | None = None
    error_rate: float | None = None
    numerator_kind: str = "failed_requests"
    error_definition: str = ""
    coverage: float = 1.0
    reproducible_query: str = ""
    limitation: str | None = None


class CohortComparisonResult(BaseModel):
    """Pre/post-deployment cohort comparison with denominator and saturation checks."""

    model_config = ConfigDict(extra="forbid")

    service: str
    environment: str
    release_id: str | None = None
    deployed_at: datetime | None = None
    cutoff_at: datetime
    pre_window_start: datetime
    pre_window_end: datetime
    post_window_start: datetime
    post_window_end: datetime
    windows: list[CohortRateWindow] = Field(default_factory=list)
    pre_total_requests: int = 0
    post_total_requests: int = 0
    error_definition: str = "*"
    shared_resource_saturated: bool = False
    verdict: ComparisonVerdict = ComparisonVerdict.INDETERMINATE
    missing_data: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    summary: str = ""


class ProposalHistoryEntry(BaseModel):
    """Immutable snapshot of a proposal version across case revisions."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    revision_number: int = 1
    version: int = 1
    action: ActionType
    target_service: str
    target_release_id: str
    status: ProposalStatus
    hypothesis: str = ""
    justification: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    outcome: str = "initial"
    turn_id: str | None = None
    created_at: datetime
    updated_at: datetime


class FollowUpCandidate(BaseModel):
    """Read-only follow-up investigation finding returned to backend for validation and publication."""

    model_config = ConfigDict(extra="forbid")

    intent: ConversationIntent
    answer: str
    hypothesis_under_test: str | None = None
    cited_evidence_ids: list[str] = Field(default_factory=list)
    used_existing_evidence_only: bool = False
    comparison: CohortComparisonResult | None = None
    comparison_outcome: str = "no_change"
    affects_rollback: bool = True
    recommended_follow_up_action: ActionType | None = None
    follow_up_target_service: str | None = None
    follow_up_hypothesis: str | None = None
    suggested_next_check: str | None = None
    missing_data: list[str] = Field(default_factory=list)
    cutoff_at: datetime


class ConversationTurn(BaseModel):
    """Persistent conversational turn linked to a case, revision, and evidence citations."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str
    case_id: str
    client_message_id: str
    actor: str
    question: str
    base_revision: int = 1
    proposal_id: str | None = None
    cutoff_at: datetime | None = None
    intent: ConversationIntent = ConversationIntent.CHALLENGE_HYPOTHESIS
    status: TurnStatus = TurnStatus.PENDING
    user_hypothesis: str | None = None
    referenced_evidence_ids: list[str] = Field(default_factory=list)
    comparison: CohortComparisonResult | None = None
    answer: str = ""
    result_outcome: str = ""
    published_revision: int | None = None
    pending_data: list[str] = Field(default_factory=list)
    work_summary: str = ""
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class ChatSubmissionResult(BaseModel):
    """Response returned by POST /api/casos/{case_id}/mensajes."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str
    case_id: str
    status: TurnStatus
    base_revision: int
    proposal_status: str | None = None
    duplicate: bool = False
    status_code: int = 202


class PendingWorkItem(BaseModel):
    """Durable background job persisted alongside a case state change."""

    model_config = ConfigDict(extra="forbid")

    work_id: str
    case_id: str
    revision_number: int = 1
    work_type: str = "investigate"
    status: str = "pending"
    last_step_index: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class CaseRecord(BaseModel):
    """Complete state of an investigation case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    version: int = 1
    mode: OperationalMode = OperationalMode.ASSISTED
    service_config_version: str = "v1"
    episode_id: str | None = None
    alert: Alert
    status: CaseStatus = CaseStatus.OPEN
    created_at: datetime
    updated_at: datetime
    received_events: list[AlertEnvelope] = Field(default_factory=list)
    revisions: list[CaseRevision] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    connector_gaps: list[str] = Field(default_factory=list)
    partial_findings: Finding | None = None
    suggested_next_check: str | None = None
    proposal: ActionProposal | None = None
    proposal_history: list[ProposalHistoryEntry] = Field(default_factory=list)
    conversation_turns: list[ConversationTurn] = Field(default_factory=list)
    reviews: list[ProposalReview] = Field(default_factory=list)
    decision: HumanDecision | None = None
    execution: ExecutionResult | None = None
    external_operations: list[ExternalOperationRecord] = Field(default_factory=list)
    verification: VerificationResult | None = None
    events: list[CaseEvent] = Field(default_factory=list)
    final_summary: str = ""
    crossed_threshold: bool = False
    threshold_crossed_at: datetime | None = None


class PilotMetricsSummary(BaseModel):
    """Aggregated value and accuracy metrics across pilot episodes."""

    model_config = ConfigDict(extra="forbid")

    total_episodes: int = 0
    evaluated_episodes: int = 0
    pending_review_episodes: int = 0
    useful_recommendations: int = 0
    false_alarm_rate: float = 0.0
    confirmed_cause_rate: float = 0.0
    avg_seconds_to_useful_recommendation: float | None = None
    avg_observed_margin_minutes_when_crossed: float | None = None
