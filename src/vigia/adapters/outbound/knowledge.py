"""TF-IDF incident similarity and Markdown runbook search adapter."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from vigia.domain.contracts import IncidentPrecedent, RunbookMatch

_TOKEN_RE = re.compile(r"[a-z0-9_-]{2,}")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _tfidf_cosine(query_tokens: list[str], docs_tokens: list[list[str]]) -> list[float]:
    """Compute TF-IDF cosine similarity between query and a small corpus."""
    if not query_tokens or not docs_tokens:
        return [0.0 for _ in docs_tokens]

    num_docs = len(docs_tokens)
    df: Counter[str] = Counter()
    for tokens in docs_tokens:
        df.update(set(tokens))

    def idf(term: str) -> float:
        return math.log((1 + num_docs) / (1 + df[term])) + 1.0

    def vectorize(tokens: list[str]) -> dict[str, float]:
        counts = Counter(tokens)
        total = max(len(tokens), 1)
        return {t: (c / total) * idf(t) for t, c in counts.items()}

    q_vec = vectorize(query_tokens)
    q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0

    scores: list[float] = []
    for tokens in docs_tokens:
        d_vec = vectorize(tokens)
        d_norm = math.sqrt(sum(v * v for v in d_vec.values())) or 1.0
        dot = sum(q_vec[t] * d_vec[t] for t in q_vec if t in d_vec)
        scores.append(dot / (q_norm * d_norm))
    return scores


class LocalKnowledgeAdapter:
    """Searches JSON historical incidents and Markdown runbook files."""

    def __init__(self, incidents_path: Path, runbooks_dir: Path) -> None:
        self._incidents_path = incidents_path
        self._runbooks_dir = runbooks_dir

    def search_similar_incidents(
        self,
        description: str,
        k: int = 3,
    ) -> list[IncidentPrecedent]:
        if not self._incidents_path.exists():
            return []
        raw = json.loads(self._incidents_path.read_text(encoding="utf-8"))
        items = raw.get("incidents", []) if isinstance(raw, dict) else raw
        if not items:
            return []

        docs_tokens = [
            _tokenize(
                f"{it.get('title', '')} {it.get('symptoms', '')} "
                f"{it.get('root_cause', '')} {it.get('service', '')}"
            )
            for it in items
        ]
        scores = _tfidf_cosine(_tokenize(description), docs_tokens)

        ranked = sorted(
            zip(items, scores),
            key=lambda pair: pair[1],
            reverse=True,
        )
        results: list[IncidentPrecedent] = []
        for item, score in ranked[:k]:
            if score <= 0.0:
                continue
            results.append(
                IncidentPrecedent(
                    incident_id=item["id"],
                    similarity=round(score, 3),
                    title=item["title"],
                    root_cause=item["root_cause"],
                    resolution=item["resolution"],
                    source=self._incidents_path.name,
                )
            )
        return results

    def search_runbook(self, query: str) -> list[RunbookMatch]:
        if not self._runbooks_dir.exists():
            return []
        query_tokens = set(_tokenize(query))
        if not query_tokens:
            return []

        scored: list[tuple[int, RunbookMatch]] = []
        for md_file in sorted(self._runbooks_dir.glob("*.md")):
            text = md_file.read_text(encoding="utf-8")
            doc_tokens = set(_tokenize(text))
            overlap = len(query_tokens & doc_tokens)
            if overlap == 0:
                continue
            match = self._parse_runbook(md_file, text)
            scored.append((overlap, match))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [match for _, match in scored[:3]]

    def _parse_runbook(self, path: Path, text: str) -> RunbookMatch:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = path.stem
        runbook_id = path.stem.split("-")[0].upper()
        if path.stem.upper().startswith("RB-"):
            parts = path.stem.split("-")
            runbook_id = f"{parts[0].upper()}-{parts[1]}" if len(parts) > 1 else parts[0].upper()

        conditions = ""
        steps: list[str] = []
        for line in lines:
            if line.startswith("# "):
                header = line[2:].strip()
                if ":" in header:
                    maybe_id, maybe_title = header.split(":", 1)
                    runbook_id = maybe_id.strip()
                    title = maybe_title.strip()
                else:
                    title = header
            elif line.lower().startswith("conditions:") or line.lower().startswith(
                "applicable when:"
            ):
                conditions = line.split(":", 1)[1].strip()
            elif re.match(r"^(\d+\.|-)\s+", line):
                steps.append(re.sub(r"^(\d+\.|-)\s+", "", line))

        return RunbookMatch(
            runbook_id=runbook_id,
            title=title,
            applicable_conditions=conditions or "See runbook body.",
            steps=steps,
            source=path.name,
        )

