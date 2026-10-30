"""commit_halted_record writes the ledger-halt pointer on a ledger-check halt and unlinks it otherwise."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.emit import _DISPATCH_REPORT_DIR
from coordinator_core.win_portability import no_console_creationflags

from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALTED_RECORD_PARAM,
    LEDGER_HALT_FILE,
    RECORD_DIR_ROOT,
    ledger_halt_path,
    read_ledger_halt,
)
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261015T000000-aaaaaa"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    return root


def _write_record(root: Path, run_id: str, names=("ask.md", "pm-intent.md", "run.json")) -> Path:
    rec = root / RECORD_DIR_ROOT / run_id
    rec.mkdir(parents=True, exist_ok=True)
    for name in names:
        (rec / name).write_text(f"{name}\n", encoding="utf-8")
    return rec


def _call(root, **extra) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, **extra}
    return land_run._handler(params, repo_root=root)


PLAN = "docs/plans/2026-10-16-demo.md"
SIZING = "state/sizings/demo.yaml"
LEDGER_HALT = {
    "halted": "ledger-check",
    "plan": PLAN,
    "items": [{"id": "R1"}, {"id": "R2"}],
}


def _with_sizing(root):
    rec = _write_record(root, RUN)
    (rec / "run.json").write_text(f'{{"sizing_rel": "{SIZING}"}}\n', encoding="utf-8")
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / SIZING).write_text("a: 1\n", encoding="utf-8")


def test_ledger_check_halt_writes_pointer_with_blocking_ids(repo):
    _with_sizing(repo)

    out = _call(repo, **{HALTED_RECORD_PARAM: LEDGER_HALT})

    assert out["committed"] is True, out
    halt = read_ledger_halt(ledger_halt_path(repo, PLAN))
    assert halt is not None
    assert (halt.run_id, halt.plan_rel, halt.sizing_rel, halt.blocking) == (RUN, PLAN, SIZING, ("R1", "R2"))
    assert ledger_halt_path(repo, PLAN).as_posix() not in _git(["show", "--name-only", "--format=", "HEAD"], repo)


def test_nothing_to_commit_halt_still_writes_pointer(repo):
    _with_sizing(repo)
    _call(repo, **{HALTED_RECORD_PARAM: LEDGER_HALT})
    ledger_halt_path(repo, PLAN).unlink()

    out = _call(repo, **{HALTED_RECORD_PARAM: LEDGER_HALT})

    assert out.get("nothing_to_commit") is True
    assert read_ledger_halt(ledger_halt_path(repo, PLAN)) is not None


def test_other_halt_unlinks_stale_pointer(repo):
    _with_sizing(repo)
    _call(repo, **{HALTED_RECORD_PARAM: LEDGER_HALT})
    assert ledger_halt_path(repo, PLAN).is_file()

    _call(repo, **{HALTED_RECORD_PARAM: {"halted": "usage-limit", "plan": PLAN}})

    assert not ledger_halt_path(repo, PLAN).exists()


def test_halt_without_plan_writes_nothing(repo):
    _with_sizing(repo)

    _call(repo, **{HALTED_RECORD_PARAM: {"halted": "ledger-check", "items": [{"id": "R1"}]}})

    assert not ledger_halt_path(repo, PLAN).exists()
    assert not (repo / _DISPATCH_REPORT_DIR).exists()


def test_escaping_plan_writes_nothing(repo):
    _with_sizing(repo)

    _call(repo, **{HALTED_RECORD_PARAM: {**LEDGER_HALT, "plan": "../outside.md"}})

    assert not (repo.parent / ".coordinator-local").exists()
    assert not (repo / _DISPATCH_REPORT_DIR).exists()
    assert not list(repo.rglob(LEDGER_HALT_FILE))


def test_spawn_count_matches_a_halt_without_a_plan(repo, monkeypatch):
    _with_sizing(repo)
    real = subprocess.Popen
    count = {"n": 0}

    def spy(*a, **k):
        count["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    first = _call(repo, **{HALTED_RECORD_PARAM: {"halted": "ledger-check", "items": [{"id": "R1"}]}})
    assert first["committed"] is True, first
    # The halt commit is the zero-spawn in-process writer (commit_v2 / DR-344 brightline), so the planless
    # baseline is 0 by design. The pin is that writing the pointer adds no spawn over it; the liveness
    # probe below keeps the equality from passing vacuously on a dead spy.
    without_plan = count["n"]

    _git(["reset", "-q", "--hard", "HEAD~1"], repo)
    _with_sizing(repo)
    count["n"] = 0
    _call(repo, **{HALTED_RECORD_PARAM: LEDGER_HALT})
    assert read_ledger_halt(ledger_halt_path(repo, PLAN)) is not None
    assert count["n"] == without_plan
    _git(["--version"], repo)
    assert count["n"] == without_plan + 1, "spawn spy is not counting"


TEST_HALT = {
    "halted": "review_blocked",
    "blocker": "failing_test",
    "plan": PLAN,
    "items": [{"id": "R1", "remaining": ["tests/test_x.py::test_y fails: assert 1 == 2"], "evidence": "e"}],
}


def test_failing_test_halt_writes_pointer_and_resume_ledger_carries_the_gap_lines(repo):
    from dataclasses import replace

    from coordinator_core.ops.dispatch_emit.ask_contract import read_halt_record
    from coordinator_core.ops.dispatch_emit.ask_gate import _resume_ledger

    _with_sizing(repo)
    (repo / RECORD_DIR_ROOT / RUN / "manifest.json").write_text(
        '{"rows": [{"id": "R1"}], "rulable_ids": ["R1"]}\n', encoding="utf-8"
    )

    out = _call(repo, **{HALTED_RECORD_PARAM: TEST_HALT})

    assert out["committed"] is True, out
    halt = read_ledger_halt(ledger_halt_path(repo, PLAN))
    assert halt is not None
    assert (halt.run_id, halt.plan_rel, halt.sizing_rel, halt.blocking) == (RUN, PLAN, SIZING, ("R1",))
    record = read_halt_record(repo, RUN)
    assert record is not None
    halt = replace(halt, remaining=record.remaining)
    sizing = {"requirement_register": {"rows": [{"id": "R1", "text": "t", "status": "open"}]}}
    ledger = _resume_ledger(sizing, halt, repo)
    assert ledger["remaining"]["R1"]["remaining"] == ["tests/test_x.py::test_y fails: assert 1 == 2"]


def test_halt_blocked_on_findings_still_unlinks_the_pointer(repo):
    _with_sizing(repo)
    _call(repo, **{HALTED_RECORD_PARAM: LEDGER_HALT})
    assert ledger_halt_path(repo, PLAN).is_file()

    _call(repo, **{HALTED_RECORD_PARAM: {"halted": "review_blocked", "plan": PLAN, "items": [{"id": "R1"}]}})

    assert not ledger_halt_path(repo, PLAN).exists()


def test_failing_test_halt_through_the_halted_record_path_writes_the_pointer(repo):
    from coordinator_core.ops.dispatch_emit.halted_record_commit import commit_halted_record

    _with_sizing(repo)
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: TEST_HALT}
    commit_halted_record(repo, repo, params)
    assert read_ledger_halt(ledger_halt_path(repo, PLAN)) is not None
