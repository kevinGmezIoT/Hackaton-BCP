"""Case-bound tools exposing read-only ports and proposal registration to the model."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from vigia.application.approval_service import (
    ApprovalService,
    InactiveReleaseError,
    InsufficientRollbackEvidenceError,
    ProposalConflictError,
)
from vigia.application.cohort_comparison import compare_service_cohort_rates
from vigia.application.evidence_registry import EvidenceRegistry, InvalidCitationError
from vigia.domain.contracts import (
    ActionType,
    AuditRecord,
    CaseEvent,
    CaseRecord,
    CohortComparisonResult,
    ComparisonVerdict,
    ConnectorStatus,
    EventType,
    Finding,
    OperationalMode,
    ProposalStatus,
    Release,
    RiskLevel,
)
from vigia.domain.ports import (
    ArchitecturePort,
    AuditSinkPort,
    CaseStorePort,
    CodeRepositoryPort,
    KnowledgePort,
    ReleaseSourcePort,
    ServiceCatalogPort,
    TelemetryPort,
)

TOOL_ROLE_MAP: dict[str, str] = {
    "query_metrics": "Detection",
    "detect_anomaly": "Detection",
    "query_dependencies": "Architecture",
    "resolve_release": "Changes",
    "list_changes": "Changes",
    "compare_revisions": "Changes",
    "read_snippet": "Code",
    "search_logs": "Diagnosis",
    "compare_cohort_error_rates": "Diagnosis",
    "search_similar_incidents": "Diagnosis",
    "search_runbook": "Diagnosis",
    "propose_action": "Recommendation",
}


def _sha_matches(candidate: str, expected: str) -> bool:
    c = candidate.strip().lower()
    e = expected.strip().lower()
    if len(c) < 6 or len(e) < 6:
        return False
    return e.startswith(c) or c.startswith(e)


class ResolveReleaseInput(BaseModel):
    service: str = Field(description="Service name or alias to resolve active releases for.")
    environment: str | None = Field(
        default=None,
        description="Target environment (defaults to the case environment).",
    )


class CompareRevisionsInput(BaseModel):
    repository: str = Field(description="Authorized repository identifier.")
    base_sha: str = Field(description="Previous baseline commit SHA.")
    target_sha: str = Field(description="Deployed or candidate commit SHA.")
    path: str | None = Field(default=None, description="Optional file path to narrow the diff.")


class ReadSnippetInput(BaseModel):
    repository: str = Field(description="Authorized repository identifier.")
    sha: str = Field(description="Exact commit SHA to read from.")
    path: str = Field(description="Relative file path inside the repository.")
    start_line: int = Field(default=1, ge=1, description="1-based start line.")
    end_line: int = Field(default=80, ge=1, description="1-based inclusive end line.")


class QueryDependenciesInput(BaseModel):
    service: str = Field(description="Service name or alias.")
    direction: str = Field(
        default="outbound",
        description="Dependency direction: 'outbound' or 'inbound'.",
    )
    version: str | None = Field(default=None, description="Optional release/revision tag.")


class QueryMetricsInput(BaseModel):
    service: str = Field(description="Service name or alias.")
    metric: str = Field(default="latency_p95_ms", description="Metric name.")
    window_minutes: int = Field(default=30, ge=1, le=240, description="Lookback window in minutes.")


class DetectAnomalyInput(BaseModel):
    service: str = Field(description="Service name or alias.")
    metric: str = Field(default="latency_p95_ms", description="Metric name.")


class SearchLogsInput(BaseModel):
    service: str = Field(description="Service name or alias.")
    pattern: str = Field(description="Case-insensitive substring or pattern to count in logs.")
    window_minutes: int = Field(default=30, ge=1, le=240, description="Lookback window in minutes.")
    cohort: str | None = Field(
        default=None,
        description="Optional release/version cohort filter if available.",
    )
    window_start: str | None = Field(
        default=None,
        description="Optional ISO-8601 window start for pre/post deployment comparisons (must be <= cutoff).",
    )
    window_end: str | None = Field(
        default=None,
        description="Optional ISO-8601 window end for pre/post deployment comparisons (clamped to <= cutoff).",
    )


class ListChangesInput(BaseModel):
    service: str = Field(description="Service name or alias.")
    hours: int = Field(default=3, ge=1, le=72, description="Hours before cutoff to inspect.")


class SearchIncidentsInput(BaseModel):
    description: str = Field(description="Symptom and mechanism description for similarity search.")
    k: int = Field(default=3, ge=1, le=10, description="Number of precedents to return.")


class SearchRunbookInput(BaseModel):
    query: str = Field(description="Keywords describing the failure pattern or error.")


class CompareCohortErrorRatesInput(BaseModel):
    service: str = Field(description="Service name or alias to compare cohort error rates for.")
    pattern: str = Field(
        default="*",
        description="Error pattern or '*' for all ERROR logs.",
    )
    window_minutes: int | None = Field(
        default=None,
        ge=1,
        le=240,
        description="Optional equal pre/post window duration in minutes; defaults to deployment age up to cutoff.",
    )
    check_shared_saturation: bool = Field(
        default=True,
        description="Check shared resource saturation and request volume doubling.",
    )


class ProposeActionInput(BaseModel):
    action: ActionType = Field(
        description=(
            "Action to propose: 'create_preventive_ticket', 'notify_team', "
            "'scale_capacity', or 'rollback_change'."
        )
    )
    target_service: str = Field(description="Service targeted by the proposed action.")
    target_release_id: str = Field(
        description="Deployed release ID (e.g. 'CHG-4821') or 'none' if not release-specific."
    )
    hypothesis: str = Field(description="Concise root-cause hypothesis.")
    justification: str = Field(description="Evidence-backed rationale for the proposed action.")
    evidence_ids: list[str] = Field(
        description="List of EV-xxx IDs observed in this case that support the proposal."
    )
    verification_plan: str = Field(
        description="Metric, service, and expected reading to verify recovery after execution."
    )
    contradicting_evidence_ids: list[str] = Field(
        default_factory=list,
        description="Optional EV-xxx IDs that weaken or contradict the hypothesis.",
    )
    gaps: list[str] = Field(
        default_factory=list,
        description="Missing sources, ambiguities, or pending checks.",
    )
    exposed_services: list[str] = Field(
        default_factory=list,
        description="Affected or dependent services identified during investigation.",
    )
    inferred_claims: list[str] = Field(
        default_factory=list,
        description="Claims inferred rather than directly observed.",
    )


@dataclass
class CaseDependencies:
    """Outbound ports wired into an active investigation session."""

    catalog: ServiceCatalogPort
    releases: ReleaseSourcePort
    code_repo: CodeRepositoryPort
    architecture: ArchitecturePort
    telemetry: TelemetryPort
    knowledge: KnowledgePort
    approvals: ApprovalService
    audit: AuditSinkPort
    case_store: CaseStorePort | None = None


class CaseToolKit:
    """Executes validated read/proposal tools bound to a single CaseRecord and cutoff."""

    def __init__(
        self,
        case: CaseRecord,
        deps: CaseDependencies,
        *,
        max_tool_calls: int = 18,
        read_only: bool = False,
        cutoff_override: datetime | None = None,
    ) -> None:
        self._case = case
        self._deps = deps
        self._registry = EvidenceRegistry(case)
        self._max_tool_calls = max_tool_calls
        self._read_only = read_only
        self._cutoff_override = cutoff_override
        self.tool_call_count = 0
        self.paused_for_approval = False
        self.last_comparison: CohortComparisonResult | None = None
        self.recorded_evidence_ids: list[str] = []

    def _canonical_service(self, service_or_alias: str) -> str:
        return self._deps.catalog.resolve_canonical_name(service_or_alias)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _current_cutoff(self) -> datetime:
        if self._cutoff_override is not None:
            return self._cutoff_override
        if self._case.revisions:
            return self._case.revisions[-1].cutoff_at
        return self._case.alert.effective_cutoff

    def _check_budget(self, tool_name: str) -> str | None:
        if self.paused_for_approval and tool_name != "propose_action":
            return json.dumps(
                {
                    "status": "paused_for_approval",
                    "note": "Case is paused awaiting human review or approval. Summarize findings now.",
                }
            )
        if self.tool_call_count >= self._max_tool_calls:
            return json.dumps(
                {
                    "status": "budget_exhausted",
                    "note": (
                        f"Maximum tool budget ({self._max_tool_calls}) reached. "
                        "Emit partial findings and the next suggested check."
                    ),
                }
            )
        self.tool_call_count += 1
        return None

    def _active_releases_for_repo(self, repository: str) -> list[Release]:
        env = self._case.alert.environment
        cutoff = self._current_cutoff()
        active: list[Release] = []
        for svc in self._deps.catalog.list_services():
            if svc.repository != repository:
                continue
            res = self._deps.releases.resolve_active_releases(svc.service, env, cutoff)
            if res.status == ConnectorStatus.OK:
                active.extend(res.releases)
        return active

    def _classify_repo_revision(
        self,
        repository: str,
        target_sha: str,
        base_sha: str | None = None,
    ) -> tuple[str, str | None]:
        """Link a repository SHA to active releases in the case environment at cutoff."""
        active = self._active_releases_for_repo(repository)
        for rel in active:
            if _sha_matches(target_sha, rel.deployed_sha):
                if base_sha is None or _sha_matches(base_sha, rel.previous_sha):
                    return "deployed", rel.release_id
                return "deployed_target_unmatched_base", rel.release_id
        return "undeployed_candidate", None

    def _classify_log_vs_deploy(
        self,
        *,
        service: str,
        env: str,
        cutoff: datetime,
        effective_start: datetime,
        effective_end: datetime,
        total: int,
        first_occurrence: datetime | None,
    ) -> tuple[str | None, str | None]:
        """Compare error log timing against the active deployment timestamp."""
        res = self._deps.releases.resolve_active_releases(service, env, cutoff)
        if res.status != ConnectorStatus.OK or not res.releases:
            return None, None
        latest_rel = res.releases[0]
        deployed_at = latest_rel.deployed_at
        if total == 0:
            if effective_end <= deployed_at:
                return "pre_deploy_clean", latest_rel.release_id
            return "no_errors_observed", latest_rel.release_id
        if (first_occurrence is not None and first_occurrence < deployed_at) or (
            effective_end <= deployed_at
        ):
            return "pre_deploy_errors_detected", latest_rel.release_id
        if effective_start >= deployed_at or (
            first_occurrence is not None and first_occurrence >= deployed_at
        ):
            return "post_deploy_errors_only", latest_rel.release_id
        return None, latest_rel.release_id

    def _audit_and_emit(
        self,
        *,
        tool_name: str,
        sanitized_args: dict[str, Any],
        status: str,
        summary: str,
        references: list[str],
        event_type: EventType = EventType.STEP,
        payload: dict[str, Any] | None = None,
    ) -> None:
        timestamp = self._now()
        role = TOOL_ROLE_MAP.get(tool_name, "Investigation")
        audit_rec = AuditRecord(
            case_id=self._case.case_id,
            timestamp=timestamp,
            actor="investigator_agent",
            action_or_tool=tool_name,
            sanitized_args=sanitized_args,
            status=status,
            summary=summary,
            references=references,
        )
        self._deps.audit.record_audit(audit_rec)
        event = CaseEvent(
            event_type=event_type,
            case_id=self._case.case_id,
            timestamp=timestamp,
            role=role,
            summary=summary,
            tool_name=tool_name,
            sanitized_args=sanitized_args,
            references=references,
            payload={"status": status, **(payload or {})},
        )
        self._case.events.append(event)
        self._case.updated_at = timestamp
        for ref in references:
            if ref.startswith("EV-") and ref not in self.recorded_evidence_ids:
                self.recorded_evidence_ids.append(ref)
        if not self._read_only and self._deps.case_store is not None:
            self._deps.case_store.save_case(
                self._case,
                audit_records=[audit_rec],
            )
            event = self._case.events[-1]
        self._deps.audit.emit_event(event)

    def resolve_release(self, service: str, environment: str | None = None) -> str:
        if blocked := self._check_budget("resolve_release"):
            return blocked
        canonical = self._canonical_service(service)
        env = environment or self._case.alert.environment
        cutoff = self._current_cutoff()
        result = self._deps.releases.resolve_active_releases(canonical, env, cutoff)

        evidence_ids: list[str] = []
        if result.status == ConnectorStatus.OK and result.releases:
            for rel in result.releases:
                ev = self._registry.record(
                    service=canonical,
                    environment=env,
                    observed_at=cutoff,
                    source="release_source",
                    reproducible_reference=f"{rel.release_id}@{rel.repository}:{rel.deployed_sha}",
                    observed_fact=(
                        f"Active release {rel.release_id} deployed at {rel.deployed_at.isoformat()} "
                        f"(previous={rel.previous_sha}, deployed={rel.deployed_sha}, "
                        f"status={rel.status}, cohort={rel.cohort}): {rel.summary}"
                    ),
                    revision=rel.deployed_sha,
                    observation_type="release",
                    cohort=rel.cohort,
                )
                evidence_ids.append(ev.evidence_id)
            summary = (
                f"Resolved {len(result.releases)} active release(s) for {canonical}: "
                + ", ".join(r.release_id for r in result.releases)
            )
        else:
            gap = f"release_source ({canonical}/{env}): {result.status.value} - {result.note}"
            self._registry.record_gap(gap)
            summary = gap

        self._audit_and_emit(
            tool_name="resolve_release",
            sanitized_args={"service": canonical, "environment": env},
            payload={"despliegues": [
                {"id": r.release_id, "hora": r.deployed_at.strftime("%H:%M:%S"),
                 "desplegado_en": r.deployed_at.isoformat()}
                for r in result.releases
            ]},
            status=result.status.value,
            summary=summary,
            references=evidence_ids,
        )
        return json.dumps(
            {
                "status": result.status.value,
                "service": canonical,
                "environment": env,
                "evidence_ids": evidence_ids,
                "releases": [r.model_dump(mode="json") for r in result.releases],
                "note": result.note,
            }
        )

    def list_changes(self, service: str, hours: int = 3) -> str:
        if blocked := self._check_budget("list_changes"):
            return blocked
        canonical = self._canonical_service(service)
        env = self._case.alert.environment
        cutoff = self._current_cutoff()
        result = self._deps.releases.list_recent_changes(canonical, env, hours, cutoff)

        evidence_ids: list[str] = []
        for rel in result.releases:
            ev = self._registry.record(
                service=rel.service,
                environment=env,
                observed_at=cutoff,
                source="change_log",
                reproducible_reference=f"{rel.release_id}@{rel.repository}:{rel.deployed_sha}",
                observed_fact=(
                    f"Change {rel.release_id} on {rel.service} ({rel.environment}) at "
                    f"{rel.deployed_at.isoformat()}, status={rel.status}: {rel.summary}"
                ),
                revision=rel.deployed_sha,
                window=f"last_{hours}h_until_{cutoff.isoformat()}",
                observation_type="change_log",
                cohort=rel.cohort,
            )
            evidence_ids.append(ev.evidence_id)

        if result.status != ConnectorStatus.OK:
            self._registry.record_gap(
                f"change_log ({canonical}): {result.status.value} - {result.note}"
            )

        summary = f"Found {len(result.releases)} change(s) for {canonical} in last {hours}h"
        self._audit_and_emit(
            tool_name="list_changes",
            sanitized_args={"service": canonical, "hours": hours},
            payload={"despliegues": [
                {"id": r.release_id, "hora": r.deployed_at.strftime("%H:%M:%S"),
                 "desplegado_en": r.deployed_at.isoformat(),
                 "servicio": r.service, "descripcion": r.summary}
                for r in sorted(result.releases, key=lambda item: item.deployed_at, reverse=True)
            ]},
            status=result.status.value,
            summary=summary,
            references=evidence_ids,
        )
        return json.dumps(
            {
                "status": result.status.value,
                "service": canonical,
                "evidence_ids": evidence_ids,
                "changes": [r.model_dump(mode="json") for r in result.releases],
                "note": result.note,
            }
        )

    def compare_revisions(
        self,
        repository: str,
        base_sha: str,
        target_sha: str,
        path: str | None = None,
    ) -> str:
        if blocked := self._check_budget("compare_revisions"):
            return blocked
        cutoff = self._current_cutoff()
        deployment_status, release_id = self._classify_repo_revision(
            repository, target_sha, base_sha=base_sha
        )
        result = self._deps.code_repo.compare_revisions(repository, base_sha, target_sha, path)
        evidence_ids: list[str] = []
        env = self._case.alert.environment

        if result.status == ConnectorStatus.OK and result.files:
            if deployment_status == "undeployed_candidate":
                prefix = f"[UNDEPLOYED CANDIDATE - NOT ACTIVE IN {env} AT CUTOFF] "
                self._registry.record_gap(
                    f"Diff {repository}:{base_sha[:8]}..{target_sha[:8]} is not deployed in {env} at cutoff."
                )
            else:
                prefix = f"[DEPLOYED RELEASE {release_id}] "

            for file_diff in result.files:
                ev = self._registry.record(
                    service=repository,
                    environment=env,
                    observed_at=cutoff,
                    source="git_diff" if deployment_status != "undeployed_candidate" else "git_diff_undeployed",
                    reproducible_reference=f"{repository}:{base_sha}..{target_sha}:{file_diff.path}",
                    observed_fact=(
                        f"{prefix}Diff in {file_diff.path} ({base_sha[:8]}..{target_sha[:8]}), "
                        f"truncated={file_diff.truncated}:\n{file_diff.patch}"
                    ),
                    revision=target_sha,
                    observation_type="code_diff",
                    comparison_result=deployment_status,
                )
                evidence_ids.append(ev.evidence_id)
            summary = (
                f"Diff {repository} {base_sha[:8]}..{target_sha[:8]} "
                f"({deployment_status}): {len(result.files)} file(s)"
            )
        else:
            gap = f"git_diff ({repository}): {result.status.value} - {result.note}"
            self._registry.record_gap(gap)
            summary = gap

        self._audit_and_emit(
            tool_name="compare_revisions",
            sanitized_args={
                "repository": repository,
                "base_sha": base_sha,
                "target_sha": target_sha,
                "path": path,
            },
            status=result.status.value,
            summary=summary,
            references=evidence_ids,
        )
        return json.dumps(
            {
                "status": result.status.value,
                "repository": repository,
                "base_sha": base_sha,
                "target_sha": target_sha,
                "deployment_status": deployment_status,
                "release_id": release_id,
                "evidence_ids": evidence_ids,
                "files": [f.model_dump(mode="json") for f in result.files],
                "note": result.note,
            }
        )

    def read_snippet(
        self,
        repository: str,
        sha: str,
        path: str,
        start_line: int = 1,
        end_line: int = 80,
    ) -> str:
        if blocked := self._check_budget("read_snippet"):
            return blocked
        cutoff = self._current_cutoff()
        deployment_status, release_id = self._classify_repo_revision(repository, sha)
        result = self._deps.code_repo.read_snippet(repository, sha, path, start_line, end_line)
        evidence_ids: list[str] = []
        env = self._case.alert.environment

        if result.status == ConnectorStatus.OK:
            if deployment_status == "undeployed_candidate":
                prefix = f"[UNDEPLOYED CANDIDATE - NOT ACTIVE IN {env} AT CUTOFF] "
                self._registry.record_gap(
                    f"Snippet {repository}@{sha[:8]}:{path} is not deployed in {env} at cutoff."
                )
            else:
                prefix = f"[DEPLOYED RELEASE {release_id}] "

            ev = self._registry.record(
                service=repository,
                environment=env,
                observed_at=cutoff,
                source="git_show" if deployment_status != "undeployed_candidate" else "git_show_undeployed",
                reproducible_reference=(
                    f"{repository}@{result.sha}:{result.path}#L{result.start_line}-L{result.end_line}"
                ),
                observed_fact=(
                    f"{prefix}Lines {result.start_line}-{result.end_line} of {result.path} at {result.sha}"
                ),
                revision=result.sha,
                observation_type="code_snippet",
                comparison_result=deployment_status,
            )
            evidence_ids.append(ev.evidence_id)
            summary = (
                f"Read {result.path}#L{result.start_line}-L{result.end_line} "
                f"at {result.sha[:8]} ({deployment_status})"
            )
        else:
            gap = f"git_show ({repository}:{path}): {result.status.value} - {result.note}"
            self._registry.record_gap(gap)
            summary = gap

        self._audit_and_emit(
            tool_name="read_snippet",
            sanitized_args={
                "repository": repository,
                "sha": sha,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
            },
            status=result.status.value,
            summary=summary,
            references=evidence_ids,
        )
        return json.dumps(
            {
                "status": result.status.value,
                "deployment_status": deployment_status,
                "release_id": release_id,
                "evidence_ids": evidence_ids,
                "snippet": result.model_dump(mode="json"),
            }
        )

    def query_dependencies(
        self,
        service: str,
        direction: str = "outbound",
        version: str | None = None,
    ) -> str:
        if blocked := self._check_budget("query_dependencies"):
            return blocked
        canonical = self._canonical_service(service)
        env = self._case.alert.environment
        cutoff = self._current_cutoff()
        result = self._deps.architecture.query_dependencies(canonical, env, version, direction)
        evidence_ids: list[str] = []
        if result.status == ConnectorStatus.OK and result.relations:
            for rel in result.relations:
                ev = self._registry.record(
                    service=canonical,
                    environment=env,
                    observed_at=cutoff,
                    source=rel.source_kind,
                    reproducible_reference=rel.reference,
                    observed_fact=(
                        f"{rel.source_service} --[{rel.relation_type.value}]--> "
                        f"{rel.target_service} (corroborated={rel.corroborated}, "
                        f"ambiguous={rel.ambiguous})"
                    ),
                    revision=rel.revision,
                    observation_type="architecture",
                )
                evidence_ids.append(ev.evidence_id)
                if rel.ambiguous:
                    self._registry.record_gap(
                        f"Ambiguous architecture edge {rel.source_service} -> {rel.target_service} ({rel.reference})"
                    )
            summary = f"Found {len(result.relations)} {direction} dependency relation(s) for {canonical}"
        else:
            gap = f"architecture ({canonical}): {result.status.value} - {result.note}"
            self._registry.record_gap(gap)
            summary = gap

        self._audit_and_emit(
            tool_name="query_dependencies",
            sanitized_args={"service": canonical, "direction": direction, "version": version},
            status=result.status.value,
            summary=summary,
            references=evidence_ids,
        )
        return json.dumps(
            {
                "status": result.status.value,
                "service": canonical,
                "evidence_ids": evidence_ids,
                "relations": [r.model_dump(mode="json") for r in result.relations],
                "note": result.note,
            }
        )

    def query_metrics(
        self,
        service: str,
        metric: str = "latency_p95_ms",
        window_minutes: int = 30,
    ) -> str:
        if blocked := self._check_budget("query_metrics"):
            return blocked
        canonical = self._canonical_service(service)
        env = self._case.alert.environment
        cutoff = self._current_cutoff()
        result = self._deps.telemetry.query_metrics(
            canonical, env, metric, window_minutes, cutoff, allow_post_action=False
        )
        evidence_ids: list[str] = []
        if result.status == ConnectorStatus.OK and result.points:
            ev = self._registry.record(
                service=canonical,
                environment=env,
                observed_at=cutoff,
                source="metrics",
                reproducible_reference=result.reproducible_query,
                observed_fact=(
                    f"{metric} current={result.current_value} {result.unit}, "
                    f"15m_ago={result.value_15m_ago} {result.unit}, points={len(result.points)}, "
                    f"volume={result.request_volume if result.request_volume is not None else 'unknown'}, "
                    f"coverage={result.coverage:.2f}"
                ),
                window=f"{result.window_start.isoformat()}..{result.window_end.isoformat()}",
                observation_type="metrics",
            )
            evidence_ids.append(ev.evidence_id)
            summary = (
                f"{canonical} {metric}: current={result.current_value} {result.unit} "
                f"(15m ago: {result.value_15m_ago}, coverage={result.coverage:.2f})"
            )
        else:
            gap = f"metrics ({canonical}/{metric}): {result.status.value} - {result.note}"
            self._registry.record_gap(gap)
            summary = gap

        self._audit_and_emit(
            tool_name="query_metrics",
            payload={"actual": result.current_value, "hace_15_min": result.value_15m_ago,
                     "unidad": result.unit,
                     "puntos": [[p.timestamp.strftime("%H:%M:%S"), p.value] for p in result.points]},
            sanitized_args={
                "service": canonical,
                "metric": metric,
                "window_minutes": window_minutes,
            },
            status=result.status.value,
            summary=summary,
            references=evidence_ids,
        )
        return json.dumps(
            {
                "status": result.status.value,
                "evidence_ids": evidence_ids,
                "coverage": result.coverage,
                "request_volume": result.request_volume,
                "metrics": result.model_dump(mode="json"),
            }
        )

    def detect_anomaly(self, service: str, metric: str = "latency_p95_ms") -> str:
        if blocked := self._check_budget("detect_anomaly"):
            return blocked
        canonical = self._canonical_service(service)
        env = self._case.alert.environment
        cutoff = self._current_cutoff()
        result = self._deps.telemetry.detect_anomaly(canonical, env, metric, cutoff)
        evidence_ids: list[str] = []
        if result.status == ConnectorStatus.OK:
            ev = self._registry.record(
                service=canonical,
                environment=env,
                observed_at=cutoff,
                source="analytics_anomaly",
                reproducible_reference=result.reproducible_query,
                observed_fact=(
                    f"Anomaly={result.is_anomalous}, z_score={result.z_score:.2f}, "
                    f"current={result.current_value}, threshold={result.threshold}, "
                    f"projection_status={result.projection_status}, "
                    f"projected_minutes_to_threshold={result.projected_minutes_to_threshold}"
                ),
                window=f"{result.window_start.isoformat()}..{result.window_end.isoformat()}",
                observation_type="anomaly",
                comparison_result=result.projection_status,
            )
            evidence_ids.append(ev.evidence_id)
            summary = (
                f"Anomaly={result.is_anomalous} (z={result.z_score:.2f}, "
                f"projection={result.projection_status}:{result.projected_minutes_to_threshold} min)"
            )
        else:
            gap = f"anomaly ({canonical}/{metric}): {result.status.value} - {result.note}"
            self._registry.record_gap(gap)
            summary = gap

        self._audit_and_emit(
            tool_name="detect_anomaly",
            sanitized_args={"service": canonical, "metric": metric},
            payload={"anomalia": result.is_anomalous, "minutos_para_cruce": result.projected_minutes_to_threshold,
                     "z": result.z_score, "umbral": result.threshold, "actual": result.current_value},
            status=result.status.value,
            summary=summary,
            references=evidence_ids,
        )
        return json.dumps(
            {
                "status": result.status.value,
                "evidence_ids": evidence_ids,
                "projection_status": result.projection_status,
                "anomaly": result.model_dump(mode="json"),
            }
        )

    def search_logs(
        self,
        service: str,
        pattern: str,
        window_minutes: int = 30,
        cohort: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> str:
        if blocked := self._check_budget("search_logs"):
            return blocked
        canonical = self._canonical_service(service)
        env = self._case.alert.environment
        cutoff = self._current_cutoff()

        try:
            parsed_start = datetime.fromisoformat(window_start) if window_start else None
            parsed_end = datetime.fromisoformat(window_end) if window_end else None
        except ValueError as exc:
            return json.dumps({"status": "validation_error", "error": f"Invalid ISO window: {exc}"})

        if parsed_end is not None and parsed_end > cutoff:
            parsed_end = cutoff

        result = self._deps.telemetry.search_logs(
            canonical,
            env,
            pattern,
            window_minutes,
            cutoff,
            cohort,
            window_start=parsed_start,
            window_end=parsed_end,
        )
        evidence_ids: list[str] = []
        comparison_result, active_release_id = self._classify_log_vs_deploy(
            service=canonical,
            env=env,
            cutoff=cutoff,
            effective_start=result.window_start,
            effective_end=result.window_end,
            total=result.total,
            first_occurrence=result.first_occurrence,
        )

        if result.status in {ConnectorStatus.OK, ConnectorStatus.NO_RESULTS}:
            first_str = result.first_occurrence.isoformat() if result.first_occurrence else "none"
            ev = self._registry.record(
                service=canonical,
                environment=env,
                observed_at=cutoff,
                source="logs",
                reproducible_reference=result.reproducible_query,
                observed_fact=(
                    f"Pattern '{pattern}' matched {result.total} log(s) "
                    f"(first_occurrence={first_str}, cohorts={result.cohort_counts}, "
                    f"requests={result.cohort_requests}, rates={result.cohort_error_rates}, "
                    f"uncovered={result.uncovered_cohorts})"
                ),
                revision=active_release_id,
                window=f"{result.window_start.isoformat()}..{result.window_end.isoformat()}",
                observation_type="logs",
                cohort=cohort,
                comparison_result=comparison_result,
            )
            evidence_ids.append(ev.evidence_id)
            if result.uncovered_cohorts:
                self._registry.record_gap(
                    f"logs ({canonical}): zero-request uncovered cohort(s) {result.uncovered_cohorts}"
                )
            summary = (
                f"Logs '{pattern}' on {canonical}: {result.total} matches "
                f"(first={first_str}, rates={result.cohort_error_rates})"
            )
        else:
            gap = f"logs ({canonical}): {result.status.value} - {result.note}"
            self._registry.record_gap(gap)
            summary = gap

        self._audit_and_emit(
            tool_name="search_logs",
            payload={"total": result.total, "primera_ocurrencia_iso": (
                result.first_occurrence.isoformat() if result.first_occurrence else None
            ), "primera_ocurrencia": (
                result.first_occurrence.strftime("%H:%M:%S") if result.first_occurrence else None
            )},
            sanitized_args={
                "service": canonical,
                "pattern": pattern,
                "window_minutes": window_minutes,
                "cohort": cohort,
                "window_start": window_start,
                "window_end": window_end,
            },
            status=result.status.value,
            summary=summary,
            references=evidence_ids,
        )
        return json.dumps(
            {
                "status": result.status.value,
                "evidence_ids": evidence_ids,
                "coverage": result.coverage,
                "comparison_result": comparison_result,
                "logs": result.model_dump(mode="json"),
            }
        )

    def compare_cohort_error_rates(
        self,
        service: str,
        pattern: str = "*",
        window_minutes: int | None = None,
        check_shared_saturation: bool = True,
    ) -> str:
        """Compare pre/post-deployment error rates per cohort up to the active cutoff."""
        if blocked := self._check_budget("compare_cohort_error_rates"):
            return blocked
        canonical = self._canonical_service(service)
        env = self._case.alert.environment
        cutoff = self._current_cutoff()
        svc_cfg = self._deps.catalog.get_service(canonical)

        comparison = compare_service_cohort_rates(
            service=canonical,
            environment=env,
            cutoff_at=cutoff,
            telemetry=self._deps.telemetry,
            releases=self._deps.releases,
            service_config=svc_cfg,
            pattern=pattern,
            window_minutes=window_minutes,
            check_shared_saturation=check_shared_saturation,
        )
        self.last_comparison = comparison
        window_str = (
            f"{comparison.pre_window_start.isoformat()}..{comparison.post_window_end.isoformat()}"
        )
        queries = sorted({w.reproducible_query for w in comparison.windows if w.reproducible_query})
        reproducible_ref = " | ".join(queries) if queries else f"{canonical}/{env}"
        cohort_summaries = [
            (
                f"{w.label}:{w.cohort}={w.errors}/{w.requests if w.requests is not None else '?'}"
                f"({f'{w.error_rate * 100:.2f}%' if w.error_rate is not None else 'indeterminada'})"
            )
            for w in comparison.windows
        ]
        ev = self._registry.record(
            service=canonical,
            environment=env,
            observed_at=cutoff,
            source="cohort_comparison",
            reproducible_reference=reproducible_ref,
            observed_fact=(
                f"{comparison.summary} Windows=[{'; '.join(cohort_summaries)}] "
                f"pre_vol={comparison.pre_total_requests} post_vol={comparison.post_total_requests} "
                f"saturated={comparison.shared_resource_saturated} verdict={comparison.verdict.value}"
            ),
            revision=comparison.release_id,
            window=window_str,
            observation_type="cohort_comparison",
            cohort=",".join(sorted({w.cohort for w in comparison.windows if w.cohort})) or None,
            error_definition=comparison.error_definition,
            comparison_result=comparison.verdict.value,
        )
        for missing in comparison.missing_data:
            self._registry.record_gap(f"cohort_comparison ({canonical}): {missing}")

        evidence_ids = [ev.evidence_id]
        status = (
            "indeterminate"
            if comparison.verdict == ComparisonVerdict.INDETERMINATE
            else "ok"
        )
        self._audit_and_emit(
            tool_name="compare_cohort_error_rates",
            sanitized_args={
                "service": canonical,
                "pattern": pattern,
                "window_minutes": window_minutes,
                "check_shared_saturation": check_shared_saturation,
            },
            status=status,
            summary=comparison.summary,
            references=evidence_ids,
        )
        return json.dumps(
            {
                "status": status,
                "evidence_ids": evidence_ids,
                "comparison": comparison.model_dump(mode="json"),
            }
        )

    def search_similar_incidents(self, description: str, k: int = 3) -> str:
        if blocked := self._check_budget("search_similar_incidents"):
            return blocked
        cutoff = self._current_cutoff()
        matches = self._deps.knowledge.search_similar_incidents(
            description, k=k, cutoff_at=cutoff
        )
        evidence_ids: list[str] = []
        for inc in matches:
            ev = self._registry.record(
                service=self._case.alert.service,
                environment=self._case.alert.environment,
                observed_at=cutoff,
                source="incident_history",
                reproducible_reference=f"{inc.source}#{inc.incident_id}",
                observed_fact=(
                    f"{inc.incident_id} (sim={inc.similarity:.2f}): "
                    f"root_cause={inc.root_cause}; resolution={inc.resolution}"
                ),
                observation_type="incident_history",
            )
            evidence_ids.append(ev.evidence_id)

        summary = f"Found {len(matches)} similar historical incident(s)"
        self._audit_and_emit(
            tool_name="search_similar_incidents",
            sanitized_args={"description": description[:120], "k": k},
            status="ok" if matches else "no_results",
            summary=summary,
            references=evidence_ids,
        )
        return json.dumps(
            {
                "status": "ok" if matches else "no_results",
                "evidence_ids": evidence_ids,
                "incidents": [m.model_dump(mode="json") for m in matches],
            }
        )

    def search_runbook(self, query: str) -> str:
        if blocked := self._check_budget("search_runbook"):
            return blocked
        cutoff = self._current_cutoff()
        matches = self._deps.knowledge.search_runbook(query)
        evidence_ids: list[str] = []
        for rb in matches:
            ev = self._registry.record(
                service=self._case.alert.service,
                environment=self._case.alert.environment,
                observed_at=cutoff,
                source="runbooks",
                reproducible_reference=f"{rb.source}#{rb.runbook_id}",
                observed_fact=(
                    f"{rb.runbook_id} ({rb.title}): conditions={rb.applicable_conditions}; "
                    f"steps={'; '.join(rb.steps[:3])}"
                ),
                observation_type="runbook",
            )
            evidence_ids.append(ev.evidence_id)

        summary = f"Found {len(matches)} matching runbook(s)"
        self._audit_and_emit(
            tool_name="search_runbook",
            sanitized_args={"query": query[:120]},
            status="ok" if matches else "no_results",
            summary=summary,
            references=evidence_ids,
        )
        return json.dumps(
            {
                "status": "ok" if matches else "no_results",
                "evidence_ids": evidence_ids,
                "runbooks": [m.model_dump(mode="json") for m in matches],
            }
        )

    def propose_action(
        self,
        action: ActionType | str,
        target_service: str,
        target_release_id: str,
        hypothesis: str,
        justification: str,
        evidence_ids: list[str],
        verification_plan: str,
        contradicting_evidence_ids: list[str] | None = None,
        gaps: list[str] | None = None,
        exposed_services: list[str] | None = None,
        inferred_claims: list[str] | None = None,
    ) -> str:
        if self._read_only:
            return json.dumps(
                {
                    "status": "read_only_mode",
                    "error": "Follow-up investigation is strictly read-only; return candidate findings to backend.",
                }
            )
        if blocked := self._check_budget("propose_action"):
            return blocked
        canonical_target = self._canonical_service(target_service)
        parsed_action = ActionType(action) if isinstance(action, str) else action
        finding = Finding(
            hypothesis=hypothesis,
            supporting_evidence_ids=list(evidence_ids),
            contradicting_evidence_ids=list(contradicting_evidence_ids or []),
            gaps=list(gaps or self._case.connector_gaps),
            exposed_services=list(exposed_services or [self._case.alert.service]),
            inferred_claims=list(inferred_claims or []),
        )
        try:
            proposal, was_duplicate = self._deps.approvals.register_proposal(
                case=self._case,
                action=parsed_action,
                target_service=canonical_target,
                target_release_id=target_release_id,
                finding=finding,
                justification=justification,
                evidence_ids=list(evidence_ids),
                verification_plan=verification_plan,
                now=self._now(),
            )
        except (
            InvalidCitationError,
            InactiveReleaseError,
            InsufficientRollbackEvidenceError,
        ) as exc:
            self._audit_and_emit(
                tool_name="propose_action",
                sanitized_args={
                    "action": parsed_action.value,
                    "target_release_id": target_release_id,
                    "evidence_ids": evidence_ids,
                },
                status="validation_error",
                summary=f"Rejected proposal validation: {exc}",
                references=[],
            )
            return json.dumps({"status": "validation_error", "error": str(exc)})
        except ProposalConflictError as exc:
            assert self._case.proposal is not None
            self._audit_and_emit(
                tool_name="propose_action",
                sanitized_args={
                    "action": parsed_action.value,
                    "target_service": canonical_target,
                    "target_release_id": target_release_id,
                },
                status="proposal_locked",
                summary=f"Blocked proposal replacement: {exc}",
                references=[self._case.proposal.proposal_id],
            )
            return json.dumps(
                {
                    "status": "proposal_locked",
                    "proposal_id": self._case.proposal.proposal_id,
                    "error": str(exc),
                }
            )

        is_shadow = self._case.mode == OperationalMode.SHADOW
        if is_shadow or proposal.risk_level != RiskLevel.LOW:
            self.paused_for_approval = True

        if was_duplicate:
            return json.dumps(
                {
                    "status": "already_registered",
                    "proposal_id": proposal.proposal_id,
                    "risk_level": proposal.risk_level.value,
                    "note": "Equivalent proposal was already registered for this case revision.",
                }
            )

        self._audit_and_emit(
            tool_name="propose_action",
            sanitized_args={
                "action": proposal.action.value,
                "target_service": proposal.target_service,
                "target_release_id": proposal.target_release_id,
                "evidence_ids": proposal.evidence_ids,
            },
            status=proposal.status.value,
            summary=(
                f"Proposed {proposal.action.value} on {proposal.target_service} "
                f"(release={proposal.target_release_id}, risk={proposal.risk_level.value}, "
                f"mode={self._case.mode.value})"
            ),
            references=proposal.evidence_ids,
            event_type=EventType.PROPOSAL,
            payload={
                "proposal_id": proposal.proposal_id,
                "risk_level": proposal.risk_level.value,
                "action": proposal.action.value,
                "mode": self._case.mode.value,
                "status": proposal.status.value,
                "proposal_version": proposal.version,
                "justificacion": proposal.justification,
            },
        )
        if is_shadow:
            return json.dumps(
                {
                    "status": ProposalStatus.RECOMMENDED.value,
                    "proposal_id": proposal.proposal_id,
                    "risk_level": proposal.risk_level.value,
                    "note": (
                        "Shadow mode recommendation persisted for human review. "
                        "No external system was modified; provide the final report now."
                    ),
                }
            )
        return json.dumps(
            {
                "status": "awaiting_approval"
                if proposal.risk_level != RiskLevel.LOW
                else proposal.status.value,
                "proposal_id": proposal.proposal_id,
                "risk_level": proposal.risk_level.value,
                "note": (
                    "Proposal registered and paused for human approval. "
                    "Do not call additional tools; provide the final report now."
                    if proposal.risk_level != RiskLevel.LOW
                    else "Low-risk action executed automatically."
                ),
            }
        )

    def build_langchain_tools(self) -> list[StructuredTool]:
        """Return LangChain StructuredTool instances bound to this case toolkit."""
        tools = [
            StructuredTool.from_function(
                func=self.query_metrics,
                name="query_metrics",
                description="Query bounded time-series metrics for a service up to cutoff.",
                args_schema=QueryMetricsInput,
            ),
            StructuredTool.from_function(
                func=self.detect_anomaly,
                name="detect_anomaly",
                description="Run Z-score anomaly detection and linear threshold projection.",
                args_schema=DetectAnomalyInput,
            ),
            StructuredTool.from_function(
                func=self.query_dependencies,
                name="query_dependencies",
                description="Inspect architectural dependencies (outbound or inbound) for a service.",
                args_schema=QueryDependenciesInput,
            ),
            StructuredTool.from_function(
                func=self.resolve_release,
                name="resolve_release",
                description="Resolve active production/environment releases and SHAs at cutoff time.",
                args_schema=ResolveReleaseInput,
            ),
            StructuredTool.from_function(
                func=self.list_changes,
                name="list_changes",
                description="List recent deployments/changes for a service within the lookback window.",
                args_schema=ListChangesInput,
            ),
            StructuredTool.from_function(
                func=self.compare_revisions,
                name="compare_revisions",
                description="Run a bounded git diff between base_sha and target_sha in an authorized repo.",
                args_schema=CompareRevisionsInput,
            ),
            StructuredTool.from_function(
                func=self.read_snippet,
                name="read_snippet",
                description="Read citable line ranges of a file at an exact commit SHA.",
                args_schema=ReadSnippetInput,
            ),
            StructuredTool.from_function(
                func=self.search_logs,
                name="search_logs",
                description=(
                    "Count and sample redacted log errors and per-cohort error rates "
                    "using relative lookback or explicit pre/post-deploy window_start/window_end <= cutoff."
                ),
                args_schema=SearchLogsInput,
            ),
            StructuredTool.from_function(
                func=self.compare_cohort_error_rates,
                name="compare_cohort_error_rates",
                description=(
                    "Compare code-computed pre/post-deployment error rates across stable and new "
                    "cohorts with denominators, coverage, and shared resource saturation checks."
                ),
                args_schema=CompareCohortErrorRatesInput,
            ),
            StructuredTool.from_function(
                func=self.search_similar_incidents,
                name="search_similar_incidents",
                description="Search historical incidents matching a symptom or failure mechanism.",
                args_schema=SearchIncidentsInput,
            ),
            StructuredTool.from_function(
                func=self.search_runbook,
                name="search_runbook",
                description="Search operational runbooks for applicable mitigation procedures.",
                args_schema=SearchRunbookInput,
            ),
        ]
        if not self._read_only:
            tools.append(
                StructuredTool.from_function(
                    func=self.propose_action,
                    name="propose_action",
                    description=(
                        "Register a single evidence-backed preventive action recommendation or proposal. "
                        "Shadow mode saves it for human review; assisted medium/high risk pauses for approval."
                    ),
                    args_schema=ProposeActionInput,
                )
            )
        return tools
