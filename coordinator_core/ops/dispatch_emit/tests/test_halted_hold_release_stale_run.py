"""halted_hold_release.stale_release: a not-live, not-refire-pending halted run frees its hold past the bound."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIZING = "state/sizings/s-stale.yaml"
OLD = hr.STALE_HOLD_BOUND_SECONDS + 600


def _mint(age, tail: str = "977c47") -> str:
    """A run id stamped `age` seconds ago: the stale bound counts from this stamp."""
    return time.strftime("ask-%Y%m%dT%H%M%S", time.gmtime(time.time() - age)) + "-" + tail


HALTED = _mint(OLD)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, **no_console_creationflags())
    return tmp_path


def _halt(root, status: str = "halted", age=OLD, run_id: str = HALTED, sizing=SIZING):
    rec = root / "state/warp" / run_id
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    path = rec / "halted.json"
    path.write_text(json.dumps({
        "status": status, "sizing_rel": sizing, "next_action": {"kind": "refire"},
        "run_artifacts": {"withheld": {"a.py": "halt-record-only"}},
    }), encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def test_past_bound_with_refire_next_action_and_no_token_is_released(root) -> None:
    _halt(root)
    assert hr.stale_release(root, HALTED) == hr.RELEASE_STALE_NOT_LIVE
    assert hr.release_reason(root, HALTED, "ask-20261030T223203-aaaaaa") == hr.RELEASE_STALE_NOT_LIVE


def test_inside_bound_still_holds(root) -> None:
    young = _mint(hr.STALE_HOLD_BOUND_SECONDS - 600)
    _halt(root, age=hr.STALE_HOLD_BOUND_SECONDS - 600, run_id=young)
    assert hr.stale_release(root, young) is None


def test_live_token_of_the_run_holds(root) -> None:
    _halt(root)
    run_token.mint(root, HALTED, "wf/x.mjs", sizing_rel=SIZING)
    assert hr.stale_release(root, HALTED) is None


def test_claimed_mid_commit_token_of_the_run_holds(root) -> None:
    _halt(root)
    token = run_token.mint(root, HALTED, "wf/x.mjs", sizing_rel=SIZING)
    assert run_token.redeem(root, HALTED, token, "wf/x.mjs")
    assert hr.stale_release(root, HALTED) is None


def test_refire_pending_on_the_same_sizing_holds(root) -> None:
    _halt(root)
    run_token.mint(root, "ask-20261030T223203-bbbbbb", "wf/y.mjs", sizing_rel=SIZING)
    assert hr.stale_release(root, HALTED) is None


def test_live_token_on_another_sizing_does_not_hold(root) -> None:
    _halt(root)
    run_token.mint(root, "ask-20261030T223203-bbbbbb", "wf/y.mjs", sizing_rel="state/sizings/other.yaml")
    assert hr.stale_release(root, HALTED) == hr.RELEASE_STALE_NOT_LIVE


def test_resumed_marker_past_bound_without_token_is_released(root) -> None:
    _halt(root, status="resumed")
    assert hr.stale_release(root, HALTED) == hr.RELEASE_STALE_NOT_LIVE


def test_landed_or_absent_marker_is_not_a_stale_release(root) -> None:
    _halt(root, status="landed")
    assert hr.stale_release(root, HALTED) is None
    assert hr.stale_release(root, "ask-20261030T000000-ffffff") is None


def test_unreadable_marker_holds(root) -> None:
    path = _halt(root)
    path.write_text("{not json", encoding="utf-8")
    old = time.time() - OLD
    os.utime(path, (old, old))
    assert hr.stale_release(root, HALTED) is None


def test_now_argument_moves_the_clock(root) -> None:
    fresh = _mint(0)
    _halt(root, age=0, run_id=fresh)
    assert hr.stale_release(root, fresh) is None
    assert hr.stale_release(root, fresh, now=time.time() + OLD) == hr.RELEASE_STALE_NOT_LIVE


def test_token_listing_is_read_once_per_scope(root, monkeypatch: pytest.MonkeyPatch) -> None:
    _halt(root)
    _halt(root, run_id="ask-20261030T102304-977c48")
    calls = []
    real = run_token.held_records
    monkeypatch.setattr(run_token, "held_records", lambda r: calls.append(r) or real(r))
    with hr.release_scope():
        hr.stale_release(root, HALTED)
        hr.stale_release(root, "ask-20261030T102304-977c48")
    assert len(calls) == 1


def _git(root, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True, capture_output=True,
        text=True, **no_console_creationflags(),
    )
    return out.stdout.strip()


def _moved_halt(root, *, move_head: bool, reason: str = "halt-record-only", sizing: str = SIZING):
    """A young halted run whose recorded base holds `a.py`; HEAD rewrites `a.py` after it when `move_head`;
    `a.py` is dirty in the worktree either way. Returns (run id, halted.json bytes)."""
    (root / "a.py").write_text("base\n", encoding="utf-8")
    _git(root, "add", "a.py")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    if move_head:
        (root / "a.py").write_text("moved\n", encoding="utf-8")
        _git(root, "commit", "-q", "-am", "moved")
    (root / "a.py").write_text("peer dirt\n", encoding="utf-8")
    run_id = _mint(60)
    path = _halt(root, age=60, run_id=run_id, sizing=sizing)
    marker = json.loads(path.read_text(encoding="utf-8"))
    marker["run_base_sha"] = base
    marker["run_artifacts"] = {"withheld": {"a.py": reason}}
    path.write_text(json.dumps(marker), encoding="utf-8")
    return run_id, path.read_bytes()


def test_hold_on_a_path_head_moved_past_its_base_is_released_and_the_record_is_untouched(root) -> None:
    run_id, before = _moved_halt(root, move_head=True)
    assert hr.held_paths(root, run_id, "ask-20261030T223203-aaaaaa") == frozenset()
    assert (root / "state/warp" / run_id / "halted.json").read_bytes() == before


def test_hold_on_a_path_head_has_not_moved_past_still_holds(root) -> None:
    run_id, _ = _moved_halt(root, move_head=False)
    assert hr.held_paths(root, run_id, "ask-20261030T223203-aaaaaa") == frozenset({"a.py"})


def test_moved_head_does_not_release_a_run_with_a_live_token(root) -> None:
    run_id, _ = _moved_halt(root, move_head=True)
    run_token.mint(root, run_id, "wf/x.mjs", sizing_rel=SIZING)
    assert hr.held_paths(root, run_id, "ask-20261030T223203-aaaaaa") == frozenset({"a.py"})


def test_moved_head_does_not_release_a_pending_refire_of_the_same_sizing(root) -> None:
    run_id, _ = _moved_halt(root, move_head=True)
    run_token.mint(root, "ask-20261030T223203-bbbbbb", "wf/y.mjs", sizing_rel=SIZING)
    assert hr.held_paths(root, run_id, "ask-20261030T223203-aaaaaa") == frozenset({"a.py"})


def test_moved_head_releases_only_a_halt_record_only_withhold(root) -> None:
    run_id, _ = _moved_halt(root, move_head=True, reason="run-dirt-uncommitted")
    assert hr.held_paths(root, run_id, "ask-20261030T223203-aaaaaa") == frozenset({"a.py"})
