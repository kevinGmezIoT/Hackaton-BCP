"""Uncompressed Draw.io XML architecture parser adapter."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from domain.contracts import (
    ConnectorStatus,
    DependencyQueryResult,
    DependencyRelation,
    RelationType,
)
from domain.ports import ServiceCatalogPort

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_cell_label(raw_value: str | None) -> str:
    """Strip HTML formatting from a Draw.io mxCell value attribute."""
    if not raw_value:
        return ""
    text = _TAG_RE.sub(" ", html.unescape(raw_value))
    return re.sub(r"\s+", " ", text).strip()


def _infer_relation_type(label: str) -> RelationType:
    lowered = label.lower()
    if any(word in lowered for word in ("call", "llama", "http", "grpc", "rest", "api")):
        return RelationType.CALLS
    if any(word in lowered for word in ("provision", "aprovision", "db", "pool", "terraform")):
        return RelationType.PROVISIONS
    if any(word in lowered for word in ("config", "env", "secret")):
        return RelationType.CONFIGURES
    return RelationType.CALLS if lowered else RelationType.UNKNOWN


class DrawioArchitectureAdapter:
    """Extracts service dependencies from uncompressed Draw.io XML files."""

    def __init__(
        self,
        diagram_paths: dict[str, Path],
        catalog: ServiceCatalogPort,
        *,
        revision: str = "HEAD",
    ) -> None:
        self._diagram_paths = diagram_paths
        self._catalog = catalog
        self._revision = revision

    def query_dependencies(
        self,
        service: str,
        environment: str,
        version: str | None = None,
        direction: str = "outbound",
    ) -> DependencyQueryResult:
        canonical = self._catalog.resolve_canonical_name(service)
        diagram_path = self._diagram_paths.get(canonical) or next(
            iter(self._diagram_paths.values()), None
        )
        if diagram_path is None or not diagram_path.exists():
            return DependencyQueryResult(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                service=canonical,
                environment=environment,
                note=f"Architecture diagram not found for '{canonical}'.",
            )

        try:
            relations = self._parse_drawio(diagram_path, version or self._revision)
        except ET.ParseError as exc:
            return DependencyQueryResult(
                status=ConnectorStatus.CONNECTOR_UNAVAILABLE,
                service=canonical,
                environment=environment,
                note=f"Invalid Draw.io XML in {diagram_path.name}: {exc}",
            )

        filtered: list[DependencyRelation] = []
        for rel in relations:
            if direction == "outbound" and rel.source_service.lower() == canonical.lower():
                filtered.append(rel)
            elif direction == "inbound" and rel.target_service.lower() == canonical.lower():
                filtered.append(rel.model_copy(update={"direction": "inbound"}))
            elif direction == "both" and (
                rel.source_service.lower() == canonical.lower()
                or rel.target_service.lower() == canonical.lower()
            ):
                filtered.append(rel)

        if not filtered:
            return DependencyQueryResult(
                status=ConnectorStatus.NO_RESULTS,
                service=canonical,
                environment=environment,
                note=f"No {direction} dependencies declared for '{canonical}' in {diagram_path.name}.",
            )

        return DependencyQueryResult(
            status=ConnectorStatus.OK,
            service=canonical,
            environment=environment,
            relations=filtered,
        )

    def _parse_drawio(self, path: Path, revision: str) -> list[DependencyRelation]:
        tree = ET.parse(path)
        root = tree.getroot()
        relations: list[DependencyRelation] = []

        diagrams = root.findall(".//diagram")
        if not diagrams:
            diagrams = [root]

        for diag_idx, diagram in enumerate(diagrams):
            page_name = diagram.attrib.get("name") or f"page-{diag_idx + 1}"
            cells = diagram.findall(".//mxCell")
            node_labels: dict[str, str] = {}

            for cell in cells:
                if cell.attrib.get("vertex") == "1":
                    cell_id = cell.attrib.get("id", "")
                    label = _clean_cell_label(cell.attrib.get("value"))
                    if cell_id and label:
                        node_labels[cell_id] = self._catalog.resolve_canonical_name(label)

            for cell in cells:
                if cell.attrib.get("edge") != "1":
                    continue
                edge_id = cell.attrib.get("id", "unknown")
                src_id = cell.attrib.get("source")
                tgt_id = cell.attrib.get("target")
                if not src_id or not tgt_id or src_id not in node_labels or tgt_id not in node_labels:
                    continue

                edge_label = _clean_cell_label(cell.attrib.get("value"))
                style = cell.attrib.get("style", "")
                lacks_direction = "endArrow=none" in style
                relation_type = _infer_relation_type(edge_label)
                ambiguous = lacks_direction or relation_type == RelationType.UNKNOWN

                relations.append(
                    DependencyRelation(
                        source_service=node_labels[src_id],
                        target_service=node_labels[tgt_id],
                        relation_type=relation_type,
                        direction="ambiguous" if lacks_direction else "outbound",
                        source_kind="drawio_xml",
                        reference=f"{path.name}#{page_name}/cell:{edge_id}",
                        revision=revision,
                        corroborated=False,
                        ambiguous=ambiguous,
                    )
                )
        return relations

