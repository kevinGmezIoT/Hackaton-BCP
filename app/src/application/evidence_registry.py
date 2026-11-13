"""Case evidence registry and citation validation."""

from __future__ import annotations

from datetime import datetime

from domain.contracts import CaseRecord, Evidence


class InvalidCitationError(ValueError):
    """Raised when a finding or proposal cites evidence IDs absent from the case."""


class EvidenceRegistry:
    """Manages verifiable evidence entries and gaps within a CaseRecord."""

    def __init__(self, case: CaseRecord) -> None:
        self._case = case

    def record(
        self,
        *,
        service: str,
        environment: str,
        observed_at: datetime,
        source: str,
        reproducible_reference: str,
        observed_fact: str,
        revision: str | None = None,
        window: str | None = None,
    ) -> Evidence:
        """Append a new citable evidence item and return it."""
        evidence_id = f"EV-{len(self._case.evidence) + 1:03d}"
        item = Evidence(
            evidence_id=evidence_id,
            case_id=self._case.case_id,
            service=service,
            environment=environment,
            observed_at=observed_at,
            source=source,
            reproducible_reference=reproducible_reference,
            observed_fact=observed_fact,
            revision=revision,
            window=window,
        )
        self._case.evidence.append(item)
        return item

    def record_gap(self, gap: str) -> None:
        """Record a connector outage, missing data source, or ambiguity."""
        if gap not in self._case.connector_gaps:
            self._case.connector_gaps.append(gap)

    def known_ids(self) -> set[str]:
        """Return all evidence IDs currently registered in the case."""
        return {item.evidence_id for item in self._case.evidence}

    def validate_citations(self, evidence_ids: list[str]) -> None:
        """Verify that at least one valid evidence ID is cited and all exist in the case."""
        if not evidence_ids:
            raise InvalidCitationError(
                "Proposal or finding must cite at least one evidence ID from the case."
            )
        known = self.known_ids()
        missing = [eid for eid in evidence_ids if eid not in known]
        if missing:
            raise InvalidCitationError(
                f"Cited evidence IDs do not exist in case {self._case.case_id}: {missing}"
            )

    def get_many(self, evidence_ids: list[str]) -> list[Evidence]:
        """Return Evidence objects matching the given IDs, preserving order."""
        by_id = {item.evidence_id: item for item in self._case.evidence}
        return [by_id[eid] for eid in evidence_ids if eid in by_id]

