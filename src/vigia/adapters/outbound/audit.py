"""JSON Lines audit log and in-memory event sink adapter."""

from __future__ import annotations

from pathlib import Path

from vigia.domain.contracts import AuditRecord, CaseEvent


class JsonlAuditSinkAdapter:
    """Persists audit records to a JSONL file and buffers timeline events."""

    def __init__(self, audit_path: Path | None = None) -> None:
        self._audit_path = audit_path
        self._records: list[AuditRecord] = []
        self._events: list[CaseEvent] = []
        if self._audit_path is not None:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)

    def record_audit(self, record: AuditRecord) -> None:
        self._records.append(record)
        if self._audit_path is not None:
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")

    def emit_event(self, event: CaseEvent) -> None:
        self._events.append(event)

    def list_audit_records(self, case_id: str | None = None) -> list[AuditRecord]:
        if case_id is None:
            return list(self._records)
        return [r for r in self._records if r.case_id == case_id]

    def list_events(self, case_id: str | None = None) -> list[CaseEvent]:
        if case_id is None:
            return list(self._events)
        return [e for e in self._events if e.case_id == case_id]

