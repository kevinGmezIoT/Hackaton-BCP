"""Inbound HTTP/SSE API, Grafana webhook normalizer, and Pilot Console server."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from vigia.adapters.outbound.case_store import (
    CaseVersionConflictError,
    ChatMessageConflictError,
    StaleRevisionError,
)
from vigia.application.approval_service import (
    InactiveReleaseError,
    ProposalAlreadyResolvedError,
    ProposalNotFoundError,
    ProposalUnderReviewError,
    ProposalWithdrawnError,
    ShadowModeOperationError,
    StaleProposalVersionError,
    UnauthorizedApproverError,
)
from vigia.application.case_service import CaseNotFoundError, CaseService, UnknownServiceError
from vigia.adapters.inbound.bridge_api import BridgeAgentApi
from vigia.domain.contracts import (
    Alert,
    AlertEnvelope,
    AlertState,
    CaseRecord,
    DecisionChoice,
    OperationalMode,
    ReviewVerdict,
    TurnStatus,
)


class ReviewRequest(BaseModel):
    case_id: str
    proposal_id: str
    verdict: ReviewVerdict
    comment: str = ""
    proposal_revision: int | None = None
    intervened_externally: bool = False
    confirmed_cause: str | None = None


class ApprovalRequest(BaseModel):
    case_id: str
    proposal_id: str
    decision: DecisionChoice
    comment: str = ""
    idempotency_key: str | None = None
    proposal_version: int = Field(ge=1)


class ChatMessageRequest(BaseModel):
    client_message_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    base_revision: int | None = None
    proposal_id: str | None = None


@dataclass
class ApiResponse:
    """Framework-agnostic HTTP response emitted by VigiaHttpApp."""

    status_code: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body)


def _normalize_timestamp(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse_alert_webhook(payload: dict[str, Any]) -> list[AlertEnvelope]:
    """Convert either normalized AlertEnvelope JSON or Grafana Alerting batch webhooks."""
    if "alert" in payload and "event_id" in payload:
        env = AlertEnvelope.model_validate(payload)
        obs = env.alert.observed_at
        if obs.tzinfo is None:
            env = env.model_copy(
                update={"alert": env.alert.model_copy(update={"observed_at": obs.replace(tzinfo=timezone.utc)})}
            )
        return [env]

    if "alerts" in payload and isinstance(payload["alerts"], list):
        source = str(payload.get("receiver") or payload.get("source") or "grafana-alerting")
        envelopes: list[AlertEnvelope] = []
        for idx, item in enumerate(payload["alerts"]):
            labels = item.get("labels", {})
            annotations = item.get("annotations", {})
            values = item.get("values", {})
            starts_at = _normalize_timestamp(item.get("startsAt"))
            observed_at = _normalize_timestamp(item.get("endsAt") if item.get("status") == "resolved" else item.get("startsAt"))
            fingerprint = str(item.get("fingerprint") or f"gf-{idx}")
            state_raw = str(item.get("status", payload.get("status", "firing"))).lower()
            state = AlertState.RESOLVED if state_raw == "resolved" else AlertState.FIRING

            episode_id = labels.get("episode_id") or f"{fingerprint}-{starts_at.strftime('%Y%m%d%H%M')}"
            event_id = str(item.get("event_id") or f"{fingerprint}-{state.value}-{observed_at.isoformat()}")
            service = str(labels.get("service") or annotations.get("service") or "")
            environment = str(labels.get("environment") or labels.get("env") or "production")
            metric = str(labels.get("metric") or annotations.get("metric") or "latency_p95_ms")

            current_val = values.get("B") or annotations.get("current_value")
            threshold_val = annotations.get("threshold_value")
            alert = Alert(
                service=service,
                environment=environment,
                metric=metric,
                observed_at=observed_at,
                cutoff_at=observed_at,
                symptom=str(annotations.get("summary") or annotations.get("description") or ""),
                current_value=float(current_val) if current_val is not None else None,
                threshold_value=float(threshold_val) if threshold_val is not None else None,
            )
            envelopes.append(
                AlertEnvelope(
                    source=source,
                    event_id=event_id,
                    episode_id=str(episode_id),
                    state=state,
                    alert=alert,
                )
            )
        return envelopes

    raise ValueError("Unsupported alert payload: expected AlertEnvelope or Grafana 'alerts' batch.")


def _case_to_console_dto(case: CaseRecord, service: CaseService) -> dict[str, Any]:
    """Format case state separating symptom service from probable root-cause dependency."""
    svc_cfg = service._deps.catalog.get_service(case.alert.service)
    metric_ref = next(
        (
            ev.reproducible_reference
            for ev in reversed(case.evidence)
            if ev.source == "metrics"
        ),
        None,
    )
    finding = case.proposal.finding if case.proposal else case.partial_findings
    return {
        "case_id": case.case_id,
        "episode_id": case.episode_id,
        "version": case.version,
        "mode": case.mode.value,
        "status": case.status.value,
        "service": case.alert.service,
        "environment": case.alert.environment,
        "service_config_version": case.service_config_version,
        "cutoff_at": case.alert.effective_cutoff.isoformat(),
        "symptom": {
            "service": case.alert.service,
            "metric": case.alert.metric,
            "unit": svc_cfg.telemetry.metric_unit if svc_cfg else "ms",
            "current_value": case.alert.current_value,
            "baseline_value": case.alert.baseline_value,
            "threshold_value": case.alert.threshold_value,
            "projected_minutes_to_threshold": case.alert.projected_minutes_to_threshold,
            "metric_reference": metric_ref,
        },
        "probable_cause": {
            "target_service": case.proposal.target_service if case.proposal else None,
            "target_release_id": case.proposal.target_release_id if case.proposal else None,
            "finding": finding.model_dump(mode="json") if finding else None,
        },
        "sources_consulted": sorted({ev.source for ev in case.evidence}),
        "evidence": [ev.model_dump(mode="json") for ev in case.evidence],
        "connector_gaps": list(case.connector_gaps),
        "revisions": [rev.model_dump(mode="json") for rev in case.revisions],
        "active_revision": case.revisions[-1].model_dump(mode="json") if case.revisions else None,
        "previous_conclusion": (
            case.revisions[0].model_dump(mode="json") if len(case.revisions) > 1 else None
        ),
        "current_conclusion": (
            case.revisions[-1].model_dump(mode="json") if case.revisions else None
        ),
        "proposal": case.proposal.model_dump(mode="json") if case.proposal else None,
        "proposal_history": [entry.model_dump(mode="json") for entry in case.proposal_history],
        "conversation_turns": [turn.model_dump(mode="json") for turn in case.conversation_turns],
        "suggested_next_check": case.suggested_next_check,
        "reviews": [r.model_dump(mode="json") for r in case.reviews],
        "decision": case.decision.model_dump(mode="json") if case.decision else None,
        "execution": case.execution.model_dump(mode="json") if case.execution else None,
        "external_operations": [op.model_dump(mode="json") for op in case.external_operations],
        "verification": case.verification.model_dump(mode="json") if case.verification else None,
        "final_summary": case.final_summary,
    }


def _case_summary_dto(case: CaseRecord, service: CaseService) -> dict[str, Any]:
    """Format lightweight case summary for polling and incident discovery."""
    svc_cfg = service._deps.catalog.get_service(case.alert.service)
    finding = case.proposal.finding if case.proposal else case.partial_findings
    return {
        "case_id": case.case_id,
        "episode_id": case.episode_id,
        "version": case.version,
        "mode": case.mode.value,
        "status": case.status.value,
        "service": case.alert.service,
        "environment": case.alert.environment,
        "cutoff_at": case.alert.effective_cutoff.isoformat(),
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
        "symptom": {
            "service": case.alert.service,
            "metric": case.alert.metric,
            "unit": svc_cfg.telemetry.metric_unit if svc_cfg else "ms",
            "current_value": case.alert.current_value,
            "baseline_value": case.alert.baseline_value,
            "threshold_value": case.alert.threshold_value,
            "projected_minutes_to_threshold": case.alert.projected_minutes_to_threshold,
            "summary": case.alert.symptom,
        },
        "probable_cause": {
            "target_service": case.proposal.target_service if case.proposal else None,
            "target_release_id": case.proposal.target_release_id if case.proposal else None,
            "hypothesis": finding.hypothesis if finding else None,
        },
        "proposal": (
            {
                "proposal_id": case.proposal.proposal_id,
                "version": case.proposal.version,
                "action": case.proposal.action.value,
                "risk_level": case.proposal.risk_level.value,
                "status": case.proposal.status.value,
            }
            if case.proposal is not None
            else None
        ),
        "verification": (
            {
                "recovered": case.verification.recovered,
                "case_status": case.verification.case_status.value,
                "metric_value": case.verification.metric_value,
            }
            if case.verification is not None
            else None
        ),
        "evidence_count": len(case.evidence),
        "event_count": len(case.events),
    }


def _render_console_html(mode: OperationalMode) -> str:
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Vigía · Consola Piloto ({mode.value})</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; background: #f3f5f8; color: #13213a; }}
    .card {{ background: #fff; border: 1px solid #d7dee7; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
    .row {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
    button {{ padding: 8px 14px; border-radius: 6px; border: 1px solid #0f6a88; cursor: pointer; background: #0f6a88; color: #fff; }}
    button.danger {{ background: #fff; color: #b3382e; border-color: #b3382e; }}
    pre {{ background: #eef2f6; padding: 10px; border-radius: 6px; overflow-x: auto; }}
  </style>
</head>
<body>
  <h1>Vigía · Consola Piloto</h1>
  <div class="card">
    <div class="row">
      <label>Caso: <input id="caseId" value="CASE-1042"></label>
      <button onclick="loadCase()">Cargar caso</button>
    </div>
  </div>
  <div class="card" id="summary">Sin caso cargado.</div>
  <div class="card" id="revisions"></div>
  <div class="card" id="actions"></div>
  <div class="card">
    <h3>Investigación conversacional</h3>
    <div id="chatStatus" style="color:#0f6a88;font-size:0.9em;margin-bottom:8px;"></div>
    <div id="chatHistory" style="max-height:260px;overflow-y:auto;margin-bottom:10px;"></div>
    <div class="row">
      <input id="chatInput" style="flex:1;padding:8px;" placeholder="Ej. ¿Por qué recomiendas el rollback? o ¿Y si fue el tráfico?">
      <button onclick="sendChat()">Preguntar</button>
    </div>
  </div>
  <div class="card">
    <h3>Fuentes y evidencias</h3>
    <div id="sources"></div>
  </div>
  <div class="card">
    <h3>Eventos SSE</h3>
    <ul id="events"></ul>
  </div>
  <script>
    let evtSource = null;
    let currentCase = null;
    const seenEventSeq = new Set();
    let msgCounter = 1;

    function el(tag, text, cls) {{
      const node = document.createElement(tag);
      if (text !== undefined && text !== null) node.textContent = String(text);
      if (cls) node.className = cls;
      return node;
    }}

    function lineWithLabel(label, value) {{
      const p = document.createElement("p");
      const b = el("b", label);
      p.appendChild(b);
      p.appendChild(document.createTextNode(" " + String(value ?? "")));
      return p;
    }}

    function renderSummary(data) {{
      const box = document.getElementById("summary");
      const frag = document.createDocumentFragment();
      frag.appendChild(el("h2", `${{data.case_id}} (${{data.status}} · modo ${{data.mode}})`));
      frag.appendChild(
        lineWithLabel(
          `Síntoma (${{data.symptom.service}}):`,
          `${{data.symptom.metric}} = ${{data.symptom.current_value}} ${{data.symptom.unit}} (umbral ${{data.symptom.threshold_value}})`
        )
      );
      const hyp = data.probable_cause && data.probable_cause.finding
        ? data.probable_cause.finding.hypothesis
        : "En investigación";
      frag.appendChild(
        lineWithLabel(
          `Causa probable (${{(data.probable_cause && data.probable_cause.target_service) || "pendiente"}}):`,
          hyp
        )
      );
      const counts = document.createElement("p");
      counts.appendChild(el("b", "Evidencias:"));
      counts.appendChild(document.createTextNode(` ${{data.evidence.length}} | `));
      counts.appendChild(el("b", "Lagunas:"));
      counts.appendChild(document.createTextNode(` ${{data.connector_gaps.length}}`));
      frag.appendChild(counts);
      box.replaceChildren(frag);
    }}

    function renderRevisions(data) {{
      const revDiv = document.getElementById("revisions");
      const frag = document.createDocumentFragment();
      if (data.previous_conclusion && data.current_conclusion) {{
        frag.appendChild(el("h3", "Comparación de revisiones"));
        frag.appendChild(
          lineWithLabel(
            `Revisión inicial (#${{data.previous_conclusion.revision_number}}):`,
            `${{data.previous_conclusion.action_outcome || "initial"}} · ${{data.previous_conclusion.summary || ""}}`
          )
        );
        frag.appendChild(
          lineWithLabel(
            `Revisión vigente (#${{data.current_conclusion.revision_number}}):`,
            `${{data.current_conclusion.action_outcome || ""}} · ${{data.current_conclusion.summary || ""}}`
          )
        );
      }} else if (data.active_revision) {{
        frag.appendChild(
          lineWithLabel(
            "Revisión diagnóstica vigente:",
            `#${{data.active_revision.revision_number}} (corte ${{data.active_revision.cutoff_at}})`
          )
        );
      }}
      revDiv.replaceChildren(frag);
    }}

    function renderActions(data) {{
      const actions = document.getElementById("actions");
      const frag = document.createDocumentFragment();
      if (data.proposal && data.mode === "shadow") {{
        const btn = el("button", "Evaluar recomendación (revisión sombra)");
        btn.addEventListener("click", () => submitReview(data.case_id, data.proposal.proposal_id));
        frag.appendChild(btn);
      }} else if (data.proposal && data.mode === "assisted" && data.proposal.status === "pending") {{
        const prop = data.proposal;
        const approveBtn = el("button", `Aprobar v${{prop.version}}`);
        approveBtn.addEventListener("click", () => decide(data.case_id, prop.proposal_id, prop.version, "approve"));
        const rejectBtn = el("button", "Rechazar", "danger");
        rejectBtn.addEventListener("click", () => decide(data.case_id, prop.proposal_id, prop.version, "reject"));
        frag.appendChild(approveBtn);
        frag.appendChild(document.createTextNode(" "));
        frag.appendChild(rejectBtn);
      }} else if (data.proposal && data.proposal.status === "under_review") {{
        frag.appendChild(el("i", "Propuesta en reevaluación conversacional; aprobación bloqueada hasta finalizar."));
      }} else {{
        frag.appendChild(el("i", `Sin acciones aprobables pendientes. ${{data.suggested_next_check || ""}}`));
      }}
      actions.replaceChildren(frag);
    }}

    function renderSources(data) {{
      const container = document.getElementById("sources");
      const frag = document.createDocumentFragment();
      for (const ev of data.evidence || []) {{
        const det = document.createElement("details");
        det.appendChild(el("summary", `[${{ev.evidence_id}}] (${{ev.source}}) ${{ev.observed_fact}}`));
        det.appendChild(el("pre", ev.reproducible_reference));
        frag.appendChild(det);
      }}
      container.replaceChildren(frag);
    }}

    async function loadCase() {{
      const id = document.getElementById("caseId").value.trim();
      const res = await fetch(`/api/caso?caso=${{encodeURIComponent(id)}}`, {{ credentials: "same-origin" }});
      const data = await res.json();
      if (!res.ok) {{
        document.getElementById("summary").textContent = JSON.stringify(data);
        return;
      }}
      currentCase = data;
      renderSummary(data);
      renderRevisions(data);
      renderActions(data);
      renderSources(data);
      await loadMessages(id);
      if (evtSource) evtSource.close();
      seenEventSeq.clear();
      document.getElementById("events").replaceChildren();
      evtSource = new EventSource(`/api/eventos?caso=${{encodeURIComponent(id)}}`, {{ withCredentials: true }});
      evtSource.onmessage = (e) => {{
        if (e.lastEventId && seenEventSeq.has(e.lastEventId)) return;
        if (e.lastEventId) seenEventSeq.add(e.lastEventId);
        document.getElementById("events").appendChild(el("li", e.data));
      }};
    }}

    async function loadMessages(id) {{
      const res = await fetch(`/api/casos/${{encodeURIComponent(id)}}/mensajes`, {{ credentials: "same-origin" }});
      if (!res.ok) return;
      const data = await res.json();
      const box = document.getElementById("chatHistory");
      const frag = document.createDocumentFragment();
      for (const t of data.turns || []) {{
        const row = document.createElement("div");
        row.style.marginBottom = "8px";
        row.style.borderLeft = "3px solid #0f6a88";
        row.style.paddingLeft = "8px";

        const header = document.createElement("div");
        header.appendChild(el("b", t.actor));
        header.appendChild(document.createTextNode(` (rev ${{t.base_revision}} · ${{t.status}}): ${{t.question}}`));
        row.appendChild(header);

        row.appendChild(el("div", t.answer || "Investigando..."));
        if (t.referenced_evidence_ids && t.referenced_evidence_ids.length) {{
          row.appendChild(el("small", `Citas: ${{t.referenced_evidence_ids.join(", ")}}`));
        }}
        frag.appendChild(row);
      }}
      box.replaceChildren(frag);
    }}

    async function sendChat() {{
      if (!currentCase) return;
      const text = document.getElementById("chatInput").value.trim();
      if (!text) return;
      document.getElementById("chatStatus").textContent = "Investigando...";
      const body = {{
        client_message_id: `ui-${{Date.now()}}-${{msgCounter++}}`,
        text,
        base_revision: currentCase.active_revision ? currentCase.active_revision.revision_number : 1,
        proposal_id: currentCase.proposal ? currentCase.proposal.proposal_id : null
      }};
      await fetch(`/api/casos/${{encodeURIComponent(currentCase.case_id)}}/mensajes`, {{
        method: "POST",
        credentials: "same-origin",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(body)
      }});
      document.getElementById("chatInput").value = "";
      document.getElementById("chatStatus").textContent = "";
      await loadCase();
    }}

    async function submitReview(caseId, propId) {{
      await fetch("/api/revision", {{
        method: "POST",
        credentials: "same-origin",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{case_id: caseId, proposal_id: propId, verdict: "useful", comment: "Validado en consola"}})
      }});
      loadCase();
    }}

    async function decide(caseId, propId, propVersion, decision) {{
      await fetch("/api/aprobacion", {{
        method: "POST",
        credentials: "same-origin",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{case_id: caseId, proposal_id: propId, proposal_version: propVersion, decision}})
      }});
      loadCase();
    }}
  </script>
</body>
</html>"""


