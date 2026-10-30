"""declared_hold_release: a halted holder with no hunk present loses its hold and the release is recorded."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import declared_hold_release as dhr
from coordinator_core.ops.dispatch_emit import halted_hold_release as hhr
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BASE = "def f():\n    return 1\n\ndef g():\n    return 2\n"
KEPT_ONE = "def f():\n    return 1\n\ndef g():\n    return 2\n\ndef held():\n    return 'holder'\n"
KEPT_TWO = KEPT_ONE.replace("return 1", "return 10")
PATH = "pkg/mod.py"
RUN = "ask-20261027T131023-078f35"


def _git(root: Path, *args: str, stdin: str = "") -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, input=stdin,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _holder(tmp_path: Path, kept_text: str, *, head_text: str = BASE, work_text: str = BASE) -> Path:
    """Repo with `PATH` at BASE, then HEAD at `head_text`, worktree at `work_text`; RUN halted holding it."""
    _git(tmp_path, "init", "-q")
    _put(tmp_path / PATH, BASE)
    _git(tmp_path, "add", PATH)
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    if head_text != BASE:
        _put(tmp_path / PATH, head_text)
        _git(tmp_path, "commit", "-q", "-am", "head")
    _put(tmp_path / PATH, work_text)
    blob = _git(tmp_path, "hash-object", "-w", "--stdin", stdin=kept_text)
    rec = tmp_path / "state/warp" / RUN
    _put(rec / "halted.json", json.dumps({
        "status": "halted", "run_base_sha": base,
        "run_artifacts": {"withheld": {PATH: "halt-record-only"}},
    }))
    _put(rec / "kept.json", json.dumps({
        "chunks": {"C1": {"paths": {PATH: blob}}, "C2": {"paths": {"other.py": "0" * 40}}},
        "plan_gate": None,
    }))
    return rec


def _released(rec: Path) -> list:
    return json.loads((rec / "kept.json").read_text(encoding="utf-8")).get("released_holds", [])


def test_absent_hunks_release_the_hold_and_record_it(tmp_path):
    rec = _holder(tmp_path, KEPT_ONE)
    assert dhr.release_absent(tmp_path, [PATH], own_run_id="ask-20261027T200000-aaaaaa") == {RUN: [PATH]}
    record = _released(rec)
    assert len(record) == 1 and record[0]["reason"] == dhr.RELEASE_REASON
    assert record[0]["paths"] == [{"chunk": "C1", "path": PATH, "blob": record[0]["paths"][0]["blob"]}]
    kept = json.loads((rec / "kept.json").read_text(encoding="utf-8"))
    assert "C1" not in kept["chunks"] and "C2" in kept["chunks"]
    assert PATH in hhr.released_path_reasons(tmp_path, RUN)
    assert PATH not in halted_run.halted_withheld(rec)


def test_one_hunk_still_in_the_worktree_keeps_the_hold(tmp_path):
    rec = _holder(tmp_path, KEPT_TWO, work_text=KEPT_TWO.replace("return 'holder'", "return 'peer'"))
    assert dhr.release_absent(tmp_path, [PATH], own_run_id="x") == {}
    assert _released(rec) == []
    assert PATH in halted_run.halted_withheld(rec)


def test_a_hunk_present_in_head_keeps_the_hold(tmp_path):
    rec = _holder(tmp_path, KEPT_ONE, head_text=KEPT_ONE, work_text=BASE)
    assert dhr.release_absent(tmp_path, [PATH], own_run_id="x") == {}
    assert PATH in halted_run.halted_withheld(rec)


def _untracked_holder(tmp_path: Path, *, create: bool) -> Path:
    """RUN halted holding NEW, which is in no commit; `create` puts a differing file in the worktree."""
    new = "pkg/new.py"
    _git(tmp_path, "init", "-q")
    _put(tmp_path / "seed.txt", "seed\n")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    if create:
        _put(tmp_path / new, "def other():\n    return 0\n")
    blob = _git(tmp_path, "hash-object", "-w", "--stdin", stdin="def held():\n    return 'holder'\n")
    rec = tmp_path / "state/warp" / RUN
    _put(rec / "halted.json", json.dumps({
        "status": "halted", "run_base_sha": base,
        "run_artifacts": {"withheld": {new: "halt-record-only"}},
    }))
    _put(rec / "kept.json", json.dumps({"chunks": {"C1": {"paths": {new: blob}}}, "plan_gate": None}))
    return rec


def test_untracked_file_present_in_worktree_keeps_the_hold(tmp_path):
    rec = _untracked_holder(tmp_path, create=True)
    assert dhr.release_absent(tmp_path, ["pkg/new.py"], own_run_id="x") == {}
    assert _released(rec) == []
    assert "pkg/new.py" in halted_run.halted_withheld(rec)


def test_untracked_file_absent_from_worktree_releases_the_hold(tmp_path):
    rec = _untracked_holder(tmp_path, create=False)
    assert dhr.release_absent(tmp_path, ["pkg/new.py"], own_run_id="x") == {RUN: ["pkg/new.py"]}
    assert len(_released(rec)) == 1


def test_own_run_and_unrelated_paths_are_untouched(tmp_path):
    rec = _holder(tmp_path, KEPT_ONE)
    assert dhr.release_absent(tmp_path, [PATH], own_run_id=RUN) == {}
    assert dhr.release_absent(tmp_path, ["elsewhere.py"], own_run_id="x") == {}
    assert PATH in halted_run.halted_withheld(rec)


def test_missing_kept_blob_holds(tmp_path):
    rec = _holder(tmp_path, KEPT_ONE)
    kept = json.loads((rec / "kept.json").read_text(encoding="utf-8"))
    kept["chunks"]["C1"]["paths"][PATH] = "f" * 40
    _put(rec / "kept.json", json.dumps(kept))
    assert dhr.release_absent(tmp_path, [PATH], own_run_id="x") == {}
    assert PATH in halted_run.halted_withheld(rec)


def test_missing_base_sha_holds(tmp_path):
    rec = _holder(tmp_path, KEPT_ONE)
    marker = json.loads((rec / "halted.json").read_text(encoding="utf-8"))
    del marker["run_base_sha"]
    _put(rec / "halted.json", json.dumps(marker))
    assert dhr.release_absent(tmp_path, [PATH], own_run_id="x") == {}
    assert PATH in halted_run.halted_withheld(rec)


def test_hunks_all_absent_is_per_hunk():
    assert dhr.hunks_all_absent(BASE, KEPT_ONE, BASE)
    assert not dhr.hunks_all_absent(BASE, KEPT_ONE, KEPT_ONE)
    assert not dhr.hunks_all_absent(BASE, KEPT_TWO, BASE, "def held():\n    return 'holder'\n")


def test_hunks_all_absent_counts_a_landed_removal_as_present():
    kept = "def f():\n    return 1\n"
    assert dhr.hunks_all_absent(BASE, kept, BASE)
    assert not dhr.hunks_all_absent(BASE, kept, kept)
