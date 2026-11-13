"""Regression for real questions that previously always triggered the clarification template."""

from datetime import datetime

from conftest import ScriptedChatModel
from langchain_core.messages import AIMessage

from test_decisions import _run_investigator_to_proposal
from vigia.application.conversation_service import classify_turn
from vigia.domain.contracts import ConversationIntent, ProposalStatus, TurnStatus


class ContextModel(ScriptedChatModel):
    prompts: list[str] = []
    exposed_tools: list[str] = []

    def bind_tools(self, tools, **kwargs):
        self.exposed_tools = [tool.name for tool in tools]
        return self

    def _generate(self, messages, **kwargs):
        self.prompts.append("\n".join(str(m.content) for m in messages if m.type == "human"))
        return super()._generate(messages, **kwargs)


def test_recorded_operator_questions_reach_model_and_read_tools(transfers_workspace):
    service, case, simulation = _run_investigator_to_proposal(transfers_workspace)
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    service._clock = lambda: cutoff
    model = ContextModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "logs", "name": "search_logs",
            "args": {"service": "pagos-core", "pattern": "Connection pool exhausted"}}]),
        AIMessage(content="Los logs registran errores de conexiones agotadas [EV-003]."),
        AIMessage(content="Puedo usar las fuentes disponibles del caso de Transferencias [EV-001]."),
        AIMessage(content="Transferencias es el servicio afectado en este caso [EV-001]."),
        AIMessage(content="", tool_calls=[{"id": "metrics", "name": "query_metrics",
            "args": {"service": "transferencias", "metric": "latency_p95_ms", "window_minutes": 30}}]),
        AIMessage(content="La última lectura de latencia p95 en esta ventana es 420 ms [EV-004]."),
    ])
    service._investigator._model = model
    questions = ["como van los logs del servicio?", "las que tengas", "transferencias",
                 "cuanto fue la ultima latencia registrada?"]
    proposal_before = case.proposal.model_dump()
    try:
        for index, question in enumerate(questions):
            result = service.submit_chat_message(
                case_id=case.case_id, client_message_id=f"real-{index}", actor="operador",
                text=question)
            assert result.status == TurnStatus.PENDING
            answered = service.process_pending_work(case.case_id)
            turn = answered.conversation_turns[-1]
            assert turn.intent == ConversationIntent.GENERAL_QUESTION
            assert turn.status == TurnStatus.COMPLETED
            assert "¿Qué servicio y métrica" not in turn.answer
            assert turn.referenced_evidence_ids
            assert answered.proposal.model_dump() == proposal_before
            assert len(answered.revisions) == 1
        assert model.cursor == 6
        assert "propose_action" not in model.exposed_tools
        assert "como van los logs del servicio?" in model.prompts[-1]
        assert "latency_p95_ms" in model.prompts[-1]
        assert "Existing proposal" in model.prompts[-1]
        assert "420 ms" in answered.conversation_turns[-1].answer
        assert not simulation.operations
        intent, _, _ = classify_turn(answered, "¿Y ahora?")
        assert intent == ConversationIntent.CURRENT_STATUS
    finally:
        service.close()


def test_information_question_does_not_unlock_proposal_under_review(transfers_workspace):
    service, case, _ = _run_investigator_to_proposal(transfers_workspace)
    case.proposal.status = ProposalStatus.UNDER_REVIEW
    service._store.save_case(case)
    service._investigator._model = ContextModel(
        responses=[AIMessage(content="La propuesta sigue en revisión. La evidencia está en [EV-001].")])
    try:
        service.submit_chat_message(case_id=case.case_id, client_message_id="info",
                                    actor="operador", text="qué encontraron?")
        updated = service.process_pending_work(case.case_id)
        assert updated.proposal.status == ProposalStatus.UNDER_REVIEW
        assert updated.proposal.version == case.proposal.version
        assert updated.revisions == case.revisions
    finally:
        service.close()
