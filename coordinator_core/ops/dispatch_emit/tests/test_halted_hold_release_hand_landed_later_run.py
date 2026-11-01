"""A halted run's hold frees when its kept hunks were hand-landed and later commits edited over them.

The 1d023b shape: the halted run's content was hand-committed, then other runs rewrote the same lines, so
HEAD no longer matches the kept blob or carries its hunks; a later run landing the same paths is not withheld.
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

PRODUCT = "warm/live_runs.py"
TEST = "warm/tests/test_live_runs.py"
BASE_TEXT = "a\nb\nc\nd\ne\n"
KEPT_TEXT = "a\nKEPT-HUNK\nc\nd\ne\n"
LANDED_TEXT = "a\nKEPT-HUNK\nREVIEW-FIX\nc\nd\ne\n"
LATER_TEXT = "a\nLATER-RUN-REWRITE\nc\nd\ne\n"


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


def _halted_run(root: Path, hand_landed: str, head_after: str) -> str:
    """Run H halted withholding PRODUCT and TEST (halt-record-only); `hand_landed` is committed by hand for
    both, then `head_after` is committed over it as a later run's edit."""
    _git(root, "init", "-q")
    _write(root / PRODUCT, BASE_TEXT)
    _write(root / TEST, BASE_TEXT)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    kept = _git(root, "hash-object", "-w", "--stdin", stdin=KEPT_TEXT)
    halted = _rid(300, "aaaaaa")
    rec = root / "state/warp" / halted
    sizing = "state/sizings/s.yaml"
    _write(root / sizing, "status: open\n")
    _write(rec / "run.json", json.dumps({"sizing_rel": sizing}))
    _write(rec / "halted.json", json.dumps({
        "status": "halted", "sizing_rel": sizing, "run_base_sha": base,
        "run_artifacts": {"withheld": {PRODUCT: "halt-record-only", TEST: "halt-record-only"}},
    }))
    _write(rec / "kept.json", json.dumps({
        "chunks": {"X1": {"paths": {PRODUCT: kept, TEST: kept}}}, "plan_gate": None,
    }))
    for text in (hand_landed, head_after):
        _write(root / PRODUCT, text)
        _write(root / TEST, text)
        _git(root, "commit", "-q", "-am", "landing")
    for path in (PRODUCT, TEST):  # a dirty tree: the clean-tree release cannot free the run
        _write(root / path, head_after + "# uncommitted\n")
    return halted


def _held(root: Path) -> dict:
    return land_run.foreign_halted_withheld(root, _rid(5, "ffffff"), None, [PRODUCT, TEST])


def test_hand_landed_then_rewritten_paths_are_not_withheld_from_a_later_run(tmp_path):
    halted = _halted_run(tmp_path, LANDED_TEXT, LATER_TEXT)
    assert set(hr.held_paths(tmp_path, halted, _rid(5, "ffffff"), None)) == set()
    assert _held(tmp_path) == {}


def test_hand_landed_then_rewritten_blob_equal_landing_is_not_withheld(tmp_path):
    halted = _halted_run(tmp_path, KEPT_TEXT, LATER_TEXT)
    assert set(hr.held_paths(tmp_path, halted, _rid(5, "ffffff"), None)) == set()
    assert _held(tmp_path) == {}


def test_paths_never_landed_keep_holding(tmp_path):
    """Both sides: later commits that never carried the kept hunks free nothing."""
    halted = _halted_run(tmp_path, "a\nUNRELATED\nc\nd\ne\n", LATER_TEXT)
    assert set(hr.held_paths(tmp_path, halted, _rid(5, "ffffff"), None)) == {PRODUCT, TEST}
    assert set(_held(tmp_path)) == {PRODUCT, TEST}


def test_landing_older_than_the_probed_versions_bound_keeps_holding(tmp_path):
    """The bound is a stated limit: a kept landing buried under more than `_HISTORY_VERSIONS` rewrites holds."""
    halted = _halted_run(tmp_path, LANDED_TEXT, LATER_TEXT)
    for n in range(hr._HISTORY_VERSIONS + 1):
        for path in (PRODUCT, TEST):
            _write(tmp_path / path, f"a\nREWRITE-{n}\nc\nd\ne\n")
        _git(tmp_path, "commit", "-q", "-am", f"rewrite {n}")
    for path in (PRODUCT, TEST):
        _write(tmp_path / path, "a\nREWRITE-final\nc\nd\ne\n# uncommitted\n")
    assert set(hr.held_paths(tmp_path, halted, _rid(5, "ffffff"), None)) == {PRODUCT, TEST}
