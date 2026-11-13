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


def test_cli_loads_dotenv_without_overriding_existing_env(tmp_path: Path, monkeypatch) -> None:
    from vigia.entrypoints.cli import _load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nGEMINI_API_KEY='from-dotenv'\nEXISTING_VAR=\"ignored\"\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("EXISTING_VAR", "from-shell")

    _load_dotenv(env_file)

    assert os.environ.get("GEMINI_API_KEY") == "from-dotenv"
    assert os.environ.get("EXISTING_VAR") == "from-shell"


def test_cli_external_workspace_overrides_demo_bootstrap(tmp_path: Path, monkeypatch) -> None:
    import shutil
    from vigia.demo import copy_demo_scenario
    from vigia.domain.contracts import OperationalMode, RuntimeProfile
    from vigia.entrypoints.cli import _build_runtime_config
    from vigia.runtime import build_pilot_case_service

    ext_dir = copy_demo_scenario(tmp_path / "external-data")
    metrics_txt = tmp_path / "custom-metrics.txt"
    shutil.copy2(ext_dir / "metrics.csv", metrics_txt)
    (ext_dir / "metrics.csv").unlink()
    db_file = tmp_path / "persisted" / "vigia.db"

    import argparse

    args = argparse.Namespace(
        fixtures_dir=str(ext_dir),
        catalog=None,
        metrics_path=str(metrics_txt),
        logs_path=None,
        db_path=str(db_file),
        audit_path=None,
        prometheus_url=None,
        loki_url=None,
        github_api_url=None,
        github_repo=["pagos-core=org/pagos-core"],
        local_repo=None,
    )
    monkeypatch.delenv("VIGIA_GITHUB_REPOS", raising=False)

    cfg, base_sha, deployed_sha = _build_runtime_config(
        args,
        tmp_path / "ignored-tmp",
        RuntimeProfile.DEMO,
        OperationalMode.ASSISTED,
    )
    assert base_sha == ""
    assert deployed_sha == ""
    assert cfg.fixtures_dir == ext_dir
    assert cfg.metrics_path == metrics_txt
    assert cfg.db_path == db_file
    assert cfg.github_api_url == "https://api.github.com"
    assert cfg.github_service_repos == {"pagos-core": "org/pagos-core"}

    # Even with profile=DEMO, custom metrics_path (.txt) and GitHub mapping are wired in.
    service, _, _ = build_pilot_case_service(cfg, model="vigia-unused")
    try:
        series = service._deps.telemetry.query_metrics(
            "transferencias",
            "production",
            "latency_p95_ms",
            window_minutes=30,
            cutoff_at=__import__("datetime").datetime.fromisoformat("2026-11-12T09:12:00-05:00"),
        )
        assert series.points
        assert db_file.exists()
    finally:
        service.close()
