"""Packaged synthetic Transfers -> pagos-core demo resources."""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path


def _copy_traversable(src: Traversable, dst: Path) -> None:
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            if child.name in {"__init__.py", "__pycache__"}:
                continue
            _copy_traversable(child, dst / child.name)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())


def copy_demo_scenario(target_dir: Path) -> Path:
    """Copy packaged demo scenario resources into target_dir and return its path."""
    root = resources.files("vigia.demo")
    _copy_traversable(root, target_dir)
    return target_dir

