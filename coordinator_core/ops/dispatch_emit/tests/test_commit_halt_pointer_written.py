"""commit_halted_record persists run-outcome.json and writes a commit-halt pointer for an all-landed commit refusal."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RUN_OUTCOME_FILE,
    ledger_halt_path,
    read_ledger_halt,
    read_run_outcome,
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _git,
    _write_record,
    repo,
)
from coordinator_core.ops.dispatch_emit import terminal_commit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "docs/plans/2026-10-16-demo.md"
SIZING = "state/sizings/demo.yaml"
COMMIT_HALT = {"halted": "commit", "plan": PLAN, "sizing": SIZING}
REVIEW = {"integration_stem": "s", "slices": 1, "fixes": 1}


def _outcome(**extra) -> dict:
    return {"inline_review": REVIEW, "incomplete_chunks": [], "landed_chunks": ["C1"], **extra}


def _call(root, halted, **extra) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: halted, **extra}
    return terminal_commit._handler(params, repo_root=root)


def _with_sizing(root):
    rec = _write_record(root, RUN)
    (rec / "run.json").write_text(f'{{"sizing_rel": "{SIZING}"}}\n', encoding="utf-8")
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / SIZING).write_text("a: 1\n", encoding="utf-8")


def test_commit_halt_with_outcome_lands_outcome_and_pointer(repo):
    _with_sizing(repo)

    out = _call(repo, COMMIT_HALT, run_outcome=_outcome())

    assert out["committed"] is True, out
    rel = f"{RECORD_DIR_ROOT}/{RUN}/{RUN_OUTCOME_FILE}"
    assert rel in _git(["show", "--name-only", "--format=", "HEAD"], repo)
    assert read_run_outcome(repo, RUN)["inline_review"] == REVIEW
    halt = read_ledger_halt(ledger_halt_path(repo, PLAN))
    assert halt is not None
    assert (halt.run_id, halt.plan_rel, halt.sizing_rel, halt.blocking) == (RUN, PLAN, SIZING, ())
    assert (halt.halted, halt.outcome) == ("commit", rel)


def test_commit_halt_with_incomplete_chunk_writes_neither(repo):
    _with_sizing(repo)

    _call(repo, COMMIT_HALT, run_outcome=_outcome(incomplete_chunks=["C2"]))

    assert not (repo / RECORD_DIR_ROOT / RUN / RUN_OUTCOME_FILE).exists()
    assert not ledger_halt_path(repo, PLAN).exists()


def test_commit_halt_without_outcome_unlinks_stale_pointer(repo):
    _with_sizing(repo)
    _call(repo, COMMIT_HALT, run_outcome=_outcome())
    assert ledger_halt_path(repo, PLAN).is_file()

    _call(repo, COMMIT_HALT)

    assert not ledger_halt_path(repo, PLAN).exists()


def test_ledger_check_pointer_bytes_unchanged(repo):
    _with_sizing(repo)

    _call(repo, {"halted": "ledger-check", "plan": PLAN, "items": [{"id": "R1"}]})

    data = json.loads(ledger_halt_path(repo, PLAN).read_text(encoding="utf-8"))
    assert data == {"run_id": RUN, "plan": PLAN, "sizing": SIZING, "blocking": ["R1"]}
