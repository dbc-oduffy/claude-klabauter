"""dispatch.close_halted_run_rejected: EM-declared rejected close of a halted run."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, halted_withheld_index, kept_work
from coordinator_core.ops.dispatch_emit import halted_run_rejected as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-x-20261026T000000-abcdef"
HALT_AT = "2026-01-10T00:00:00Z"
POST_HALT = "2026-01-20T00:00:00Z"
REASON = "only loosened tests to tolerate stray files"


def _git(repo: Path, *args: str, stdin: str | None = None, date: str | None = None) -> str:
    env = {**os.environ, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else None
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args], cwd=str(repo),
        check=True, capture_output=True, text=True, input=stdin, env=env, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, files: dict, date: str = POST_HALT) -> str:
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "-q", "-m", "c", date=date)
    return _git(repo, "rev-parse", "HEAD")


def _blob(repo: Path, text: str) -> str:
    return _git(repo, "hash-object", "-w", "--stdin", stdin=text)


@pytest.fixture
def halted(tmp_path):
    """A repo with a halted RUN withholding a.txt and b.txt; both unchanged at HEAD since the halt anchor."""
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"a.txt": "a0", "b.txt": "b0"}, date="2026-01-01T00:00:00Z")
    record = tmp_path / ask_contract.RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(record, halted=RUN, sizing_rel=None, run_base_sha=base)
    ask_contract.write_run_stage_at(record, "halted", now=HALT_AT)
    halted_run.record_withheld(record, {"a.txt": "kept", "b.txt": "kept"})
    kept_work.write(record, {"c": {"paths": {"a.txt": _blob(tmp_path, "a-kept"), "b.txt": _blob(tmp_path, "b-kept")}}}, None)
    return tmp_path, record


def _call(root, rejected, reason=REASON):
    return op._handler({"run_id": RUN, "rejected_paths": rejected, "reason": reason}, root)


def _indexed(root) -> bool:
    return (root / halted_withheld_index.INDEX_DIR / f"{RUN}.json").is_file()


def _unchanged(root, record, before):
    assert (record / halted_run.HALTED_FILE).read_text(encoding="utf-8") == before
    assert _indexed(root)


def test_rejected_paths_equal_to_anchor_close_run_and_release_index(halted):
    root, record = halted
    assert _indexed(root)
    reply = _call(root, ["a.txt", "b.txt"])
    assert reply["ok"] is True and reply["reason"] == REASON
    assert {d["disposition"] for d in reply["dispositions"].values()} == {"rejected"}
    marker = halted_run._read(record)
    assert marker["status"] == halted_run.STATUS_CLOSED
    assert marker["closed"]["reason"] == REASON and marker["closed"]["disposition"] == "rejected"
    assert marker[halted_run.PATH_DISPOSITIONS_FIELD] == reply["dispositions"]
    assert not _indexed(root)


def test_mixed_rejected_and_landed_closes(halted):
    root, record = halted
    _commit(root, {"b.txt": "b-kept"})
    reply = _call(root, ["a.txt"])
    assert reply["ok"] is True
    assert reply["dispositions"]["a.txt"]["disposition"] == "rejected"
    assert reply["dispositions"]["b.txt"]["disposition"] == "landed"
    assert not _indexed(root)


def test_rejected_path_changed_at_head_refuses_naming_path(halted):
    root, record = halted
    _commit(root, {"a.txt": "a-later"})
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root, ["a.txt", "b.txt"])
    assert reply["ok"] is False and "a.txt" in reply["error"] and "b.txt" not in reply["error"]
    _unchanged(root, record, before)


def test_unrejected_untouched_path_refuses(halted):
    root, record = halted
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root, ["a.txt"])
    assert reply["ok"] is False and "b.txt" in reply["error"]
    _unchanged(root, record, before)


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_reason_is_required(halted, reason):
    root, record = halted
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    reply = _call(root, ["a.txt", "b.txt"], reason=reason)
    assert reply["ok"] is False and "reason" in reply["error"]
    _unchanged(root, record, before)


def test_rejected_paths_required_and_must_be_withheld(halted):
    root, record = halted
    before = (record / halted_run.HALTED_FILE).read_text(encoding="utf-8")
    assert _call(root, [])["ok"] is False
    reply = _call(root, ["a.txt", "zzz.txt"])
    assert reply["ok"] is False and "zzz.txt" in reply["error"]
    _unchanged(root, record, before)


def test_non_halted_run_refuses(halted):
    root, record = halted
    assert _call(root, ["a.txt", "b.txt"])["ok"] is True
    reply = _call(root, ["a.txt", "b.txt"])
    assert reply["ok"] is False and "closed" in reply["error"]
