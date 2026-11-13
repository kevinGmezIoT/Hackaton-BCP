"""Terminal CLI entrypoint supporting Demo/Pilot profiles, onboarding probes, and HTTP/SSE console."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from vigia.adapters.inbound.http_api import VigiaHttpApp, serve_http
from vigia.application.cohort_comparison import infer_error_definition
from vigia.demo import copy_demo_scenario
from vigia.domain.contracts import (
    Alert,
    DecisionChoice,
    OperationalMode,
    ProposalStatus,
    RuntimeProfile,
)
from vigia.runtime import PilotRuntimeConfig, build_pilot_case_service


class _OfflineDemoChatModel(BaseChatModel):
    """Deterministic scripted chat model supporting initial investigation and read-only follow-up."""

    deployed_sha: str = ""
    base_sha: str = ""
    bound_tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "vigia-offline-demo"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_OfflineDemoChatModel":
        names = [
            getattr(t, "name", None) or (t.get("name", "") if isinstance(t, dict) else "")
            for t in tools
        ]
        return self.model_copy(update={"bound_tool_names": [n for n in names if n]})

    def _latest_tool_payload(self, messages: list[BaseMessage]) -> dict[str, Any]:
        for msg in reversed(messages):
            if getattr(msg, "type", "") == "tool":
                try:
                    parsed = json.loads(str(msg.content))
                    if isinstance(parsed, dict):
                        return parsed
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
        return {}

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        is_follow_up = "propose_action" not in self.bound_tool_names
        ai_turns = sum(1 for m in messages if getattr(m, "type", "") == "ai")

        if not is_follow_up:
            initial_plan = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "name": "detect_anomaly",
                            "args": {"service": "transferencias", "metric": "latency_p95_ms"},
                        },
                        {
                            "id": "call_2",
                            "name": "query_dependencies",
                            "args": {"service": "transferencias", "direction": "outbound"},
                        },
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_3",
                            "name": "resolve_release",
                            "args": {"service": "pagos-core", "environment": "production"},
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_4",
                            "name": "compare_revisions",
                            "args": {
                                "repository": "applications/pagos-core",
                                "base_sha": self.base_sha,
                                "target_sha": self.deployed_sha,
                            },
                        },
                        {
                            "id": "call_5",
                            "name": "read_snippet",
                            "args": {
                                "repository": "applications/pagos-core",
                                "sha": self.deployed_sha,
                                "path": "src/pool.py",
                                "start_line": 1,
                                "end_line": 20,
                            },
                        },
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_6",
                            "name": "search_logs",
                            "args": {
                                "service": "pagos-core",
                                "pattern": "Connection pool exhausted",
                                "window_start": "2026-11-12T08:55:00-05:00",
                                "window_end": "2026-11-12T09:12:00-05:00",
                            },
                        },
                        {
                            "id": "call_7",
                            "name": "search_similar_incidents",
                            "args": {
                                "description": "Connection pool exhausted after pool_size reduction in pagos-core",
                                "k": 2,
                            },
                        },
                        {
                            "id": "call_8",
                            "name": "search_runbook",
                            "args": {"query": "Connection pool exhausted rollback"},
                        },
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_9",
                            "name": "propose_action",
                            "args": {
                                "action": "rollback_change",
                                "target_service": "pagos-core",
                                "target_release_id": "CHG-4821",
                                "hypothesis": (
                                    "CHG-4821 reduced DB pool_size from 80 to 50 in pagos-core "
                                    "(src/pool.py#L4-L8), causing pool exhaustion upstream in transferencias."
                                ),
                                "justification": (
                                    "312 Connection pool exhausted errors started at 09:01 exclusively in cohort CHG-4821; "
                                    "matches INC-3512, INC-3307 and RB-087."
                                ),
                                "evidence_ids": [
                                    "EV-001",
                                    "EV-002",
                                    "EV-003",
                                    "EV-004",
                                    "EV-005",
                                    "EV-006",
                                    "EV-007",
                                    "EV-008",
                                    "EV-009",
                                    "EV-010",
                                ],
                                "verification_plan": (
                                    "Query transferencias latency_p95_ms 7-10 min post-rollback; "
                                    "expect recovery below 800 ms toward 180 ms baseline."
                                ),
                                "exposed_services": ["transferencias", "pagos-core"],
                            },
                        }
                    ],
                ),
                AIMessage(
                    content=(
                        "**Symptom & Projected Impact**: Transferencias p95 latency rose from 180 ms to 420 ms "
                        "(~24 min to 800 ms threshold) [EV-001].\n"
                        "**Probable Cause & Dependency Path**: Transferencias calls pagos-core [EV-002]; "
                        f"active release CHG-4821 ({self.deployed_sha[:8]}) reduced `pool_size` 80 -> 50 in "
                        "`src/pool.py` [EV-003, EV-004, EV-005], producing 312 pool exhaustion errors since 09:01 [EV-006].\n"
                        "**Proposed Action**: Rollback `CHG-4821` on `pagos-core`."
                    )
                ),
            ]
            idx = min(ai_turns, len(initial_plan) - 1)
            return ChatResult(generations=[ChatGeneration(message=initial_plan[idx])])

        user_msg = ""
        for msg in messages:
            if getattr(msg, "type", "") == "human":
                user_msg = str(msg.content)
                break
        lowered = user_msg.lower()
        asks_current_status = any(
            k in lowered for k in ("ahora", "currently", "right now", "still", "actualmente", "sigue")
        ) and not any(k in lowered for k in ("tráfico", "trafico", "traffic", "cohor", "tasa", "rate", "carga"))

        if ai_turns == 0:
            if asks_current_status:
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "id": "fu_metric_1",
                                        "name": "query_metrics",
                                        "args": {
                                            "service": "transferencias",
                                            "metric": "latency_p95_ms",
                                            "window_minutes": 15,
                                        },
                                    }
                                ],
                            )
                        )
                    ]
                )
            pattern = infer_error_definition(user_msg)
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": "fu_cohort_1",
                                    "name": "compare_cohort_error_rates",
                                    "args": {
                                        "service": "pagos-core",
                                        "pattern": pattern,
                                        "check_shared_saturation": True,
                                    },
                                }
                            ],
                        )
                    )
                ]
            )

        payload = self._latest_tool_payload(messages)
        ev_ids = payload.get("evidence_ids") or []
        cite_str = ", ".join(str(eid) for eid in ev_ids)
        if asks_current_status:
            cur = payload.get("current_value")
            text = (
                f"Estado actualizado de `transferencias` (`latency_p95_ms`): {cur} ms [{cite_str}]."
                if cite_str
                else f"Consulta de estado ejecutada para `transferencias` (`latency_p95_ms`): {cur}."
            )
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

        comp = payload.get("comparison") if isinstance(payload.get("comparison"), dict) else {}
        summary = comp.get("summary") or payload.get("note") or "Comparación por cohorte completada."
        text = f"{summary} [{cite_str}]." if cite_str else str(summary)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


def _bootstrap_demo_workspace(temp_root: Path) -> tuple[Path, dict[str, Path], str, str]:
    """Create a temporary workspace with packaged demo resources and a local Git repo."""
    repo_dir = temp_root / "pagos-core-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    git("init")
    git("config", "user.email", "vigia-demo@example.com")
    git("config", "user.name", "Vigia Demo")

    pool_file = repo_dir / "src" / "pool.py"
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text(
        '"""Database connection pool configuration for pagos-core."""\n\n'
        "DEFAULT_POOL_CONFIG = {\n"
        '    "host": "pagos-db.internal",\n'
        '    "pool_size": 80,\n'
        '    "acquire_timeout_ms": 250,\n'
        "}\n",
        encoding="utf-8",
    )
    git("add", ".")
    git("commit", "-m", "Baseline CHG-4810 with pool_size=80")
    base_sha = git("rev-parse", "HEAD")

    pool_file.write_text(
        '"""Database connection pool configuration for pagos-core."""\n\n'
        "DEFAULT_POOL_CONFIG = {\n"
        '    "host": "pagos-db.internal",\n'
        '    "pool_size": 50,\n'
        '    "acquire_timeout_ms": 250,\n'
        "}\n",
        encoding="utf-8",
    )
    git("add", ".")
    git("commit", "-m", "CHG-4821 tune pool_size from 80 to 50")
    deployed_sha = git("rev-parse", "HEAD")

    readme_file = repo_dir / "README.md"
    readme_file.write_text("# pagos-core\nUpdated logging format.\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "chore: update README (undeployed)")

    fixtures_copy = copy_demo_scenario(temp_root / "fixtures")
    releases_payload = {
        "releases": [
            {
                "release_id": "CHG-4821",
                "service": "pagos-core",
                "environment": "production",
                "deployed_at": "2026-11-12T08:55:00-05:00",
                "previous_sha": base_sha,
                "deployed_sha": deployed_sha,
                "repository": "applications/pagos-core",
                "status": "active",
                "cohort": "100%",
                "summary": "Tune DB connection pool settings",
            }
        ]
    }
    (fixtures_copy / "releases.json").write_text(
        json.dumps(releases_payload, indent=2), encoding="utf-8"
    )
    return fixtures_copy, {"applications/pagos-core": repo_dir}, base_sha, deployed_sha


def _load_dotenv(env_path: Path) -> None:
    """Load simple KEY=VALUE pairs from a .env file without overriding existing env vars."""
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    _load_dotenv(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(description="Vigía Investigator CLI & Pilot Server")
    parser.add_argument(
        "--service", default="Transferencias", help="Target alert service name or alias."
    )
    parser.add_argument(
        "--cutoff", default="2026-11-12T09:12:00-05:00", help="ISO cutoff timestamp."
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("VIGIA_MODEL", "google_genai:gemini-2.5-flash"),
        help="LangChain model identifier (or set VIGIA_MODEL).",
    )
    parser.add_argument(
        "--profile",
        choices=["demo", "pilot"],
        default="demo",
        help="Runtime adapter profile.",
    )
    parser.add_argument(
        "--mode",
        choices=["shadow", "assisted"],
        default=None,
        help="Operational mode (defaults to assisted in demo, shadow in pilot).",
    )
    parser.add_argument(
        "--demo-offline",
        action="store_true",
        help="Run the Transfers -> pagos-core scenario using a deterministic offline scripted model.",
    )
    parser.add_argument(
        "--probe-service",
        action="store_true",
        help="Run onboarding connectivity check for --service and exit.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the HTTP/SSE Pilot API & Console server.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP server host.")
    parser.add_argument("--port", type=int, default=8000, help="HTTP server port.")
    parser.add_argument(
        "--decision",
        choices=["approve", "reject", "prompt"],
        default="prompt",
        help="Decision to apply when a proposal awaits human approval.",
    )
    parser.add_argument(
        "--actor", default="oncall-lead", help="Authenticated actor ID for decision."
    )
    args = parser.parse_args(argv)

    profile = RuntimeProfile(args.profile)
    mode = OperationalMode(
        args.mode or ("shadow" if profile == RuntimeProfile.PILOT else "assisted")
    )

    with tempfile.TemporaryDirectory(prefix="vigia-demo-") as tmp:
        tmp_path = Path(tmp)
        fixtures_dir, repos, base_sha, deployed_sha = _bootstrap_demo_workspace(tmp_path)
        model: str | BaseChatModel = (
            _OfflineDemoChatModel(base_sha=base_sha, deployed_sha=deployed_sha)
            if args.demo_offline
            else args.model
        )
        runtime_cfg = PilotRuntimeConfig(
            profile=profile,
            mode=mode,
            fixtures_dir=fixtures_dir,
            authorized_repos=repos,
            db_path=tmp_path / "vigia.db",
            audit_path=tmp_path / "audit.jsonl",
            strict_rollback_validation=(profile == RuntimeProfile.PILOT),
        )
        service, _, _ = build_pilot_case_service(runtime_cfg, model=model)
        try:
            cutoff_dt = datetime.fromisoformat(args.cutoff)

            if args.probe_service:
                report = service.probe_service_onboarding(args.service, at_time=cutoff_dt)
                print(json.dumps(report.model_dump(mode="json"), indent=2))
                return 0 if report.ready else 1

            if args.serve:
                app = VigiaHttpApp(
                    service,
                    webhook_token=os.environ.get("VIGIA_WEBHOOK_TOKEN"),
                    require_user_auth=(profile == RuntimeProfile.PILOT),
                    session_secret=os.environ.get("VIGIA_SESSION_SECRET"),
                    trusted_proxy_token=os.environ.get("VIGIA_TRUSTED_PROXY_TOKEN"),
                    allow_demo_console_session=(profile == RuntimeProfile.DEMO),
                    auto_process_alerts=True,
                )
                print(f"Serving Vigía Pilot API & Console at http://{args.host}:{args.port} (mode={mode.value})")
                try:
                    serve_http(app, host=args.host, port=args.port)
                except KeyboardInterrupt:
                    return 0
                return 0

            alert = Alert(
                service=args.service,
                environment="production",
                metric="latency_p95_ms",
                observed_at=cutoff_dt,
                cutoff_at=cutoff_dt,
                symptom="p95 latency rose from 180 ms to 420 ms in 15 minutes",
                baseline_value=180.0,
                current_value=420.0,
                threshold_value=800.0,
                projected_minutes_to_threshold=24.0,
            )
            case = service.start_case(alert, mode=mode)
            print(f"\n=== Case {case.case_id} ({case.status.value}, mode={case.mode.value}) ===")
            print(case.final_summary)

            if case.proposal is None:
                print("\nNo proposal registered.")
                if case.suggested_next_check:
                    print(f"Suggested next check: {case.suggested_next_check}")
                return 0

            print(
                f"\nProposal {case.proposal.proposal_id}: action={case.proposal.action.value} "
                f"target={case.proposal.target_service} ({case.proposal.target_release_id}) "
                f"risk={case.proposal.risk_level.value} status={case.proposal.status.value}"
            )
            if case.mode == OperationalMode.SHADOW:
                print("Shadow mode active: recommendation persisted for human review; zero external actions executed.")
                return 0
            if case.proposal.status != ProposalStatus.PENDING:
                if case.execution:
                    print(f"Execution: {case.execution.detail}")
                return 0

            choice_raw = args.decision
            if choice_raw == "prompt":
                choice_raw = input("Decision [approve/reject]: ").strip().lower()
            if choice_raw not in {"approve", "reject"}:
                print("Invalid decision; leaving proposal pending.", file=sys.stderr)
                return 1

            updated = service.resolve_approval(
                case_id=case.case_id,
                proposal_id=case.proposal.proposal_id,
                actor=args.actor,
                decision=DecisionChoice(choice_raw),
            )
            print(f"\nAfter decision ({choice_raw}): case status = {updated.status.value}")
            if updated.execution:
                print(f"Execution: {updated.execution.detail}")
            if updated.verification:
                print(f"Verification: {updated.verification.detail}")
        finally:
            service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
