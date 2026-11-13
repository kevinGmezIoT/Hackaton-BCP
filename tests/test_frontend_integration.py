"""Real HTTP contract checks against FrontendHackatonCIO's bridge and Vigía."""

import importlib
import asyncio
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")
uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")

from test_decisions import _run_investigator_to_proposal
from vigia.adapters.inbound.http_api import VigiaHttpApp, create_http_server, CaseWorkWorker
from vigia.adapters.outbound.ui_bridge import BridgeClient, BridgeRelay


@pytest.fixture()
def frontend(monkeypatch):
    folder = Path(__file__).resolve().parents[1] / "FrontendHackatonCIO" / "api"
    if not folder.is_dir():
        pytest.skip("La prueba requiere el repositorio del frontend.")
    monkeypatch.syspath_prepend(str(folder))
    main = importlib.import_module("main")
    from estado import Estado
    monkeypatch.setattr(main, "estado", Estado(audit_path=None))
    monkeypatch.setattr(main, "TOKEN", "integration-token")
    monkeypatch.setattr(main, "AGENTE_URL", "")
    return main


@contextmanager
def serve_bridge(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=2))
        def run():
            with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
                runner.run(server.serve(sockets=[sock]))
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            assert server.started
            yield url
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()


@pytest.mark.parametrize("vote", ["aprobar", "rechazar"])
def test_real_bridge_roundtrip_and_restart(frontend, transfers_workspace, tmp_path, vote):
    service, case, simulation = _run_investigator_to_proposal(transfers_workspace)
    with serve_bridge(frontend.app) as url, httpx.Client(base_url=url, timeout=5) as ui:
        relay = BridgeRelay(service, BridgeClient(url, "integration-token"), tmp_path / "relay.db", simulated=True)
        try:
            assert ui.post("/api/ingesta/eventos", json={}).status_code == 401
            relay.deliver()
            events = ui.get("/api/caso").json()["eventos"]
            assert any(e["tipo"] == "propuesta" for e in events)
            p = next(e for e in events if e["tipo"] == "propuesta")
            assert ui.post("/api/aprobacion", json={"caso": case.case_id, "decision": vote}).status_code == 409
            body = {"caso": case.case_id, "decision": vote, "actor": "Operador UI",
                    "proposal_id": p["proposal_id"], "proposal_version": p["proposal_version"]}
            assert ui.post("/api/aprobacion", json=body).status_code == 200
            relay.poll()
            relay.deliver()
            assert ("CHG-4821" in simulation.rolled_back_releases) == (vote == "aprobar")
            assert service.get_case(case.case_id).decision.actor == "Operador UI"
            final = ui.get("/api/caso").json()["eventos"]
            assert sum(e["tipo"] == "decision" for e in final) == 1
            assert final[-1]["recuperado"] == (vote == "aprobar")
            ui.post("/api/reiniciar")
            relay.deliver()
            replay = ui.get("/api/caso").json()["eventos"]
            assert len(replay) == len(final)
            assert replay[-1]["recuperado"] == (vote == "aprobar")
        finally:
            relay.db.close()
            service.close()


def test_remote_chat_uses_case_context_and_preserves_token_boundary(frontend, transfers_workspace, monkeypatch):
    service, case, _ = _run_investigator_to_proposal(transfers_workspace)
    app = VigiaHttpApp(service, bridge_token="integration-token", require_user_auth=True)
    backend = create_http_server(app, host="127.0.0.1", port=0)
    worker = CaseWorkWorker(service)
    app.set_work_enqueued_callback(lambda _: worker.wake())
    worker.start()
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(frontend, "AGENTE_URL", f"http://127.0.0.1:{backend.server_port}")
    try:
        with serve_bridge(frontend.app) as url, httpx.Client(base_url=url, timeout=5) as ui:
            body = {"caso": case.case_id, "mensaje": "¿Por qué recomiendas revertir?",
                    "client_message_id": "ui-question"}
            direct = httpx.post(frontend.AGENTE_URL + "/chat", json=body, timeout=5)
            assert direct.status_code == 401
            reply = ui.post("/api/chat", json=body)
            assert reply.status_code == 200
            assert "CHG-4821" in reply.json()["respuesta"]
            assert reply.json()["fuentes"]
            ui.post("/api/chat", json=body)
            assert len(service.list_messages(case.case_id)) == 1
            assert service.get_case(case.case_id).decision is None
    finally:
        backend.shutdown()
        backend.server_close()
        thread.join()
        worker.stop()
        service.close()


@pytest.mark.parametrize("variant", ["causal_change", "shared_load"])
def test_reinvestigation_updates_remote_card(frontend, transfers_workspace, tmp_path, variant):
    from test_conversational import _write_two_release_manifest, _write_logs_for_variant, _open_initial_case
    _write_two_release_manifest(transfers_workspace)
    _write_logs_for_variant(transfers_workspace, variant)
    service, simulation, case, _ = _open_initial_case(transfers_workspace)
    with serve_bridge(frontend.app) as url, httpx.Client(base_url=url, timeout=5) as ui:
        relay = BridgeRelay(service, BridgeClient(url, "integration-token"), tmp_path / "relay.db", simulated=True)
        try:
            relay.deliver()
            original = case.proposal
            old_vote = {"caso": case.case_id, "decision": "aprobar",
                        "proposal_id": original.proposal_id, "proposal_version": original.version}
            service.submit_chat_message(case_id=case.case_id, client_message_id="challenge",
                                        actor="Operador UI", text="¿Y si fue el tráfico? Compara las tasas.")
            relay.deliver()
            assert ui.post("/api/aprobacion", json=old_vote).status_code == 409
            service.process_pending_work(case.case_id)
            relay.deliver()
            assert ui.post("/api/aprobacion", json=old_vote).status_code == 409
            if variant == "causal_change":
                events = ui.get("/api/caso").json()["eventos"]
                p = next(e for e in reversed(events) if e["tipo"] == "propuesta")
                assert p["proposal_version"] == 2 and p["estado"] == "pending"
                assert ui.post("/api/aprobacion", json={**old_vote, "proposal_version": 2}).status_code == 200
                relay.poll()
                assert "CHG-4821" in simulation.rolled_back_releases
            else:
                assert case.case_id not in frontend.estado.pendientes
                assert not simulation.rolled_back_releases
        finally:
            relay.db.close()
            service.close()
