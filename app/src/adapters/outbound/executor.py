"""Simulated action executor and shared simulation state for MVP/testing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from domain.contracts import (
    ActionProposal,
    ActionType,
    ExecutionResult,
    ProposalStatus,
)


@dataclass
class SimulationState:
    """Explicit in-memory state updated by simulated actions."""

    rolled_back_releases: set[str] = field(default_factory=set)
    scaled_services: dict[str, int] = field(default_factory=dict)
    tickets_created: list[str] = field(default_factory=list)
    notifications_sent: list[str] = field(default_factory=list)
    suppress_post_action_metrics: bool = False


class SimulatedActionExecutor:
    """Simulates preventive action execution without mutating external production systems."""

    def __init__(self, state: SimulationState | None = None) -> None:
        self.state = state or SimulationState()
        self._ticket_seq = 1000

    def execute_action(self, proposal: ActionProposal) -> ExecutionResult:
        now = datetime.now(timezone.utc)
        if proposal.action == ActionType.ROLLBACK_CHANGE:
            self.state.rolled_back_releases.add(proposal.target_release_id)
            detail = (
                f"Simulated rollback of release '{proposal.target_release_id}' on "
                f"'{proposal.target_service}' completed."
            )
            return ExecutionResult(
                proposal_id=proposal.proposal_id,
                action=proposal.action,
                status=ProposalStatus.EXECUTED,
                detail=detail,
                executed_at=now,
            )

        if proposal.action == ActionType.SCALE_CAPACITY:
            current = self.state.scaled_services.get(proposal.target_service, 1)
            self.state.scaled_services[proposal.target_service] = current + 1
            detail = (
                f"Simulated capacity scale-out for '{proposal.target_service}' "
                f"(level={current + 1})."
            )
            return ExecutionResult(
                proposal_id=proposal.proposal_id,
                action=proposal.action,
                status=ProposalStatus.EXECUTED,
                detail=detail,
                executed_at=now,
            )

        if proposal.action == ActionType.CREATE_PREVENTIVE_TICKET:
            ticket_id = self._next_ticket_id()
            self.state.tickets_created.append(ticket_id)
            return ExecutionResult(
                proposal_id=proposal.proposal_id,
                action=proposal.action,
                status=ProposalStatus.EXECUTED,
                detail=f"Simulated preventive ticket '{ticket_id}' created.",
                executed_at=now,
                preventive_ticket_id=ticket_id,
            )

        self.state.notifications_sent.append(proposal.target_service)
        return ExecutionResult(
            proposal_id=proposal.proposal_id,
            action=proposal.action,
            status=ProposalStatus.EXECUTED,
            detail=f"Simulated notification sent to owner of '{proposal.target_service}'.",
            executed_at=now,
        )

    def create_preventive_ticket(
        self,
        case_id: str,
        proposal: ActionProposal,
        actor: str,
        reason: str,
    ) -> ExecutionResult:
        now = datetime.now(timezone.utc)
        ticket_id = self._next_ticket_id()
        self.state.tickets_created.append(ticket_id)
        return ExecutionResult(
            proposal_id=proposal.proposal_id,
            action=ActionType.CREATE_PREVENTIVE_TICKET,
            status=ProposalStatus.REJECTED,
            detail=(
                f"Proposal '{proposal.proposal_id}' rejected by {actor}; "
                f"preventive ticket '{ticket_id}' opened for case '{case_id}' ({reason})."
            ),
            executed_at=now,
            preventive_ticket_id=ticket_id,
        )

    def _next_ticket_id(self) -> str:
        self._ticket_seq += 1
        return f"TKT-{self._ticket_seq}"

