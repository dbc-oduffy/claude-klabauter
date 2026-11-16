"""dispatch.record_halted_run_landed closes a run with a run-open checkpoint and no halt record, given a landing sha carrying its paths."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_bases
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261114T000000-abc123"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, rel: str, text: str, message: str) -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, "one", "one", "one")
    rd = record_dir(tmp_path, RUN)
    rel = (rd / "manifest.json").relative_to(tmp_path).as_posix()
    _commit(tmp_path, rel, json.dumps({"head_sha": base}), "manifest")
    _commit(
        tmp_path, "pkg/a.py", "a = 1\n",
        f"checkpoint(wave 0): 1 row — C1\n\nCheckpoint-Not-Landed: run-open\nCheckpoint-Base: {base}",
    )
    return base


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


def test_landing_sha_carrying_the_checkpoint_paths_closes_the_run(tmp_path):
    base = _repo(tmp_path)
    land = _commit(tmp_path, "pkg/a.py", "a = 2\n", "hand-land")
    out = op._handler({"run_id": RUN, "sha": land}, tmp_path)
    assert out["ok"] is True and out["written"] is True and out["sha"] == land, out
    assert out["records_commit"] == _git(tmp_path, "rev-parse", "HEAD")
    assert run_closed_bases(_git(tmp_path, "log", "-1", "--format=%B")) == {base}


def test_landing_sha_missing_a_checkpoint_path_still_refuses(tmp_path):
    _repo(tmp_path)
    other = _commit(tmp_path, "pkg/other.py", "x = 1\n", "unrelated")
    head = _git(tmp_path, "rev-parse", "HEAD")
    out = op._handler({"run_id": RUN, "sha": other}, tmp_path)
    assert out["ok"] is False and "pkg/a.py" in out["error"], out
    assert _git(tmp_path, "rev-parse", "HEAD") == head


def test_already_closed_checkpoint_keeps_the_never_ran_refusal(tmp_path):
    base = _repo(tmp_path)
    land = _commit(
        tmp_path, "pkg/a.py", "a = 2\n", f"hand-land\n\nCheckpoint-Run-Closed: {base}",
    )
    out = op._handler({"run_id": RUN, "sha": land}, tmp_path)
    assert out["ok"] is False and "never ran" in out["error"], out


def test_run_without_a_checkpoint_keeps_the_never_ran_refusal(tmp_path):
    _git(tmp_path, "init", "-q")
    only = _commit(tmp_path, "one", "one", "one")
    out = op._handler({"run_id": RUN, "sha": only}, tmp_path)
    assert out["ok"] is False and "never ran" in out["error"], out


def _two_checkpoints(tmp_path):
    base = _git(tmp_path, "init", "-q") or _commit(tmp_path, "one", "one", "one")
    rd = record_dir(tmp_path, RUN)
    rel = (rd / "manifest.json").relative_to(tmp_path).as_posix()
    _commit(tmp_path, rel, json.dumps({"head_sha": base}), "manifest")
    for path, row in (("pkg/a.py", "C1"), ("pkg/b.py", "C2")):
        _commit(
            tmp_path, path, "x = 1\n",
            f"checkpoint(wave 0): 1 row - {row}\n\nCheckpoint-Not-Landed: run-open\nCheckpoint-Base: {base}",
        )
    return base


def test_two_landing_shas_each_carrying_one_checkpoint_path_close_the_run(tmp_path):
    base = _two_checkpoints(tmp_path)
    first = _commit(tmp_path, "pkg/a.py", "x = 2\n", "land a")
    second = _commit(tmp_path, "pkg/b.py", "x = 2\n", "land b")
    out = op._handler({"run_id": RUN, "shas": [first, second]}, tmp_path)
    assert out["ok"] is True, out
    assert out["checkpoint_close_commit"] == _git(tmp_path, "rev-parse", "HEAD")
    assert run_closed_bases(_git(tmp_path, "log", "-1", "--format=%B")) == {base}


def test_a_multi_sha_landing_missing_one_checkpoint_path_still_refuses(tmp_path):
    _two_checkpoints(tmp_path)
    first = _commit(tmp_path, "pkg/a.py", "x = 2\n", "land a")
    second = _commit(tmp_path, "pkg/other.py", "x = 1\n", "unrelated")
    out = op._handler({"run_id": RUN, "shas": [first, second]}, tmp_path)
    assert out["ok"] is False and "pkg/b.py" in out["error"], out
