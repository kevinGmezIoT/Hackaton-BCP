"""Simulated and pipeline action executor with operation tracking and timeout reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from vigia.domain.contracts import (
    ActionProposal,
    ActionType,
    ExecutionResult,
    ExternalOperationRecord,
    OperationStatus,
    ProposalStatus,
)


@dataclass
class SimulationState:
    """Explicit in-memory state updated by simulated or pipeline actions."""

    rolled_back_releases: set[str] = field(default_factory=set)
    scaled_services: dict[str, int] = field(default_factory=dict)
    tickets_created: list[str] = field(default_factory=list)
    notifications_sent: list[str] = field(default_factory=list)
    suppress_post_action_metrics: bool = False
    next_operation_outcome: OperationStatus = OperationStatus.EXECUTED
    operations: dict[str, ExternalOperationRecord] = field(default_factory=dict)
    operations_by_key: dict[str, str] = field(default_factory=dict)
    allowed_actions_by_service: dict[str, set[ActionType]] = field(default_factory=dict)


class SimulatedActionExecutor:
    """Simulates preventive action execution and pipeline operation reconciliation."""

    def __init__(self, state: SimulationState | None = None) -> None:
        self.state = state or SimulationState()
        self._ticket_seq = 1000
        self._op_seq = 5000

    def _is_action_allowed(self, service: str, action: ActionType) -> bool:
        allowed = self.state.allowed_actions_by_service.get(service.lower())
        if allowed is None:
            return True
        return action in allowed

    def execute_action(self, proposal: ActionProposal) -> ExecutionResult:
        now = datetime.now(timezone.utc)
        op = self.dispatch_operation(
            case_id=proposal.case_id,
            environment="production",
            proposal=proposal,
            operation_key=f"op:{proposal.case_id}:{proposal.proposal_id}:{proposal.target_release_id}",
            now=now,
        )
        return self._operation_to_execution_result(proposal, op)

    def dispatch_operation(
        self,
        *,
        case_id: str,
        environment: str,
        proposal: ActionProposal,
        operation_key: str,
        now: datetime | None = None,
    ) -> ExternalOperationRecord:
        timestamp = now or datetime.now(timezone.utc)
        existing_op_id = self.state.operations_by_key.get(operation_key)
        if existing_op_id is not None and existing_op_id in self.state.operations:
            return self.state.operations[existing_op_id]

        self._op_seq += 1
        op_id = f"OP-{self._op_seq}"

        if not self._is_action_allowed(proposal.target_service, proposal.action):
            record = ExternalOperationRecord(
                operation_id=op_id,
                case_id=case_id,
                proposal_id=proposal.proposal_id,
                service=proposal.target_service,
                environment=environment,
                action=proposal.action,
                target_release_id=proposal.target_release_id,
                operation_key=operation_key,
                status=OperationStatus.FAILED,
                detail=(
                    f"Action '{proposal.action.value}' is not enabled for service "
                    f"'{proposal.target_service}' in '{environment}'."
                ),
                started_at=timestamp,
                completed_at=timestamp,
            )
            self.state.operations[op_id] = record
            self.state.operations_by_key[operation_key] = op_id
            return record

        outcome = self.state.next_operation_outcome
        self.state.next_operation_outcome = OperationStatus.EXECUTED

        if outcome == OperationStatus.TIMEOUT:
            record = ExternalOperationRecord(
                operation_id=op_id,
                case_id=case_id,
                proposal_id=proposal.proposal_id,
                service=proposal.target_service,
                environment=environment,
                action=proposal.action,
                target_release_id=proposal.target_release_id,
                operation_key=operation_key,
                status=OperationStatus.TIMEOUT,
                detail=f"Pipeline operation '{op_id}' timed out awaiting confirmation.",
                started_at=timestamp,
                completed_at=None,
            )
            self.state.operations[op_id] = record
            self.state.operations_by_key[operation_key] = op_id
            return record

        if outcome == OperationStatus.FAILED:
            record = ExternalOperationRecord(
                operation_id=op_id,
                case_id=case_id,
                proposal_id=proposal.proposal_id,
                service=proposal.target_service,
                environment=environment,
                action=proposal.action,
                target_release_id=proposal.target_release_id,
                operation_key=operation_key,
                status=OperationStatus.FAILED,
                detail=f"Pipeline operation '{op_id}' failed in deployment controller.",
                started_at=timestamp,
                completed_at=timestamp,
            )
            self.state.operations[op_id] = record
            self.state.operations_by_key[operation_key] = op_id
            return record

        ticket_id: str | None = None
        if proposal.action == ActionType.ROLLBACK_CHANGE:
            self.state.rolled_back_releases.add(proposal.target_release_id)
            detail = (
                f"Simulated rollback of release '{proposal.target_release_id}' on "
                f"'{proposal.target_service}' completed."
            )
        elif proposal.action == ActionType.SCALE_CAPACITY:
            current = self.state.scaled_services.get(proposal.target_service, 1)
            self.state.scaled_services[proposal.target_service] = current + 1
            detail = (
                f"Simulated capacity scale-out for '{proposal.target_service}' "
                f"(level={current + 1})."
            )
        elif proposal.action == ActionType.CREATE_PREVENTIVE_TICKET:
            ticket_id = self._next_ticket_id()
            self.state.tickets_created.append(ticket_id)
            detail = f"Simulated preventive ticket '{ticket_id}' created."
        else:
            self.state.notifications_sent.append(proposal.target_service)
            detail = f"Simulated notification sent to owner of '{proposal.target_service}'."

        record = ExternalOperationRecord(
            operation_id=op_id,
            case_id=case_id,
            proposal_id=proposal.proposal_id,
            service=proposal.target_service,
            environment=environment,
            action=proposal.action,
            target_release_id=proposal.target_release_id,
            operation_key=operation_key,
            status=OperationStatus.EXECUTED,
            detail=detail,
            started_at=timestamp,
            completed_at=timestamp,
            external_ticket_id=ticket_id,
        )
        self.state.operations[op_id] = record
        self.state.operations_by_key[operation_key] = op_id
        return record

    def get_operation_status(self, operation_id: str) -> ExternalOperationRecord | None:
        return self.state.operations.get(operation_id)

    def complete_timed_out_operation(
        self,
        operation_id: str,
        *,
        status: OperationStatus = OperationStatus.EXECUTED,
        completed_at: datetime | None = None,
    ) -> ExternalOperationRecord:
        """Simulate the pipeline finishing an operation that previously timed out."""
        op = self.state.operations[operation_id]
        timestamp = completed_at or datetime.now(timezone.utc)
        if status == OperationStatus.EXECUTED and op.action == ActionType.ROLLBACK_CHANGE:
            self.state.rolled_back_releases.add(op.target_release_id)
            detail = (
                f"Reconciled pipeline operation '{operation_id}': rollback of "
                f"'{op.target_release_id}' on '{op.service}' completed."
            )
        else:
            detail = f"Reconciled pipeline operation '{operation_id}' with status '{status.value}'."
        updated = op.model_copy(
            update={
                "status": status,
                "detail": detail,
                "completed_at": timestamp,
            }
        )
        self.state.operations[operation_id] = updated
        return updated

    def _operation_to_execution_result(
        self,
        proposal: ActionProposal,
        op: ExternalOperationRecord,
    ) -> ExecutionResult:
        if op.status == OperationStatus.EXECUTED:
            prop_status = ProposalStatus.EXECUTED
        elif op.status == OperationStatus.FAILED:
            prop_status = ProposalStatus.FAILED
        else:
            prop_status = ProposalStatus.APPROVED
        return ExecutionResult(
            proposal_id=proposal.proposal_id,
            action=proposal.action,
            status=prop_status,
            detail=op.detail,
            executed_at=op.completed_at or op.started_at,
            preventive_ticket_id=op.external_ticket_id,
            operation_id=op.operation_id,
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
