"""Exercise the outbound bridge against a loopback HTTP server using the UI wire format."""

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

import pytest

from test_decisions import _run_investigator_to_proposal
from vigia.adapters.outbound.ui_bridge import BridgeClient, BridgeRelay, bridge_events
from vigia.domain.contracts import Alert
from vigia.entrypoints.cli import _OfflineDemoChatModel
from vigia.runtime import build_case_service


def test_demo_emits_deployment_and_first_error_markers(transfers_workspace):
    ws = transfers_workspace
    service, _, _ = build_case_service(
        fixtures_dir=ws.fixtures_dir, authorized_repos={"applications/pagos-core": ws.repo_dir},
        model=_OfflineDemoChatModel(base_sha=ws.base_sha, deployed_sha=ws.deployed_sha),
    )
    try:
        case = service.start_case(Alert(service="transferencias",
                                        observed_at=datetime.fromisoformat("2026-11-12T09:12:00-05:00")))
        timeline = [m for event in case.events for m in bridge_events(case, event, True)]
        steps = {m.get("herramienta"): m for m in timeline}
        change = steps["listar_cambios"]["resultado"]
        assert (change["id"], change["hora"]) == ("CHG-4821", "08:55:00")
        assert steps["buscar_logs"]["resultado"]["primera_ocurrencia"] == "09:01:04"
        assert steps["detectar_anomalia"]["resultado"]["minutos_para_cruce"] > 0
    finally:
        service.close()


@pytest.mark.parametrize("vote", ["aprobar", "rechazar"])
def test_bridge_delivers_retries_waits_and_closes(transfers_workspace, tmp_path, monkeypatch, vote):
    service, case, state = _run_investigator_to_proposal(transfers_workspace)
    case_id = case.case_id
    received = {}
    ids = []
    batches = []
    votes = ["pendiente", vote]
    monkeypatch.setenv("VIGIA_BRIDGE_EVENTS_PATH", "/ui/pasos")
    monkeypatch.setenv("VIGIA_BRIDGE_DECISION_PATH", "/ui/{caso}/voto")
    monkeypatch.setenv("VIGIA_BRIDGE_METRICS_PATH", "/ui/muestras")

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, payload):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_GET(self):
            assert self.headers["X-Vigia-Token"] == "demo-token"
            assert self.path == f"/ui/{case_id}/voto?espera=25"
            self.respond(200, {"decision": votes.pop(0)})

        def do_POST(self):
            assert self.headers["X-Vigia-Token"] == "demo-token"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/ui/muestras":
                batches.append(body)
                self.respond(200, {})
                return
            assert self.path == "/ui/pasos"
            ids.append(body["id"])
            received[body["id"]] = body
            self.respond(503 if len(ids) == 1 else 200, {})

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = BridgeClient(f"http://127.0.0.1:{server.server_port}", "demo-token")
        path = tmp_path / "delivery.sqlite"
        relay = BridgeRelay(service, client, path, simulated=True)
        try:
            with pytest.raises(HTTPError):
                relay.deliver()
            relay.deliver()
            assert ids[0] == ids[1]
            messages = list(received.values())
            assert any(m.get("herramienta") == "detectar_anomalia" and
                       m["resultado"]["minutos_para_cruce"] is not None for m in messages)
            proposal = next(m for m in messages if m["tipo"] == "propuesta")
            assert (proposal["riesgo"], proposal["objetivo"], proposal["accion"]) == (
                "alto", "CHG-4821", "revertir_cambio")
            assert proposal["evidencia"] and proposal["justificacion"]
            relay.poll()
            assert service.get_case(case_id).decision is None
            relay.poll()
            relay.deliver()
            assert not votes
            messages = list(received.values())
            assert messages[-1]["tipo"] == "mensaje"
            assert any(m.get("rol") == "Acción" and m["simulada"] for m in messages)
            assert ("CHG-4821" in state.rolled_back_releases) == (vote == "aprobar")
            if vote == "aprobar":
                assert messages[-2]["rol"] == "Verificación"
            previous = ids.copy()
            relay.db.close()
            relay = BridgeRelay(service, client, path, simulated=True)
            relay.deliver()
            relay.poll()
            assert ids == previous
            assert not batches
            samples = [{"latencia_ms": 182, "ok": True}, {"latencia_ms": 3000, "ok": False}]
            client.publish_metrics(samples)
            assert batches == [{"muestras": samples}]
        finally:
            relay.db.close()
            service.close()
            server.shutdown()
            thread.join()
