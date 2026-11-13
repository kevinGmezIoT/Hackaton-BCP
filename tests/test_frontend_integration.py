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
from vigia.adapters.inbound.bridge_api import BridgeAgentApi
from vigia.domain.contracts import ConnectorStatus


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


def test_manual_investigation_accepts_empty_metrics(transfers_workspace, monkeypatch, capsys):
    service, case, _ = _run_investigator_to_proposal(transfers_workspace)
    try:
        telemetry = service._deps.telemetry
        series = telemetry.query_metrics("transferencias", "production", "latency_p95_ms",
                                         30, case.alert.effective_cutoff)
        empty = series.model_copy(update={"status": ConnectorStatus.NO_RESULTS, "points": [],
                                          "note": "No metrics samples in requested window."})
        monkeypatch.setattr(telemetry, "query_metrics", lambda *args, **kwargs: empty)
        api = BridgeAgentApi(service, "integration-token")
        status, result = api.handle(api.investigate_path, "integration-token",
                                    '{"servicio":"transferencias"}')
        assert status == 202
        opened = service.get_case(result["caso"])
        assert opened.alert.current_value is None
        assert "Métricas no disponibles" in opened.alert.symptom
        assert service.get_case(case.case_id).proposal is not None
        output = capsys.readouterr().err
        assert "estado=no_results muestras=0" in output
        assert "Se continúa el diagnóstico" in output
        assert "integration-token" not in output
    finally:
        service.close()


@pytest.mark.parametrize("failure", ["unavailable", "timeout"])
def test_diagnosis_reads_code_and_logs_without_metrics(transfers_workspace, monkeypatch, failure):
    from datetime import datetime
    from conftest import ScriptedChatModel
    from langchain_core.messages import AIMessage
    from vigia.runtime import build_case_service
    from vigia.domain.contracts import CaseStatus

    ws = transfers_workspace
    cutoff = datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    model = ScriptedChatModel(responses=[
        AIMessage(content="", tool_calls=[{"id": "metric", "name": "query_metrics",
                                          "args": {"service": "transferencias"}}]),
        AIMessage(content="", tool_calls=[
            {"id": "release", "name": "list_changes", "args": {"service": "pagos-core"}},
            {"id": "code", "name": "read_snippet", "args": {"repository": "applications/pagos-core",
             "sha": ws.deployed_sha, "path": "src/pool.py", "start_line": 1, "end_line": 20}},
            {"id": "logs", "name": "search_logs", "args": {"service": "pagos-core",
             "pattern": "Connection pool exhausted"}}]),
        AIMessage(content="Diagnóstico parcial: revisar el pool y los errores tras CHG-4821. "
                          "La latencia y el margen requieren métricas.")])
    service, _, _ = build_case_service(
        fixtures_dir=ws.fixtures_dir, authorized_repos={"applications/pagos-core": ws.repo_dir},
        model=model, clock=lambda: cutoff)
    telemetry = service._deps.telemetry
    original = telemetry.query_metrics
    def missing(*args, **kwargs):
        if failure == "timeout":
            raise TimeoutError("Metrics timeout")
        return original(*args, **kwargs).model_copy(update={
            "status": ConnectorStatus.CONNECTOR_UNAVAILABLE, "points": [], "current_value": None})
    monkeypatch.setattr(telemetry, "query_metrics", missing)
    try:
        api = BridgeAgentApi(service, "test-token")
        status, result = api.handle(api.investigate_path, "test-token", '{"servicio":"transferencias"}')
        assert status == 202
        if failure == "timeout":
            # The bootstrap timed out. Subsequent tool reads return an unavailable result.
            monkeypatch.setattr(telemetry, "query_metrics", lambda *args, **kwargs:
                original(*args, **kwargs).model_copy(update={
                    "status": ConnectorStatus.CONNECTOR_UNAVAILABLE, "points": [], "current_value": None}))
        diagnosed = service.process_pending_work(result["caso"])
        assert diagnosed.status == CaseStatus.INCONCLUSIVE
        assert {"change_log", "git_show", "logs"} <= {ev.source for ev in diagnosed.evidence}
        assert diagnosed.connector_gaps
        assert diagnosed.alert.current_value is None and diagnosed.proposal is None
        assert "Diagnóstico parcial" in diagnosed.final_summary
    finally:
        service.close()


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


