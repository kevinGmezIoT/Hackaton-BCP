"""Packaging smoke test: build wheel, install outside checkout, and run CLI."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path


def test_wheel_contains_runtime_and_demo_and_cli_runs_outside_checkout(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    dist_dir = tmp_path / "dist"

    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(dist_dir.glob("vigia-*.whl"))
    assert len(wheels) == 1
    wheel_path = wheels[0]

    with zipfile.ZipFile(wheel_path) as zf:
        names = set(zf.namelist())
    assert "vigia/runtime.py" in names
    assert "vigia/demo/services.yaml" in names
    assert "vigia/demo/metrics.csv" in names
    assert "vigia/demo/docs/transfers.drawio" in names

    # Install wheel into a clean isolated target directory outside checkout.
    install_dir = tmp_path / "site"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--no-deps",
            "--target",
            str(install_dir),
            str(wheel_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()
    isolated_env = os.environ.copy()
    isolated_env["PYTHONPATH"] = str(install_dir)

    locate_proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import vigia.runtime; print(vigia.runtime.__file__)",
        ],
        cwd=str(outside_cwd),
        env=isolated_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert Path(locate_proc.stdout.strip()).resolve().is_relative_to(install_dir.resolve())

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "vigia.entrypoints.cli",
            "--demo-offline",
            "--decision",
            "approve",
        ],
        cwd=str(outside_cwd),
        env=isolated_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "case status = closed" in proc.stdout
    assert "184.0 ms" in proc.stdout
