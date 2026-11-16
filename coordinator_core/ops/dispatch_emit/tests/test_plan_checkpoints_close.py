"""dispatch.close_plan_checkpoints: one close commit for every open run-open checkpoint of a plan, no run needed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity
from coordinator_core.ops.dispatch_emit import plan_checkpoints_close as op
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_bases
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "docs/plans/2026-11/2026-11-16-alpha-aaaaaa.md"
OTHER = "docs/plans/2026-11/2026-11-16-beta-bbbbbb.md"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=str(repo), check=True, capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _checkpoint(repo: Path, plan: str, base: str, row: str = "C1") -> None:
    msg = (
        f"checkpoint(wave 1): 1 row — {row}\n\nCheckpoint-Plan: {plan}\n"
        f"Checkpoint-Base: {base}\nCheckpoint-Not-Landed: mid-run"
    )
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)


def _closed(repo: Path, base: str) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"close\n\nCheckpoint-Run-Closed: {base}")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict]:
    """Plan alpha holds three run-open checkpoints (one already closed) and no halt record; plan beta holds one."""
    _git(tmp_path, "init", "-q")
    for rel in (PLAN, OTHER):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("plan\n", encoding="utf-8")
    _git(tmp_path, "add", "--", PLAN, OTHER)
    _git(tmp_path, "commit", "-q", "-m", "plans")
    bases = {k: c * 40 for k, c in {"a1": "1", "a2": "2", "a3": "3", "b1": "4"}.items()}
    _checkpoint(tmp_path, PLAN, bases["a1"])
    _checkpoint(tmp_path, PLAN, bases["a2"], "C2")
    _checkpoint(tmp_path, OTHER, bases["b1"])
    _checkpoint(tmp_path, PLAN, bases["a3"], "C3")
    _closed(tmp_path, bases["a3"])
    return tmp_path, bases


def _count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD"))


def test_closes_every_open_base_of_the_plan_with_only_plan_path(repo) -> None:
    root, bases = repo
    before = _count(root)
    reply = op._handler({"plan_path": PLAN}, root)
    assert reply["ok"] is True
    assert set(reply["closed"]) == {bases["a1"], bases["a2"]}
    assert _count(root) == before + 1
    assert run_closed_bases(_git(root, "log", "-1", "--format=%B")) == {bases["a1"], bases["a2"]}
    assert landing_integrity.open_plan_bases(root, PLAN) == []
    assert landing_integrity.open_plan_bases(root, OTHER) == [bases["b1"]]


def test_second_call_is_a_noop_without_a_commit(repo) -> None:
    root, _ = repo
    op._handler({"plan_path": PLAN}, root)
    before = _count(root)
    reply = op._handler({"plan_path": PLAN}, root)
    assert reply["ok"] is True and reply["noop"] is True and reply["closed"] == []
    assert _count(root) == before


def test_moved_plan_matches_by_file_name(repo) -> None:
    root, bases = repo
    moved = root / "docs/plans/archive/2026-11-16-alpha-aaaaaa.md"
    moved.parent.mkdir(parents=True)
    moved.write_text("plan\n", encoding="utf-8")
    _git(root, "add", "--", "docs/plans/archive")
    _git(root, "commit", "-q", "-m", "moved")
    reply = op._handler({"plan_path": "docs/plans/archive/2026-11-16-alpha-aaaaaa.md"}, root)
    assert set(reply["closed"]) == {bases["a1"], bases["a2"]}


def test_scan_is_not_capped_short_of_old_checkpoints(repo, monkeypatch) -> None:
    root, bases = repo
    for i in range(6):
        _git(root, "commit", "-q", "--allow-empty", "-m", f"filler {i}")
    monkeypatch.setattr(landing_integrity, "_SIBLING_LOG_CAP", 3)
    assert set(landing_integrity.open_plan_bases(root, PLAN)) == {bases["a1"], bases["a2"]}
    # The capped run-less scan still stops short: the cap is what the new route lifts.
    capped = landing_integrity._open_sibling_bases(root, None, {"plan_path": PLAN}, (), (), True)
    assert capped == []


def test_since_bounds_the_scan(repo) -> None:
    root, bases = repo
    newest_open = _git(root, "log", "--format=%H", "--grep=Checkpoint-Base: " + bases["a2"], "-1")
    assert landing_integrity.open_plan_bases(root, PLAN, since=newest_open) == [bases["a2"]]


def test_unknown_plan_and_bad_params_write_nothing(repo) -> None:
    root, _ = repo
    before = _count(root)
    assert op._handler({"plan_path": "docs/plans/none-zzzzzz.md"}, root)["noop"] is True
    assert op._handler({}, root)["ok"] is False
    assert op._handler({"plan_path": PLAN, "since": ""}, root)["ok"] is False
    assert op._handler({"plan_path": PLAN}, None)["ok"] is False
    assert _count(root) == before


def test_missing_anchor_file_refuses_without_a_commit(repo) -> None:
    root, _ = repo
    before = _count(root)
    reply = op._handler({"plan_path": "docs/plans/gone/2026-11-16-alpha-aaaaaa.md"}, root)
    assert reply["ok"] is False
    assert _count(root) == before
