"""Outbound delivery of the case timeline and human decisions to the demo UI."""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from vigia.application.approval_service import ApprovalError
from vigia.application.case_service import CaseService
from vigia.domain.contracts import CaseEvent, CaseRecord, DecisionChoice, EventType, ProposalStatus

log = logging.getLogger(__name__)
TOOLS = {
    "query_metrics": "consultar_metricas", "detect_anomaly": "detectar_anomalia",
    "query_dependencies": "consultar_dependencias", "resolve_release": "resolver_despliegue",
    "list_changes": "listar_cambios", "compare_revisions": "comparar_revisiones",
    "read_snippet": "leer_codigo", "search_logs": "buscar_logs",
    "compare_cohort_error_rates": "comparar_tasas_por_cohorte",
    "search_similar_incidents": "buscar_incidentes_similares", "search_runbook": "buscar_runbook",
    "propose_action": "proponer_accion",
}
ROLES = {"Detection": "Detección", "Architecture": "Diagnóstico", "Changes": "Cambios",
         "Code": "Diagnóstico", "Diagnosis": "Diagnóstico", "Recommendation": "Recomendación"}
ACTIONS = {"rollback_change": "revertir_cambio", "scale_capacity": "escalar_capacidad",
           "notify_team": "notificar_equipo", "create_preventive_ticket": "crear_ticket_preventivo"}
RISKS = {"low": "bajo", "medium": "medio", "high": "alto"}


def bridge_time(value: datetime) -> str:
    raw = os.environ.get("VIGIA_BRIDGE_UTC_OFFSET", "-05:00")
    hours, minutes = raw.lstrip("+-").split(":")
    offset = timedelta(hours=int(hours), minutes=int(minutes))
    if raw.startswith("-"):
        offset = -offset
    return value.astimezone(timezone(offset)).strftime("%H:%M:%S")


def bridge_case_id(case_id: str, revision: int) -> str:
    """The existing bridge correlates votes by case, so each revision gets its own card."""
    return case_id if revision == 1 else f"{case_id}-R{revision}"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None  # Never forward the bridge credential to a redirect target.


