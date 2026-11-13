"""Composition root wiring domain, application, and adapters."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel

from vigia.adapters.outbound.audit import JsonlAuditSinkAdapter
from vigia.adapters.outbound.drawio import DrawioArchitectureAdapter
from vigia.adapters.outbound.executor import SimulatedActionExecutor, SimulationState
from vigia.adapters.outbound.knowledge import LocalKnowledgeAdapter
from vigia.adapters.outbound.local_git import JsonReleaseSourceAdapter, LocalGitRepositoryAdapter
from vigia.adapters.outbound.service_catalog import YamlServiceCatalogAdapter
from vigia.adapters.outbound.telemetry import CsvTelemetryAdapter
from vigia.application.approval_service import ApprovalService
from vigia.application.case_service import CaseService
from vigia.application.investigator import InvestigatorAgent
from vigia.application.tools import CaseDependencies


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
) -> tuple[CaseService, SimulationState, JsonlAuditSinkAdapter]:
    """Wire local file/Git adapters into a ready-to-run CaseService."""
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
    approvals = ApprovalService(releases, executor)
    audit = JsonlAuditSinkAdapter(audit_path)

    deps = CaseDependencies(
        catalog=catalog,
        releases=releases,
        code_repo=code_repo,
        architecture=architecture,
        telemetry=telemetry,
        knowledge=knowledge,
        approvals=approvals,
        audit=audit,
    )
    investigator = InvestigatorAgent(model=model, max_tool_calls=max_tool_calls)
    return CaseService(deps, investigator, clock=clock), state, audit

