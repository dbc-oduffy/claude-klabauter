"""A halted run whose withheld paths are all clean in the tree holds nothing; one dirty path keeps the hold.

The kept blob here is deliberately not at HEAD, so the kept-at-head release cannot be what frees the run.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

A = "ops/a.py"
B = "ops/b.py"


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


def _fixture(root: Path, withheld: dict) -> str:
    """Committed A and B at 'head\\n'; the halted run withholds `withheld` paths with a kept blob that differs from HEAD."""
    _git(root, "init", "-q")
    _write(root / A, "head\n")
    _write(root / B, "head\n")
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
        "run_artifacts": {"withheld": {p: "halt-record-only" for p in withheld}},
    }))
    _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {p: kept for p in withheld}}}, "plan_gate": None}))
    return halted


def _reason(root: Path, halted: str):
    return hr.release_reason(root, halted, _rid(5, "ffffff"), None)


def test_every_withheld_path_clean_releases_the_hold(tmp_path):
    halted = _fixture(tmp_path, [A, B])
    assert _reason(tmp_path, halted) == hr.RELEASE_WITHHELD_CLEAN
    assert hr.held_paths(tmp_path, halted, _rid(5, "ffffff"), None) == frozenset()


def test_one_dirty_withheld_path_keeps_the_hold(tmp_path):
    halted = _fixture(tmp_path, [A, B])
    _write(tmp_path / B, "head\nwip\n")
    assert _reason(tmp_path, halted) is None
    assert hr.held_paths(tmp_path, halted, _rid(5, "ffffff"), None) == {A, B}


def test_untracked_withheld_file_keeps_the_hold(tmp_path):
    halted = _fixture(tmp_path, [A])
    _write(tmp_path / "ops/new.py", "x\n")
    marker = tmp_path / "state/warp" / halted / "halted.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["run_artifacts"]["withheld"] = {"ops/new.py": "halt-record-only"}
    _write(marker, json.dumps(data))
    assert _reason(tmp_path, halted) is None


def test_unreadable_index_keeps_the_hold(tmp_path):
    halted = _fixture(tmp_path, [A])
    (tmp_path / ".git/index").write_bytes(b"not an index")
    assert _reason(tmp_path, halted) is None
