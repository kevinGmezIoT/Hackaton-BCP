"""Authenticated callbacks consumed by the remote UI bridge."""

from __future__ import annotations

import hmac
import json
import os
import uuid

from pydantic import ValidationError

from vigia.application.case_service import CaseService, UnknownServiceError
from vigia.adapters.outbound.case_store import ChatMessageConflictError, StaleRevisionError
from vigia.domain.contracts import Alert, AlertEnvelope, ConnectorStatus


class BridgeAgentApi:
    def __init__(self, service: CaseService, token: str):
        self.service = service
        self.token = token
        self.chat_path = os.environ.get("VIGIA_BRIDGE_CHAT_PATH", "/chat")
        self.investigate_path = os.environ.get("VIGIA_BRIDGE_INVESTIGATE_PATH", "/investigar")
        self.actor = os.environ.get("VIGIA_BRIDGE_ACTOR", "operador-puente")

    def handle(self, path: str, token: str, body: bytes | str) -> tuple[int, dict]:
        if not self.token or not hmac.compare_digest(token, self.token):
            return 401, {"error": "Credencial del puente ausente o inválida."}
        try:
            request = json.loads(body)
            if not isinstance(request, dict):
                raise ValueError("El cuerpo debe ser un objeto JSON.")
            if path == self.investigate_path:
                return self.investigate(request)
            return self.chat(request)
        except (ValidationError, ValueError) as exc:
            return 400, {"error": str(exc)}
        except (ChatMessageConflictError, StaleRevisionError) as exc:
            return 409, {"error": str(exc)}
        except KeyError:
            return 404, {"error": "El caso o turno solicitado no existe."}

    def investigate(self, request: dict) -> tuple[int, dict]:
        name = request.get("servicio")
        config = self.service._deps.catalog.get_service(name) if isinstance(name, str) else None
        if config is None:
            raise UnknownServiceError("Servicio ausente del catálogo.")
        now = self.service._now()
        metric = config.telemetry.impact_metric
        reading = self.service._deps.telemetry.query_metrics(
            config.service, config.environment, metric, 30, now)
        if reading.status != ConnectorStatus.OK or not reading.points:
            return 503, {"error": "No hay telemetría disponible para iniciar esta investigación."}
        key = request.get("client_message_id") or str(uuid.uuid4())
        result = self.service.ingest_alert(AlertEnvelope(
            source="puente-ui", event_id=str(key), episode_id=f"manual-{key}",
            alert=Alert(service=config.service, environment=config.environment, metric=metric,
                        observed_at=now, cutoff_at=now, current_value=reading.current_value,
                        threshold_value=config.threshold_ms, symptom="Revisión solicitada desde la consola")))
        return 202, {"caso": result.case_id}

    def chat(self, request: dict) -> tuple[int, dict]:
        case_id = request.get("caso")
        message = request.get("mensaje")
        key = request.get("client_message_id")
        if not all(isinstance(v, str) and v.strip() for v in (case_id, message, key)):
            raise ValueError("Se requieren caso, mensaje y client_message_id.")
        result = self.service.submit_chat_message(
            case_id=case_id, client_message_id=key, actor=self.actor, text=message,
            base_revision=request.get("base_revision"), proposal_id=request.get("proposal_id"))
        turn = next(t for t in self.service.list_messages(case_id) if t.turn_id == result.turn_id)
        case = self.service.get_case(case_id)
        tools = [e.tool_name for e in case.events
                 if e.tool_name and set(e.references) & set(turn.referenced_evidence_ids)]
        return (202 if turn.status.value in {"pending", "in_progress"} else 200), {
            "caso": case_id, "turno": turn.turn_id, "estado": turn.status.value,
            "respuesta": turn.answer, "herramientas": list(dict.fromkeys(tools)),
            "fuentes": turn.referenced_evidence_ids}
