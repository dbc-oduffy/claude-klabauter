"""Timelines of runs recorded halted `abandoned_at_minted` while their Workflow was executing (054f4c,
e67326) and of one genuinely unstarted mint (cb5854): live activity spares the halt wherever it lands,
a live run token never does."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RUN_DIR_ROOT,
    record_dir,
    stamp_plan_phase_at,
    write_run_stage_at,
)
from coordinator_core.session import core as session_core

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
PEER = "5e1f0000-0000-4000-8000-0000000000b2"
ASK = "ask-20261116T201405-054f4c"
SIZING = "state/sizings/2026-11-16-x.yaml"
MIN = 60.0


def _stamp(age: float) -> str:
    return datetime.fromtimestamp(time.time() - age, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_mtime(path: Path, age: float) -> None:
    when = time.time() - age
    os.utime(path, (when, when))


def _make_run(
    repo: Path, run_id: str, *, stage: str, age: float, run_json: dict, plan_age: float | None = None,
    activity_age: float | None = None, held: bool = False,
) -> Path:
    d = record_dir(repo, run_id)
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    if held:
        (d / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1", "writes": ["src/held.py"]}]}), encoding="utf-8")
        run_token.mint(repo, run_id, "ask.mjs", session_id=PEER)
    write_run_stage_at(d, stage, now=_stamp(age))
    if plan_age is not None:
        stamp_plan_phase_at(d, now=_stamp(plan_age))
    for entry in d.iterdir():
        _set_mtime(entry, age)
    _set_mtime(d, age)
    if activity_age is not None:
        write_run_stage_at(d, stage, now=_stamp(activity_age))
    return d


def _scratch_write(repo: Path, run_id: str, rel: str, age: float) -> Path:
    """A file written `age` seconds ago under the run's scratch dir; every directory above it keeps an
    old mtime, as an in-place rewrite of an existing file leaves it."""
    base = Path(repo) / RUN_DIR_ROOT / run_id
    target = base / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    _set_mtime(target, age)
    old = 4 * 3600
    for parent in (*target.parents,):
        if parent == Path(repo):
            break
        _set_mtime(parent, old)
        if parent == base:
            break
    return target


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    session_core.init(SELF, cwd=str(root))
    session_core.init(PEER, cwd=str(root))
    return root


def _live(repo: Path, run_id: str) -> bool:
    record = next(r for r in run_token.held_records(repo) if r["run_id"] == run_id)
    return wrd._live_peer_run(repo, record, SELF, str(repo))


def _halted_kind(d: Path) -> str | None:
    marker = d / "halted.json"
    return json.loads(marker.read_text(encoding="utf-8"))["halted"] if marker.is_file() else None


def test_054f4c_nested_scratch_write_spares_a_planning_run(repo):
    ask = _make_run(repo, ASK, stage="minted", age=60 * MIN, plan_age=50 * MIN, run_json={"sizing_rel": SIZING}, held=True)
    _scratch_write(repo, ASK, "blitz/wave-0-x/out.json", age=2 * MIN)
    assert _live(repo, ASK)
    assert _halted_kind(ask) is None


def test_054f4c_quiet_planning_run_is_still_halted(repo):
    ask = _make_run(repo, ASK, stage="minted", age=60 * MIN, plan_age=50 * MIN, run_json={"sizing_rel": SIZING}, held=True)
    _scratch_write(repo, ASK, "blitz/wave-0-x/out.json", age=40 * MIN)
    assert not _live(repo, ASK)
    assert _halted_kind(ask) == wrd.ABANDONED_AT_MINTED


def test_nested_write_deeper_than_the_walk_bound_is_not_seen(repo):
    _make_run(repo, ASK, stage="minted", age=60 * MIN, run_json={"sizing_rel": SIZING})
    _scratch_write(repo, ASK, "a/b/c/d/deep.json", age=1 * MIN)
    now = time.time()
    seen = wrd.run_activity_epoch(repo, ASK, now)
    assert seen is None or now - seen > wrd.MINTED_STALE_SECONDS


def test_e67326_child_scratch_write_spares_a_quiet_ask_run(repo):
    ask = _make_run(repo, ASK, stage="minted", age=40 * MIN, plan_age=40 * MIN, run_json={"sizing_rel": SIZING}, held=True)
    child = _make_run(repo, "warp-child-1", stage="staged", age=40 * MIN, run_json={"sizing_rel": SIZING})
    _scratch_write(repo, "warp-child-1", "wave-1/step.log", age=1 * MIN)
    _set_mtime(Path(repo) / RUN_DIR_ROOT / "warp-child-1", 1 * MIN)
    assert child.is_dir()
    assert _live(repo, ASK)
    assert _halted_kind(ask) is None


def test_e67326_child_last_activity_spares_a_quiet_ask_run(repo):
    ask = _make_run(repo, ASK, stage="minted", age=40 * MIN, plan_age=40 * MIN, run_json={"sizing_rel": SIZING}, held=True)
    _make_run(repo, "warp-child-2", stage="staged", age=40 * MIN, activity_age=1 * MIN, run_json={"sizing_rel": SIZING})
    assert _live(repo, ASK)
    assert _halted_kind(ask) is None


def test_child_scratch_dir_mtime_alone_makes_it_a_candidate(repo):
    _make_run(repo, ASK, stage="minted", age=40 * MIN, run_json={"sizing_rel": SIZING})
    _make_run(repo, "warp-child-3", stage="staged", age=40 * MIN, run_json={"resumed_from": ASK})
    scratch = Path(repo) / RUN_DIR_ROOT / "warp-child-3"
    scratch.mkdir(parents=True)
    (scratch / "note.txt").write_text("x", encoding="utf-8")
    now = time.time()
    seen = wrd.run_activity_epoch(repo, ASK, now)
    assert seen is not None and now - seen <= wrd.MINTED_STALE_SECONDS


def test_live_token_without_activity_is_not_liveness(repo):
    ask = _make_run(repo, "ask-20261116T221417-cb5854", stage="minted", age=31 * MIN, run_json={"sizing_rel": SIZING}, held=True)
    assert run_token.is_live(repo, "ask-20261116T221417-cb5854")
    assert not _live(repo, "ask-20261116T221417-cb5854")
    assert _halted_kind(ask) == wrd.ABANDONED_AT_MINTED


def test_live_token_on_a_quiet_child_does_not_spare_the_ask(repo):
    ask = _make_run(repo, ASK, stage="minted", age=40 * MIN, run_json={"sizing_rel": SIZING}, held=True)
    _make_run(repo, "warp-child-4", stage="staged", age=40 * MIN, run_json={"sizing_rel": SIZING}, held=True)
    assert not _live(repo, ASK)
    assert _halted_kind(ask) == wrd.ABANDONED_AT_MINTED


def test_run_activity_epoch_over_2000_nested_entries_is_cheap(repo):
    _make_run(repo, ASK, stage="minted", age=60 * MIN, run_json={"sizing_rel": SIZING})
    base = Path(repo) / RUN_DIR_ROOT / ASK
    for i in range(20):
        sub = base / f"wave-{i}"
        sub.mkdir(parents=True)
        for j in range(99):
            (sub / f"f{j}.txt").write_text("x", encoding="utf-8")
    future = time.time() + 10 * wrd.MINTED_STALE_SECONDS
    start = time.process_time()
    seen = wrd.run_activity_epoch(repo, ASK, future)
    assert time.process_time() - start < 0.05
    assert seen is not None and future - seen <= wrd.MINTED_STALE_SECONDS


def test_run_activity_epoch_under_the_entry_cap_still_reads_quiet(repo):
    _make_run(repo, ASK, stage="minted", age=60 * MIN, run_json={"sizing_rel": SIZING})
    base = Path(repo) / RUN_DIR_ROOT / ASK
    base.mkdir(parents=True, exist_ok=True)
    (base / "f.txt").write_text("x", encoding="utf-8")
    future = time.time() + 10 * wrd.MINTED_STALE_SECONDS
    seen = wrd.run_activity_epoch(repo, ASK, future)
    assert seen is not None and future - seen > wrd.MINTED_STALE_SECONDS
