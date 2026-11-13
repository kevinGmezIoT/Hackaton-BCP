"""YAML-backed service catalog adapter."""

from __future__ import annotations

from pathlib import Path

import yaml

from domain.contracts import ServiceConfig


class YamlServiceCatalogAdapter:
    """Loads registered services and resolves aliases and telemetry selectors."""

    def __init__(self, services: list[ServiceConfig]) -> None:
        self._by_name: dict[str, ServiceConfig] = {}
        self._alias_map: dict[str, str] = {}
        for svc in services:
            key = svc.service.lower()
            self._by_name[key] = svc
            self._alias_map[key] = svc.service
            self._alias_map[svc.telemetry.service_selector.lower()] = svc.service
            for alias in svc.aliases:
                self._alias_map[alias.lower()] = svc.service

    @classmethod
    def from_yaml(cls, path: Path) -> YamlServiceCatalogAdapter:
        """Load service registrations from a YAML file."""
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        entries = raw.get("services", []) if isinstance(raw, dict) else raw
        services = [ServiceConfig.model_validate(item) for item in entries]
        return cls(services)

    def get_service(self, service_or_alias: str) -> ServiceConfig | None:
        canonical = self._alias_map.get(service_or_alias.strip().lower())
        if canonical is None:
            return None
        return self._by_name.get(canonical.lower())

    def resolve_canonical_name(self, service_or_alias: str) -> str:
        return self._alias_map.get(service_or_alias.strip().lower(), service_or_alias.strip())

    def list_services(self) -> list[ServiceConfig]:
        return list(self._by_name.values())

