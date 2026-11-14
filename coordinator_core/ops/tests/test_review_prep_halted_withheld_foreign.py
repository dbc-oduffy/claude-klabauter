"""Worktree-basis review.prep names a dirty file a halted run's withheld print covers as foreign and slices only the run's own."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

HELD = "ops/held.py"
OWN = "ops/own.py"
HELD_BYTES = "head\nhalted-wip\n"


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


def _fixture(root: Path, *, release: bool = False) -> tuple[str, str, str]:
    """HELD and OWN committed; both dirty. A halted run's withheld print records HELD's bytes;
    the second run declares both paths. Returns (halted id, second run id, base sha)."""
    _git(root, "init", "-q")
    _write(root / HELD, "head\n")
    _write(root / OWN, "own = 0\n")
    _write(root / "docs/plans/p.md", "---\nplan_id: pln-test-1\n---\n\n# p\n")
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
        "run_artifacts": {"withheld": {HELD: "halt-record-only"}},
    }))
    _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {HELD: kept}}}, "plan_gate": None}))
    _write(root / HELD, HELD_BYTES)
    _write(rec / "withheld-prints.json", json.dumps({HELD: blob_sha(HELD_BYTES.encode("utf-8"))}))
    if release:
        _write(root / HELD, "head\n")
    _write(root / OWN, "own = 1\n")
    second = _rid(5, "bbbbbb")
    _write(root / "state/warp" / second / "manifest.json", json.dumps({"review_declared_paths": [HELD, OWN]}))
    return halted, second, base


def _prep(root: Path, run: str, base: str) -> dict:
    return rp._handler(
        {"run_id": run, "base": base, "worktree": True, "plan_path": "docs/plans/p.md", "session_id": "sess-own"},
        root,
    )


def _sliced(reply: dict) -> list:
    return [f for s in reply["slices"] for f in s["files"]]


def test_a_halted_runs_withheld_dirty_file_is_foreign_and_unsliced(tmp_path):
    _, second, base = _fixture(tmp_path)
    reply = _prep(tmp_path, second, base)
    assert reply["error"] is None
    assert reply["foreign_claims"] == [HELD]
    assert HELD not in _sliced(reply)
    assert OWN in _sliced(reply)
    assert HELD not in reply["peer_held"]


def test_a_released_halted_hold_is_not_named_foreign(tmp_path):
    _, second, base = _fixture(tmp_path, release=True)
    reply = _prep(tmp_path, second, base)
    assert HELD not in reply["foreign_claims"]
    assert OWN in _sliced(reply)
