"""GitHub Deployments and Commits/Contents adapters with allowlist and cutoff enforcement."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any

from vigia.adapters.outbound.http_telemetry import HttpGetFn, _default_http_get
from vigia.adapters.outbound.local_git import _SAFE_SHA_PATTERN, _validate_relative_path
from vigia.domain.contracts import (
    CodeDiff,
    CodeSnippet,
    ConnectorStatus,
    DiffFile,
    Release,
    ReleaseResolution,
)


def _parse_github_timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _map_github_deployment_state(raw_state: str | None) -> str:
    if raw_state is None:
        return "unknown"
    normalized = raw_state.strip().lower()
    if normalized in {"active", "success"}:
        return "active"
    if normalized == "canary":
        return "canary"
    if normalized == "inactive":
        return "inactive"
    if normalized in {"failure", "error", "failed"}:
        return "failed"
    if normalized in {"queued", "pending", "in_progress"}:
        return "pending"
    if normalized == "rolled_back":
        return "rolled_back"
    return normalized or "unknown"


class GitHubReleaseSourceAdapter:
    """Resolves active deployments and change history from GitHub Deployments API."""

    def __init__(
        self,
        service_repositories: dict[str, str],
        *,
        api_base_url: str = "https://api.github.com",
        auth_token: str | None = None,
        http_get: HttpGetFn | None = None,
        rolled_back_release_ids: set[str] | None = None,
        max_retries_per_case: int = 2,
    ) -> None:
        self._service_repos = {k.lower(): v for k, v in service_repositories.items()}
        self._base_url = api_base_url.rstrip("/")
        self._auth_token = auth_token
        self._http_get = http_get or _default_http_get
        self._rolled_back = (
            rolled_back_release_ids if rolled_back_release_ids is not None else set()
        )
        self._max_retries = max_retries_per_case
        self._retries_used = 0

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def _get_json_with_retry(
        self, url: str, params: dict[str, str]
    ) -> tuple[int, Any]:
        while True:
            try:
                code, payload = self._http_get(url, params, self._headers())
            except (TimeoutError, ConnectionError, OSError) as exc:
                code, payload = 503, {"error": str(exc)}
            if code in {502, 503, 504} and self._retries_used < self._max_retries:
                self._retries_used += 1
                continue
            return code, payload

    def _resolve_deployment_status(
        self, repo: str, item: dict[str, Any], rel_id: str
    ) -> tuple[ConnectorStatus, str, str]:
        if rel_id in self._rolled_back:
            return ConnectorStatus.OK, "rolled_back", ""
        if "status" in item and item["status"] is not None:
            return ConnectorStatus.OK, _map_github_deployment_state(str(item["status"])), ""

        statuses_payload: Any = item.get("statuses")
        if statuses_payload is None:
            raw_id = item.get("id")
            statuses_url = str(
                item.get("statuses_url")
                or f"{self._base_url}/repos/{repo}/deployments/{raw_id}/statuses"
            )
            code, statuses_payload = self._get_json_with_retry(statuses_url, {})
            if code in {401, 403}:
                return (
                    ConnectorStatus.UNAUTHORIZED,
                    "unknown",
                    "GitHub deployment statuses permission denied.",
                )
            if code != 200:
                err_msg = (
                    statuses_payload.get("error", f"HTTP {code}")
                    if isinstance(statuses_payload, dict)
                    else f"HTTP {code}"
                )
                return ConnectorStatus.CONNECTOR_UNAVAILABLE, "unknown", str(err_msg)

        statuses = (
            statuses_payload
            if isinstance(statuses_payload, list)
            else statuses_payload.get("statuses", []) if isinstance(statuses_payload, dict) else []
        )
        if not statuses:
            return ConnectorStatus.OK, "pending", ""

        # GitHub returns deployment statuses newest-first, or with created_at timestamps.
        if any(isinstance(s, dict) and s.get("created_at") for s in statuses):
            latest = max(
                (s for s in statuses if isinstance(s, dict)),
                key=lambda s: _parse_github_timestamp(s.get("created_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
            )
        else:
            latest = statuses[0]
        raw_state = latest.get("state") if isinstance(latest, dict) else None
        return ConnectorStatus.OK, _map_github_deployment_state(raw_state), ""

    def _fetch_deployments(
        self, service: str, environment: str
    ) -> tuple[ConnectorStatus, list[Release], str]:
        repo = self._service_repos.get(service.lower())
        if repo is None:
            return (
                ConnectorStatus.UNAUTHORIZED,
                [],
                f"Service '{service}' has no authorized GitHub repository.",
            )
        url = f"{self._base_url}/repos/{repo}/deployments"
        params = {"environment": environment}
        code, payload = self._get_json_with_retry(url, params)

        if code in {401, 403}:
            return ConnectorStatus.UNAUTHORIZED, [], "GitHub Deployments permission denied."
        if code != 200:
            err_msg = payload.get("error", f"HTTP {code}") if isinstance(payload, dict) else f"HTTP {code}"
            return (
                ConnectorStatus.CONNECTOR_UNAVAILABLE,
                [],
                str(err_msg),
            )

        raw_list = payload.get("deployments", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_list, list):
            return ConnectorStatus.CONNECTOR_UNAVAILABLE, [], "Unexpected GitHub deployments response."

        releases: list[Release] = []
        for item in raw_list:
            dep_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            rel_id = str(
                item.get("release_id")
                or dep_payload.get("release_id")
                or item.get("id")
                or ""
            )
            deployed_dt = _parse_github_timestamp(
                item.get("deployed_at") or item.get("created_at") or item.get("updated_at")
            )
            if not rel_id or deployed_dt is None:
                continue
            status_conn, status, status_note = self._resolve_deployment_status(repo, item, rel_id)
            if status_conn != ConnectorStatus.OK:
                return status_conn, [], status_note
            releases.append(
                Release(
                    release_id=rel_id,
                    service=service,
                    environment=str(item.get("environment") or environment),
                    deployed_at=deployed_dt,
                    previous_sha=str(
                        item.get("previous_sha") or dep_payload.get("previous_sha") or ""
                    ),
                    deployed_sha=str(item.get("sha") or item.get("deployed_sha") or ""),
                    repository=repo,
                    status=status,
                    cohort=str(item.get("cohort") or dep_payload.get("cohort") or "100%"),
                    summary=str(item.get("description") or item.get("summary") or ""),
                )
            )
        return ConnectorStatus.OK, releases, ""

    def resolve_active_releases(
        self,
        service: str,
        environment: str,
        at_time: datetime,
    ) -> ReleaseResolution:
        status, releases, note = self._fetch_deployments(service, environment)
        if status != ConnectorStatus.OK:
            return ReleaseResolution(
                status=status,
                service=service,
                environment=environment,
                resolved_at=at_time,
                note=note,
            )
        candidates = [
            r
            for r in releases
            if r.environment.lower() == environment.lower()
            and r.deployed_at <= at_time
            and r.status in {"active", "canary"}
        ]
        candidates.sort(key=lambda r: r.deployed_at, reverse=True)
        if not candidates:
            return ReleaseResolution(
                status=ConnectorStatus.NO_RESULTS,
                service=service,
                environment=environment,
                resolved_at=at_time,
                note=f"No active GitHub deployments for '{service}' in '{environment}' at {at_time.isoformat()}.",
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
        status, releases, note = self._fetch_deployments(service, environment)
        if status != ConnectorStatus.OK:
            return ReleaseResolution(
                status=status,
                service=service,
                environment=environment,
                resolved_at=at_time,
                note=note,
            )
        since = at_time - timedelta(hours=hours)
        candidates = [
            r
            for r in releases
            if r.environment.lower() == environment.lower()
            and since <= r.deployed_at <= at_time
        ]
        candidates.sort(key=lambda r: r.deployed_at, reverse=True)
        return ReleaseResolution(
            status=ConnectorStatus.OK if candidates else ConnectorStatus.NO_RESULTS,
            service=service,
            environment=environment,
            resolved_at=at_time,
            releases=candidates,
            note="" if candidates else f"No deployments in the last {hours}h.",
        )


class GitHubRepositoryAdapter:
    """Read-only GitHub Commits and Contents adapter scoped to authorized repositories."""

    def __init__(
        self,
        authorized_repositories: set[str],
        *,
        api_base_url: str = "https://api.github.com",
        auth_token: str | None = None,
        http_get: HttpGetFn | None = None,
        max_patch_lines: int = 80,
    ) -> None:
        self._authorized = set(authorized_repositories)
        self._base_url = api_base_url.rstrip("/")
        self._auth_token = auth_token
        self._http_get = http_get or _default_http_get
        self._max_patch_lines = max_patch_lines

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def compare_revisions(
        self,
        repository: str,
        base_sha: str,
        target_sha: str,
        path: str | None = None,
    ) -> CodeDiff:
        if repository not in self._authorized:
            return CodeDiff(
                status=ConnectorStatus.UNAUTHORIZED,
                repository=repository,
                base_sha=base_sha,
                target_sha=target_sha,
                note=f"Repository '{repository}' is not authorized.",
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

        url = f"{self._base_url}/repos/{repository}/compare/{base_sha}...{target_sha}"
        code, payload = self._http_get(url, {}, self._headers())
        if code in {401, 403}:
            return CodeDiff(
                status=ConnectorStatus.UNAUTHORIZED,
                repository=repository,
                base_sha=base_sha,
                target_sha=target_sha,
                note="GitHub compare permission denied.",
            )
        if code != 200:
            return CodeDiff(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                repository=repository,
                base_sha=base_sha,
                target_sha=target_sha,
                note=str(payload.get("error", f"HTTP {code}")),
            )

        raw_files: list[dict[str, Any]] = payload.get("files", [])
        diff_files: list[DiffFile] = []
        for f in raw_files:
            filename = str(f.get("filename", ""))
            if path is not None and filename != path:
                continue
            patch_lines = str(f.get("patch", "")).splitlines()
            truncated = len(patch_lines) > self._max_patch_lines
            kept = patch_lines[: self._max_patch_lines]
            if truncated:
                kept.append("... [patch truncated; call read_snippet for full context]")
            diff_files.append(
                DiffFile(path=filename, patch="\n".join(kept), truncated=truncated)
            )

        if not diff_files:
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
            files=diff_files,
        )

    def read_snippet(
        self,
        repository: str,
        sha: str,
        path: str,
        start_line: int = 1,
        end_line: int = 80,
    ) -> CodeSnippet:
        if repository not in self._authorized or not _SAFE_SHA_PATTERN.match(sha) or not _validate_relative_path(path):
            return CodeSnippet(
                status=ConnectorStatus.UNAUTHORIZED,
                repository=repository,
                sha=sha,
                path=path,
                start_line=start_line,
                end_line=end_line,
                content="",
                note="Unauthorized repository, SHA, or path.",
            )
        url = f"{self._base_url}/repos/{repository}/contents/{path}"
        code, payload = self._http_get(url, {"ref": sha}, self._headers())
        if code in {401, 403}:
            return CodeSnippet(
                status=ConnectorStatus.UNAUTHORIZED,
                repository=repository,
                sha=sha,
                path=path,
                start_line=start_line,
                end_line=end_line,
                content="",
                note="GitHub contents permission denied.",
            )
        if code == 404:
            return CodeSnippet(
                status=ConnectorStatus.NO_RESULTS,
                repository=repository,
                sha=sha,
                path=path,
                start_line=start_line,
                end_line=end_line,
                content="",
                note=f"File '{path}' not found at {sha}.",
            )
        if code != 200:
            return CodeSnippet(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                repository=repository,
                sha=sha,
                path=path,
                start_line=start_line,
                end_line=end_line,
                content="",
                note=str(payload.get("error", f"HTTP {code}")),
            )

        raw_content = payload.get("content", "")
        if payload.get("encoding") == "base64":
            decoded = base64.b64decode(raw_content).decode("utf-8")
        else:
            decoded = str(raw_content)

        lines = decoded.splitlines()
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

