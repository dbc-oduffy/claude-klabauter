"""The met_by path resolves a run's halt commit without a path-limited ``git log`` history walk."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SOLO = "warp-2026-10-20-solo-20261020T000000-abcdef"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


@pytest.fixture
def halted(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "before")
    marker = tmp_path / "state" / "warp" / SOLO / "halted.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({"status": "halted", "halted": "ledger-check", "sizing_rel": None, "blocking": ["R1"]}),
        encoding="utf-8",
    )
    _git(tmp_path, "add", "--", "state")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    halt = _git(tmp_path, "rev-parse", "HEAD")
    after = [_commit(tmp_path, f"after{i}") for i in range(3)]
    return tmp_path, marker, halt, after


def _unbounded_path_logs(spawns) -> list:
    return [s.argv for s in spawns if "log" in s.argv and "--" in s.argv]


def test_met_by_closes_a_halted_run_with_no_path_limited_git_log(halted, monkeypatch: pytest.MonkeyPatch) -> None:
    root, marker, halt, after = halted
    with _count_spawns_attributed(monkeypatch) as spawns:
        out = op._handler({"run_id": SOLO, "sha": after[-1], "met_by": {"R1": after[0]}}, root)
    assert out["ok"], out
    assert json.loads(marker.read_text(encoding="utf-8"))["met_by"] == {"R1": after[0]}
    assert _unbounded_path_logs(spawns) == []


def test_the_halt_commit_is_the_one_that_added_halted_json(halted) -> None:
    root, marker, halt, _ = halted
    assert op._halt_commit(root, marker.parent) == (halt, None)


def test_a_batched_halt_subject_names_the_commit_when_halted_json_is_gitignored(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("state/warp/\n", encoding="utf-8")
    _commit(tmp_path, "before")
    marker = tmp_path / "state" / "warp" / SOLO / "halted.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({"status": "halted", "halted": "ledger-check", "sizing_rel": None, "blocking": ["R1"]}),
        encoding="utf-8",
    )
    (tmp_path / "evidence").write_text("e", encoding="utf-8")
    _git(tmp_path, "add", "--", "evidence")
    _git(tmp_path, "commit", "-q", "-m", f"warp: record halted run-a, {SOLO}, run-b run artifacts")
    halt = _git(tmp_path, "rev-parse", "HEAD")
    after = _commit(tmp_path, "after")
    with _count_spawns_attributed(monkeypatch) as spawns:
        out = op._handler({"run_id": SOLO, "sha": after, "met_by": {"R1": after}}, tmp_path)
    assert out["ok"], out
    assert op._halt_commit(tmp_path, marker.parent) == (halt, None)
    assert _unbounded_path_logs(spawns) == []
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", f"warp: record halted {SOLO}-x run artifacts")
    assert op._halt_commit(tmp_path, marker.parent) == (halt, None)


def test_a_halt_commit_past_the_walk_window_reads_as_not_found(halted, monkeypatch: pytest.MonkeyPatch) -> None:
    root, marker, _, _ = halted
    monkeypatch.setattr(op, "_HALT_WALK_COMMITS", 2)
    assert op._halt_commit(root, marker.parent) == (None, None)


def test_a_met_by_before_the_halt_still_refuses(halted) -> None:
    root, _, halt, after = halted
    before = _git(root, "rev-parse", f"{halt}~1")
    out = op._handler({"run_id": SOLO, "sha": after[-1], "met_by": {"R1": before}}, root)
    assert not out["ok"] and "before the halt" in out["error"]
