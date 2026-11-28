"""A takeover of a halted run whose recorded baton was archived follows `continued_into` to the live or terminal record."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton, record_dir, write_run_baton
from coordinator_core.ops.dispatch_emit.resume_gate import TAKEOVER_LOG, resolve_baton_record
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    REL,
    WARP,
    _halt,
    composed,
    repo,
)
from coordinator_core.ops.fleet._common import handoff_archive_dest

pytestmark = pytest.mark.complete

RECORDED = "state/handoffs/2026-11/2026-11-26_175744_fixture-baton.md"
MID = "archive/handoffs/2026-11/2026-11-27_010101_fixture-successor.md"
LIVE = "state/handoffs/2026-11/2026-11-28_020202_fixture-live.md"


def _put(repo: Path, rel: str, continued_into: str | None) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"continued_into: {continued_into}\n" if continued_into else ""
    path.write_text(f"---\nhandoff_id: hnd-{path.stem}\n{line}---\nbody\n", encoding="utf-8")


def _archived_chain(repo: Path, tail: str | None) -> str:
    """Archive the recorded baton at its archive dest, continued into MID, which continues into `tail`."""
    dest = handoff_archive_dest(repo, repo / RECORDED).relative_to(repo).as_posix()
    _put(repo, dest, MID)
    _put(repo, MID, tail)
    write_run_baton(repo, WARP, RECORDED)
    return dest


def _stage(repo: Path, session: str) -> None:
    (record_dir(repo, WARP) / "run-stage.json").write_text(
        json.dumps({"stage": "commit", "session_id": session}), encoding="utf-8")


def _take(repo: Path):
    return op._dispatch_emit(
        {"ask": True, "sizing_path": REL, "session_id": "s1", "takeover": {"reason": "peer died"}}, repo_root=repo)


def test_takeover_rebinds_an_archived_baton_to_the_live_end_of_its_chain(repo, composed) -> None:
    _halt(repo, WARP, "a" * 40)
    _stage(repo, "other")
    _archived_chain(repo, LIVE)
    _put(repo, LIVE, None)
    assert not (repo / RECORDED).exists()
    assert _take(repo)["run_id"] == WARP
    assert read_run_baton(repo, WARP) == LIVE
    assert (record_dir(repo, WARP) / TAKEOVER_LOG).is_file()


def test_takeover_rebinds_to_the_archived_terminal_record_when_no_live_one_exists(repo, composed) -> None:
    _halt(repo, WARP, "a" * 40)
    _stage(repo, "other")
    _archived_chain(repo, None)
    assert _take(repo)["run_id"] == WARP
    assert read_run_baton(repo, WARP) == MID


def test_a_failed_baton_rewrite_refuses_the_takeover_naming_fresh_and_the_kept_work(repo, composed, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit import ask_contract

    _halt(repo, WARP, "a" * 40)
    _stage(repo, "other")
    _archived_chain(repo, LIVE)
    _put(repo, LIVE, None)

    def _fail(*_a, **_k):
        raise OSError("disk")

    monkeypatch.setattr(ask_contract, "write_run_baton", _fail)
    with pytest.raises(ValueError, match=r"args\.fresh.*kept work"):
        _take(repo)
    assert read_run_baton(repo, WARP) == RECORDED
    assert not (record_dir(repo, WARP) / TAKEOVER_LOG).exists()


def test_takeover_with_a_baton_that_resolves_nowhere_names_fresh_and_the_kept_work(repo, composed) -> None:
    _halt(repo, WARP, "a" * 40)
    _stage(repo, "other")
    write_run_baton(repo, WARP, RECORDED)
    with pytest.raises(ValueError, match=r"args\.fresh.*kept work"):
        _take(repo)
    assert not (record_dir(repo, WARP) / TAKEOVER_LOG).exists()


def test_a_continued_into_cycle_terminates(repo) -> None:
    dest = _archived_chain(repo, None)
    _put(repo, MID, dest)
    assert resolve_baton_record(repo, RECORDED) == MID


def test_a_dangling_continued_into_stays_on_the_last_resolvable_record(repo) -> None:
    _archived_chain(repo, "state/handoffs/2026-11/never-existed.md")
    assert resolve_baton_record(repo, RECORDED) == MID
