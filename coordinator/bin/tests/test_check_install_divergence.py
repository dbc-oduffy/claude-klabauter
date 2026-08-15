"""test_check_install_divergence — pytest tests for the reachability guard in run().

Spec backlink:
  docs/plans/2026-06-26-coordinator-install-update-friction-fix-slate.md § C-R2a

Coverage (AC3 — two tests):
  test_unreachable_baseline_degrades:
    A fabricated unreachable 40-hex SHA against a real source repo causes run() to
    degrade to the two-way comparison path (exit 2) with no traceback — the guard
    flips baseline_sha to None and the existing fallback fires cleanly.

  test_reachable_baseline_takes_three_way:
    A genuinely reachable baseline SHA still takes the three-way path (exit 0 when
    live mirrors source), confirming the guard does NOT silently disable three-way
    for valid baselines — regression net against the silent-disable failure mode.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# ---------------------------------------------------------------------------
# Module import — filename uses hyphens, so importlib.util is required
# ---------------------------------------------------------------------------
_BIN_DIR = Path(__file__).parent.parent


def _load_divergence_module():
    """Load check-install-divergence.py by file path (hyphenated name bypass)."""
    spec = importlib.util.spec_from_file_location(
        "check_install_divergence",
        _BIN_DIR / "check-install-divergence.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_divergence_module()
run = _mod.run


# ---------------------------------------------------------------------------
# Helpers — minimal git repo factory
# ---------------------------------------------------------------------------

def _init_source_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a minimal git repo with one tracked file; return (repo_path, HEAD_sha).

    The repo has a single commit so HEAD is a valid, reachable commit object.
    """
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
    """Copy all git-tracked files from source into live (byte-for-byte)."""
    tracked = subprocess.check_output(
        ["git", "-C", str(source), "ls-files"],
        text=True,
    ).splitlines()
    for relpath in tracked:
        dest = live / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((source / relpath).read_bytes())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_unreachable_baseline_degrades(tmp_path: Path) -> None:
    """Fabricated unreachable SHA causes guard to flip baseline to None → exit 2.

    The two-way fallback fires (live mirrors source exactly → no divergence → exit 2).
    No traceback must be raised — the guard must handle unreachable SHAs gracefully.
    """
    source, _head_sha = _init_source_repo(tmp_path)
    live = tmp_path / "live"
    live.mkdir()
    _mirror_source_to_live(source, live)

    # 40 valid hex chars that are definitively not a commit in this fresh repo.
    fabricated_sha = "a0b1c2d3" * 5
    assert len(fabricated_sha) == 40

    exit_code = run(source, live, baseline_sha_cli=fabricated_sha)

    assert exit_code == 2, (
        f"Expected exit 2 (two-way clean fallback when baseline SHA is unreachable), "
        f"got {exit_code}"
    )


def test_reachable_baseline_takes_three_way(tmp_path: Path) -> None:
    """Reachable HEAD SHA passes the guard → three-way path fires → exit 0 (clean).

    Exit 0 (not 2) is the discriminating signal: the two-way fallback returns 2 on
    a clean install; only the three-way path returns 0 (unchanged + forward-safe).
    Regression net: confirms the guard does NOT degrade valid baselines to two-way.
    """
    source, head_sha = _init_source_repo(tmp_path)
    live = tmp_path / "live"
    live.mkdir()
    _mirror_source_to_live(source, live)

    exit_code = run(source, live, baseline_sha_cli=head_sha)

    assert exit_code == 0, (
        f"Expected exit 0 (three-way clean) when baseline SHA is reachable, "
        f"got {exit_code} — guard may be incorrectly degrading reachable baselines"
    )
