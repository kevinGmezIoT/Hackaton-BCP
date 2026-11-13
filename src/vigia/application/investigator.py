"""Alternative A: single tool-calling investigator built with langchain.agents.create_agent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

from vigia.application.prompts import (
    FOLLOW_UP_SYSTEM_PROMPT,
    INVESTIGATOR_SYSTEM_PROMPT,
    build_case_user_prompt,
    build_follow_up_user_prompt,
)
from vigia.application.tools import CaseDependencies, CaseToolKit
from vigia.domain.contracts import (
    ActionType,
    CaseEvent,
    CaseRecord,
    CaseStatus,
    ComparisonVerdict,
    ConversationIntent,
    ConversationTurn,
    EventType,
    Finding,
    FollowUpCandidate,
    ProposalStatus,
    ServiceConfig,
)


def _extract_message_text(message: Any) -> str:
    """Normalize LangChain message content to plain text."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(content)


class InvestigatorAgent:
    """Runs Alternative A investigation using LangChain create_agent and case-bound tools."""

    def __init__(
        self,
        model: str | BaseChatModel,
        *,
        max_tool_calls: int = 18,
        recursion_limit: int = 36,
    ) -> None:
        self._model = model
        self._max_tool_calls = max_tool_calls
        self._recursion_limit = recursion_limit

    def run(
        self,
        case: CaseRecord,
        deps: CaseDependencies,
        service_config: ServiceConfig | None,
    ) -> CaseRecord:
        """Investigate the case up to its active revision cutoff and update case state."""
        case.status = CaseStatus.INVESTIGATING
        toolkit = CaseToolKit(case, deps, max_tool_calls=self._max_tool_calls)
        lc_tools = toolkit.build_langchain_tools()

        agent = create_agent(
            model=self._model,
            tools=lc_tools,
            system_prompt=INVESTIGATOR_SYSTEM_PROMPT,
        )

        active_cutoff = (
            case.revisions[-1].cutoff_at.isoformat()
            if case.revisions
            else case.alert.effective_cutoff.isoformat()
        )
        user_prompt = build_case_user_prompt(
            case.alert,
            service_config,
            mode=case.mode,
            cutoff_override=active_cutoff,
        )
        result = agent.invoke(
            {"messages": [HumanMessage(content=user_prompt)]},
            config={"recursion_limit": self._recursion_limit},
        )

        messages = result.get("messages", [])
        final_text = _extract_message_text(messages[-1]) if messages else ""
        case.final_summary = final_text
        now = datetime.now(timezone.utc)
        case.updated_at = now

        active_rev = case.revisions[-1].revision_number if case.revisions else 1
        proposal_for_rev = (
            case.proposal
            if case.proposal is not None and case.proposal.revision_number == active_rev
            else None
        )

        if proposal_for_rev is None:
            case.status = CaseStatus.INCONCLUSIVE
            case.partial_findings = Finding(
                hypothesis=final_text[:280]
                if final_text
                else "Investigation ended without a verified action proposal.",
                supporting_evidence_ids=[ev.evidence_id for ev in case.evidence],
                gaps=list(case.connector_gaps),
                exposed_services=[case.alert.service],
            )
            case.suggested_next_check = (
                "Review collected evidence and connector gaps, verify pre-deploy error baselines, "
                "or provide missing telemetry/architecture access."
            )
        elif proposal_for_rev.status == ProposalStatus.RECOMMENDED:
            case.status = CaseStatus.AWAITING_REVIEW
        elif proposal_for_rev.status == ProposalStatus.PENDING:
            case.status = CaseStatus.AWAITING_APPROVAL
        elif proposal_for_rev.status == ProposalStatus.EXECUTED:
            case.status = CaseStatus.MITIGATED

        event = CaseEvent(
            event_type=EventType.MESSAGE,
            case_id=case.case_id,
            timestamp=now,
            role="Investigator",
            summary=final_text[:300] if final_text else "Investigation turn completed.",
            references=[ev.evidence_id for ev in case.evidence],
            payload={"revision_number": active_rev, "case_status": case.status.value},
        )
        case.events.append(event)
        if deps.case_store is not None:
            deps.case_store.save_case(case)
            event = case.events[-1]
        deps.audit.emit_event(event)
        return case

    def run_follow_up(
        self,
        case: CaseRecord,
        deps: CaseDependencies,
        service_config: ServiceConfig | None,
        *,
        turn: ConversationTurn,
        target_cutoff: datetime,
    ) -> tuple[CaseRecord, FollowUpCandidate]:
        """Run a read-only follow-up investigation without directly mutating published proposals."""
        working_case = case.model_copy(deep=True)
        toolkit = CaseToolKit(
            working_case,
            deps,
            max_tool_calls=self._max_tool_calls,
            read_only=True,
            cutoff_override=target_cutoff,
        )
        lc_tools = toolkit.build_langchain_tools()
        agent = create_agent(
            model=self._model,
            tools=lc_tools,
            system_prompt=FOLLOW_UP_SYSTEM_PROMPT,
        )

        target_service = (
            working_case.proposal.target_service if working_case.proposal else working_case.alert.service
        )
        target_release_id = (
            working_case.proposal.target_release_id if working_case.proposal else None
        )
        prev_summaries = [
            f"Q: {t.question} -> A: {t.answer[:160]}"
            for t in working_case.conversation_turns
            if t.turn_id != turn.turn_id and t.answer
        ]
        ev_summaries = [
            f"[{ev.evidence_id}] ({ev.source}, rev={ev.revision_number}, "
            f"observed_at={ev.observed_at.isoformat()}): {ev.observed_fact[:160]}"
            for ev in working_case.evidence
            if ev.observed_at <= target_cutoff
        ]
        user_prompt = build_follow_up_user_prompt(
            working_case.case_id,
            service=working_case.alert.service,
            target_service=target_service,
            target_release_id=target_release_id,
            question=turn.question,
            base_revision=turn.base_revision,
            cutoff_at=target_cutoff.isoformat(),
            previous_turns_summary=prev_summaries,
            existing_evidence_summary=ev_summaries,
            existing_gaps=working_case.connector_gaps,
            metric=working_case.alert.metric,
            case_summary=(
                f"status={working_case.status.value}; "
                f"alert_observed_at={working_case.alert.observed_at.isoformat()}; "
                f"alert_value={working_case.alert.current_value}; "
                f"symptom={working_case.alert.symptom}; "
                f"diagnosis={working_case.final_summary[:4000]}"
            ),
            proposal_summary=(
                f"{working_case.proposal.action.value}: "
                f"{working_case.proposal.justification}"
                if working_case.proposal else "No action proposal is registered."
            ),
        )
        result = agent.invoke(
            {"messages": [HumanMessage(content=user_prompt)]},
            config={"recursion_limit": self._recursion_limit},
        )
        messages = result.get("messages", [])
        final_text = _extract_message_text(messages[-1]) if messages else ""
        for event in working_case.events:
            if event.sequence_id == 0:
                event.payload = {**event.payload, "turn_id": turn.turn_id}

        cited_ids = list(dict.fromkeys(toolkit.recorded_evidence_ids))
        if not cited_ids:
            cited_ids = [ev.evidence_id for ev in working_case.evidence if ev.evidence_id in final_text]
        if not cited_ids and working_case.proposal:
            cited_ids = list(working_case.proposal.evidence_ids)

        comparison = toolkit.last_comparison
        outcome = "no_change"
        rec_action: ActionType | None = None
        follow_target: str | None = None
        follow_hyp: str | None = None
        suggested_next: str | None = None
        missing_data: list[str] = []

        if comparison is not None:
            missing_data = list(comparison.missing_data)
            if comparison.verdict == ComparisonVerdict.SUPPORTS_CAUSAL_CHANGE:
                outcome = "maintained"
            elif comparison.verdict == ComparisonVerdict.CONTRADICTS_DEPLOYMENT:
                outcome = "withdrawn"
                rec_action = ActionType.CREATE_PREVENTIVE_TICKET
                follow_target = comparison.service
                follow_hyp = (
                    "Both cohorts show elevated error rates alongside increased load or shared resource saturation; "
                    "rollback is withdrawn in favor of checking the saturated shared resource."
                )
                suggested_next = (
                    f"Inspect and mitigate shared resource saturation on '{comparison.service}' "
                    f"instead of rolling back '{target_release_id or 'release'}'."
                )
            else:
                outcome = "inconclusive"
                suggested_next = (
                    "Provide missing cohort denominator or telemetry coverage before deciding rollback: "
                    + ("; ".join(missing_data) if missing_data else "coverage incomplete.")
                )
        elif turn.intent == ConversationIntent.CURRENT_STATUS:
            outcome = "status_update"

        candidate = FollowUpCandidate(
            intent=turn.intent,
            answer=final_text or (comparison.summary if comparison else "Follow-up inspection completed."),
            hypothesis_under_test=turn.user_hypothesis,
            cited_evidence_ids=cited_ids,
            used_existing_evidence_only=toolkit.tool_call_count == 0,
            comparison=comparison,
            comparison_outcome=outcome,
            affects_rollback=True,
            recommended_follow_up_action=rec_action,
            follow_up_target_service=follow_target,
            follow_up_hypothesis=follow_hyp,
            suggested_next_check=suggested_next,
            missing_data=missing_data,
            cutoff_at=target_cutoff,
        )
        return working_case, candidate
