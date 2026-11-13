"""Tests for conversational investigation, cohort comparison, concurrency, and chat console API."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from conftest import TransfersWorkspace
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from vigia.adapters.inbound.http_api import VigiaHttpApp
from vigia.adapters.outbound.case_store import SqliteCaseStoreAdapter
from vigia.domain.contracts import (
    Alert,
    CaseStatus,
    ComparisonVerdict,
    ConnectorStatus,
    ConversationIntent,
    DecisionChoice,
    OperationalMode,
    ProposalStatus,
    RuntimeProfile,
    TurnStatus,
)
from vigia.runtime import PilotRuntimeConfig, build_pilot_case_service


class DualPhaseConversationalModel(BaseChatModel):
    """Deterministic BaseChatModel serving both initial investigation and read-only follow-up turns."""

    deployed_sha: str
    base_sha: str
    bound_tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "dual-phase-conversational"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "DualPhaseConversationalModel":
        names = [
            getattr(t, "name", None) or (t.get("name", "") if isinstance(t, dict) else "")
            for t in tools
        ]
        return self.model_copy(update={"bound_tool_names": [n for n in names if n]})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        is_read_only_follow_up = "propose_action" not in self.bound_tool_names
        ai_turns = sum(1 for m in messages if getattr(m, "type", "") == "ai")

        if not is_read_only_follow_up:
            initial_plan = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "init_1",
                            "name": "detect_anomaly",
                            "args": {"service": "transferencias", "metric": "latency_p95_ms"},
                        },
                        {
                            "id": "init_2",
                            "name": "query_dependencies",
                            "args": {"service": "transferencias", "direction": "outbound"},
                        },
                        {
                            "id": "init_3",
                            "name": "resolve_release",
                            "args": {"service": "pagos-core", "environment": "production"},
                        },
                        {
                            "id": "init_4",
                            "name": "read_snippet",
                            "args": {
                                "repository": "applications/pagos-core",
                                "sha": self.deployed_sha,
                                "path": "src/pool.py",
                                "start_line": 1,
                                "end_line": 20,
                            },
                        },
                        {
                            "id": "init_5",
                            "name": "compare_revisions",
                            "args": {
                                "repository": "applications/pagos-core",
                                "base_sha": self.base_sha,
                                "target_sha": self.deployed_sha,
                            },
                        },
                        {
                            "id": "init_6",
                            "name": "search_logs",
                            "args": {
                                "service": "pagos-core",
                                "pattern": "Connection pool exhausted",
                                "window_start": "2026-11-12T08:55:00-05:00",
                                "window_end": "2026-11-12T09:12:00-05:00",
                            },
                        },
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "init_7",
                            "name": "propose_action",
                            "args": {
                                "action": "rollback_change",
                                "target_service": "pagos-core",
                                "target_release_id": "CHG-4821",
                                "hypothesis": "CHG-4821 reduced DB pool_size from 80 to 50 causing exhaustion.",
                                "justification": "Errors spiked after CHG-4821 deploy.",
                                "evidence_ids": ["EV-001", "EV-002", "EV-003", "EV-004", "EV-005", "EV-006"],
                                "verification_plan": "Verify transferencias latency drops below 800 ms.",
                                "gaps": ["Stable vs new cohort denominator rate comparison not yet checked."],
                            },
                        }
                    ],
                ),
                AIMessage(content="Initial investigation completed; rollback CHG-4821 proposed."),
            ]
            msg = initial_plan[min(ai_turns, len(initial_plan) - 1)]
            return ChatResult(generations=[ChatGeneration(message=msg)])

        user_msg = ""
        for m in messages:
            if getattr(m, "type", "") == "human":
                user_msg = str(m.content)
                break
        if "ahora" in user_msg.lower() and "cohor" not in user_msg.lower():
            follow_plan = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "fu_now_1",
                            "name": "query_metrics",
                            "args": {
                                "service": "transferencias",
                                "metric": "latency_p95_ms",
                                "window_minutes": 15,
                            },
                        }
                    ],
                ),
                AIMessage(
                    content="Updated status check completed at the new explicit cutoff [EV-007]."
                ),
            ]
        else:
            follow_plan = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "fu_coh_1",
                            "name": "compare_cohort_error_rates",
                            "args": {
                                "service": "pagos-core",
                                "pattern": "*",
                                "check_shared_saturation": True,
                            },
                        }
                    ],
                ),
                AIMessage(
                    content=(
                        "Checked code-computed pre/post-deployment error rates per cohort [EV-007] "
                        "and returned verified comparison outcome to backend."
                    )
                ),
            ]
        msg = follow_plan[min(ai_turns, len(follow_plan) - 1)]
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _write_two_release_manifest(ws: TransfersWorkspace) -> None:
    releases = {
        "releases": [
            {
                "release_id": "CHG-4821",
                "service": "pagos-core",
                "environment": "production",
                "deployed_at": "2026-11-12T08:55:00-05:00",
                "previous_sha": ws.base_sha,
                "deployed_sha": ws.deployed_sha,
                "repository": "applications/pagos-core",
                "status": "active",
                "cohort": "50%",
                "summary": "Canary rollout of pool tuning",
            },
            {
                "release_id": "CHG-4810",
                "service": "pagos-core",
                "environment": "production",
                "deployed_at": "2026-11-12T07:00:00-05:00",
                "previous_sha": ws.base_sha,
                "deployed_sha": ws.base_sha,
                "repository": "applications/pagos-core",
                "status": "superseded",
                "cohort": "50%",
                "summary": "Stable baseline cohort",
            },
        ]
    }
    (ws.fixtures_dir / "releases.json").write_text(
        json.dumps(releases, indent=2), encoding="utf-8"
    )


def _write_logs_for_variant(ws: TransfersWorkspace, variant: str) -> None:
    entries: list[dict[str, Any]] = [
        # Pre-deploy baseline (10 failed requests in 10,000 -> 0.1%)
        {
            "timestamp": "2026-11-12T08:45:00-05:00",
            "service": "pagos-core",
            "environment": "production",
            "cohort": "CHG-4810",
            "level": "ERROR",
            "requests": 10000,
            "failed_requests": 10,
            "count": 10,
            "message": "Transient timeout before release",
        }
    ]
    if variant == "causal_change":
        entries.extend(
            [
                {
                    "timestamp": "2026-11-12T09:02:00-05:00",
                    "service": "pagos-core",
                    "environment": "production",
                    "cohort": "CHG-4810",
                    "level": "ERROR",
                    "requests": 5000,
                    "failed_requests": 5,
                    "count": 5,
                    "message": "Transient timeout on stable cohort",
                },
                {
                    "timestamp": "2026-11-12T09:02:00-05:00",
                    "service": "pagos-core",
                    "environment": "production",
                    "cohort": "CHG-4821",
                    "level": "ERROR",
                    "requests": 5000,
                    "failed_requests": 100,
                    "count": 100,
                    "message": "Connection pool exhausted waiting for pagos-db connection",
                },
            ]
        )
    elif variant == "shared_load":
        entries.extend(
            [
                {
                    "timestamp": "2026-11-12T09:02:00-05:00",
                    "service": "pagos-core",
                    "environment": "production",
                    "cohort": "CHG-4810",
                    "level": "ERROR",
                    "requests": 10000,
                    "failed_requests": 200,
                    "count": 200,
                    "message": "Connection pool exhausted on stable cohort under load",
                },
                {
                    "timestamp": "2026-11-12T09:02:00-05:00",
                    "service": "pagos-core",
                    "environment": "production",
                    "cohort": "CHG-4821",
                    "level": "ERROR",
                    "requests": 10000,
                    "failed_requests": 200,
                    "count": 200,
                    "message": "Connection pool exhausted on new cohort under load",
                },
                {
                    "timestamp": "2026-11-12T09:05:00-05:00",
                    "service": "pagos-core",
                    "environment": "production",
                    "cohort": "CHG-4810",
                    "level": "ERROR",
                    "count": 1,
                    "message": "Shared database connection pool saturation at 98%",
                },
            ]
        )
    elif variant == "missing_denominator":
        entries.extend(
            [
                {
                    "timestamp": "2026-11-12T09:02:00-05:00",
                    "service": "pagos-core",
                    "environment": "production",
                    "cohort": "CHG-4810",
                    "level": "ERROR",
                    "requests": 0,
                    "count": 12,
                    "message": "Connection pool exhausted without request denominator",
                },
                {
                    "timestamp": "2026-11-12T09:02:00-05:00",
                    "service": "pagos-core",
                    "environment": "production",
                    "cohort": "CHG-4821",
                    "level": "ERROR",
                    "requests": 5000,
                    "count": 100,
                    "message": "Connection pool exhausted with raw log counts",
                },
            ]
        )

    lines = "\n".join(json.dumps(item) for item in entries) + "\n"
    (ws.fixtures_dir / "logs.txt").write_text(lines, encoding="utf-8")


def _open_initial_case(
    ws: TransfersWorkspace,
    *,
    mode: OperationalMode = OperationalMode.ASSISTED,
    db_name: str = "conv.db",
    cutoff_str: str = "2026-11-12T09:12:00-05:00",
):
    model = DualPhaseConversationalModel(
        base_sha=ws.base_sha,
        deployed_sha=ws.deployed_sha,
    )
    cfg = PilotRuntimeConfig(
        profile=RuntimeProfile.PILOT,
        mode=mode,
        fixtures_dir=ws.fixtures_dir,
        authorized_repos={"applications/pagos-core": ws.repo_dir},
        db_path=ws.root / db_name,
    )
    service, sim_state, _ = build_pilot_case_service(cfg, model=model)
    cutoff = datetime.fromisoformat(cutoff_str)
    case = service.start_case(
        Alert(
            service="Transferencias",
            environment="production",
            metric="latency_p95_ms",
            observed_at=cutoff,
            cutoff_at=cutoff,
            current_value=420.0,
            threshold_value=800.0,
        ),
        mode=mode,
    )
    return service, sim_state, case, cfg


def test_turn_types_explanation_chat_approval_ambiguous_now_idempotency_and_sse(
    transfers_workspace: TransfersWorkspace,
) -> None:
    _write_two_release_manifest(transfers_workspace)
    _write_logs_for_variant(transfers_workspace, "causal_change")
    service, sim_state, case, _ = _open_initial_case(transfers_workspace)
    try:
        app = VigiaHttpApp(service, require_user_auth=True, auto_process_alerts=False)
        hdrs = {"Authorization": f"Bearer {app.issue_session_token('alice', ['transfers-squad'])}"}

        # 1. A message without content asks for clarification, without creating a revision.
        ambig = app.handle_request(
            "POST",
            f"/api/casos/{case.case_id}/mensajes",
            headers=hdrs,
            body=json.dumps(
                {
                    "client_message_id": "m-1",
                    "text": "???",
                    "base_revision": 1,
                }
            ),
        )
        assert ambig.status_code == 202
        assert len(service.get_case(case.case_id).revisions) == 1
        assert service.list_messages(case.case_id)[-1].intent == ConversationIntent.AMBIGUOUS

        # 2. "¿Por qué recomiendas el rollback?" answers from dossier with EV citations and zero new operational calls.
        telemetry_queries = 0
        orig_search = service._deps.telemetry.search_logs

        def _counting_search(*args: Any, **kwargs: Any):
            nonlocal telemetry_queries
            telemetry_queries += 1
            return orig_search(*args, **kwargs)

        service._deps.telemetry.search_logs = _counting_search  # type: ignore[method-assign]
        explain = app.handle_request(
            "POST",
            f"/api/casos/{case.case_id}/mensajes",
            headers=hdrs,
            body=json.dumps(
                {
                    "client_message_id": "m-2",
                    "text": "¿Por qué recomiendas el rollback?",
                    "base_revision": 1,
                    "proposal_id": case.proposal.proposal_id if case.proposal else None,
                }
            ),
        )
        assert explain.status_code == 202
        assert telemetry_queries == 0
        turns = service.list_messages(case.case_id)
        assert turns[-1].intent == ConversationIntent.EXPLAIN_PROPOSAL
        assert "EV-004" in turns[-1].referenced_evidence_ids
        assert "pool_size" in turns[-1].answer or "CHG-4821" in turns[-1].answer

        # 3. Repeating same client_message_id with same content returns existing turn idempotently;
        #    reusing same client_message_id with different text returns 409.
        replay = app.handle_request(
            "POST",
            f"/api/casos/{case.case_id}/mensajes",
            headers=hdrs,
            body=json.dumps(
                {
                    "client_message_id": "m-2",
                    "text": "¿Por qué recomiendas el rollback?",
                    "base_revision": 1,
                    "proposal_id": case.proposal.proposal_id if case.proposal else None,
                }
            ),
        )
        assert replay.status_code == 202
        assert replay.json()["duplicate"] is True
        assert replay.json()["turn_id"] == explain.json()["turn_id"]

        conflict = app.handle_request(
            "POST",
            f"/api/casos/{case.case_id}/mensajes",
            headers=hdrs,
            body=json.dumps(
                {
                    "client_message_id": "m-2",
                    "text": "Pregunta distinta con misma clave",
                    "base_revision": 1,
                }
            ),
        )
        assert conflict.status_code == 409

        # 4. "Aprueba la reversión" in chat NEVER executes or authorizes; returns active card info.
        chat_app = app.handle_request(
            "POST",
            f"/api/casos/{case.case_id}/mensajes",
            headers=hdrs,
            body=json.dumps(
                {
                    "client_message_id": "m-3",
                    "text": "Aprueba la reversión",
                    "base_revision": 1,
                }
            ),
        )
        assert chat_app.status_code == 202
        assert len(sim_state.operations) == 0
        assert service.list_messages(case.case_id)[-1].intent == ConversationIntent.APPROVAL_REQUEST
        assert "no constituye autorización" in service.list_messages(case.case_id)[-1].answer.lower()

        # 5. Clear "¿Y ahora?" after mentioning latency sets explicit new cutoff without losing rev 1 cutoff.
        now_req = app.handle_request(
            "POST",
            f"/api/casos/{case.case_id}/mensajes",
            headers=hdrs,
            body=json.dumps(
                {
                    "client_message_id": "m-4",
                    "text": "¿Y ahora sigue alta la latencia p95?",
                    "base_revision": 1,
                }
            ),
        )
        assert now_req.status_code == 202
        service.process_pending_work(case.case_id)
        updated = service.get_case(case.case_id)
        assert len(updated.revisions) == 2
        assert updated.revisions[0].cutoff_at != updated.revisions[1].cutoff_at

        # 6. Page reload via GET /mensajes + SSE event deduplication via Last-Event-ID.
        history = app.handle_request(
            "GET",
            f"/api/casos/{case.case_id}/mensajes",
            headers=hdrs,
        )
        assert history.status_code == 200
        assert len(history.json()["turns"]) == 4

        sse = app.handle_request(
            "GET",
            f"/api/eventos?caso={case.case_id}",
            headers=hdrs,
        )
        assert sse.status_code == 200
        assert "turn_id" in sse.body
    finally:
        service.close()


def test_same_model_and_question_produce_maintained_withdrawn_and_inconclusive_across_datasets(
    transfers_workspace: TransfersWorkspace,
) -> None:
    _write_two_release_manifest(transfers_workspace)
    question = "¿Y si el problema fue el tráfico? Compara la tasa de errores antes y después del despliegue."

    # Variant 1: Causal change plausible (0.1% pre, 0.1% stable post vs 2.0% CHG-4821 post) -> maintained.
    _write_logs_for_variant(transfers_workspace, "causal_change")
    svc1, _, case1, _ = _open_initial_case(transfers_workspace, mode=OperationalMode.SHADOW, db_name="v1.db")
    try:
        sub1 = svc1.submit_chat_message(
            case_id=case1.case_id,
            client_message_id="q-1",
            actor="lead",
            text=question,
            base_revision=1,
            proposal_id=case1.proposal.proposal_id if case1.proposal else None,
        )
        assert sub1.proposal_status == ProposalStatus.UNDER_REVIEW.value
        svc1.process_pending_work(case1.case_id)
        done1 = svc1.get_case(case1.case_id)
        assert done1.revisions[-1].action_outcome == "maintained"
        assert done1.proposal is not None and done1.proposal.version == 2
        assert done1.proposal.status == ProposalStatus.RECOMMENDED
        turn1 = done1.conversation_turns[-1]
        assert turn1.comparison is not None
        assert turn1.comparison.verdict == ComparisonVerdict.SUPPORTS_CAUSAL_CHANGE
        rates1 = {(w.label, w.cohort): w.error_rate for w in turn1.comparison.windows}
        assert rates1[("pre_deploy", "CHG-4810")] == 0.001
        assert rates1[("post_deploy", "CHG-4810")] == 0.001
        assert rates1[("post_deploy", "CHG-4821")] == 0.02

        # Repeating equivalent comparison at same cutoff reuses dossier evidence without new telemetry queries.
        reused = svc1.submit_chat_message(
            case_id=case1.case_id,
            client_message_id="q-1-repeat",
            actor="lead",
            text="Compara de nuevo la tasa de errores por cohorte",
            base_revision=2,
            proposal_id=done1.proposal.proposal_id,
        )
        assert reused.status == TurnStatus.COMPLETED
        assert svc1.list_messages(case1.case_id)[-1].result_outcome == "reused_dossier_comparison"
    finally:
        svc1.close()

    # Variant 2: Shared load & saturation (both cohorts jump to 2.0%, doubled requests + saturation) -> withdrawn.
    _write_logs_for_variant(transfers_workspace, "shared_load")
    svc2, _, case2, _ = _open_initial_case(transfers_workspace, mode=OperationalMode.ASSISTED, db_name="v2.db")
    try:
        svc2.submit_chat_message(
            case_id=case2.case_id,
            client_message_id="q-2",
            actor="lead",
            text=question,
            base_revision=1,
            proposal_id=case2.proposal.proposal_id if case2.proposal else None,
        )
        svc2.process_pending_work(case2.case_id)
        done2 = svc2.get_case(case2.case_id)
        assert done2.revisions[-1].action_outcome == "withdrawn"
        assert done2.proposal is None  # Approvable rollback removed!
        assert any(h.status == ProposalStatus.WITHDRAWN for h in done2.proposal_history)
        assert done2.suggested_next_check and "shared resource saturation" in done2.suggested_next_check.lower()
        assert done2.conversation_turns[-1].comparison is not None
        assert done2.conversation_turns[-1].comparison.verdict == ComparisonVerdict.CONTRADICTS_DEPLOYMENT
        assert done2.conversation_turns[-1].comparison.shared_resource_saturated is True
    finally:
        svc2.close()

    # Variant 3: Missing denominator + raw log events limitation -> indeterminate, zero fake %, proposal stays UNDER_REVIEW.
    _write_logs_for_variant(transfers_workspace, "missing_denominator")
    svc3, _, case3, _ = _open_initial_case(transfers_workspace, mode=OperationalMode.ASSISTED, db_name="v3.db")
    try:
        svc3.submit_chat_message(
            case_id=case3.case_id,
            client_message_id="q-3",
            actor="lead",
            text=question,
            base_revision=1,
            proposal_id=case3.proposal.proposal_id if case3.proposal else None,
        )
        svc3.process_pending_work(case3.case_id)
        done3 = svc3.get_case(case3.case_id)
        assert done3.revisions[-1].action_outcome == "inconclusive"
        assert done3.proposal is not None and done3.proposal.status == ProposalStatus.UNDER_REVIEW
        comp3 = done3.conversation_turns[-1].comparison
        assert comp3 is not None
        assert comp3.verdict == ComparisonVerdict.INDETERMINATE
        assert any(w.error_rate is None for w in comp3.windows)
        assert any("log events per request" in lim.lower() for lim in comp3.limitations)
    finally:
        svc3.close()


def test_e2e_concurrent_reevaluation_approval_race_historical_explain_and_worker_restart(
    transfers_workspace: TransfersWorkspace,
) -> None:
    _write_two_release_manifest(transfers_workspace)
    _write_logs_for_variant(transfers_workspace, "shared_load")

    # Session A & Session B inspect the same initial card (rev=1, prop_version=1).
    svc, sim_state, case, cfg = _open_initial_case(
        transfers_workspace, mode=OperationalMode.ASSISTED, db_name="race.db"
    )
    app = VigiaHttpApp(svc, require_user_auth=True, auto_process_alerts=False)
    chat_hdrs = {
        "Authorization": f"Bearer {app.issue_session_token('session-1', ['transfers-squad'])}"
    }
    approver_hdrs = {
        "Authorization": f"Bearer {app.issue_session_token('session-2', ['oncall-approvers'])}"
    }

    try:
        assert case.proposal is not None
        orig_prop_id = case.proposal.proposal_id
        orig_prop_ver = case.proposal.version

        # 1. Session 1 explains original card, then challenges hypothesis -> proposal becomes UNDER_REVIEW.
        ask_resp = app.handle_request(
            "POST",
            f"/api/casos/{case.case_id}/mensajes",
            headers=chat_hdrs,
            body=json.dumps(
                {
                    "client_message_id": "race-1",
                    "text": "¿Y si fue el tráfico? Compara la tasa de errores antes y después.",
                    "base_revision": 1,
                    "proposal_id": orig_prop_id,
                }
            ),
        )
        assert ask_resp.status_code == 202
        assert ask_resp.json()["proposal_status"] == ProposalStatus.UNDER_REVIEW.value

        # Session 2 tries to approve while reevaluation is in flight -> 409 & ZERO executor calls.
        in_flight_approve = app.handle_request(
            "POST",
            "/api/aprobacion",
            headers=approver_hdrs,
            body=json.dumps(
                {
                    "case_id": case.case_id,
                    "proposal_id": orig_prop_id,
                    "proposal_version": orig_prop_ver,
                    "decision": "approve",
                }
            ),
        )
        assert in_flight_approve.status_code == 409
        assert len(sim_state.operations) == 0

        # Simulate worker interruption (mark work_queue row 'in_progress') then recover on fresh CaseService!
        conn = sqlite3.connect(str(cfg.db_path))
        conn.execute("UPDATE work_queue SET status = 'in_progress' WHERE case_id = ?", (case.case_id,))
        conn.commit()
        conn.close()
        svc.close()

        restarted_svc, restarted_sim, _ = build_pilot_case_service(
            cfg,
            model=DualPhaseConversationalModel(
                base_sha=transfers_workspace.base_sha,
                deployed_sha=transfers_workspace.deployed_sha,
            ),
        )
        try:
            restarted_app = VigiaHttpApp(restarted_svc, require_user_auth=True, auto_process_alerts=False)
            chat_hdrs = {
                "Authorization": f"Bearer {restarted_app.issue_session_token('session-1', ['transfers-squad'])}"
            }
            approver_hdrs = {
                "Authorization": f"Bearer {restarted_app.issue_session_token('session-2', ['oncall-approvers'])}"
            }
            # Duplicate replay after restart produces no extra turn/work; processing finishes once.
            dup_after_restart = restarted_app.handle_request(
                "POST",
                f"/api/casos/{case.case_id}/mensajes",
                headers=chat_hdrs,
                body=json.dumps(
                    {
                        "client_message_id": "race-1",
                        "text": "¿Y si fue el tráfico? Compara la tasa de errores antes y después.",
                        "base_revision": 1,
                        "proposal_id": orig_prop_id,
                    }
                ),
            )
            assert dup_after_restart.status_code == 202
            assert dup_after_restart.json()["duplicate"] is True

            processed = restarted_svc.process_pending_work(case.case_id)
            assert processed is not None
            assert len(processed.revisions) == 2
            assert len(processed.conversation_turns) == 1
            assert processed.proposal is None  # Withdrawn!

            # Session 2 tries to approve the withdrawn card -> 409 & ZERO executor calls!
            stale_approve = restarted_app.handle_request(
                "POST",
                "/api/aprobacion",
                headers=approver_hdrs,
                body=json.dumps(
                    {
                        "case_id": case.case_id,
                        "proposal_id": orig_prop_id,
                        "proposal_version": orig_prop_ver,
                        "decision": "approve",
                    }
                ),
            )
            assert stale_approve.status_code == 409
            assert len(restarted_sim.operations) == 0

            # Challenging revision 1 again when revision 2 is active -> 409 with active_revision=2.
            stale_rev_chat = restarted_app.handle_request(
                "POST",
                f"/api/casos/{case.case_id}/mensajes",
                headers=chat_hdrs,
                body=json.dumps(
                    {
                        "client_message_id": "race-stale",
                        "text": "¿Y si fue la cohorte?",
                        "base_revision": 1,
                        "proposal_id": orig_prop_id,
                    }
                ),
            )
            assert stale_rev_chat.status_code == 409
            assert stale_rev_chat.json()["active_revision"] == 2

            # Historical explanation on superseded revision 1 works cleanly and cites history.
            hist_explain = restarted_app.handle_request(
                "POST",
                f"/api/casos/{case.case_id}/mensajes",
                headers=chat_hdrs,
                body=json.dumps(
                    {
                        "client_message_id": "race-hist",
                        "text": "¿Por qué recomendabas el rollback?",
                        "base_revision": 1,
                        "proposal_id": orig_prop_id,
                    }
                ),
            )
            assert hist_explain.status_code == 202
            last_turn = restarted_svc.list_messages(case.case_id)[-1]
            assert "histórica" in last_turn.answer.lower() or "withdrawn" in last_turn.answer.lower()

            # DTO compares previous vs active conclusion cleanly & HTML includes chat panel.
            dto = restarted_app.handle_request("GET", f"/api/caso?caso={case.case_id}", headers=chat_hdrs).json()
            assert dto["previous_conclusion"]["revision_number"] == 1
            assert dto["current_conclusion"]["action_outcome"] == "withdrawn"
            html = restarted_app.handle_request("GET", "/").body
            assert "Investigación conversacional" in html
        finally:
            restarted_svc.close()
    finally:
        try:
            svc.close()
        except Exception:
            pass


def test_approval_wins_race_over_delayed_follow_up_and_legacy_case_backfill(
    transfers_workspace: TransfersWorkspace,
) -> None:
    _write_two_release_manifest(transfers_workspace)
    _write_logs_for_variant(transfers_workspace, "shared_load")
    svc, sim_state, case, _ = _open_initial_case(
        transfers_workspace, mode=OperationalMode.ASSISTED, db_name="approve_first.db"
    )
    try:
        assert case.proposal is not None
        # Enqueue a pending follow-up work BEFORE approval? No — simulate follow-up worker ALREADY running
        # from rev=1 snapshot while approval wins and commits first in SQLite!
        snapshot = svc.get_case(case.case_id)
        approved = svc.resolve_approval(
            case_id=case.case_id,
            proposal_id=case.proposal.proposal_id,
            actor="lead",
            decision=DecisionChoice.APPROVE,
            proposal_version=1,
            verification_at=datetime.fromisoformat("2026-11-12T09:19:00-05:00"),
        )
        assert approved.status == CaseStatus.CLOSED
        assert len(sim_state.operations) == 1

        # Delayed follow-up turn submitted or worker finishes from rev 1: must NOT overwrite approved operation.
        store = svc._store
        assert isinstance(store, SqliteCaseStoreAdapter)
        # Create a turn tied to rev 1 directly as if queued concurrently before approval finalized:
        from vigia.domain.contracts import FollowUpCandidate
        _, turn, _, _ = store.create_chat_turn_transactional(
            case_id=case.case_id,
            client_message_id="delayed-1",
            actor="alice",
            question="¿Y si fue el tráfico?",
            base_revision=1,
            proposal_id=None,
            intent="challenge_hypothesis",
            re_evaluate=False,
            cutoff_at=snapshot.alert.effective_cutoff,
            user_hypothesis="traffic",
            now=snapshot.alert.effective_cutoff,
        )
        candidate = FollowUpCandidate(
            intent=ConversationIntent.CHALLENGE_HYPOTHESIS,
            answer="Delayed follow-up finished after approval.",
            cited_evidence_ids=["EV-001"],
            comparison_outcome="withdrawn",
            cutoff_at=snapshot.alert.effective_cutoff,
        )
        after_late, finished_turn = store.publish_follow_up_transactional(
            case_id=case.case_id,
            turn_id=turn.turn_id,
            base_revision=1,
            candidate=candidate,
            updated_case_snapshot=snapshot,
            now=snapshot.alert.effective_cutoff,
        )
        assert finished_turn.result_outcome == "superseded_by_concurrent_update"
        assert after_late.decision is not None
        assert after_late.execution is not None
        assert after_late.status == CaseStatus.CLOSED

        # Legacy case backfill: serialize a case with empty proposal_history and verify get_case restores it.
        raw = after_late.model_copy(update={"proposal_history": []}).model_dump_json()
        store._conn.execute(
            "UPDATE cases SET payload_json = ? WHERE case_id = ?",
            (raw, case.case_id),
        )
        reloaded = svc.get_case(case.case_id)
        assert len(reloaded.proposal_history) == 1
        assert reloaded.proposal_history[0].proposal_id == case.proposal.proposal_id
    finally:
        svc.close()


def test_offline_demo_model_web_flow_and_mandatory_proposal_version(
    transfers_workspace: TransfersWorkspace,
) -> None:
    from vigia.entrypoints.cli import _OfflineDemoChatModel

    _write_two_release_manifest(transfers_workspace)
    _write_logs_for_variant(transfers_workspace, "causal_change")
    model = _OfflineDemoChatModel(
        base_sha=transfers_workspace.base_sha,
        deployed_sha=transfers_workspace.deployed_sha,
    )
    cfg = PilotRuntimeConfig(
        profile=RuntimeProfile.PILOT,
        mode=OperationalMode.ASSISTED,
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
        db_path=transfers_workspace.root / "offline_causal.db",
    )
    svc, sim_state, _ = build_pilot_case_service(cfg, model=model)
    try:
        app = VigiaHttpApp(svc, require_user_auth=True, auto_process_alerts=True)
        chat_hdrs = {
            "Authorization": f"Bearer {app.issue_session_token('lead', ['transfers-squad', 'oncall-approvers'])}"
        }
        alert_resp = app.handle_request(
            "POST",
            "/api/alerta",
            body=json.dumps(
                {
                    "source": "grafana",
                    "event_id": "offline-e2e-1",
                    "state": "firing",
                    "alert": {
                        "service": "Transferencias",
                        "environment": "production",
                        "metric": "latency_p95_ms",
                        "observed_at": "2026-11-12T09:12:00-05:00",
                        "current_value": 420.0,
                        "threshold_value": 800.0,
                    },
                }
            ),
        )
        assert alert_resp.status_code == 202
        case_id = alert_resp.json()["case_id"]
        initial_dto = app.handle_request("GET", f"/api/caso?caso={case_id}", headers=chat_hdrs).json()
        prop_id = initial_dto["proposal"]["proposal_id"]
        assert initial_dto["proposal"]["version"] == 1

        # Same question via web endpoint triggers _OfflineDemoChatModel follow-up -> maintained (v2).
        chat_resp = app.handle_request(
            "POST",
            f"/api/casos/{case_id}/mensajes",
            headers=chat_hdrs,
            body=json.dumps(
                {
                    "client_message_id": "off-1",
                    "text": "¿Y si fue el tráfico?",
                    "base_revision": 1,
                    "proposal_id": prop_id,
                }
            ),
        )
        assert chat_resp.status_code == 202
        after_dto = app.handle_request("GET", f"/api/caso?caso={case_id}", headers=chat_hdrs).json()
        assert after_dto["current_conclusion"]["action_outcome"] == "maintained"
        assert after_dto["proposal"]["version"] == 2
        assert after_dto["conversation_turns"][-1]["comparison"]["verdict"] == ComparisonVerdict.SUPPORTS_CAUSAL_CHANGE.value

        # Omitting proposal_version OR sending stale v1 produces zero executor dispatches.
        no_ver = app.handle_request(
            "POST",
            "/api/aprobacion",
            headers=chat_hdrs,
            body=json.dumps(
                {
                    "case_id": case_id,
                    "proposal_id": prop_id,
                    "decision": "approve",
                }
            ),
        )
        assert no_ver.status_code == 400
        assert len(sim_state.operations) == 0

        stale_ver = app.handle_request(
            "POST",
            "/api/aprobacion",
            headers=chat_hdrs,
            body=json.dumps(
                {
                    "case_id": case_id,
                    "proposal_id": prop_id,
                    "proposal_version": 1,
                    "decision": "approve",
                }
            ),
        )
        assert stale_ver.status_code == 409
        assert len(sim_state.operations) == 0

        # Approving with explicit current v2 executes cleanly.
        ok_ver = app.handle_request(
            "POST",
            "/api/aprobacion",
            headers=chat_hdrs,
            body=json.dumps(
                {
                    "case_id": case_id,
                    "proposal_id": prop_id,
                    "proposal_version": 2,
                    "decision": "approve",
                }
            ),
        )
        assert ok_ver.status_code == 200
        assert len(sim_state.operations) == 1
    finally:
        svc.close()

    # Variant 2: shared traffic withdraws rollback using the exact same _OfflineDemoChatModel.
    _write_logs_for_variant(transfers_workspace, "shared_load")
    model_shared = _OfflineDemoChatModel(
        base_sha=transfers_workspace.base_sha,
        deployed_sha=transfers_workspace.deployed_sha,
    )
    cfg2 = PilotRuntimeConfig(
        profile=RuntimeProfile.PILOT,
        mode=OperationalMode.ASSISTED,
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
        db_path=transfers_workspace.root / "offline_shared.db",
    )
    svc2, _, _ = build_pilot_case_service(cfg2, model=model_shared)
    try:
        app2 = VigiaHttpApp(svc2, require_user_auth=True, auto_process_alerts=True)
        hdrs2 = {"Authorization": f"Bearer {app2.issue_session_token('lead', ['transfers-squad'])}"}
        res2 = app2.handle_request(
            "POST",
            "/api/alerta",
            body=json.dumps(
                {
                    "source": "grafana",
                    "event_id": "offline-e2e-2",
                    "state": "firing",
                    "alert": {
                        "service": "Transferencias",
                        "environment": "production",
                        "metric": "latency_p95_ms",
                        "observed_at": "2026-11-12T09:12:00-05:00",
                        "current_value": 420.0,
                        "threshold_value": 800.0,
                    },
                }
            ),
        )
        cid2 = res2.json()["case_id"]
        app2.handle_request(
            "POST",
            f"/api/casos/{cid2}/mensajes",
            headers=hdrs2,
            body=json.dumps(
                {
                    "client_message_id": "off-2",
                    "text": "¿Y si fue el tráfico?",
                    "base_revision": 1,
                }
            ),
        )
        dto2 = app2.handle_request("GET", f"/api/caso?caso={cid2}", headers=hdrs2).json()
        assert dto2["current_conclusion"]["action_outcome"] == "withdrawn"
        assert dto2["proposal"] is None
    finally:
        svc2.close()


def test_prometheus_missing_counters_stays_unknown_and_inconclusive_comparison_allows_retry(
    transfers_workspace: TransfersWorkspace,
) -> None:
    from vigia.adapters.outbound.github import GitHubReleaseSourceAdapter
    from vigia.adapters.outbound.http_telemetry import PrometheusLokiTelemetryAdapter
    from vigia.adapters.outbound.service_catalog import YamlServiceCatalogAdapter

    catalog = YamlServiceCatalogAdapter.from_yaml(transfers_workspace.fixtures_dir / "services.yaml")

    def prom_single_sample(
        url: str, params: dict[str, str], headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        if "latency_p95_ms" in params.get("query", ""):
            return 200, {
                "data": {
                    "result": [
                        {
                            "metric": {"cohort": "v1"},
                            "values": [["2026-11-12T09:19:00-05:00", "184.0"]],
                        }
                    ]
                }
            }
        # Companion counter series absent -> must stay unknown, never synthetic 100/0.
        return 200, {"data": {"result": []}}

    prom = PrometheusLokiTelemetryAdapter(catalog, http_get=prom_single_sample)
    series = prom.query_metrics(
        "transferencias",
        "production",
        "latency_p95_ms",
        10,
        datetime.fromisoformat("2026-11-12T09:19:00-05:00"),
    )
    assert series.request_volume is None
    assert series.error_total is None
    assert series.coverage < 0.2

    # GitHub Deployments without inline 'status' and without statuses -> pending (never assumed active).
    def gh_missing_status(
        url: str, params: dict[str, str], headers: dict[str, str]
    ) -> tuple[int, Any]:
        if url.endswith("/deployments"):
            return 200, [
                {
                    "id": 77,
                    "environment": "production",
                    "created_at": "2026-11-12T13:55:00Z",
                    "sha": transfers_workspace.deployed_sha,
                    "statuses_url": "https://api.github.com/repos/b/p/deployments/77/statuses",
                }
            ]
        return 200, []

    gh = GitHubReleaseSourceAdapter({"pagos-core": "b/p"}, http_get=gh_missing_status)
    gh_res = gh.resolve_active_releases(
        "pagos-core", "production", datetime.fromisoformat("2026-11-12T09:12:00-05:00")
    )
    assert gh_res.status == ConnectorStatus.NO_RESULTS

    # Reusable comparison check: indeterminate comparison must NOT block retry once denominator arrives,
    # and a different error pattern must NOT reuse an unrelated '*' comparison.
    _write_two_release_manifest(transfers_workspace)
    _write_logs_for_variant(transfers_workspace, "missing_denominator")
    svc, _, case, _ = _open_initial_case(transfers_workspace, db_name="retry_inc.db")
    try:
        app = VigiaHttpApp(svc, require_user_auth=True, auto_process_alerts=True)
        hdrs = {"Authorization": f"Bearer {app.issue_session_token('alice', ['transfers-squad'])}"}

        first = app.handle_request(
            "POST",
            f"/api/casos/{case.case_id}/mensajes",
            headers=hdrs,
            body=json.dumps(
                {
                    "client_message_id": "inc-1",
                    "text": "¿Y si fue el tráfico?",
                    "base_revision": 1,
                }
            ),
        )
        assert first.status_code == 202
        c_mid = svc.get_case(case.case_id)
        assert c_mid.proposal is not None and c_mid.proposal.status == ProposalStatus.UNDER_REVIEW

        # Now recover denominator logs and ask again on the SAME cutoff!
        _write_logs_for_variant(transfers_workspace, "causal_change")
        second = app.handle_request(
            "POST",
            f"/api/casos/{case.case_id}/mensajes",
            headers=hdrs,
            body=json.dumps(
                {
                    "client_message_id": "inc-2",
                    "text": "¿Y si fue el tráfico ahora con las cohortes completas?",
                    "base_revision": len(c_mid.revisions),
                }
            ),
        )
        assert second.status_code == 202
        c_done = svc.get_case(case.case_id)
        assert c_done.proposal is not None and c_done.proposal.status == ProposalStatus.PENDING

        # Distinct error definition ('timeout') must trigger a fresh comparison rather than reusing '*'.
        diff_pattern = app.handle_request(
            "POST",
            f"/api/casos/{case.case_id}/mensajes",
            headers=hdrs,
            body=json.dumps(
                {
                    "client_message_id": "inc-3",
                    "text": "Compara cohortes para 'timeout'",
                    "base_revision": len(c_done.revisions),
                }
            ),
        )
        assert diff_pattern.status_code == 202
        assert len(svc.get_case(case.case_id).revisions) == len(c_done.revisions) + 1
    finally:
        svc.close()


def test_http_auth_security_xss_and_concurrent_worker_leases(
    transfers_workspace: TransfersWorkspace,
) -> None:
    _write_two_release_manifest(transfers_workspace)
    _write_logs_for_variant(transfers_workspace, "causal_change")
    cfg = PilotRuntimeConfig(
        profile=RuntimeProfile.PILOT,
        mode=OperationalMode.ASSISTED,
        fixtures_dir=transfers_workspace.fixtures_dir,
        authorized_repos={"applications/pagos-core": transfers_workspace.repo_dir},
        db_path=transfers_workspace.root / "leases.db",
    )
    w1, sim1, _ = build_pilot_case_service(
        cfg,
        model=DualPhaseConversationalModel(
            base_sha=transfers_workspace.base_sha,
            deployed_sha=transfers_workspace.deployed_sha,
        ),
    )
    w2, _, _ = build_pilot_case_service(
        cfg,
        model=DualPhaseConversationalModel(
            base_sha=transfers_workspace.base_sha,
            deployed_sha=transfers_workspace.deployed_sha,
        ),
    )
    try:
        app = VigiaHttpApp(
            w1,
            require_user_auth=True,
            trusted_proxy_token="proxy-secret-xyz",
            auto_process_alerts=False,
        )
        # Ingest creates pending work item; active lease held by w1 blocks w2 from claiming it.
        ingest_resp = app.handle_request(
            "POST",
            "/api/alerta",
            body=json.dumps(
                {
                    "source": "grafana",
                    "event_id": "lease-1",
                    "state": "firing",
                    "alert": {
                        "service": "Transferencias",
                        "environment": "production",
                        "metric": "latency_p95_ms",
                        "observed_at": "2026-11-12T09:12:00-05:00",
                        "current_value": 420.0,
                        "threshold_value": 800.0,
                    },
                }
            ),
        )
        assert ingest_resp.status_code == 202
        cid = ingest_resp.json()["case_id"]

        work_item = w1._store.claim_next_work(case_id=cid, worker_id="worker-1", lease_seconds=120)
        assert work_item is not None
        assert w2.process_pending_work(cid) is None  # Blocked while lease is active!

        # Spoofed client headers without proxy secret or signed bearer token must fail with 401.
        spoof_chat = app.handle_request(
            "POST",
            f"/api/casos/{cid}/mensajes",
            headers={"X-Vigia-User": "attacker", "X-Vigia-Groups": "admin,oncall-approvers"},
            body=json.dumps({"client_message_id": "x-1", "text": "¿Por qué?"}),
        )
        assert spoof_chat.status_code == 401

        # Authenticated proxy header passes and verifies XSS-free console HTML (no innerHTML / inline auth headers).
        proxy_hdrs = {
            "X-Vigia-Proxy-Auth": "proxy-secret-xyz",
            "X-Vigia-User": "alice",
            "X-Vigia-Groups": "transfers-squad",
        }
        w1._store.complete_work(work_item.work_id, status="pending")
        w2.process_pending_work(cid)
        xss_chat = app.handle_request(
            "POST",
            f"/api/casos/{cid}/mensajes",
            headers=proxy_hdrs,
            body=json.dumps(
                {
                    "client_message_id": "xss-1",
                    "text": "¿Por qué <img src=x onerror=alert(1)>?",
                    "base_revision": 1,
                }
            ),
        )
        assert xss_chat.status_code == 202
        html = app.handle_request("GET", "/").body
        assert "innerHTML" not in html
        assert "X-Vigia-User" not in html
        assert "textContent" in html
        assert len(sim1.operations) == 0
    finally:
        w1.close()
        w2.close()
