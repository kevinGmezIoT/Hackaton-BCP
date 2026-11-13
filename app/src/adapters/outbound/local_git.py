"""Local Git repository and JSON release manifest adapters."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from domain.contracts import (
    CodeDiff,
    CodeSnippet,
    ConnectorStatus,
    DiffFile,
    Release,
    ReleaseResolution,
)

_SAFE_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{6,40}$")


def _validate_relative_path(path: str) -> bool:
    """Reject absolute paths or parent traversal segments."""
    if path.startswith("/") or path.startswith("\\") or ":" in path:
        return False
    posix = PurePosixPath(path.replace("\\", "/"))
    return ".." not in posix.parts


class LocalGitRepositoryAdapter:
    """Read-only, allowlist-scoped Git adapter running bounded diff and show commands."""

    def __init__(
        self,
        authorized_repos: dict[str, Path],
        *,
        max_patch_lines: int = 80,
    ) -> None:
        self._repos = {k: v.resolve() for k, v in authorized_repos.items()}
        self._max_patch_lines = max_patch_lines

    def _resolve_repo(self, repository: str) -> Path | None:
        return self._repos.get(repository)

    def _run_git(self, repo_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def compare_revisions(
        self,
        repository: str,
        base_sha: str,
        target_sha: str,
        path: str | None = None,
    ) -> CodeDiff:
        repo_path = self._resolve_repo(repository)
        if repo_path is None or not repo_path.exists():
            return CodeDiff(
                status=ConnectorStatus.UNAUTHORIZED,
                repository=repository,
                base_sha=base_sha,
                target_sha=target_sha,
                note=f"Repository '{repository}' is not authorized or does not exist.",
            )
        if not (_SAFE_SHA_PATTERN.match(base_sha) and _SAFE_SHA_PATTERN.match(target_sha)):
            return CodeDiff(
                status=ConnectorStatus.UNAUTHORIZED,
                repository=repository,
                base_sha=base_sha,
                target_sha=target_sha,
                note="Invalid commit SHA format.",
            )
        if path is not None and not _validate_relative_path(path):
            return CodeDiff(
                status=ConnectorStatus.UNAUTHORIZED,
                repository=repository,
                base_sha=base_sha,
                target_sha=target_sha,
                note=f"Unauthorized path '{path}'.",
            )

        args = ["diff", "--unified=3", base_sha, target_sha]
        if path is not None:
            args.extend(["--", path])

        proc = self._run_git(repo_path, args)
        if proc.returncode != 0:
            return CodeDiff(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                repository=repository,
                base_sha=base_sha,
                target_sha=target_sha,
                note=proc.stderr.strip() or "git diff failed.",
            )

        files = self._split_diff_by_file(proc.stdout)
        if not files:
            return CodeDiff(
                status=ConnectorStatus.NO_RESULTS,
                repository=repository,
                base_sha=base_sha,
                target_sha=target_sha,
                note="No differences found between revisions.",
            )
        return CodeDiff(
            status=ConnectorStatus.OK,
            repository=repository,
            base_sha=base_sha,
            target_sha=target_sha,
            files=files,
        )

    def read_snippet(
        self,
        repository: str,
        sha: str,
        path: str,
        start_line: int = 1,
        end_line: int = 80,
    ) -> CodeSnippet:
        repo_path = self._resolve_repo(repository)
        if repo_path is None or not repo_path.exists():
            return CodeSnippet(
                status=ConnectorStatus.UNAUTHORIZED,
                repository=repository,
                sha=sha,
                path=path,
                start_line=start_line,
                end_line=end_line,
                content="",
                note=f"Repository '{repository}' is not authorized or does not exist.",
            )
        if not _SAFE_SHA_PATTERN.match(sha) or not _validate_relative_path(path):
            return CodeSnippet(
                status=ConnectorStatus.UNAUTHORIZED,
                repository=repository,
                sha=sha,
                path=path,
                start_line=start_line,
                end_line=end_line,
                content="",
                note="Invalid SHA or unauthorized path.",
            )

        proc = self._run_git(repo_path, ["show", f"{sha}:{path}"])
        if proc.returncode != 0:
            return CodeSnippet(
                status=ConnectorStatus.NO_RESULTS,
                repository=repository,
                sha=sha,
                path=path,
                start_line=start_line,
                end_line=end_line,
                content="",
                note=proc.stderr.strip() or f"File '{path}' not found at {sha}.",
            )

        lines = proc.stdout.splitlines()
        clamped_start = max(1, start_line)
        clamped_end = min(len(lines), max(clamped_start, end_line))
        numbered = [
            f"{idx}: {lines[idx - 1]}" for idx in range(clamped_start, clamped_end + 1)
        ]
        return CodeSnippet(
            status=ConnectorStatus.OK,
            repository=repository,
            sha=sha,
            path=path,
            start_line=clamped_start,
            end_line=clamped_end,
            content="\n".join(numbered),
        )

    def _split_diff_by_file(self, raw_diff: str) -> list[DiffFile]:
        if not raw_diff.strip():
            return []
        chunks = re.split(r"(?=^diff --git )", raw_diff, flags=re.MULTILINE)
        files: list[DiffFile] = []
        for chunk in chunks:
            if not chunk.strip():
                continue
            lines = chunk.splitlines()
            header = lines[0]
            match = re.search(r" b/(.+)$", header)
            file_path = match.group(1) if match else "unknown"
            truncated = len(lines) > self._max_patch_lines
            kept_lines = lines[: self._max_patch_lines]
            if truncated:
                kept_lines.append("... [patch truncated; call read_snippet for full context]")
            files.append(
                DiffFile(
                    path=file_path,
                    patch="\n".join(kept_lines),
                    truncated=truncated,
                )
            )
        return files


class JsonReleaseSourceAdapter:
    """Reads deployment manifest JSON and resolves active releases at a point in time."""

    def __init__(
        self,
        releases_path: Path,
        *,
        rolled_back_release_ids: set[str] | None = None,
    ) -> None:
        self._releases_path = releases_path
        self._rolled_back_ids = (
            rolled_back_release_ids if rolled_back_release_ids is not None else set()
        )

    def mark_rolled_back(self, release_id: str) -> None:
        """Record a release as rolled back in the current session."""
        self._rolled_back_ids.add(release_id)

    def _load_all(self) -> list[Release]:
        raw = json.loads(self._releases_path.read_text(encoding="utf-8"))
        entries = raw.get("releases", []) if isinstance(raw, dict) else raw
        releases: list[Release] = []
        for item in entries:
            rel = Release.model_validate(item)
            if rel.release_id in self._rolled_back_ids:
                rel = rel.model_copy(update={"status": "rolled_back"})
            releases.append(rel)
        return releases

    def resolve_active_releases(
        self,
        service: str,
        environment: str,
        at_time: datetime,
    ) -> ReleaseResolution:
        if not self._releases_path.exists():
            return ReleaseResolution(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                service=service,
                environment=environment,
                resolved_at=at_time,
                note=f"Release manifest not found at {self._releases_path}",
            )

        candidates = [
            rel
            for rel in self._load_all()
            if rel.service.lower() == service.lower()
            and rel.environment.lower() == environment.lower()
            and rel.deployed_at <= at_time
            and rel.status in {"active", "canary"}
        ]
        candidates.sort(key=lambda r: r.deployed_at, reverse=True)
        if not candidates:
            return ReleaseResolution(
                status=ConnectorStatus.NO_RESULTS,
                service=service,
                environment=environment,
                resolved_at=at_time,
                note=f"No active releases found for '{service}' in '{environment}' at {at_time.isoformat()}.",
            )
        return ReleaseResolution(
            status=ConnectorStatus.OK,
            service=service,
            environment=environment,
            resolved_at=at_time,
            releases=candidates,
        )

    def list_recent_changes(
        self,
        service: str,
        environment: str,
        hours: int,
        at_time: datetime,
    ) -> ReleaseResolution:
        if not self._releases_path.exists():
            return ReleaseResolution(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                service=service,
                environment=environment,
                resolved_at=at_time,
                note=f"Release manifest not found at {self._releases_path}",
            )

        since = at_time - timedelta(hours=hours)
        candidates = [
            rel
            for rel in self._load_all()
            if rel.service.lower() == service.lower()
            and rel.environment.lower() == environment.lower()
            and since <= rel.deployed_at <= at_time
        ]
        candidates.sort(key=lambda r: r.deployed_at, reverse=True)
        status = ConnectorStatus.OK if candidates else ConnectorStatus.NO_RESULTS
        return ReleaseResolution(
            status=status,
            service=service,
            environment=environment,
            resolved_at=at_time,
            releases=candidates,
            note="" if candidates else f"No deployments in the last {hours}h up to {at_time.isoformat()}.",
        )

