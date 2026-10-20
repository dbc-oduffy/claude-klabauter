"""commit_halted_record writes the ledger-halt pointer on a ledger-check halt and unlinks it otherwise."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    ledger_halt_path,
    read_ledger_halt,
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _git,
    _write_record,
    repo,
)
from coordinator_core.ops.dispatch_emit import terminal_commit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _call(root, **extra) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, **extra}
    return terminal_commit._handler(params, repo_root=root)


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
    assert not (repo / ".coordinator-local").exists()


def test_escaping_plan_writes_nothing(repo):
    _with_sizing(repo)

    _call(repo, **{HALTED_RECORD_PARAM: {**LEDGER_HALT, "plan": "../outside.md"}})

    assert not (repo.parent / ".coordinator-local").exists()
    assert not (repo / ".coordinator-local").exists()


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
