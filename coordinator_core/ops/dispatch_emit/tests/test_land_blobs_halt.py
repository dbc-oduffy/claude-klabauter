"""Halt record of the rebuilt landing op: base and run blob shas per declared path, refusal when incomplete."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_blobs_contract as contract
from coordinator_core.ops.dispatch_emit import land_blobs_halt as halt
from coordinator_core.ops.dispatch_emit import stranded_sibling
from coordinator_core.ops.dispatch_emit.tests._git_fixture import _git, seed_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261114T000000-abcdef"


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = seed_repo(tmp_path / "repo")
    (root / "a.py").write_text("one\n", encoding="utf-8")
    (root / "gone.py").write_text("bye\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "base"], root)
    return root


def _record(root: Path) -> dict:
    """The record read back through the guards' own reader, so a shape drift fails here."""
    return stranded_sibling.read(root / ".coordinator-local" / "rec")


def _write(root: Path, declared: list, reply: dict | None = None) -> dict:
    return halt.halt_with_record(root, RUN, declared, reply or {"halted": True}, record_dir=root / ".coordinator-local" / "rec")


def test_halt_records_base_and_run_blob_for_every_declared_path(repo: Path) -> None:
    (repo / "a.py").write_bytes(b"two\n")
    (repo / "new.py").write_bytes(b"fresh\n")
    (repo / "gone.py").unlink()
    reply = _write(repo, ["a.py", "new.py", "gone.py"])
    assert reply["halted"] is True and reply["halt_record"].endswith(stranded_sibling.TOUCHED_FILE)
    assert _record(repo) == {
        "a.py": {"base": _blob(b"one\n"), "run": _blob(b"two\n")},
        "new.py": {"base": None, "run": _blob(b"fresh\n")},
        "gone.py": {"base": _blob(b"bye\n"), "run": None},
    }


def test_kept_blob_is_the_run_blob_when_the_worktree_moved_on(repo: Path) -> None:
    rec_dir = repo / ".coordinator-local" / "rec"
    rec_dir.mkdir(parents=True)
    kept = _blob(b"mine\n")
    (rec_dir / "kept.json").write_text(json.dumps({"chunks": {"C1": {"paths": {"a.py": kept}}}}), encoding="utf-8")
    (repo / "a.py").write_text("peer edit\n", encoding="utf-8")
    _write(repo, ["a.py"])
    assert _record(repo)["a.py"] == {"base": _blob(b"one\n"), "run": kept}


def test_unreadable_path_refuses_and_leaves_no_record(repo: Path) -> None:
    (repo / "dir.py").mkdir()
    reply = _write(repo, ["a.py", "dir.py"])
    assert reply["refused"] == contract.HALT_RECORD_INCOMPLETE
    assert reply["unrecorded_paths"] == ["dir.py"] and reply["committed"] is False
    assert not (repo / ".coordinator-local" / "rec" / stranded_sibling.TOUCHED_FILE).exists()


def test_write_failure_refuses_naming_every_declared_path(repo: Path) -> None:
    blocker = repo / ".coordinator-local" / "rec"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("a file where the record dir belongs", encoding="utf-8")
    reply = _write(repo, ["a.py"])
    assert reply["refused"] == contract.HALT_RECORD_INCOMPLETE and reply["unrecorded_paths"] == ["a.py"]


def test_default_record_dir_is_the_run_record(repo: Path) -> None:
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    result = halt.write_halt_record(repo, RUN, ["a.py"])
    assert result["recorded"] is True
    assert (repo / RECORD_DIR_ROOT / RUN / stranded_sibling.TOUCHED_FILE).is_file()
