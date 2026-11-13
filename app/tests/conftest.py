"""Shared pytest fixtures for Vigía Alternative A tests."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


@dataclass
class TransfersWorkspace:
    """Isolated temporary workspace containing fixtures and a real Git repo."""

    root: Path
    fixtures_dir: Path
    repo_dir: Path
    base_sha: str
    deployed_sha: str
    undeployed_sha: str


class ScriptedChatModel(BaseChatModel):
    """Deterministic BaseChatModel test double supporting LangChain tool calling."""

    responses: list[AIMessage]
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-test-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        idx = min(self.cursor, len(self.responses) - 1)
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[idx])])


def _run_git(repo_dir: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def transfers_workspace(tmp_path: Path) -> TransfersWorkspace:
    """Create an isolated Transfers -> pagos-core workspace with a real 3-commit Git repo."""
    source_fixtures = Path(__file__).resolve().parent / "fixtures" / "transfers"
    fixtures_dir = tmp_path / "fixtures"
    shutil.copytree(source_fixtures, fixtures_dir)

    repo_dir = tmp_path / "pagos-core-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    _run_git(repo_dir, "init")
    _run_git(repo_dir, "config", "user.email", "test@vigia.local")
    _run_git(repo_dir, "config", "user.name", "Vigia Test")

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
    _run_git(repo_dir, "add", ".")
    _run_git(repo_dir, "commit", "-m", "Baseline CHG-4810 pool_size=80")
    base_sha = _run_git(repo_dir, "rev-parse", "HEAD")

    pool_file.write_text(
        '"""Database connection pool configuration for pagos-core."""\n\n'
        "DEFAULT_POOL_CONFIG = {\n"
        '    "host": "pagos-db.internal",\n'
        '    "pool_size": 50,\n'
        '    "acquire_timeout_ms": 250,\n'
        "}\n",
        encoding="utf-8",
    )
    _run_git(repo_dir, "add", ".")
    _run_git(repo_dir, "commit", "-m", "CHG-4821 reduce pool_size from 80 to 50")
    deployed_sha = _run_git(repo_dir, "rev-parse", "HEAD")

    # Later commit on main that is ONLY in staging, never deployed to production.
    (repo_dir / "src" / "logging_config.py").write_text(
        'LOG_LEVEL = "DEBUG"\n', encoding="utf-8"
    )
    _run_git(repo_dir, "add", ".")
    _run_git(repo_dir, "commit", "-m", "CHG-4899 enable debug logging (staging only)")
    undeployed_sha = _run_git(repo_dir, "rev-parse", "HEAD")

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
                "summary": "Reduce pagos-db pool_size from 80 to 50",
            },
            {
                "release_id": "CHG-4899",
                "service": "pagos-core",
                "environment": "staging",
                "deployed_at": "2026-11-12T09:08:00-05:00",
                "previous_sha": deployed_sha,
                "deployed_sha": undeployed_sha,
                "repository": "applications/pagos-core",
                "status": "active",
                "cohort": "100%",
                "summary": "Enable debug logging in staging only",
            },
        ]
    }
    (fixtures_dir / "releases.json").write_text(
        json.dumps(releases_payload, indent=2), encoding="utf-8"
    )

    return TransfersWorkspace(
        root=tmp_path,
        fixtures_dir=fixtures_dir,
        repo_dir=repo_dir,
        base_sha=base_sha,
        deployed_sha=deployed_sha,
        undeployed_sha=undeployed_sha,
    )