class BridgeClient:
    def __init__(self, url: str, token: str):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or not token:
            raise ValueError("Configure la URL HTTP del puente y VIGIA_BRIDGE_TOKEN.")
        bridge_time(datetime.now(timezone.utc))
        self.url = url.rstrip("/")
        self.events_path = os.environ.get("VIGIA_BRIDGE_EVENTS_PATH", "/api/ingesta/eventos")
        self.decision_path = os.environ.get("VIGIA_BRIDGE_DECISION_PATH", "/api/casos/{caso}/decision")
        self.metrics_path = os.environ.get("VIGIA_BRIDGE_METRICS_PATH", "/api/ingesta/metricas")
        self.health_path = os.environ.get("VIGIA_BRIDGE_HEALTH_PATH", "/api/salud")
        for path in (self.events_path, self.decision_path, self.metrics_path, self.health_path):
            if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
                raise ValueError("Las rutas del puente deben comenzar con / y no incluir consulta.")
        if self.decision_path.count("{caso}") != 1:
            raise ValueError("VIGIA_BRIDGE_DECISION_PATH debe contener {caso}.")
        self.headers = {"X-Vigia-Token": token, "Content-Type": "application/json"}
        self.opener = build_opener(_NoRedirect())

    def _post(self, path: str, body: dict) -> None:
        req = Request(self.url + path, headers=self.headers,
                      data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        with self.opener.open(req, timeout=5):
            pass

    def publish(self, event: dict) -> None:
        self._post(self.events_path, event)

    def publish_metrics(self, samples: list[dict]) -> None:
        if not isinstance(samples, list) or not samples:
            raise ValueError("Se requiere un lote de muestras.")
        for item in samples:
            if not isinstance(item, dict) or type(item.get("ok")) is not bool:
                raise ValueError("Cada muestra requiere ok booleano.")
            latency = item.get("latencia_ms")
            if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
                raise ValueError("Cada muestra requiere latencia_ms finita y positiva o cero.")
        self._post(self.metrics_path, {"muestras": samples})

    def decision(self, case_id: str) -> dict:
        path = self.decision_path.replace("{caso}", quote(case_id, safe="")) + "?espera=25"
        with self.opener.open(Request(self.url + path, headers=self.headers), timeout=32) as res:
            value = json.load(res)
        return {"decision": value} if isinstance(value, str) else value

    def session(self) -> str | None:
        with self.opener.open(Request(self.url + self.health_path, headers=self.headers), timeout=5) as res:
            value = json.load(res)
        return value.get("instance_id") or ("empty" if value.get("eventos") == 0 else None)


def bridge_events(case: CaseRecord, event: CaseEvent, simulated: bool) -> list[dict]:
    revision = event.payload.get("revision_number", 1)
    if event.event_type == EventType.CONVERSATION and event.payload.get("status") == "pending":
        revision = event.payload.get("base_revision", 1) + 1
    if event.event_type in {EventType.DECISION, EventType.EXECUTION, EventType.VERIFICATION} and case.proposal:
        revision = case.proposal.revision_number
    base = {"id": f"{case.case_id}-{event.sequence_id}", "caso": case.case_id,
            "hora": bridge_time(event.timestamp)}
    base["caso"] = bridge_case_id(case.case_id, revision)
    payload = dict(event.payload)
    if payload.get("despliegues"):
        payload["despliegues"] = [
            {**r, "hora": bridge_time(datetime.fromisoformat(r["desplegado_en"]))}
            if r.get("desplegado_en") else dict(r) for r in payload["despliegues"]
        ]
    if payload.get("primera_ocurrencia_iso"):
        payload["primera_ocurrencia"] = bridge_time(datetime.fromisoformat(payload["primera_ocurrencia_iso"]))
    evidence = [{"texto": ev.observed_fact, "fuente": ev.source}
                for ev in case.evidence if ev.evidence_id in event.references]
    kind = event.event_type
    reviewing = (kind == EventType.CONVERSATION and payload.get("intent") == "challenge_hypothesis"
                 and payload.get("status") == "pending" and case.proposal is not None
                 and case.proposal.status == ProposalStatus.UNDER_REVIEW)
    if kind in {EventType.PROPOSAL, EventType.REVISION} or reviewing:
        if reviewing:
            return [{**base, "tipo": "mensaje",
                     "texto": "Vigía está comprobando la recomendación anterior.", "resultado": payload}]
        if kind != EventType.PROPOSAL:
            proposal = case.proposal
            if proposal is None:
                return [{**base, "tipo": "mensaje",
                         "texto": "Vigía retiró la propuesta anterior. Revisa la evidencia del nuevo diagnóstico.",
                         "resultado": payload}]
            if proposal.status not in {ProposalStatus.PENDING, ProposalStatus.RECOMMENDED}:
                return [{**base, "tipo": "mensaje",
                         "texto": "La revisión sigue pendiente de evidencia suficiente.", "resultado": payload}]
            payload = {**payload, "action": proposal.action.value, "risk_level": proposal.risk_level.value,
                       "proposal_id": proposal.proposal_id, "proposal_version": proposal.version,
                       "status": "under_review" if reviewing else proposal.status.value,
                       "justificacion": proposal.justification}
            evidence = [{"texto": ev.observed_fact, "fuente": ev.source}
                        for ev in case.evidence if ev.evidence_id in proposal.evidence_ids]
        action = ACTIONS[payload["action"]]
        proposal = case.proposal
        if payload.get("status") != "pending" or payload["risk_level"] == "low":
            return [{**base, "tipo": "paso", "rol": "Recomendación", "herramienta": "proponer_accion",
                     "resumen": payload.get("justificacion", event.summary),
                     "resultado": payload, "simulada": simulated}]
        justification = payload.get("justificacion", event.summary)
        if simulated:
            justification = "Ejecución configurada como simulada. " + justification
        return [{**base, "tipo": "propuesta", "rol": "Recomendación",
                 "accion": action, "riesgo": RISKS[payload["risk_level"]],
                 "objetivo": event.sanitized_args.get("target_release_id") or (
                     proposal.target_release_id or proposal.target_service if proposal else ""),
                 "justificacion": justification, "evidencia": evidence,
                 "proposal_id": payload["proposal_id"], "proposal_version": payload.get("proposal_version", 1),
                 "estado": payload.get("status", "pending"), "simulada": simulated}]
    if kind == EventType.STEP:
        tool = TOOLS.get(event.tool_name, event.tool_name or "investigar")
        summary = f"Consulta de {tool.replace('_', ' ')} completada."
        if payload.get("status") not in {None, "ok", "no_results"}:
            summary = f"No se pudo completar la consulta: {payload['status']}."
        elif event.tool_name == "query_metrics" and payload.get("actual") is not None:
            summary = f"La métrica registra {payload['actual']} {payload['unidad']}."
        elif event.tool_name == "detect_anomaly":
            margin = payload.get("minutos_para_cruce")
            summary = (f"Margen estimado de {margin} minutos hasta el umbral." if margin is not None
                       else "La señal no permite proyectar un tiempo hasta el impacto.")
        elif payload.get("despliegues"):
            payload.update(payload["despliegues"][0])
            summary = f"Se identificó el despliegue {payload['id']} de las {payload['hora']}."
        elif event.tool_name == "search_logs" and "total" in payload:
            summary = f"Se encontraron {payload['total']} registros que coinciden con la búsqueda."
        elif evidence:
            summary = f"Se obtuvieron {len(evidence)} evidencias con {tool.replace('_', ' ')}."
        result = payload["despliegues"] if event.tool_name == "list_changes" else {
            **payload, "evidencia": evidence, "detalle": event.summary}
        return [{**base, "tipo": "paso", "rol": ROLES.get(event.role, "Diagnóstico"),
                 "herramienta": tool, "args": event.sanitized_args, "resumen": summary,
                 "resultado": result}]
    if kind in {EventType.DECISION, EventType.EXECUTION, EventType.VERIFICATION}:
        verification = kind == EventType.VERIFICATION
        rejected = payload.get("decision") == "reject"
        status = payload.get("execution_status") or payload.get("status")
        if verification:
            summary = ("Recuperación verificada." if payload.get("recovered")
                       else "La recuperación sigue pendiente de confirmación.")
        elif rejected:
            summary = "La persona responsable rechazó la propuesta. El caso queda en seguimiento."
        else:
            summary = {"executed": "Se completó la acción aprobada.",
                       "failed": "Falló la ejecución de la acción aprobada."}.get(
                           status, "Se registró la operación. Pendiente de confirmación.")
        role = "Detección" if verification else ("Acción" if rejected or status == "executed" else "Diagnóstico")
        step = {**base, "tipo": "paso", "rol": role,
                "herramienta": "verificar_recuperacion" if verification else ACTIONS.get(
                    case.execution.action.value if case.execution else "", "resolver_accion"),
                "args": {}, "resultado": payload, "resumen": summary}
        if simulated:
            step["simulada"] = True
        result = [step]
        if payload.get("recovered") or rejected:
            result.append({**base, "id": base["id"] + "-cierre", "tipo": "mensaje", "texto": summary,
                           "recuperado": bool(payload.get("recovered")) if verification else False})
        return result
    if kind == EventType.MESSAGE:
        # An initial report must not close a case if the operator already clicked approve.
        # Its reasoning is available in the proposal and the actual tool steps.
        if any(e.event_type == EventType.PROPOSAL and e.sequence_id < event.sequence_id for e in case.events):
            return []
        return [{**base, "tipo": "mensaje", "texto": event.summary}]
    return []


class BridgeRelay:
    """Deliver committed case events in order. Poll votes independently of delivery."""

    def __init__(self, service: CaseService, client: BridgeClient, db_path: Path | str, *, simulated: bool):
        self.service = service
        self.client = client
        self.simulated = simulated
        self.stop_event = threading.Event()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS bridge_cursor "
                        "(case_id TEXT PRIMARY KEY, seq INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS bridge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.last_poll: str | None = None

    def cursor(self, case_id: str) -> int:
        with self.lock:
            row = self.db.execute("SELECT seq FROM bridge_cursor WHERE case_id=?", (case_id,)).fetchone()
        return row[0] if row else 0

    def deliver(self):
        session = self.client.session()
        replay = False
        if session or self.client.url:
            target = self.client.url + ("/" + session if session and session != "empty" else "")
            with self.lock, self.db:
                previous = self.db.execute("SELECT value FROM bridge_meta WHERE key='target'").fetchone()
                if (previous and previous[0] != target) or session == "empty":
                    replay = previous is not None
                    self.db.execute("DELETE FROM bridge_cursor")
                self.db.execute("INSERT OR REPLACE INTO bridge_meta VALUES ('target', ?)", (target,))
        for case in self.service.list_cases():
            cursor = self.cursor(case.case_id)
            for event in case.events:
                if self.stop_event.is_set():
                    return
                if event.sequence_id <= cursor:
                    continue
                if replay and event.event_type == EventType.DECISION and case.decision:
                    self.client.publish({
                        "id": f"{case.case_id}-{event.sequence_id}-decision",
                        "caso": bridge_case_id(case.case_id, case.proposal.revision_number if case.proposal else 1),
                        "tipo": "decision", "hora": event.timestamp.strftime("%H:%M:%S"),
                        "decision": "aprobar" if case.decision.decision == DecisionChoice.APPROVE else "rechazar",
                        "actor": case.decision.actor})
                for message in bridge_events(case, event, self.simulated):
                    self.client.publish(message)
                with self.lock, self.db:
                    self.db.execute("INSERT OR REPLACE INTO bridge_cursor VALUES (?,?)",
                                    (case.case_id, event.sequence_id))

    def poll(self):
        eligible = []
        for case in self.service.list_cases():
            p = case.proposal
            if not p or p.status != ProposalStatus.PENDING or case.mode.value != "assisted":
                continue
            if case.status.value != "awaiting_approval":
                continue
            delivered = self.cursor(case.case_id)
            if any(e.event_type == EventType.PROPOSAL and e.payload.get("proposal_id") == p.proposal_id
                   and 0 < e.sequence_id <= delivered for e in case.events) and any(
                       e.event_type == EventType.MESSAGE and 0 < e.sequence_id <= delivered for e in case.events):
                eligible.append(case)
        if not eligible:
            return
        case = next((c for c in sorted(eligible, key=lambda c: c.case_id)
                     if self.last_poll is None or c.case_id > self.last_poll),
                    min(eligible, key=lambda c: c.case_id))
        self.last_poll = case.case_id
        p = case.proposal
        external_case = bridge_case_id(case.case_id, p.revision_number)
        reply = self.client.decision(external_case)
        vote = reply.get("decision", reply.get("estado")) if isinstance(reply, dict) else None
        if vote in {"pendiente", "sin_propuesta"} or self.stop_event.is_set():
            return
        if vote not in {"aprobar", "rechazar"}:
            raise ValueError("El puente devolvió una decisión desconocida.")
        if reply.get("proposal_id", p.proposal_id) != p.proposal_id or reply.get("proposal_version", p.version) != p.version:
            return
        self.service.resolve_approval(case_id=case.case_id, proposal_id=p.proposal_id,
                                      proposal_version=p.version, actor=reply.get("actor") or "puente-ui",
                                      decision=DecisionChoice.APPROVE if vote == "aprobar" else DecisionChoice.REJECT,
                                      idempotency_key=f"puente/{case.case_id}/{p.proposal_id}/{p.version}")

    def _run(self, task):
        delay = 0.3
        while not self.stop_event.is_set():
            try:
                task()
                delay = 0.3
            except (OSError, ValueError, ApprovalError, RuntimeError) as exc:
                log.warning("Puente UI (%s): %s", task.__name__, exc)
                delay = min(delay * 2, 10)
            self.stop_event.wait(delay)

    def __enter__(self):
        for task in (self.deliver, self.poll):
            thread = threading.Thread(target=self._run, args=(task,), name=f"bridge-{task.__name__}", daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def __exit__(self, *exc):
        self.stop_event.set()
        for thread in self.threads:
            thread.join()
        self.db.close()
