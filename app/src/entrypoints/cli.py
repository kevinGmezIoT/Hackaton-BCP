"""Terminal CLI entrypoint to reproduce an investigation case and submit human decisions."""

from __future__ import annotations

import argparse
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

from domain.contracts import Alert, DecisionChoice
from runtime import build_case_service


class _OfflineDemoChatModel(BaseChatModel):
    """Deterministic scripted chat model used only with --demo-offline."""

    step: int = 0
    deployed_sha: str = ""
    base_sha: str = ""

    @property
    def _llm_type(self) -> str:
        return "vigia-offline-demo"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _OfflineDemoChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        plan = [
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
                            "window_minutes": 30,
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
                                "EV-009",
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
                    "**Proposed Action**: Rollback `CHG-4821` on `pagos-core` (awaiting human approval)."
                )
            ),
        ]
        idx = min(self.step, len(plan) - 1)
        self.step += 1
        return ChatResult(generations=[ChatGeneration(message=plan[idx])])


def _bootstrap_demo_workspace(
    temp_root: Path, source_fixtures: Path
) -> tuple[Path, dict[str, Path], str, str]:
    """Create a temporary workspace with a real local Git repository and releases.json."""
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

    fixtures_copy = temp_root / "fixtures"
    fixtures_copy.mkdir(parents=True, exist_ok=True)
    for rel in ["services.yaml", "metrics.csv", "logs.txt", "incidents.json"]:
        (fixtures_copy / rel).write_text(
            (source_fixtures / rel).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (fixtures_copy / "docs").mkdir(exist_ok=True)
    (fixtures_copy / "docs" / "transfers.drawio").write_text(
        (source_fixtures / "docs" / "transfers.drawio").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (fixtures_copy / "runbooks").mkdir(exist_ok=True)
    for rb in (source_fixtures / "runbooks").glob("*.md"):
        (fixtures_copy / "runbooks" / rb.name).write_text(
            rb.read_text(encoding="utf-8"), encoding="utf-8"
        )

    releases_json = (
        "{\n"
        '  "releases": [\n'
        "    {\n"
        '      "release_id": "CHG-4821",\n'
        '      "service": "pagos-core",\n'
        '      "environment": "production",\n'
        '      "deployed_at": "2026-11-12T08:55:00-05:00",\n'
        f'      "previous_sha": "{base_sha}",\n'
        f'      "deployed_sha": "{deployed_sha}",\n'
        '      "repository": "applications/pagos-core",\n'
        '      "status": "active",\n'
        '      "cohort": "100%",\n'
        '      "summary": "Tune DB connection pool settings"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )
    (fixtures_copy / "releases.json").write_text(releases_json, encoding="utf-8")
    return fixtures_copy, {"applications/pagos-core": repo_dir}, base_sha, deployed_sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vigía Alternative A Investigator CLI")
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
        "--demo-offline",
        action="store_true",
        help="Run the Transfers -> pagos-core demo using a deterministic offline scripted model.",
    )
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

    default_fixtures = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "transfers"
    with tempfile.TemporaryDirectory(prefix="vigia-demo-") as tmp:
        tmp_path = Path(tmp)
        fixtures_dir, repos, base_sha, deployed_sha = _bootstrap_demo_workspace(
            tmp_path, default_fixtures
        )
        model: str | BaseChatModel = (
            _OfflineDemoChatModel(base_sha=base_sha, deployed_sha=deployed_sha)
            if args.demo_offline
            else args.model
        )
        service, _, _ = build_case_service(
            fixtures_dir=fixtures_dir,
            authorized_repos=repos,
            model=model,
            audit_path=tmp_path / "audit.jsonl",
        )

        cutoff_dt = datetime.fromisoformat(args.cutoff)
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
        case = service.start_case(alert)
        print(f"\n=== Case {case.case_id} ({case.status.value}) ===")
        print(case.final_summary)

        if case.proposal is None:
            print("\nNo proposal registered.")
            if case.suggested_next_check:
                print(f"Suggested next check: {case.suggested_next_check}")
            return 0

        print(
            f"\nProposal {case.proposal.proposal_id}: action={case.proposal.action.value} "
            f"target={case.proposal.target_service} ({case.proposal.target_release_id}) "
            f"risk={case.proposal.risk_level.value}"
        )

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

