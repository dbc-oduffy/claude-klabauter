from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BIN_DIR = Path(__file__).parent.parent


def _load_divergence_module():
    spec = importlib.util.spec_from_file_location(
        "check_install_divergence",
        _BIN_DIR / "check-install-divergence.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_divergence_module()
run = _mod.run


def _init_source_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(
        ["git", "init", str(repo)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"],
        check=True, capture_output=True,
    )
    (repo / "install.sh").write_text("#!/bin/bash\necho hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "install.sh"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    head_sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    return repo, head_sha


def _mirror_source_to_live(source: Path, live: Path) -> None:
    tracked = subprocess.check_output(
        ["git", "-C", str(source), "ls-files"],
        text=True,
    ).splitlines()
    for relpath in tracked:
        dest = live / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((source / relpath).read_bytes())


def test_unreachable_baseline_degrades(tmp_path: Path) -> None:
    source, _head_sha = _init_source_repo(tmp_path)
    live = tmp_path / "live"
    live.mkdir()
    _mirror_source_to_live(source, live)

    fabricated_sha = "a0b1c2d3" * 5
    assert len(fabricated_sha) == 40

    exit_code = run(source, live, baseline_sha_cli=fabricated_sha)

    assert exit_code == 2, (
        f"Expected exit 2 (two-way clean fallback when baseline SHA is unreachable), "
        f"got {exit_code}"
    )


def test_reachable_baseline_takes_three_way(tmp_path: Path) -> None:
    source, head_sha = _init_source_repo(tmp_path)
    live = tmp_path / "live"
    live.mkdir()
    _mirror_source_to_live(source, live)

    exit_code = run(source, live, baseline_sha_cli=head_sha)

    assert exit_code == 0, (
        f"Expected exit 0 (three-way clean) when baseline SHA is reachable, "
        f"got {exit_code} — guard may be incorrectly degrading reachable baselines"
    )
