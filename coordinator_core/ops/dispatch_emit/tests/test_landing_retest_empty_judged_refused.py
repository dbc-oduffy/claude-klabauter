"""An empty judged set across a HEAD move is never a fresh landing retest verdict."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coordinator_core.ops.dispatch_emit import landing_retest

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "warp-empty-judged"
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True, creationflags=_NOWIN,
    )
    return done.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "tracked.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("a\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _write_record(root: Path, head: str, covered: dict[str, str]) -> None:
    path = landing_retest.fresh_verdict_path(root, RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": RUN_ID, "status": "passed", "head": head, "covered": covered}),
        encoding="utf-8",
    )


def _move_head(root: Path) -> None:
    (root / "other.txt").write_text("b\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "move")


def test_empty_covered_across_head_move_is_not_fresh(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write_record(root, _git(root, "rev-parse", "HEAD"), {})
    _move_head(root)
    assert landing_retest.fresh_targeted_verdict(root, RUN_ID) is None


def test_only_own_stage_covered_across_head_move_is_not_fresh(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    stage = f"state/warp/{RUN_ID}/run-stage.json"
    _write_record(root, _git(root, "rev-parse", "HEAD"), {stage: "absent"})
    _move_head(root)
    assert landing_retest.fresh_targeted_verdict(root, RUN_ID) is None


def test_nonempty_covered_unchanged_across_head_move_is_fresh(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    covered = landing_retest.landing_fingerprint(root, ["tracked.py"])
    _write_record(root, _git(root, "rev-parse", "HEAD"), covered)
    _move_head(root)
    assert landing_retest.fresh_targeted_verdict(root, RUN_ID) is not None
