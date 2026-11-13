"""Alternative A: single tool-calling investigator built with langchain.agents.create_agent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage

from application.prompts import INVESTIGATOR_SYSTEM_PROMPT, build_case_user_prompt
from application.tools import CaseDependencies, CaseToolKit
from domain.contracts import (
    CaseEvent,
    CaseRecord,
    CaseStatus,
    EventType,
    Finding,
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
        """Investigate the case up to its cutoff timestamp and update case state."""
        case.status = CaseStatus.INVESTIGATING
        toolkit = CaseToolKit(case, deps, max_tool_calls=self._max_tool_calls)
        lc_tools = toolkit.build_langchain_tools()

        agent = create_agent(
            model=self._model,
            tools=lc_tools,
            system_prompt=INVESTIGATOR_SYSTEM_PROMPT,
        )

        user_prompt = build_case_user_prompt(case.alert, service_config)
        result = agent.invoke(
            {"messages": [HumanMessage(content=user_prompt)]},
            config={"recursion_limit": self._recursion_limit},
        )

        messages = result.get("messages", [])
        final_text = _extract_message_text(messages[-1]) if messages else ""
        case.final_summary = final_text
        now = datetime.now(timezone.utc)
        case.updated_at = now

        if case.proposal is None:
            case.status = CaseStatus.INCONCLUSIVE
            case.partial_findings = Finding(
                hypothesis="Investigation ended without a verified action proposal.",
                supporting_evidence_ids=[ev.evidence_id for ev in case.evidence],
                gaps=list(case.connector_gaps),
                exposed_services=[case.alert.service],
            )
            case.suggested_next_check = (
                "Review collected evidence and connector gaps, or provide missing telemetry/architecture access."
            )
        elif case.status == CaseStatus.INVESTIGATING:
            case.status = CaseStatus.AWAITING_APPROVAL

        event = CaseEvent(
            event_type=EventType.MESSAGE,
            case_id=case.case_id,
            timestamp=now,
            role="Investigator",
            summary=final_text[:300] if final_text else "Investigation turn completed.",
            references=[ev.evidence_id for ev in case.evidence],
        )
        case.events.append(event)
        deps.audit.emit_event(event)
        return case

