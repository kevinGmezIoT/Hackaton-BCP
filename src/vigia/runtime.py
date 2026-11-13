"""Composition root wiring domain, application, and demo/pilot adapters."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel

from vigia.adapters.outbound.audit import JsonlAuditSinkAdapter
from vigia.adapters.outbound.case_store import SqliteCaseStoreAdapter
from vigia.adapters.outbound.drawio import DrawioArchitectureAdapter
from vigia.adapters.outbound.executor import SimulatedActionExecutor, SimulationState
from vigia.adapters.outbound.github import GitHubReleaseSourceAdapter, GitHubRepositoryAdapter
from vigia.adapters.outbound.http_telemetry import HttpGetFn, PrometheusLokiTelemetryAdapter
from vigia.adapters.outbound.knowledge import LocalKnowledgeAdapter
from vigia.adapters.outbound.local_git import JsonReleaseSourceAdapter, LocalGitRepositoryAdapter
from vigia.adapters.outbound.service_catalog import YamlServiceCatalogAdapter
from vigia.adapters.outbound.telemetry import CsvTelemetryAdapter
from vigia.application.approval_service import ApprovalService
from vigia.application.case_service import CaseService
from vigia.application.investigator import InvestigatorAgent
from vigia.application.tools import CaseDependencies
from vigia.domain.contracts import OperationalMode, RuntimeProfile


@dataclass
class PilotRuntimeConfig:
    """Configuration selecting pilot vs demo adapters and operational safeguards."""

    profile: RuntimeProfile = RuntimeProfile.PILOT
    mode: OperationalMode = OperationalMode.SHADOW
    catalog_path: Path | None = None
    fixtures_dir: Path | None = None
    metrics_path: Path | None = None
    logs_path: Path | None = None
    releases_path: Path | None = None
    incidents_path: Path | None = None
    runbooks_dir: Path | None = None
    authorized_repos: dict[str, Path] = field(default_factory=dict)
    db_path: Path | str = ":memory:"
    audit_path: Path | None = None
    prometheus_url: str | None = None
    loki_url: str | None = None
    github_api_url: str | None = None
    github_service_repos: dict[str, str] = field(default_factory=dict)
    http_get: HttpGetFn | None = None
    unauthorized_services: set[str] = field(default_factory=set)
    diagram_revisions: dict[str, str] | None = None
    simulation_state: SimulationState | None = None
    max_tool_calls: int = 18
    clock: Callable[[], datetime] | None = None
    strict_rollback_validation: bool = True


def build_case_service(
    *,
    fixtures_dir: Path,
    authorized_repos: dict[str, Path],
    model: str | BaseChatModel,
    audit_path: Path | None = None,
    simulation_state: SimulationState | None = None,
    diagram_revisions: dict[str, str] | None = None,
    max_tool_calls: int = 18,
    clock: Callable[[], datetime] | None = None,
    db_path: Path | str = ":memory:",
    mode: OperationalMode = OperationalMode.ASSISTED,
    strict_rollback_validation: bool = False,
) -> tuple[CaseService, SimulationState, JsonlAuditSinkAdapter]:
    """Wire local file/Git adapters into a ready-to-run CaseService (Demo profile)."""
    state = simulation_state or SimulationState()
    catalog = YamlServiceCatalogAdapter.from_yaml(fixtures_dir / "services.yaml")
    releases = JsonReleaseSourceAdapter(
        fixtures_dir / "releases.json",
        rolled_back_release_ids=state.rolled_back_releases,
    )
    code_repo = LocalGitRepositoryAdapter(authorized_repos)
    diagram_paths = {
        svc.service: fixtures_dir / svc.architecture.path
        for svc in catalog.list_services()
    }
    architecture = DrawioArchitectureAdapter(
        diagram_paths,
        catalog,
        diagram_revisions=diagram_revisions,
    )
    telemetry = CsvTelemetryAdapter(
        fixtures_dir / "metrics.csv",
        fixtures_dir / "logs.txt",
        catalog,
        simulation_state=state,
    )
    knowledge = LocalKnowledgeAdapter(
        fixtures_dir / "incidents.json",
        fixtures_dir / "runbooks",
    )
    executor = SimulatedActionExecutor(state)
    approvals = ApprovalService(
        releases,
        executor,
        catalog=catalog,
        mode=mode,
        strict_rollback_validation=strict_rollback_validation,
    )
    audit = JsonlAuditSinkAdapter(audit_path)
    case_store = SqliteCaseStoreAdapter(db_path)

    deps = CaseDependencies(
        catalog=catalog,
        releases=releases,
        code_repo=code_repo,
        architecture=architecture,
        telemetry=telemetry,
        knowledge=knowledge,
        approvals=approvals,
        audit=audit,
        case_store=case_store,
    )
    investigator = InvestigatorAgent(model=model, max_tool_calls=max_tool_calls)
    return (
        CaseService(deps, investigator, clock=clock, default_mode=mode),
        state,
        audit,
    )


def build_pilot_case_service(
    config: PilotRuntimeConfig,
    *,
    model: str | BaseChatModel,
) -> tuple[CaseService, SimulationState, JsonlAuditSinkAdapter]:
    """Build a CaseService selecting Demo or Pilot adapters from PilotRuntimeConfig."""
    if config.profile == RuntimeProfile.DEMO and not any(
        (
            config.catalog_path,
            config.metrics_path,
            config.logs_path,
            config.releases_path,
            config.incidents_path,
            config.runbooks_dir,
            config.prometheus_url,
            config.loki_url,
            config.github_service_repos,
        )
    ):
        assert config.fixtures_dir is not None, "DEMO profile requires fixtures_dir."
        return build_case_service(
            fixtures_dir=config.fixtures_dir,
            authorized_repos=config.authorized_repos,
            model=model,
            audit_path=config.audit_path,
            simulation_state=config.simulation_state,
            diagram_revisions=config.diagram_revisions,
            max_tool_calls=config.max_tool_calls,
            clock=config.clock,
            db_path=config.db_path,
            mode=config.mode,
            strict_rollback_validation=config.strict_rollback_validation,
        )

    base_dir = config.fixtures_dir or Path(".")
    catalog_path = config.catalog_path or (base_dir / "services.yaml")
    catalog = YamlServiceCatalogAdapter.from_yaml(catalog_path)
    state = config.simulation_state or SimulationState()
    for svc in catalog.list_services():
        state.allowed_actions_by_service[svc.service.lower()] = set(svc.allowed_actions)

    telemetry_token = os.environ.get("VIGIA_TELEMETRY_TOKEN")
    github_token = os.environ.get("VIGIA_GITHUB_TOKEN")

    if config.prometheus_url or config.loki_url:
        telemetry = PrometheusLokiTelemetryAdapter(
            catalog,
            prometheus_base_url=config.prometheus_url or "http://prometheus.internal",
            loki_base_url=config.loki_url or "http://loki.internal",
            auth_token=telemetry_token,
            http_get=config.http_get,
        )
    else:
        telemetry = CsvTelemetryAdapter(
            config.metrics_path or (base_dir / "metrics.csv"),
            config.logs_path or (base_dir / "logs.txt"),
            catalog,
            simulation_state=state,
            unauthorized_services=config.unauthorized_services,
        )

    if config.github_api_url and config.github_service_repos:
        releases = GitHubReleaseSourceAdapter(
            config.github_service_repos,
            api_base_url=config.github_api_url,
            auth_token=github_token,
            http_get=config.http_get,
            rolled_back_release_ids=state.rolled_back_releases,
        )
        code_repo = GitHubRepositoryAdapter(
            set(config.github_service_repos.values()),
            api_base_url=config.github_api_url,
            auth_token=github_token,
            http_get=config.http_get,
        )
    else:
        releases = JsonReleaseSourceAdapter(
            config.releases_path or base_dir / "releases.json",
            rolled_back_release_ids=state.rolled_back_releases,
        )
        code_repo = LocalGitRepositoryAdapter(config.authorized_repos)

    diagram_paths = {
        svc.service: base_dir / svc.architecture.path
        for svc in catalog.list_services()
    }
    architecture = DrawioArchitectureAdapter(
        diagram_paths,
        catalog,
        diagram_revisions=config.diagram_revisions,
    )
    knowledge = LocalKnowledgeAdapter(
        config.incidents_path or base_dir / "incidents.json",
        config.runbooks_dir or base_dir / "runbooks",
    )
    executor = SimulatedActionExecutor(state)
    approvals = ApprovalService(
        releases,
        executor,
        catalog=catalog,
        mode=config.mode,
        strict_rollback_validation=config.strict_rollback_validation,
    )
    audit = JsonlAuditSinkAdapter(config.audit_path)
    case_store = SqliteCaseStoreAdapter(config.db_path)

    deps = CaseDependencies(
        catalog=catalog,
        releases=releases,
        code_repo=code_repo,
        architecture=architecture,
        telemetry=telemetry,
        knowledge=knowledge,
        approvals=approvals,
        audit=audit,
        case_store=case_store,
    )
    investigator = InvestigatorAgent(model=model, max_tool_calls=config.max_tool_calls)
    return (
        CaseService(deps, investigator, clock=config.clock, default_mode=config.mode),
        state,
        audit,
    )
