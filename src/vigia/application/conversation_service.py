"""Conversational investigation orchestration layered atop the case dossier and read-only worker."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from vigia.adapters.outbound.case_store import StaleRevisionError
from vigia.application.approval_service import ensure_proposal_history
from vigia.application.cohort_comparison import (
    compute_comparison_windows,
    find_reusable_comparison,
    infer_error_definition,
)
from vigia.application.investigator import InvestigatorAgent
from vigia.application.tools import CaseDependencies
from vigia.domain.contracts import (
    CaseRecord,
    ChatSubmissionResult,
    ComparisonVerdict,
    ConversationIntent,
    ConversationTurn,
    OperationalMode,
    ProposalStatus,
    TurnStatus,
)
from vigia.domain.ports import CaseStorePort

_APPROVAL_VERBS = (
    "aprueba",
    "aprobar",
    "approve",
    "ejecuta el rollback",
    "ejecuta la reversión",
    "ejecuta la reversion",
    "rechaza",
    "rechazar",
    "reject",
)

_CHALLENGE_KEYWORDS = (
    "tráfico",
    "trafico",
    "traffic",
    "carga",
    "cohorte",
    "cohort",
    "tasa",
    "rate",
    "antes y después",
    "antes y despues",
    "versión anterior",
    "version anterior",
    "también aumentaron",
    "tambien aumentaron",
    "y si",
    "what if",
    "compara",
    "compare",
    "saturac",
    "denominador",
)

_EXPLAIN_KEYWORDS = (
    "por qué",
    "por que",
    "why",
    "explica",
    "explain",
    "evidencia",
    "código",
    "codigo",
    "archivo",
    "diff",
    "razón",
    "razon",
    "recomiendas",
)

_STATUS_KEYWORDS = (
    "y ahora",
    "ahora",
    "ahora mismo",
    "actualmente",
    "sigue",
    "todavía",
    "todavia",
    "right now",
    "currently",
    "still",
)


def _has_clear_target_in_prior_turn(case: CaseRecord, text: str) -> bool:
    """Check if the current text or prior turn makes service and metric unambiguous."""
    lowered = text.lower()
    metric_tokens = ("latencia", "latency", "p95", "error", "tasa", "rate", "pool", "cpu")
    if any(tok in lowered for tok in metric_tokens):
        return True
    if not case.conversation_turns:
        return False
    prev = case.conversation_turns[-1]
    prev_text = f"{prev.question} {prev.answer}".lower()
    has_metric = any(tok in prev_text for tok in metric_tokens)
    has_service = (
        case.alert.service.lower() in prev_text
        or (case.proposal is not None and case.proposal.target_service.lower() in prev_text)
        or prev.comparison is not None
    )
    return has_metric and has_service


def classify_turn(
    case: CaseRecord,
    text: str,
) -> tuple[ConversationIntent, bool, str | None]:
    """Classify an operator chat turn and decide whether it challenges an active proposal."""
    lowered = re.sub(r"\s+", " ", text.strip().lower())

    if any(verb in lowered for verb in _APPROVAL_VERBS):
        return ConversationIntent.APPROVAL_REQUEST, False, None

    if any(kw in lowered for kw in _CHALLENGE_KEYWORDS):
        return ConversationIntent.CHALLENGE_HYPOTHESIS, True, text.strip()

    if any(kw in lowered for kw in _STATUS_KEYWORDS):
        if _has_clear_target_in_prior_turn(case, lowered):
            return ConversationIntent.CURRENT_STATUS, False, None
        return ConversationIntent.AMBIGUOUS, False, None

    if any(kw in lowered for kw in _EXPLAIN_KEYWORDS):
        return ConversationIntent.EXPLAIN_PROPOSAL, False, None

    return ConversationIntent.AMBIGUOUS, False, None


def format_dossier_explanation(
    case: CaseRecord,
    *,
    base_revision: int | None,
    proposal_id: str | None,
) -> tuple[str, list[str]]:
    """Format an explanation strictly from persisted dossier evidence and code references."""
    ensure_proposal_history(case)
    current_rev = case.revisions[-1].revision_number if case.revisions else 1
    target_rev = base_revision or current_rev

    hist_entry = None
    if proposal_id:
        for entry in reversed(case.proposal_history):
            if entry.proposal_id == proposal_id and (
                base_revision is None or entry.revision_number == base_revision
            ):
                hist_entry = entry
                break
    elif case.proposal is not None and target_rev == case.proposal.revision_number:
        for entry in reversed(case.proposal_history):
            if entry.proposal_id == case.proposal.proposal_id:
                hist_entry = entry
                break
    elif case.proposal_history:
        for entry in reversed(case.proposal_history):
            if entry.revision_number == target_rev:
                hist_entry = entry
                break

    lines: list[str] = []
    if hist_entry is not None:
        is_historical = (
            hist_entry.revision_number != current_rev
            or hist_entry.status in {ProposalStatus.WITHDRAWN}
        )
        prefix = (
            f"Explicación histórica (revisión {hist_entry.revision_number}, estado={hist_entry.status.value}, revisión vigente={current_rev})"
            if is_historical
            else f"Recomendación vigente (revisión {hist_entry.revision_number}, versión {hist_entry.version}, estado={hist_entry.status.value})"
        )
        lines.append(
            f"{prefix}: {hist_entry.action.value} sobre `{hist_entry.target_service}` (`{hist_entry.target_release_id}`)."
        )
        if hist_entry.hypothesis:
            lines.append(f"Hipótesis: {hist_entry.hypothesis}")
        if hist_entry.justification:
            lines.append(f"Justificación: {hist_entry.justification}")
        ev_ids = list(hist_entry.evidence_ids)
    elif case.proposal is not None:
        p = case.proposal
        lines.append(
            f"Propuesta {p.proposal_id} (revisión {p.revision_number}, versión {p.version}, estado={p.status.value}): "
            f"{p.action.value} en `{p.target_service}` ({p.target_release_id})."
        )
        lines.append(f"Hipótesis: {p.finding.hypothesis}")
        ev_ids = list(p.evidence_ids)
    else:
        lines.append(
            f"Caso {case.case_id} (revisión {current_rev}, estado={case.status.value}) sin propuesta activa."
        )
        if case.partial_findings is not None:
            lines.append(f"Hallazgo actual: {case.partial_findings.hypothesis}")
        ev_ids = [ev.evidence_id for ev in case.evidence[:6]]

    ev_by_id = {ev.evidence_id: ev for ev in case.evidence}
    cited_ids: list[str] = []
    for eid in ev_ids:
        ev = ev_by_id.get(eid)
        if ev is not None:
            cited_ids.append(eid)
            lines.append(
                f"- [{ev.evidence_id}] ({ev.source}, {ev.reproducible_reference}): {ev.observed_fact}"
            )

    if case.connector_gaps:
        lines.append("Lagunas conocidas: " + "; ".join(case.connector_gaps))
    return "\n".join(lines), cited_ids


def format_approval_guidance(case: CaseRecord) -> tuple[str, list[str]]:
    """Respond to chat approval requests without authorizing or dispatching actions."""
    if case.mode == OperationalMode.SHADOW:
        if case.proposal is not None:
            return (
                f"Vigía está en modo sombra (`shadow`): el chat no aprueba ni ejecuta acciones. "
                f"Usa el panel de evaluación humana sobre la tarjeta {case.proposal.proposal_id} "
                f"(revisión {case.proposal.revision_number}).",
                list(case.proposal.evidence_ids),
            )
        return (
            "Vigía está en modo sombra (`shadow`) y actualmente no hay una propuesta activa para evaluar.",
            [],
        )

    if case.proposal is None:
        return (
            "No hay una acción pendiente de aprobación (la propuesta anterior fue retirada o resuelta). "
            "El texto del chat nunca autoriza ejecuciones.",
            [],
        )

    p = case.proposal
    if p.status == ProposalStatus.UNDER_REVIEW:
        return (
            f"La propuesta {p.proposal_id} (versión {p.version}) está actualmente en revisión (`under_review`). "
            "Espera a que finalice la comprobación antes de aprobar en la tarjeta.",
            list(p.evidence_ids),
        )
    if p.status == ProposalStatus.PENDING:
        return (
            f"El texto del chat no constituye autorización. "
            f"Tarjeta vigente: {p.proposal_id} (versión {p.version}, riesgo={p.risk_level.value}, estado={p.status.value}) "
            f"para `{p.action.value}` en `{p.target_service}` (`{p.target_release_id}`). "
            "Si tienes permisos del grupo aprobador, utiliza los controles de aprobación de la tarjeta.",
            list(p.evidence_ids),
        )
    return (
        f"La propuesta {p.proposal_id} ya tiene estado `{p.status.value}` y no requiere aprobación pendiente.",
        list(p.evidence_ids),
    )


def format_reused_comparison_answer(
    case: CaseRecord,
    *,
    service: str,
    ev_id: str,
) -> str:
    ev = next(e for e in case.evidence if e.evidence_id == ev_id)
    verdict = ev.comparison_result or ComparisonVerdict.INDETERMINATE.value
    return (
        f"El expediente ya contiene una comparación válida de cohortes para `{service}` hasta el corte "
        f"{ev.observed_at.isoformat()} [{ev.evidence_id}] (veredicto={verdict}): {ev.observed_fact}. "
        "No fue necesario repetir consultas externas."
    )


class ConversationService:
    """Handles chat turn submission, intent handling, and read-only follow-up execution."""

    def __init__(
        self,
        *,
        store: CaseStorePort,
        deps: CaseDependencies,
        investigator: InvestigatorAgent,
    ) -> None:
        self._store = store
        self._deps = deps
        self._investigator = investigator

    def submit_message(
        self,
        *,
        case_id: str,
        client_message_id: str,
        actor: str,
        text: str,
        base_revision: int | None,
        proposal_id: str | None,
        now: datetime,
    ) -> ChatSubmissionResult:
        """Persist a chat turn transactionally and enqueue follow-up investigation when needed."""
        case = self._store.get_case(case_id)
        if case is None:
            raise KeyError(f"Case '{case_id}' was not found.")

        intent, re_evaluate, hypothesis = classify_turn(case, text)
        current_rev = case.revisions[-1].revision_number if case.revisions else 1
        effective_rev = base_revision if base_revision is not None else current_rev

        immediate_answer: str | None = None
        immediate_outcome: str | None = None
        immediate_refs: list[str] = []
        target_cutoff = (
            case.revisions[-1].cutoff_at if case.revisions else case.alert.effective_cutoff
        )

        if intent == ConversationIntent.EXPLAIN_PROPOSAL:
            immediate_answer, immediate_refs = format_dossier_explanation(
                case,
                base_revision=base_revision,
                proposal_id=proposal_id,
            )
            immediate_outcome = "explained_from_dossier"
        elif intent == ConversationIntent.APPROVAL_REQUEST:
            immediate_answer, immediate_refs = format_approval_guidance(case)
            immediate_outcome = "show_approval_card"
        elif intent == ConversationIntent.AMBIGUOUS:
            immediate_answer = (
                f"¿Qué servicio y métrica específica deseas comprobar en el caso {case.case_id}? "
                f"Puedo explicar la evidencia actual, comprobar tasas por cohorte o revisar el estado actual de `{case.alert.service}`."
            )
            immediate_outcome = "ask_clarification"
        elif intent == ConversationIntent.CURRENT_STATUS:
            target_cutoff = now
        elif intent == ConversationIntent.CHALLENGE_HYPOTHESIS:
            target_svc = (
                case.proposal.target_service
                if case.proposal is not None
                else case.alert.service
            )
            active_res = self._deps.releases.resolve_active_releases(
                target_svc, case.alert.environment, target_cutoff
            )
            active_rel = active_res.releases[0] if active_res.releases else None
            rel_id = (
                active_rel.release_id
                if active_rel is not None
                else (case.proposal.target_release_id if case.proposal is not None else None)
            )
            deployed_at = (
                active_rel.deployed_at
                if active_rel is not None
                else (target_cutoff - timedelta(minutes=15))
            )
            pre_s, _, _, post_e, _ = compute_comparison_windows(
                deployed_at=deployed_at, cutoff_at=target_cutoff
            )
            expected_window = f"{pre_s.isoformat()}..{post_e.isoformat()}"
            expected_cohorts = {rel_id} if rel_id else set()
            requested_error_def = infer_error_definition(text)
            reusable = find_reusable_comparison(
                case,
                service=target_svc,
                cutoff_at=target_cutoff,
                release_id=rel_id,
                window=expected_window,
                cohorts=expected_cohorts,
                error_definition=requested_error_def,
            )
            if reusable is not None:
                if effective_rev != current_rev:
                    raise StaleRevisionError(
                        f"Follow-up references revision {effective_rev}, but active revision is {current_rev}.",
                        active_revision=current_rev,
                        active_proposal_id=case.proposal.proposal_id if case.proposal else None,
                    )
                re_evaluate = False
                immediate_answer = format_reused_comparison_answer(
                    case,
                    service=target_svc,
                    ev_id=reusable.evidence_id,
                )
                immediate_refs = [reusable.evidence_id]
                immediate_outcome = "reused_dossier_comparison"

        updated_case, turn, _work, duplicate = self._store.create_chat_turn_transactional(
            case_id=case_id,
            client_message_id=client_message_id,
            actor=actor,
            question=text,
            base_revision=base_revision,
            proposal_id=proposal_id,
            intent=intent.value,
            re_evaluate=re_evaluate,
            cutoff_at=target_cutoff,
            user_hypothesis=hypothesis,
            now=now,
            immediate_answer=immediate_answer,
            immediate_outcome=immediate_outcome,
            immediate_evidence_ids=immediate_refs,
        )
        return ChatSubmissionResult(
            turn_id=turn.turn_id,
            case_id=case_id,
            status=turn.status,
            base_revision=turn.base_revision,
            proposal_status=(
                updated_case.proposal.status.value
                if updated_case.proposal is not None
                else None
            ),
            duplicate=duplicate,
            status_code=202,
        )

    def execute_follow_up_turn(
        self,
        *,
        case: CaseRecord,
        turn_id: str,
        now: datetime,
    ) -> tuple[CaseRecord, ConversationTurn]:
        """Execute a queued follow-up turn in read-only mode and publish results transactionally."""
        turns = [t for t in case.conversation_turns if t.turn_id == turn_id]
        if not turns:
            turns = [t for t in self._store.list_turns(case.case_id) if t.turn_id == turn_id]
        if not turns:
            raise KeyError(f"Turn '{turn_id}' not found on case '{case.case_id}'.")
        turn = turns[0]
        target_cutoff = turn.cutoff_at or (
            case.revisions[-1].cutoff_at if case.revisions else case.alert.effective_cutoff
        )
        svc_cfg = self._deps.catalog.get_service(case.alert.service)
        working_case, candidate = self._investigator.run_follow_up(
            case,
            self._deps,
            svc_cfg,
            turn=turn,
            target_cutoff=target_cutoff,
        )
        return self._store.publish_follow_up_transactional(
            case_id=case.case_id,
            turn_id=turn_id,
            base_revision=turn.base_revision,
            candidate=candidate,
            updated_case_snapshot=working_case,
            now=now,
        )

