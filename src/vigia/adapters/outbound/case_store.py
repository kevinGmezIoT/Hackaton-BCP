"""Transactional SQLite case store adapter implementing CaseStorePort."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vigia.domain.contracts import (
    AlertEnvelope,
    AlertIngestionResult,
    AlertIngestOutcome,
    AlertState,
    AuditRecord,
    CaseEvent,
    CaseRecord,
    CaseRevision,
    CaseStatus,
    ConversationIntent,
    ConversationTurn,
    DecisionChoice,
    EventType,
    ExecutionResult,
    ExternalOperationRecord,
    Finding,
    FollowUpCandidate,
    HumanDecision,
    OperationalMode,
    PendingWorkItem,
    ProposalHistoryEntry,
    ProposalStatus,
    TurnStatus,
)


class CaseVersionConflictError(RuntimeError):
    """Raised when optimistic concurrency control detects a stale case version."""


class ChatMessageConflictError(RuntimeError):
    """Raised when a repeated client_message_id carries different content."""


class StaleRevisionError(RuntimeError):
    """Raised when a reevaluation targets a superseded diagnostic revision."""

    def __init__(
        self,
        message: str,
        *,
        active_revision: int,
        active_proposal_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.active_revision = active_revision
        self.active_proposal_id = active_proposal_id


def _canonical_chat_hash(question: str, base_revision: int | None, proposal_id: str | None) -> str:
    payload = {
        "question": question.strip(),
        "base_revision": base_revision,
        "proposal_id": proposal_id,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _backfill_case_history(case: CaseRecord) -> None:
    if case.proposal is not None and not case.proposal_history:
        case.proposal_history.append(
            ProposalHistoryEntry(
                proposal_id=case.proposal.proposal_id,
                revision_number=case.proposal.revision_number,
                version=case.proposal.version,
                action=case.proposal.action,
                target_service=case.proposal.target_service,
                target_release_id=case.proposal.target_release_id,
                status=case.proposal.status,
                hypothesis=case.proposal.finding.hypothesis,
                justification=case.proposal.justification,
                evidence_ids=list(case.proposal.evidence_ids),
                outcome="initial",
                created_at=case.proposal.created_at,
                updated_at=case.updated_at,
            )
        )


def derive_episode_id(envelope: AlertEnvelope, canonical_service: str) -> str:
    """Derive a stable episode identifier when the monitor does not supply one."""
    if envelope.episode_id and envelope.episode_id.strip():
        return envelope.episode_id.strip()
    obs_utc = envelope.alert.observed_at.astimezone(timezone.utc)
    bucket = obs_utc.strftime("%Y%m%d-%H")
    return f"{canonical_service}-{envelope.alert.metric}-{bucket}"


class SqliteCaseStoreAdapter:
    """ACID transactional SQLite store for cases, alert deduplication, events, and work queue."""

    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    episode_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    cutoff_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cases_episode_key ON cases(episode_key);

                CREATE TABLE IF NOT EXISTS ingested_events (
                    source TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY (source, event_id)
                );

                CREATE TABLE IF NOT EXISTS case_events (
                    sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_case_events_case ON case_events(case_id, sequence_id);

                CREATE TABLE IF NOT EXISTS audit_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    record_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS work_queue (
                    work_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    work_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_step_index INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_turns (
                    turn_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    client_message_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(case_id, actor, client_message_id)
                );

                CREATE TABLE IF NOT EXISTS operation_reservations (
                    operation_key TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL,
                    proposal_version INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    status TEXT NOT NULL,
                    operation_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS counters (
                    name TEXT PRIMARY KEY,
                    val INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO counters(name, val) VALUES ('case_seq', 1041);
                INSERT OR IGNORE INTO counters(name, val) VALUES ('work_seq', 0);
                INSERT OR IGNORE INTO counters(name, val) VALUES ('turn_seq', 0);
                """
            )
            cols = {
                str(r["name"])
                for r in self._conn.execute("PRAGMA table_info(work_queue)").fetchall()
            }
            if "payload_json" not in cols:
                self._conn.execute(
                    "ALTER TABLE work_queue ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "lease_owner" not in cols:
                self._conn.execute("ALTER TABLE work_queue ADD COLUMN lease_owner TEXT")
            if "lease_expires_at" not in cols:
                self._conn.execute("ALTER TABLE work_queue ADD COLUMN lease_expires_at TEXT")

    def _next_counter(self, name: str) -> int:
        self._conn.execute("UPDATE counters SET val = val + 1 WHERE name = ?", (name,))
        row = self._conn.execute("SELECT val FROM counters WHERE name = ?", (name,)).fetchone()
        assert row is not None
        return int(row["val"])

    def _append_event_in_tx(self, case: CaseRecord, event: CaseEvent) -> CaseEvent:
        cur = self._conn.execute(
            "INSERT INTO case_events(case_id, timestamp, event_json) VALUES (?, ?, ?)",
            (case.case_id, event.timestamp.isoformat(), event.model_dump_json()),
        )
        seq_id = int(cur.lastrowid or 0)
        numbered_event = event.model_copy(update={"sequence_id": seq_id})
        self._conn.execute(
            "UPDATE case_events SET event_json = ? WHERE sequence_id = ?",
            (numbered_event.model_dump_json(), seq_id),
        )
        if case.events and case.events[-1] is event:
            case.events[-1] = numbered_event
        else:
            replaced = False
            for idx in range(len(case.events) - 1, -1, -1):
                if case.events[idx].sequence_id == 0 and (
                    case.events[idx].summary == event.summary
                    and case.events[idx].event_type == event.event_type
                ):
                    case.events[idx] = numbered_event
                    replaced = True
                    break
            if not replaced:
                case.events.append(numbered_event)
        return numbered_event

    def ingest_alert_transactional(
        self,
        *,
        envelope: AlertEnvelope,
        canonical_service: str,
        service_config_version: str,
        mode: OperationalMode,
        now: datetime,
        re_evaluate_on_update: bool = True,
    ) -> tuple[CaseRecord, AlertIngestionResult, PendingWorkItem | None]:
        """Atomically persist an incoming alert envelope, correlate episode, and enqueue work."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT case_id, episode_id FROM ingested_events WHERE source = ? AND event_id = ?",
                    (envelope.source, envelope.event_id),
                ).fetchone()
                if existing is not None:
                    case_id = str(existing["case_id"])
                    episode_id = str(existing["episode_id"])
                    case = self._load_case_in_tx(case_id)
                    assert case is not None
                    rev_num = case.revisions[-1].revision_number if case.revisions else 1
                    self._conn.execute("COMMIT")
                    return (
                        case,
                        AlertIngestionResult(
                            case_id=case_id,
                            episode_id=episode_id,
                            event_id=envelope.event_id,
                            outcome=AlertIngestOutcome.DUPLICATE,
                            revision_number=rev_num,
                            status_code=202,
                        ),
                        None,
                    )

                episode_id = derive_episode_id(envelope, canonical_service)
                episode_key = (
                    f"{envelope.source}:{canonical_service}:{envelope.alert.environment.lower()}:{episode_id}"
                )

                active_row = self._conn.execute(
                    """
                    SELECT case_id FROM cases
                    WHERE episode_key = ? AND status != ?
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (episode_key, CaseStatus.CLOSED.value),
                ).fetchone()

                normalized_alert = envelope.alert.model_copy(
                    update={"service": canonical_service}
                )
                normalized_env = envelope.model_copy(
                    update={"episode_id": episode_id, "alert": normalized_alert}
                )

                if active_row is not None:
                    case = self._load_case_in_tx(str(active_row["case_id"]))
                    assert case is not None
                    case.received_events.append(normalized_env)
                    case.updated_at = now
                    case.version += 1
                    threshold = (
                        normalized_alert.threshold_value
                        or case.alert.threshold_value
                        or 800.0
                    )
                    if (
                        normalized_alert.current_value is not None
                        and normalized_alert.current_value >= threshold
                    ):
                        case.crossed_threshold = True
                        if case.threshold_crossed_at is None:
                            case.threshold_crossed_at = normalized_alert.observed_at

                    if envelope.state == AlertState.RESOLVED:
                        ev = CaseEvent(
                            event_type=EventType.ALERT,
                            case_id=case.case_id,
                            timestamp=normalized_alert.observed_at,
                            role="Monitor",
                            summary=(
                                f"Monitor '{envelope.source}' reported state=resolved for "
                                f"episode '{episode_id}'."
                            ),
                            payload={"event_id": envelope.event_id, "state": "resolved"},
                        )
                        self._append_event_in_tx(case, ev)
                        self._insert_ingested_in_tx(envelope, case.case_id, episode_id, now)
                        self._save_case_row_in_tx(case, episode_key)
                        self._conn.execute("COMMIT")
                        rev_num = case.revisions[-1].revision_number if case.revisions else 1
                        return (
                            case,
                            AlertIngestionResult(
                                case_id=case.case_id,
                                episode_id=episode_id,
                                event_id=envelope.event_id,
                                outcome=AlertIngestOutcome.RESOLVED_RECORDED,
                                revision_number=rev_num,
                                status_code=202,
                            ),
                            None,
                        )

                    new_cutoff = normalized_alert.effective_cutoff
                    prev_rev = case.revisions[-1] if case.revisions else None
                    prev_cutoff = prev_rev.cutoff_at if prev_rev else case.alert.effective_cutoff
                    pending_work: PendingWorkItem | None = None
                    rev_num = prev_rev.revision_number if prev_rev else 1

                    if re_evaluate_on_update and new_cutoff > prev_cutoff:
                        rev_num += 1
                        new_rev = CaseRevision(
                            revision_number=rev_num,
                            cutoff_at=new_cutoff,
                            created_at=now,
                            reason=f"episode_update:{envelope.event_id}",
                        )
                        case.revisions.append(new_rev)
                        work_seq = self._next_counter("work_seq")
                        pending_work = PendingWorkItem(
                            work_id=f"WRK-{work_seq:04d}",
                            case_id=case.case_id,
                            revision_number=rev_num,
                            work_type="investigate",
                            status="pending",
                            created_at=now,
                            updated_at=now,
                        )
                        self._insert_work_in_tx(pending_work)

                    ev = CaseEvent(
                        event_type=EventType.ALERT,
                        case_id=case.case_id,
                        timestamp=normalized_alert.observed_at,
                        role="Monitor",
                        summary=(
                            f"Merged alert sample '{envelope.event_id}' into episode '{episode_id}' "
                            f"(value={normalized_alert.current_value}, revision={rev_num})."
                        ),
                        payload={
                            "event_id": envelope.event_id,
                            "episode_id": episode_id,
                            "revision_number": rev_num,
                        },
                    )
                    self._append_event_in_tx(case, ev)
                    self._insert_ingested_in_tx(envelope, case.case_id, episode_id, now)
                    self._save_case_row_in_tx(case, episode_key)
                    self._conn.execute("COMMIT")
                    return (
                        case,
                        AlertIngestionResult(
                            case_id=case.case_id,
                            episode_id=episode_id,
                            event_id=envelope.event_id,
                            outcome=AlertIngestOutcome.MERGED,
                            revision_number=rev_num,
                            status_code=202,
                        ),
                        pending_work,
                    )

                seq = self._next_counter("case_seq")
                case_id = normalized_alert.case_id or f"CASE-{seq}"
                alert_with_id = normalized_alert.model_copy(update={"case_id": case_id})
                normalized_env = normalized_env.model_copy(update={"alert": alert_with_id})
                initial_rev = CaseRevision(
                    revision_number=1,
                    cutoff_at=alert_with_id.effective_cutoff,
                    created_at=now,
                    reason=f"initial_alert:{envelope.event_id}",
                )
                threshold = alert_with_id.threshold_value or 800.0
                crossed = (
                    alert_with_id.current_value is not None
                    and alert_with_id.current_value >= threshold
                )
                case = CaseRecord(
                    case_id=case_id,
                    version=1,
                    mode=mode,
                    service_config_version=service_config_version,
                    episode_id=episode_id,
                    alert=alert_with_id,
                    status=CaseStatus.OPEN,
                    created_at=now,
                    updated_at=now,
                    received_events=[normalized_env],
                    revisions=[initial_rev],
                    crossed_threshold=crossed,
                    threshold_crossed_at=alert_with_id.observed_at if crossed else None,
                )
                ev = CaseEvent(
                    event_type=EventType.ALERT,
                    case_id=case_id,
                    timestamp=alert_with_id.observed_at,
                    role="Monitor",
                    summary=(
                        f"Alert '{envelope.event_id}' opened case '{case_id}' for "
                        f"{canonical_service} ({alert_with_id.environment})."
                    ),
                    payload={"event_id": envelope.event_id, "episode_id": episode_id},
                )
                self._append_event_in_tx(case, ev)

                work_seq = self._next_counter("work_seq")
                pending_work = PendingWorkItem(
                    work_id=f"WRK-{work_seq:04d}",
                    case_id=case_id,
                    revision_number=1,
                    work_type="investigate",
                    status="pending",
                    created_at=now,
                    updated_at=now,
                )
                self._insert_work_in_tx(pending_work)
                self._insert_ingested_in_tx(envelope, case_id, episode_id, now)
                self._save_case_row_in_tx(case, episode_key)
                self._conn.execute("COMMIT")
                return (
                    case,
                    AlertIngestionResult(
                        case_id=case_id,
                        episode_id=episode_id,
                        event_id=envelope.event_id,
                        outcome=AlertIngestOutcome.CREATED,
                        revision_number=1,
                        status_code=202,
                    ),
                    pending_work,
                )
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _insert_ingested_in_tx(
        self,
        envelope: AlertEnvelope,
        case_id: str,
        episode_id: str,
        now: datetime,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO ingested_events(source, event_id, case_id, episode_id, state, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                envelope.source,
                envelope.event_id,
                case_id,
                episode_id,
                envelope.state.value,
                now.isoformat(),
            ),
        )

    def _insert_work_in_tx(self, item: PendingWorkItem) -> None:
        self._conn.execute(
            """
            INSERT INTO work_queue(work_id, case_id, revision_number, work_type, status, last_step_index, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.work_id,
                item.case_id,
                item.revision_number,
                item.work_type,
                item.status,
                item.last_step_index,
                json.dumps(item.payload),
                item.created_at.isoformat(),
                item.updated_at.isoformat(),
            ),
        )

    def _save_case_row_in_tx(self, case: CaseRecord, episode_key: str | None = None) -> None:
        if episode_key is None:
            row = self._conn.execute(
                "SELECT episode_key FROM cases WHERE case_id = ?", (case.case_id,)
            ).fetchone()
            episode_key = (
                str(row["episode_key"])
                if row is not None
                else f"direct:{case.alert.service}:{case.alert.environment}:{case.case_id}"
            )
        self._conn.execute(
            """
            INSERT INTO cases(case_id, episode_key, status, mode, version, cutoff_at, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                status = excluded.status,
                mode = excluded.mode,
                version = excluded.version,
                cutoff_at = excluded.cutoff_at,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                case.case_id,
                episode_key,
                case.status.value,
                case.mode.value,
                case.version,
                case.alert.effective_cutoff.isoformat(),
                case.model_dump_json(),
                case.updated_at.isoformat(),
            ),
        )

    def _load_case_in_tx(self, case_id: str) -> CaseRecord | None:
        row = self._conn.execute(
            "SELECT payload_json FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            return None
        case = CaseRecord.model_validate_json(str(row["payload_json"]))
        _backfill_case_history(case)
        return case

    def save_case(
        self,
        case: CaseRecord,
        *,
        audit_records: list[AuditRecord] | None = None,
        new_events: list[CaseEvent] | None = None,
        pending_work: PendingWorkItem | None = None,
        expected_version: int | None = None,
    ) -> CaseRecord:
        """Persist a case update along with its events, audit records, and optional work item in one transaction."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing_row = self._conn.execute(
                    "SELECT version FROM cases WHERE case_id = ?", (case.case_id,)
                ).fetchone()
                if expected_version is not None and existing_row is not None:
                    db_version = int(existing_row["version"])
                    if db_version != expected_version:
                        raise CaseVersionConflictError(
                            f"Version conflict saving case '{case.case_id}': "
                            f"expected version {expected_version}, found {db_version}."
                        )
                if existing_row is not None:
                    case.version = int(existing_row["version"]) + 1

                for ev in new_events or []:
                    self._append_event_in_tx(case, ev)

                for idx, ev in enumerate(case.events):
                    if ev.sequence_id == 0:
                        cur = self._conn.execute(
                            "INSERT INTO case_events(case_id, timestamp, event_json) VALUES (?, ?, ?)",
                            (case.case_id, ev.timestamp.isoformat(), ev.model_dump_json()),
                        )
                        seq_id = int(cur.lastrowid or 0)
                        numbered = ev.model_copy(update={"sequence_id": seq_id})
                        self._conn.execute(
                            "UPDATE case_events SET event_json = ? WHERE sequence_id = ?",
                            (numbered.model_dump_json(), seq_id),
                        )
                        case.events[idx] = numbered

                for rec in audit_records or []:
                    self._conn.execute(
                        "INSERT INTO audit_records(case_id, timestamp, record_json) VALUES (?, ?, ?)",
                        (rec.case_id, rec.timestamp.isoformat(), rec.model_dump_json()),
                    )

                if pending_work is not None:
                    self._insert_work_in_tx(pending_work)

                self._save_case_row_in_tx(case)
                self._conn.execute("COMMIT")
                return case
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_case(self, case_id: str) -> CaseRecord | None:
        with self._lock:
            return self._load_case_in_tx(case_id)

    def list_cases(self) -> list[CaseRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM cases ORDER BY rowid ASC"
            ).fetchall()
            cases = [CaseRecord.model_validate_json(str(r["payload_json"])) for r in rows]
            for c in cases:
                _backfill_case_history(c)
            return cases

    def list_events(self, case_id: str, after_sequence_id: int = 0) -> list[CaseEvent]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT event_json FROM case_events
                WHERE case_id = ? AND sequence_id > ?
                ORDER BY sequence_id ASC
                """,
                (case_id, after_sequence_id),
            ).fetchall()
            return [CaseEvent.model_validate_json(str(r["event_json"])) for r in rows]

    def claim_next_work(
        self,
        case_id: str | None = None,
        *,
        worker_id: str = "default-worker",
        lease_seconds: int = 120,
        now: datetime | None = None,
    ) -> PendingWorkItem | None:
        """Claim a pending or expired-lease work item atomically with a worker lease."""
        ref_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_iso = ref_now.isoformat()
        lease_expires = (ref_now + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if case_id is not None:
                    row = self._conn.execute(
                        """
                        SELECT * FROM work_queue
                        WHERE case_id = ?
                          AND (
                            status = 'pending'
                            OR (
                              status = 'in_progress'
                              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                            )
                          )
                        ORDER BY created_at ASC LIMIT 1
                        """,
                        (case_id, now_iso),
                    ).fetchone()
                else:
                    row = self._conn.execute(
                        """
                        SELECT * FROM work_queue
                        WHERE (
                            status = 'pending'
                            OR (
                              status = 'in_progress'
                              AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                            )
                        )
                        ORDER BY created_at ASC LIMIT 1
                        """,
                        (now_iso,),
                    ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                self._conn.execute(
                    """
                    UPDATE work_queue
                    SET status = 'in_progress',
                        lease_owner = ?,
                        lease_expires_at = ?,
                        updated_at = ?
                    WHERE work_id = ?
                    """,
                    (worker_id, lease_expires, now_iso, str(row["work_id"])),
                )
                self._conn.execute("COMMIT")
                payload_raw = row["payload_json"] if "payload_json" in row.keys() else "{}"
                return PendingWorkItem(
                    work_id=str(row["work_id"]),
                    case_id=str(row["case_id"]),
                    revision_number=int(row["revision_number"]),
                    work_type=str(row["work_type"]),
                    status="in_progress",
                    last_step_index=int(row["last_step_index"]),
                    lease_owner=worker_id,
                    lease_expires_at=datetime.fromisoformat(lease_expires),
                    payload=json.loads(str(payload_raw or "{}")),
                    created_at=datetime.fromisoformat(str(row["created_at"])),
                    updated_at=datetime.fromisoformat(now_iso),
                )
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def complete_work(
        self,
        work_id: str,
        *,
        last_step_index: int = 0,
        status: str = "completed",
        worker_id: str | None = None,
    ) -> None:
        with self._lock:
            now_iso = datetime.now(timezone.utc).isoformat()
            if worker_id is not None:
                self._conn.execute(
                    """
                    UPDATE work_queue
                    SET status = ?, last_step_index = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE work_id = ? AND (lease_owner IS NULL OR lease_owner = ?)
                    """,
                    (status, last_step_index, now_iso, work_id, worker_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE work_queue
                    SET status = ?, last_step_index = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE work_id = ?
                    """,
                    (status, last_step_index, now_iso, work_id),
                )

    def _upsert_proposal_history_in_tx(
        self,
        case: CaseRecord,
        *,
        proposal_id: str,
        revision_number: int,
        version: int,
        status: ProposalStatus,
        outcome: str,
        turn_id: str | None,
        now: datetime,
    ) -> None:
        for entry in case.proposal_history:
            if (
                entry.proposal_id == proposal_id
                and entry.revision_number == revision_number
                and entry.version == version
            ):
                entry.status = status
                entry.outcome = outcome
                entry.turn_id = turn_id or entry.turn_id
                entry.updated_at = now
                return
        assert case.proposal is not None
        case.proposal_history.append(
            ProposalHistoryEntry(
                proposal_id=proposal_id,
                revision_number=revision_number,
                version=version,
                action=case.proposal.action,
                target_service=case.proposal.target_service,
                target_release_id=case.proposal.target_release_id,
                status=status,
                hypothesis=case.proposal.finding.hypothesis,
                justification=case.proposal.justification,
                evidence_ids=list(case.proposal.evidence_ids),
                outcome=outcome,
                turn_id=turn_id,
                created_at=case.proposal.created_at,
                updated_at=now,
            )
        )

    def _save_turn_row_in_tx(self, turn: ConversationTurn, request_hash: str) -> None:
        self._conn.execute(
            """
            INSERT INTO conversation_turns(
                turn_id, case_id, actor, client_message_id, request_hash, status, payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(turn_id) DO UPDATE SET
                status = excluded.status,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                turn.turn_id,
                turn.case_id,
                turn.actor,
                turn.client_message_id,
                request_hash,
                turn.status.value,
                turn.model_dump_json(),
                turn.created_at.isoformat(),
                turn.updated_at.isoformat(),
            ),
        )

    def create_chat_turn_transactional(
        self,
        *,
        case_id: str,
        client_message_id: str,
        actor: str,
        question: str,
        base_revision: int | None,
        proposal_id: str | None,
        intent: str,
        re_evaluate: bool,
        cutoff_at: datetime,
        user_hypothesis: str | None,
        now: datetime,
        immediate_answer: str | None = None,
        immediate_outcome: str | None = None,
        immediate_evidence_ids: list[str] | None = None,
    ) -> tuple[CaseRecord, ConversationTurn, PendingWorkItem | None, bool]:
        """Atomically persist an operator chat turn, enforce revision currency, and lock active proposals."""
        req_hash = _canonical_chat_hash(question, base_revision, proposal_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    """
                    SELECT request_hash, payload_json FROM conversation_turns
                    WHERE case_id = ? AND actor = ? AND client_message_id = ?
                    """,
                    (case_id, actor, client_message_id),
                ).fetchone()
                if existing is not None:
                    if str(existing["request_hash"]) != req_hash:
                        raise ChatMessageConflictError(
                            f"Message ID '{client_message_id}' was already used with different content "
                            f"for case '{case_id}' by '{actor}'."
                        )
                    case = self._load_case_in_tx(case_id)
                    assert case is not None
                    turn = ConversationTurn.model_validate_json(str(existing["payload_json"]))
                    self._conn.execute("COMMIT")
                    return case, turn, None, True

                case = self._load_case_in_tx(case_id)
                if case is None:
                    raise KeyError(f"Case '{case_id}' was not found.")

                current_rev = case.revisions[-1].revision_number if case.revisions else 1
                req_rev = base_revision if base_revision is not None else current_rev

                if re_evaluate and req_rev != current_rev:
                    raise StaleRevisionError(
                        f"Follow-up reevaluation targets revision {req_rev}, but active revision is {current_rev}.",
                        active_revision=current_rev,
                        active_proposal_id=case.proposal.proposal_id if case.proposal else None,
                    )

                if re_evaluate and proposal_id is not None:
                    active_prop_id = case.proposal.proposal_id if case.proposal else None
                    if active_prop_id != proposal_id:
                        raise StaleRevisionError(
                            f"Proposal '{proposal_id}' is no longer the active proposal on revision {current_rev}.",
                            active_revision=current_rev,
                            active_proposal_id=active_prop_id,
                        )

                turn_seq = self._next_counter("turn_seq")
                turn_id = f"TURN-{case_id}-{turn_seq:03d}"

                if (
                    re_evaluate
                    and case.proposal is not None
                    and case.proposal.status in {ProposalStatus.PENDING, ProposalStatus.RECOMMENDED}
                ):
                    case.proposal.status = ProposalStatus.UNDER_REVIEW
                    case.status = CaseStatus.INVESTIGATING
                    self._upsert_proposal_history_in_tx(
                        case,
                        proposal_id=case.proposal.proposal_id,
                        revision_number=case.proposal.revision_number,
                        version=case.proposal.version,
                        status=ProposalStatus.UNDER_REVIEW,
                        outcome="under_review",
                        turn_id=turn_id,
                        now=now,
                    )

                is_immediate = immediate_answer is not None
                turn = ConversationTurn(
                    turn_id=turn_id,
                    case_id=case_id,
                    client_message_id=client_message_id,
                    actor=actor,
                    question=question,
                    base_revision=req_rev,
                    proposal_id=proposal_id or (case.proposal.proposal_id if case.proposal else None),
                    cutoff_at=cutoff_at,
                    intent=ConversationIntent(intent),
                    status=TurnStatus.COMPLETED if is_immediate else TurnStatus.PENDING,
                    user_hypothesis=user_hypothesis,
                    referenced_evidence_ids=list(immediate_evidence_ids or []),
                    answer=immediate_answer or "",
                    result_outcome=immediate_outcome or ("queued" if not is_immediate else "answered"),
                    published_revision=current_rev if is_immediate else None,
                    work_summary=(
                        immediate_answer[:240]
                        if immediate_answer
                        else f"Queued read-only follow-up for revision {req_rev}."
                    ),
                    created_at=now,
                    updated_at=now,
                )
                case.conversation_turns.append(turn)
                case.version += 1
                case.updated_at = now

                pending_work: PendingWorkItem | None = None
                if not is_immediate:
                    work_seq = self._next_counter("work_seq")
                    pending_work = PendingWorkItem(
                        work_id=f"WRK-{work_seq:04d}",
                        case_id=case_id,
                        revision_number=req_rev,
                        work_type="follow_up",
                        status="pending",
                        payload={"turn_id": turn_id},
                        created_at=now,
                        updated_at=now,
                    )
                    self._insert_work_in_tx(pending_work)

                ev = CaseEvent(
                    event_type=EventType.CONVERSATION,
                    case_id=case_id,
                    timestamp=now,
                    role="Operator" if not is_immediate else "Investigator",
                    summary=f"[{turn_id}] {question[:140]}",
                    references=turn.referenced_evidence_ids,
                    payload={
                        "turn_id": turn_id,
                        "intent": turn.intent.value,
                        "status": turn.status.value,
                        "base_revision": req_rev,
                        "outcome": turn.result_outcome,
                    },
                )
                self._append_event_in_tx(case, ev)
                self._save_turn_row_in_tx(turn, req_hash)
                self._save_case_row_in_tx(case)
                self._conn.execute("COMMIT")
                return case, turn, pending_work, False
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def list_turns(self, case_id: str) -> list[ConversationTurn]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM conversation_turns WHERE case_id = ? ORDER BY rowid ASC",
                (case_id,),
            ).fetchall()
            return [ConversationTurn.model_validate_json(str(r["payload_json"])) for r in rows]

    def _merge_follow_up_evidence_in_tx(
        self,
        case: CaseRecord,
        snapshot: CaseRecord,
        new_rev_num: int,
    ) -> list[str]:
        existing_ids = {ev.evidence_id for ev in case.evidence}
        added_ids: list[str] = []
        for ev in snapshot.evidence:
            if ev.evidence_id not in existing_ids:
                copied = ev.model_copy(update={"revision_number": new_rev_num})
                case.evidence.append(copied)
                existing_ids.add(copied.evidence_id)
                added_ids.append(copied.evidence_id)
        for gap in snapshot.connector_gaps:
            if gap not in case.connector_gaps:
                case.connector_gaps.append(gap)
        for event in snapshot.events:
            if event.sequence_id == 0 and event.event_type == EventType.STEP:
                copied_event = event.model_copy(update={
                    "payload": {**event.payload, "revision_number": new_rev_num}
                })
                self._append_event_in_tx(case, copied_event)
        return added_ids

    def publish_follow_up_transactional(
        self,
        *,
        case_id: str,
        turn_id: str,
        base_revision: int,
        candidate: FollowUpCandidate,
        updated_case_snapshot: CaseRecord,
        now: datetime,
    ) -> tuple[CaseRecord, ConversationTurn]:
        """Atomically publish a validated follow-up revision only if no newer revision or approval won."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                case = self._load_case_in_tx(case_id)
                if case is None:
                    raise KeyError(f"Case '{case_id}' was not found.")
                turn_row = self._conn.execute(
                    "SELECT request_hash, payload_json FROM conversation_turns WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if turn_row is None:
                    raise KeyError(f"Turn '{turn_id}' was not found.")
                req_hash = str(turn_row["request_hash"])
                turn = ConversationTurn.model_validate_json(str(turn_row["payload_json"]))

                current_rev = case.revisions[-1].revision_number if case.revisions else 1
                approval_won = case.decision is not None or (
                    case.proposal is not None
                    and case.proposal.status
                    in {ProposalStatus.APPROVED, ProposalStatus.EXECUTED, ProposalStatus.REJECTED}
                )

                if base_revision != current_rev or approval_won:
                    added_ids = self._merge_follow_up_evidence_in_tx(
                        case, updated_case_snapshot, current_rev
                    )
                    turn.status = TurnStatus.COMPLETED
                    turn.answer = candidate.answer
                    turn.comparison = candidate.comparison
                    turn.referenced_evidence_ids = list(
                        dict.fromkeys([*candidate.cited_evidence_ids, *added_ids])
                    )
                    turn.pending_data = list(candidate.missing_data)
                    turn.result_outcome = "superseded_by_concurrent_update"
                    turn.published_revision = current_rev
                    turn.work_summary = (
                        f"Follow-up completed after approval/newer revision {current_rev}; preserved as historical note."
                    )
                    turn.updated_at = now
                    case.conversation_turns = [
                        turn if t.turn_id == turn.turn_id else t for t in case.conversation_turns
                    ]
                    case.version += 1
                    case.updated_at = now

                    ev = CaseEvent(
                        event_type=EventType.CONVERSATION,
                        case_id=case_id,
                        timestamp=now,
                        role="Investigator",
                        summary=f"[{turn_id}] Historical follow-up recorded (active revision={current_rev}).",
                        references=turn.referenced_evidence_ids,
                        payload={
                            "turn_id": turn_id,
                            "status": turn.status.value,
                            "outcome": turn.result_outcome,
                            "revision_number": current_rev,
                        },
                    )
                    self._append_event_in_tx(case, ev)
                    self._save_turn_row_in_tx(turn, req_hash)
                    self._save_case_row_in_tx(case)
                    self._conn.execute("COMMIT")
                    return case, turn

                new_rev_num = current_rev + 1
                added_ids = self._merge_follow_up_evidence_in_tx(
                    case, updated_case_snapshot, new_rev_num
                )
                all_refs = list(dict.fromkeys([*candidate.cited_evidence_ids, *added_ids]))
                is_shadow = case.mode == OperationalMode.SHADOW
                outcome = candidate.comparison_outcome

                if outcome == "maintained" and case.proposal is not None:
                    case.proposal.revision_number = new_rev_num
                    case.proposal.version += 1
                    case.proposal.status = (
                        ProposalStatus.RECOMMENDED if is_shadow else ProposalStatus.PENDING
                    )
                    case.proposal.evidence_ids = list(
                        dict.fromkeys([*case.proposal.evidence_ids, *all_refs])
                    )
                    self._upsert_proposal_history_in_tx(
                        case,
                        proposal_id=case.proposal.proposal_id,
                        revision_number=new_rev_num,
                        version=case.proposal.version,
                        status=case.proposal.status,
                        outcome="maintained",
                        turn_id=turn_id,
                        now=now,
                    )
                    case.status = (
                        CaseStatus.AWAITING_REVIEW if is_shadow else CaseStatus.AWAITING_APPROVAL
                    )
                elif outcome == "withdrawn":
                    if case.proposal is not None:
                        old_prop = case.proposal
                        self._upsert_proposal_history_in_tx(
                            case,
                            proposal_id=old_prop.proposal_id,
                            revision_number=old_prop.revision_number,
                            version=old_prop.version,
                            status=ProposalStatus.WITHDRAWN,
                            outcome="withdrawn",
                            turn_id=turn_id,
                            now=now,
                        )
                        case.proposal = None
                    case.partial_findings = Finding(
                        hypothesis=candidate.follow_up_hypothesis or candidate.answer[:280],
                        supporting_evidence_ids=all_refs,
                        gaps=list(case.connector_gaps),
                        exposed_services=[
                            case.alert.service,
                            *(
                                [candidate.follow_up_target_service]
                                if candidate.follow_up_target_service
                                else []
                            ),
                        ],
                    )
                    case.suggested_next_check = candidate.suggested_next_check
                    case.status = CaseStatus.OPEN
                elif outcome == "inconclusive":
                    if case.proposal is not None:
                        if candidate.affects_rollback:
                            case.proposal.status = ProposalStatus.UNDER_REVIEW
                            case.status = CaseStatus.INVESTIGATING
                        else:
                            case.proposal.status = (
                                ProposalStatus.RECOMMENDED
                                if is_shadow
                                else ProposalStatus.PENDING
                            )
                            case.status = (
                                CaseStatus.AWAITING_REVIEW
                                if is_shadow
                                else CaseStatus.AWAITING_APPROVAL
                            )
                        self._upsert_proposal_history_in_tx(
                            case,
                            proposal_id=case.proposal.proposal_id,
                            revision_number=case.proposal.revision_number,
                            version=case.proposal.version,
                            status=case.proposal.status,
                            outcome="inconclusive",
                            turn_id=turn_id,
                            now=now,
                        )
                    case.suggested_next_check = candidate.suggested_next_check
                else:
                    if case.proposal is not None and case.proposal.status == ProposalStatus.UNDER_REVIEW:
                        case.proposal.status = (
                            ProposalStatus.RECOMMENDED if is_shadow else ProposalStatus.PENDING
                        )
                        case.status = (
                            CaseStatus.AWAITING_REVIEW if is_shadow else CaseStatus.AWAITING_APPROVAL
                        )
                        self._upsert_proposal_history_in_tx(
                            case,
                            proposal_id=case.proposal.proposal_id,
                            revision_number=case.proposal.revision_number,
                            version=case.proposal.version,
                            status=case.proposal.status,
                            outcome=outcome,
                            turn_id=turn_id,
                            now=now,
                        )

                new_rev = CaseRevision(
                    revision_number=new_rev_num,
                    cutoff_at=candidate.cutoff_at,
                    created_at=now,
                    reason=f"follow_up:{turn_id}",
                    evidence_ids=all_refs,
                    proposal_id=case.proposal.proposal_id if case.proposal else None,
                    summary=candidate.answer[:280],
                    action_outcome=outcome,
                    suggested_next_check=candidate.suggested_next_check,
                    turn_id=turn_id,
                )
                case.revisions.append(new_rev)

                turn.status = TurnStatus.COMPLETED
                turn.answer = candidate.answer
                turn.comparison = candidate.comparison
                turn.referenced_evidence_ids = all_refs
                turn.published_revision = new_rev_num
                turn.pending_data = list(candidate.missing_data)
                turn.result_outcome = outcome
                turn.work_summary = candidate.answer[:280]
                turn.updated_at = now
                case.conversation_turns = [
                    turn if t.turn_id == turn.turn_id else t for t in case.conversation_turns
                ]
                case.version += 1
                case.updated_at = now

                rev_event = CaseEvent(
                    event_type=EventType.REVISION,
                    case_id=case_id,
                    timestamp=now,
                    role="Investigator",
                    summary=f"Published revision {new_rev_num} ({outcome}): {candidate.answer[:160]}",
                    references=all_refs,
                    payload={
                        "revision_number": new_rev_num,
                        "turn_id": turn_id,
                        "action_outcome": outcome,
                        "case_status": case.status.value,
                    },
                )
                conv_event = CaseEvent(
                    event_type=EventType.CONVERSATION,
                    case_id=case_id,
                    timestamp=now,
                    role="Investigator",
                    summary=f"[{turn_id}] Follow-up finished ({outcome}).",
                    references=all_refs,
                    payload={
                        "turn_id": turn_id,
                        "status": turn.status.value,
                        "outcome": outcome,
                        "revision_number": new_rev_num,
                    },
                )
                self._append_event_in_tx(case, rev_event)
                self._append_event_in_tx(case, conv_event)
                audit_rec = AuditRecord(
                    case_id=case_id,
                    timestamp=now,
                    actor="follow_up_worker",
                    action_or_tool=f"follow_up:{outcome}",
                    sanitized_args={"turn_id": turn_id, "base_revision": base_revision},
                    status=outcome,
                    summary=candidate.answer[:280],
                    references=all_refs,
                )
                self._conn.execute(
                    "INSERT INTO audit_records(case_id, timestamp, record_json) VALUES (?, ?, ?)",
                    (case_id, now.isoformat(), audit_rec.model_dump_json()),
                )
                self._save_turn_row_in_tx(turn, req_hash)
                self._save_case_row_in_tx(case)
                self._conn.execute("COMMIT")
                return case, turn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def reserve_approval_transactional(
        self,
        *,
        case_id: str,
        proposal_id: str,
        actor: str,
        decision: DecisionChoice,
        comment: str,
        idempotency_key: str | None,
        actor_groups: list[str] | None,
        proposal_version: int | None,
        now: datetime,
        validate_fn: object,
    ) -> tuple[CaseRecord, HumanDecision, ExecutionResult | None, str | None, bool]:
        """Atomically validate and reserve an approval/rejection before external executor invocation."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                case = self._load_case_in_tx(case_id)
                if case is None:
                    raise KeyError(f"Case '{case_id}' was not found.")

                human_decision, cached_exec, op_key, was_duplicate = validate_fn(case)
                if was_duplicate:
                    self._conn.execute("COMMIT")
                    return case, human_decision, cached_exec, op_key, True

                assert op_key is not None
                prop_ver = case.proposal.version if case.proposal else (proposal_version or 1)
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO operation_reservations(
                        operation_key, case_id, proposal_id, proposal_version, actor, status, operation_id, payload_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        op_key,
                        case_id,
                        proposal_id,
                        prop_ver,
                        actor,
                        "reserved",
                        human_decision.model_dump_json(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                case.version += 1
                self._save_case_row_in_tx(case)
                self._conn.execute("COMMIT")
                return case, human_decision, None, op_key, False
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def finalize_operation_transactional(
        self,
        *,
        case_id: str,
        operation_key: str | None,
        decision: HumanDecision,
        execution: ExecutionResult,
        operation: ExternalOperationRecord | None,
        audit_record: AuditRecord,
        event: CaseEvent,
    ) -> CaseRecord:
        """Persist external executor results, operation ID, audit record, and event atomically."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                case = self._load_case_in_tx(case_id)
                assert case is not None
                if case.proposal is not None and case.proposal.proposal_id == execution.proposal_id:
                    case.proposal.status = execution.status
                    self._upsert_proposal_history_in_tx(
                        case,
                        proposal_id=case.proposal.proposal_id,
                        revision_number=case.proposal.revision_number,
                        version=case.proposal.version,
                        status=execution.status,
                        outcome=execution.status.value,
                        turn_id=None,
                        now=execution.executed_at,
                    )
                case.decision = decision
                case.execution = execution
                if operation is not None:
                    case.external_operations = [
                        existing
                        for existing in case.external_operations
                        if existing.operation_id != operation.operation_id
                    ] + [operation]
                if execution.status == ProposalStatus.EXECUTED:
                    case.status = CaseStatus.MITIGATED
                elif decision.decision == DecisionChoice.APPROVE and execution.status == ProposalStatus.APPROVED:
                    case.status = CaseStatus.OBSERVING
                else:
                    case.status = CaseStatus.OPEN
                case.updated_at = execution.executed_at
                case.version += 1

                if operation_key is not None:
                    self._conn.execute(
                        """
                        UPDATE operation_reservations
                        SET status = ?, operation_id = ?, updated_at = ?
                        WHERE operation_key = ?
                        """,
                        (
                            execution.status.value,
                            operation.operation_id if operation else None,
                            execution.executed_at.isoformat(),
                            operation_key,
                        ),
                    )
                self._append_event_in_tx(case, event)
                self._conn.execute(
                    "INSERT INTO audit_records(case_id, timestamp, record_json) VALUES (?, ?, ?)",
                    (case_id, audit_record.timestamp.isoformat(), audit_record.model_dump_json()),
                )
                self._save_case_row_in_tx(case)
                self._conn.execute("COMMIT")
                return case
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()