class VigiaHttpApp:
    """HTTP/SSE application implementing the Vigía pilot REST and SSE endpoints."""

    def __init__(
        self,
        service: CaseService,
        *,
        webhook_token: str | None = None,
        require_user_auth: bool = False,
        auto_process_alerts: bool = False,
        bridge_token: str | None = None,
        session_secret: str | None = None,
        trusted_proxy_token: str | None = None,
        allow_demo_console_session: bool = False,
        on_work_enqueued: Callable[[str], None] | None = None,
    ) -> None:
        self._service = service
        self._webhook_token = webhook_token
        self._require_user_auth = require_user_auth
        self._auto_process_alerts = auto_process_alerts
        self._bridge_api = BridgeAgentApi(service, bridge_token or "")
        self._session_secret = (session_secret or secrets.token_urlsafe(32)).encode("utf-8")
        self._trusted_proxy_token = trusted_proxy_token
        self._allow_demo_console_session = allow_demo_console_session
        self._on_work_enqueued = on_work_enqueued

    def set_work_enqueued_callback(self, callback: Callable[[str], None] | None) -> None:
        """Register a callback notified whenever async case work is queued."""
        self._on_work_enqueued = callback

    def issue_session_token(
        self,
        user: str,
        groups: list[str],
        *,
        ttl_seconds: int = 3600,
    ) -> str:
        """Issue an HMAC-signed short-lived corporate session token."""
        payload = json.dumps(
            {"u": user, "g": list(groups), "exp": int(time.time()) + ttl_seconds},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        sig = hmac.new(self._session_secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
        return f"{payload_b64}.{sig_b64}"

    def _verify_session_token(self, token: str) -> tuple[str | None, list[str]]:
        if "." not in token:
            return None, []
        payload_b64, sig_b64 = token.split(".", 1)
        expected_sig = hmac.new(
            self._session_secret, payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).decode("ascii").rstrip("=")
        if not hmac.compare_digest(sig_b64, expected_b64):
            return None, []
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        try:
            claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, []
        if not isinstance(claims, dict):
            return None, []
        if int(claims.get("exp", 0)) < int(time.time()):
            return None, []
        user = str(claims.get("u") or "").strip() or None
        raw_groups = claims.get("g") or []
        groups = [str(g).strip() for g in raw_groups if str(g).strip()] if isinstance(raw_groups, list) else []
        return user, groups

    def _extract_cookie_token(self, hdrs: dict[str, str], name: str = "vigia_session") -> str | None:
        raw_cookie = hdrs.get("cookie", "")
        for part in raw_cookie.split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            if k.strip() == name:
                return v.strip()
        return None

    def _json_response(self, status_code: int, payload: Any) -> ApiResponse:
        return ApiResponse(
            status_code=status_code,
            body=json.dumps(payload, default=str),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    def _normalize_headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        return {k.lower(): v for k, v in (headers or {}).items()}

    def _authenticate_webhook(self, hdrs: dict[str, str]) -> bool:
        if not self._webhook_token:
            return True
        auth = hdrs.get("authorization", "")
        if auth == f"Bearer {self._webhook_token}":
            return True
        return hdrs.get("x-vigia-webhook-token") == self._webhook_token

    def _extract_session_identity(
        self, hdrs: dict[str, str]
    ) -> tuple[str | None, list[str]]:
        auth_hdr = hdrs.get("authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            user, groups = self._verify_session_token(auth_hdr[7:].strip())
            if user is not None:
                return user, groups
            return None, []

        cookie_tok = self._extract_cookie_token(hdrs)
        if cookie_tok:
            user, groups = self._verify_session_token(cookie_tok)
            if user is not None:
                return user, groups
            return None, []

        proxy_hdr = hdrs.get("x-vigia-proxy-auth", "")
        if self._trusted_proxy_token and hmac.compare_digest(
            proxy_hdr, self._trusted_proxy_token
        ):
            user = hdrs.get("x-vigia-user") or hdrs.get("x-authenticated-user")
            groups_raw = hdrs.get("x-vigia-groups") or hdrs.get("x-authenticated-groups") or ""
            groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
            return user, groups

        if not self._require_user_auth:
            user = hdrs.get("x-vigia-user") or hdrs.get("x-authenticated-user")
            groups_raw = hdrs.get("x-vigia-groups") or hdrs.get("x-authenticated-groups") or ""
            groups = [g.strip() for g in groups_raw.split(",") if g.strip()]
            return (user or "oncall-lead"), (groups or ["admin"])

        return None, []

    def _can_read_case(self, case: CaseRecord, groups: list[str]) -> bool:
        if not groups and not self._require_user_auth:
            return True
        lowered = {g.lower() for g in groups}
        if "admin" in lowered or "viewer" in lowered or "oncall-approvers" in lowered:
            return True
        svc_cfg = self._service._deps.catalog.get_service(case.alert.service)
        if svc_cfg is not None and svc_cfg.owner_team.lower() in lowered:
            return True
        if case.proposal is not None:
            target_cfg = self._service._deps.catalog.get_service(case.proposal.target_service)
            if target_cfg is not None and (
                target_cfg.owner_team.lower() in lowered
                or target_cfg.approver_group.lower() in lowered
            ):
                return True
        return False

    def handle_request(
        self,
        method: str,
        raw_path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | str = b"",
    ) -> ApiResponse:
        """Dispatch an HTTP request and return a framework-agnostic ApiResponse."""
        hdrs = self._normalize_headers(headers)
        bridge_path = urllib.parse.urlsplit(raw_path).path
        if method.upper() == "POST" and bridge_path in {
            self._bridge_api.chat_path, self._bridge_api.investigate_path
        }:
            status, data = self._bridge_api.handle(bridge_path, hdrs.get("x-vigia-token", ""), body)
            if status == 202:
                if self._auto_process_alerts:
                    self._service.process_pending_work(data["caso"])
                elif self._on_work_enqueued:
                    self._on_work_enqueued(data["caso"])
            return self._json_response(status, data)
        parsed = urllib.parse.urlsplit(raw_path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query)
        upper_method = method.upper()

        if upper_method == "GET" and path == "/":
            resp_headers = {"Content-Type": "text/html; charset=utf-8"}
            if self._allow_demo_console_session and not self._require_user_auth:
                tok = self.issue_session_token(
                    "oncall-lead",
                    ["transfers-squad", "core-payments-squad", "oncall-approvers"],
                )
                resp_headers["Set-Cookie"] = (
                    f"vigia_session={tok}; HttpOnly; SameSite=Strict; Path=/"
                )
            return ApiResponse(
                status_code=200,
                body=_render_console_html(self._service._default_mode),
                headers=resp_headers,
            )

        if upper_method == "POST" and path == "/api/alerta":
            if not self._authenticate_webhook(hdrs):
                return self._json_response(401, {"error": "Invalid or missing webhook credentials."})
            try:
                raw_text = body.decode("utf-8") if isinstance(body, bytes) else body
                payload = json.loads(raw_text or "{}")
                envelopes = parse_alert_webhook(payload)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                return self._json_response(400, {"error": f"Invalid alert payload: {exc}"})

            results = []
            try:
                for env in envelopes:
                    res = self._service.ingest_alert(env)
                    results.append(res.model_dump(mode="json"))
                    if self._auto_process_alerts:
                        self._service.process_pending_work(res.case_id)
                    elif self._on_work_enqueued is not None:
                        self._on_work_enqueued(res.case_id)
            except UnknownServiceError as exc:
                return self._json_response(422, {"error": str(exc)})

            if len(results) == 1:
                return self._json_response(202, results[0])
            return self._json_response(202, {"ingested": len(results), "results": results})

        if upper_method == "GET" and path == "/api/casos":
            user, groups = self._extract_session_identity(hdrs)
            if not user:
                return self._json_response(401, {"error": "Corporate session identity required."})
            service_filter = (qs.get("servicio") or qs.get("service") or [""])[0].strip() or None
            status_filter = (qs.get("estado") or qs.get("status") or [""])[0].strip().lower() or None
            raw_limit = (qs.get("limit") or ["50"])[0].strip()
            try:
                limit = max(1, min(200, int(raw_limit)))
            except ValueError:
                return self._json_response(400, {"error": "Invalid 'limit' parameter."})

            visible = [
                case
                for case in self._service.list_cases(service=service_filter)
                if self._can_read_case(case, groups)
                and (status_filter is None or case.status.value.lower() == status_filter)
            ]
            visible.sort(
                key=lambda c: (c.updated_at, c.created_at, c.case_id),
                reverse=True,
            )
            return self._json_response(
                200,
                {
                    "cases": [_case_summary_dto(c, self._service) for c in visible[:limit]],
                    "total": len(visible),
                },
            )

        if upper_method == "GET" and path == "/api/caso":
            case_id = (qs.get("caso") or qs.get("case_id") or [""])[0].strip()
            if not case_id:
                return self._json_response(400, {"error": "Missing required query parameter 'caso'."})
            user, groups = self._extract_session_identity(hdrs)
            if not user:
                return self._json_response(401, {"error": "Corporate session identity required."})
            try:
                case = self._service.get_case(case_id)
            except CaseNotFoundError as exc:
                return self._json_response(404, {"error": str(exc)})
            if not self._can_read_case(case, groups):
                return self._json_response(403, {"error": "Forbidden: insufficient case permissions."})
            return self._json_response(200, _case_to_console_dto(case, self._service))

        if upper_method == "GET" and path == "/api/eventos":
            case_id = (qs.get("caso") or qs.get("case_id") or [""])[0].strip()
            if not case_id:
                return self._json_response(400, {"error": "Missing required query parameter 'caso'."})
            user, groups = self._extract_session_identity(hdrs)
            if not user:
                return self._json_response(401, {"error": "Corporate session identity required."})
            try:
                case = self._service.get_case(case_id)
            except CaseNotFoundError as exc:
                return self._json_response(404, {"error": str(exc)})
            if not self._can_read_case(case, groups):
                return self._json_response(403, {"error": "Forbidden: insufficient case permissions."})

            last_id_raw = hdrs.get("last-event-id") or (qs.get("after_id") or ["0"])[0]
            try:
                after_id = int(last_id_raw)
            except ValueError:
                after_id = 0
            events = self._service.list_events(case_id, after_sequence_id=after_id)
            chunks: list[str] = []
            for ev in events:
                chunks.append(
                    f"id: {ev.sequence_id}\n"
                    f"event: {ev.event_type.value}\n"
                    f"data: {ev.model_dump_json()}\n\n"
                )
            return ApiResponse(
                status_code=200,
                body="".join(chunks),
                headers={
                    "Content-Type": "text/event-stream; charset=utf-8",
                    "Cache-Control": "no-cache",
                },
            )

        if upper_method == "POST" and path == "/api/revision":
            user, groups = self._extract_session_identity(hdrs)
            if not user:
                return self._json_response(401, {"error": "Corporate session identity required."})
            try:
                raw_text = body.decode("utf-8") if isinstance(body, bytes) else body
                req = ReviewRequest.model_validate_json(raw_text)
                case = self._service.get_case(req.case_id)
            except (ValidationError, ValueError) as exc:
                return self._json_response(400, {"error": str(exc)})
            except CaseNotFoundError as exc:
                return self._json_response(404, {"error": str(exc)})

            if not self._can_read_case(case, groups):
                return self._json_response(403, {"error": "Forbidden: insufficient review permissions."})
            try:
                updated = self._service.submit_review(
                    case_id=req.case_id,
                    proposal_id=req.proposal_id,
                    reviewer=user,
                    verdict=req.verdict,
                    comment=req.comment,
                    proposal_revision=req.proposal_revision,
                    intervened_externally=req.intervened_externally,
                    confirmed_cause=req.confirmed_cause,
                )
            except ProposalNotFoundError as exc:
                return self._json_response(404, {"error": str(exc)})
            except StaleProposalVersionError as exc:
                return self._json_response(409, {"error": str(exc)})
            return self._json_response(200, _case_to_console_dto(updated, self._service))

        if upper_method == "POST" and path == "/api/aprobacion":
            user, groups = self._extract_session_identity(hdrs)
            if not user:
                return self._json_response(401, {"error": "Corporate session identity required."})
            try:
                raw_text = body.decode("utf-8") if isinstance(body, bytes) else body
                req = ApprovalRequest.model_validate_json(raw_text)
                updated = self._service.resolve_approval(
                    case_id=req.case_id,
                    proposal_id=req.proposal_id,
                    actor=user,
                    decision=req.decision,
                    comment=req.comment,
                    idempotency_key=req.idempotency_key,
                    actor_groups=groups,
                    proposal_version=req.proposal_version,
                    require_proposal_version=True,
                )
            except (ValidationError, ValueError) as exc:
                return self._json_response(400, {"error": str(exc)})
            except (CaseNotFoundError, ProposalNotFoundError) as exc:
                return self._json_response(404, {"error": str(exc)})
            except (UnauthorizedApproverError, ShadowModeOperationError) as exc:
                return self._json_response(403, {"error": str(exc)})
            except (
                ProposalUnderReviewError,
                ProposalWithdrawnError,
                ProposalAlreadyResolvedError,
                InactiveReleaseError,
                StaleProposalVersionError,
                CaseVersionConflictError,
            ) as exc:
                return self._json_response(409, {"error": str(exc)})
            return self._json_response(200, _case_to_console_dto(updated, self._service))

        if upper_method == "GET" and path == "/api/piloto/metricas":
            metrics = self._service.get_pilot_metrics()
            return self._json_response(200, metrics.model_dump(mode="json"))

        if path.startswith("/api/casos/") and path.endswith("/mensajes"):
            parts = path.split("/")
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "casos" and parts[4] == "mensajes":
                case_id = urllib.parse.unquote(parts[3]).strip()
                user, groups = self._extract_session_identity(hdrs)
                if not user:
                    return self._json_response(401, {"error": "Corporate session identity required."})
                try:
                    case = self._service.get_case(case_id)
                except CaseNotFoundError as exc:
                    return self._json_response(404, {"error": str(exc)})
                if not self._can_read_case(case, groups):
                    return self._json_response(403, {"error": "Forbidden: insufficient case permissions."})

                if upper_method == "GET":
                    turns = self._service.list_messages(case_id)
                    return self._json_response(
                        200,
                        {
                            "case_id": case_id,
                            "turns": [t.model_dump(mode="json") for t in turns],
                        },
                    )
                if upper_method == "POST":
                    try:
                        raw_text = body.decode("utf-8") if isinstance(body, bytes) else body
                        req = ChatMessageRequest.model_validate_json(raw_text)
                        result = self._service.submit_chat_message(
                            case_id=case_id,
                            client_message_id=req.client_message_id,
                            actor=user,
                            text=req.text,
                            base_revision=req.base_revision,
                            proposal_id=req.proposal_id,
                        )
                    except (ValidationError, ValueError) as exc:
                        return self._json_response(400, {"error": str(exc)})
                    except ChatMessageConflictError as exc:
                        return self._json_response(409, {"error": str(exc)})
                    except StaleRevisionError as exc:
                        return self._json_response(
                            409,
                            {
                                "error": str(exc),
                                "active_revision": exc.active_revision,
                                "active_proposal_id": exc.active_proposal_id,
                            },
                        )

                    if self._auto_process_alerts and result.status == TurnStatus.PENDING:
                        self._service.process_pending_work(case_id)
                        turns = self._service.list_messages(case_id)
                        latest = next((t for t in turns if t.turn_id == result.turn_id), None)
                        if latest is not None:
                            refreshed = self._service.get_case(case_id)
                            result = result.model_copy(
                                update={
                                    "status": latest.status,
                                    "proposal_status": (
                                        refreshed.proposal.status.value
                                        if refreshed.proposal is not None
                                        else None
                                    ),
                                }
                            )
                    elif result.status == TurnStatus.PENDING and self._on_work_enqueued is not None:
                        self._on_work_enqueued(case_id)
                    return self._json_response(202, result.model_dump(mode="json"))

        if upper_method == "GET" and path == "/api/servicio/prueba":
            svc = (qs.get("servicio") or qs.get("service") or [""])[0].strip()
            env = (qs.get("env") or ["production"])[0].strip()
            if not svc:
                return self._json_response(400, {"error": "Missing 'servicio' parameter."})
            report = self._service.probe_service_onboarding(svc, environment=env)
            return self._json_response(200, report.model_dump(mode="json"))

        return self._json_response(404, {"error": f"Route not found: {upper_method} {path}"})


def create_http_server(
    app: VigiaHttpApp,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> ThreadingHTTPServer:
    """Create a stdlib ThreadingHTTPServer wrapping VigiaHttpApp."""

    class _Handler(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b""
            hdrs = {k: v for k, v in self.headers.items()}
            resp = app.handle_request(self.command, self.path, headers=hdrs, body=raw_body)
            encoded = resp.body.encode("utf-8")
            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), _Handler)


class CaseWorkWorker:
    """Background worker draining pending investigations and follow-ups from CaseService."""

    def __init__(
        self,
        service: CaseService,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self._service = service
        self._poll_interval = poll_interval_seconds
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def wake(self) -> None:
        """Signal the worker immediately when new work is enqueued."""
        self._wake_event.set()

    def start(self) -> None:
        """Start the background worker thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="vigia-case-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Request shutdown and wait briefly for the worker thread."""
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            processed_any = False
            try:
                while not self._stop_event.is_set():
                    processed = self._service.process_pending_work()
                    if processed is None:
                        break
                    processed_any = True
            except Exception as exc:
                print(f"[vigia-worker] Error processing pending work: {exc}", file=sys.stderr)
            if not processed_any:
                self._wake_event.wait(timeout=self._poll_interval)
                self._wake_event.clear()


def serve_http(
    app: VigiaHttpApp,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    ready_event: threading.Event | None = None,
    start_worker: bool = True,
) -> None:
    """Run the Vigía HTTP server until interrupted."""
    worker: CaseWorkWorker | None = None
    if start_worker and not app._auto_process_alerts:
        worker = CaseWorkWorker(app._service)
        app.set_work_enqueued_callback(lambda _case_id: worker.wake())
        worker.start()

    server = create_http_server(app, host=host, port=port)
    if ready_event is not None:
        ready_event.set()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if worker is not None:
            worker.stop()

