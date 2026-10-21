"""read_halt_record(at_sha=) recovers a ledger-check record a later halt overwrote in the worktree."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import read_halt_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "run-1"
SIZING = "state/sizings/s.yaml"
LEDGER = {
    "status": "halted",
    "halted": "ledger-check",
    "sizing_rel": SIZING,
    "plan": "docs/plans/p.md",
    "blocking": ["R1"],
    "blocking_items": [{"id": "R1", "remaining": ["fix a", "fix b"], "evidence": "e"}],
}
MANIFEST = {"rows": [{"id": "C1"}], "owed": [{"id": "C9"}], "gated": []}


def _git(repo, *a):
    return subprocess.run(
        ["git", *a], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _write(repo, name, obj):
    path = repo / "state" / "warp" / RUN / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return _git(tmp_path, "rev-parse", "HEAD")


def _halt_commit(tmp_path):
    _write(tmp_path, "halted.json", LEDGER)
    _write(tmp_path, "manifest.json", MANIFEST)
    _git(tmp_path, "add", "state")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    return _git(tmp_path, "rev-parse", "HEAD")


def test_overwritten_worktree_record_is_read_from_the_halt_commit(tmp_path):
    _repo(tmp_path)
    halt = _halt_commit(tmp_path)
    _write(tmp_path, "halted.json", {"status": "halted", "halted": "refusal"})
    assert read_halt_record(tmp_path, RUN) is None
    rec = read_halt_record(tmp_path, RUN, at_sha=halt)
    assert rec is not None
    assert rec.blocking == ("R1",)
    assert rec.staged == {"C1", "C9"}
    assert rec.remaining["R1"]["remaining"] == ["fix a", "fix b"]


def test_ledger_check_worktree_record_ignores_at_sha(tmp_path):
    seed = _repo(tmp_path)
    _halt_commit(tmp_path)
    _write(tmp_path, "halted.json", {**LEDGER, "blocking": ["R2"]})
    rec = read_halt_record(tmp_path, RUN, at_sha=seed)
    assert rec is not None and rec.blocking == ("R2",)


def test_commit_without_the_file_answers_none(tmp_path):
    seed = _repo(tmp_path)
    _halt_commit(tmp_path)
    _write(tmp_path, "halted.json", {"status": "halted", "halted": "refusal"})
    assert read_halt_record(tmp_path, RUN, at_sha=seed) is None


@pytest.mark.parametrize("bad", ["", "abc", "z" * 40, "0" * 40, "../x"])
def test_bad_sha_answers_none(tmp_path, bad):
    _repo(tmp_path)
    _halt_commit(tmp_path)
    _write(tmp_path, "halted.json", {"status": "halted", "halted": "refusal"})
    assert read_halt_record(tmp_path, RUN, at_sha=bad) is None


def test_unsafe_run_id_answers_none_with_at_sha(tmp_path):
    seed = _repo(tmp_path)
    assert read_halt_record(tmp_path, "../escape", at_sha=seed) is None
