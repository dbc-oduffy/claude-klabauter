"""A halted run's hold on a path frees when its kept hunks there are no longer uncommitted in the tree.

The rag-56 shape: a halted run withholds several paths; one of them was hand-committed (its kept
hunks in HEAD, or rewritten there) while the others still carry the run's dirty hunks. A landing
run's own further hunks on the hand-committed path must land; the still-dirty paths keep holding.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SLICE = "ops/slice-run.ps1"
OTHER = "ops/band_job.py"
BASE_SLICE = "a\nb\nc\nd\ne\n"
KEPT_SLICE = "a\nKEPT-HUNK\nc\nd\ne\n"
LANDING_LINE = "LANDING-OWN-HUNK\n"


def _git(root: Path, *args: str, stdin: str = "") -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, input=stdin,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _rid(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _fixture(root: Path, head_slice: str, tree_slice: str) -> str:
    """Halted run H withholds SLICE and OTHER; OTHER's kept hunk is still dirty in the tree."""
    _git(root, "init", "-q")
    _write(root / SLICE, BASE_SLICE)
    _write(root / OTHER, "x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    _write(root / SLICE, head_slice)
    _git(root, "commit", "-q", "--allow-empty", "-am", "hand commit")
    kept_slice = _git(root, "hash-object", "-w", "--stdin", stdin=KEPT_SLICE)
    kept_other = _git(root, "hash-object", "-w", "--stdin", stdin="x = 2\n")
    halted = _rid(300, "aaaaaa")
    rec = root / "state/warp" / halted
    sizing = "state/sizings/s.yaml"
    _write(root / sizing, "status: open\n")
    _write(rec / "run.json", json.dumps({"sizing_rel": sizing}))
    _write(rec / "halted.json", json.dumps({
        "status": "halted", "sizing_rel": sizing, "run_base_sha": base,
        "run_artifacts": {"withheld": {SLICE: "halt-record-only", OTHER: "halt-record-only"}},
    }))
    _write(rec / "kept.json", json.dumps({
        "chunks": {"X1": {"paths": {SLICE: kept_slice, OTHER: kept_other}}}, "plan_gate": None,
    }))
    _write(root / SLICE, tree_slice)
    _write(root / OTHER, "x = 2\n")
    return halted


def _held(root: Path) -> dict:
    return land_run.foreign_halted_withheld(root, _rid(5, "ffffff"), None, [SLICE, OTHER])


def _held_paths(root: Path, halted: str) -> set:
    return set(hr.held_paths(root, halted, _rid(5, "ffffff"), None))


def test_kept_hunks_in_head_free_the_path_for_the_landing_runs_own_hunks(tmp_path):
    halted = _fixture(tmp_path, KEPT_SLICE, KEPT_SLICE + LANDING_LINE)
    assert _held_paths(tmp_path, halted) == {OTHER}


def test_kept_hunks_not_in_head_keep_holding(tmp_path):
    halted = _fixture(tmp_path, BASE_SLICE, KEPT_SLICE + LANDING_LINE)
    assert _held_paths(tmp_path, halted) == {SLICE, OTHER}


def test_kept_hunks_rewritten_in_head_keep_holding(tmp_path):
    """The kept blob is the run's resumable work: a path whose hunks HEAD only rewrote still holds."""
    halted = _fixture(tmp_path, "a\nREWRITTEN\nc\nd\ne\n", "a\nREWRITTEN\nc\nd\ne\n" + LANDING_LINE)
    assert _held_paths(tmp_path, halted) == {SLICE, OTHER}


def test_run_with_every_path_in_head_is_released_whole(tmp_path):
    halted = _fixture(tmp_path, KEPT_SLICE, KEPT_SLICE)
    assert _held_paths(tmp_path, halted) == {OTHER}
    _git(tmp_path, "commit", "-q", "-am", "other at head")
    assert _held_paths(tmp_path, halted) == set()
    assert _held(tmp_path) == {}
    assert hr.release_reason(tmp_path, halted, None) == hr.RELEASE_KEPT_AT_HEAD