def test_remote_chat_uses_case_context_and_preserves_token_boundary(frontend, transfers_workspace, monkeypatch, capsys):
    from test_conversational import _write_two_release_manifest, _write_logs_for_variant, _open_initial_case
    _write_two_release_manifest(transfers_workspace)
    _write_logs_for_variant(transfers_workspace, "causal_change")
    service, _, case, _ = _open_initial_case(transfers_workspace)
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
            assert len(service.list_messages(case.case_id)) == 2
            assert ui.post("/api/chat", json={"mensaje": "¿Por qué recomiendas revertir?"}).status_code == 200
            assert len(service.list_messages(case.case_id)) == 3
            assert service.get_case(case.case_id).decision is None
            from test_chat_questions import ContextModel
            from langchain_core.messages import AIMessage
            original_model = service._investigator._model
            monkeypatch.setattr(service, "_clock", lambda: case.alert.effective_cutoff)
            service._investigator._model = ContextModel(responses=[
                AIMessage(content="", tool_calls=[{"id": "latest", "name": "query_metrics",
                    "args": {"service": "transferencias", "metric": "latency_p95_ms"}}]),
                AIMessage(content="La última medición de la ventana es 420 ms.")])
            natural = ui.post("/api/chat", json={"mensaje": "cuanto fue la ultima latencia registrada?"})
            assert natural.status_code == 200
            assert "420 ms" in natural.json()["respuesta"]
            assert natural.json()["herramientas"] == ["query_metrics"]
            assert service.get_case(case.case_id).proposal.version == 1
            service._investigator._model = original_model
            follow = ui.post("/api/chat", json={"mensaje": "¿Y si fue el tráfico? Compara las tasas."})
            assert follow.status_code == 200
            assert follow.json()["respuesta"] and follow.json()["fuentes"]
            assert "compare_cohort_error_rates" in follow.json()["herramientas"]
            assert service.get_case(case.case_id).proposal.version == 2
            # The bridge also notifies /decision. The notification is acknowledged, then
            # the durable vote is consumed by long-poll, preventing a second executor path.
            ack = httpx.post(frontend.AGENTE_URL + "/decision", json={"caso": case.case_id, "decision": "aprobar"},
                             headers={"X-Vigia-Token": "integration-token"}, timeout=5)
            assert ack.status_code == 200
            assert service.get_case(case.case_id).decision is None
            monkeypatch.setattr(service, "_clock", lambda: case.alert.effective_cutoff)
            manual = ui.post("/api/alerta", json={"servicio": "transferencias"})
            assert manual.status_code == 200
            assert manual.json()["caso"] != case.case_id
    finally:
        backend.shutdown()
        backend.server_close()
        thread.join()
        worker.stop()
        service.close()
    logs = capsys.readouterr().err
    assert "[vigia-http] Recibido POST /chat" in logs
    assert "[vigia-http] POST /chat -> 401" in logs
    assert "[vigia-http] POST /investigar -> 202 caso=" in logs
    assert "[vigia-worker] Iniciando" in logs
    assert "integration-token" not in logs


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
            # The unmodified bridge accepts a vote on the old card. Vigía refuses to execute it.
            assert ui.post("/api/aprobacion", json=old_vote).status_code == 200
            relay.poll()
            assert not simulation.rolled_back_releases
            service.process_pending_work(case.case_id)
            relay.deliver()
            assert ui.post("/api/aprobacion", json=old_vote).status_code == 409
            if variant == "causal_change":
                events = ui.get("/api/caso").json()["eventos"]
                p = next(e for e in reversed(events) if e["tipo"] == "propuesta")
                assert p["proposal_version"] == 2 and p["caso"] == case.case_id + "-R2"
                assert ui.post("/api/aprobacion", json={**old_vote, "caso": p["caso"]}).status_code == 200
                relay.poll()
                assert "CHG-4821" in simulation.rolled_back_releases
            else:
                assert case.case_id not in frontend.estado.pendientes
                assert not simulation.rolled_back_releases
        finally:
            relay.db.close()
            service.close()
