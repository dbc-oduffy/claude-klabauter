"""A dirty withheld path stays held only while its bytes are the ones the halted run recorded."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

A = "ops/a.py"
HALTED_BYTES = "head\nhalted-wip\r\n"


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


def _fixture(root: Path, prints: bool = True) -> str:
    """A is committed at 'head\\n'; the tree holds the halted run's bytes; the halt records their print."""
    _git(root, "init", "-q")
    _write(root / A, "head\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    kept = _git(root, "hash-object", "-w", "--stdin", stdin="kept-never-landed\n")
    halted = _rid(300, "aaaaaa")
    rec = root / "state/warp" / halted
    sizing = "state/sizings/s.yaml"
    _write(root / sizing, "status: open\n")
    _write(rec / "run.json", json.dumps({"sizing_rel": sizing}))
    _write(rec / "halted.json", json.dumps({
        "status": "halted", "sizing_rel": sizing, "run_base_sha": base,
        "run_artifacts": {"withheld": {A: "halt-record-only"}},
    }))
    _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {A: kept}}}, "plan_gate": None}))
    _write(root / A, HALTED_BYTES)
    if prints:
        _write(rec / "withheld-prints.json", json.dumps({A: blob_sha(HALTED_BYTES.encode("utf-8"))}))
    return halted


def _held(root: Path, halted: str) -> frozenset:
    return hr.held_paths(root, halted, _rid(5, "ffffff"), None)


def test_bytes_matching_the_recorded_print_stay_held(tmp_path):
    halted = _fixture(tmp_path)
    assert _held(tmp_path, halted) == {A}


def test_peer_edit_with_different_bytes_releases(tmp_path):
    halted = _fixture(tmp_path)
    _write(tmp_path / A, "head\npeer-edit\n")
    assert _held(tmp_path, halted) == frozenset()


def test_clean_at_head_releases(tmp_path):
    halted = _fixture(tmp_path)
    _write(tmp_path / A, "head\n")
    assert _held(tmp_path, halted) == frozenset()


def test_missing_print_falls_back_to_dirty_means_held(tmp_path):
    halted = _fixture(tmp_path, prints=False)
    _write(tmp_path / A, "head\npeer-edit\n")
    assert _held(tmp_path, halted) == {A}


def test_unreadable_prints_file_keeps_the_hold(tmp_path):
    halted = _fixture(tmp_path)
    _write(tmp_path / "state/warp" / halted / "withheld-prints.json", "not json")
    _write(tmp_path / A, "head\npeer-edit\n")
    assert _held(tmp_path, halted) == {A}
