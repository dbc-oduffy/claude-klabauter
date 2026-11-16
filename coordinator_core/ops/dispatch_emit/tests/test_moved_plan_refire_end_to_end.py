"""A refire of a sizing whose plan edge names a moved plan stages the plan on disk, successive halts name one
plan path, and `resume_ledger` cites the newest halt commit, driven through `ask_stage.stage` and `ask_gate.gate`."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as c
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import _plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

FLAT = "docs/plans/2026-11-12-moved-plan.md"
MONTH = "docs/plans/2026-11/2026-11-12-moved-plan.md"


def _git(root, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return done.stdout.strip()


def _halt_commit(root, run_id: str, minted_plan: str) -> str:
    """Commit `run_id`'s ledger-check halt record, its plan field minted as `minted_plan`; return the commit sha."""
    halted = c.halted_naming_existing_plan(
        root, {"halted": "ledger-check", c.HALT_PLAN_FIELD: minted_plan, "sizing_rel": REL}
    )
    rec = root / "state" / "warp" / run_id
    rec.mkdir(parents=True)
    (rec / "halted.json").write_text(json.dumps(halted) + "\n", encoding="utf-8", newline="\n")
    (rec / "manifest.json").write_text(json.dumps({"rows": [{"id": "A1"}]}) + "\n", encoding="utf-8", newline="\n")
    _git(root, "add", f"state/warp/{run_id}")
    _git(root, "commit", "-q", "-m", c.halted_record_message(run_id, halted))
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def moved(repo):  # noqa: F811
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    _put(repo, plan=MONTH)
    _plan(repo, rel=FLAT)
    write_run_scripts(repo, "r3")
    older = _halt_commit(repo, "r1", MONTH)
    newer = _halt_commit(repo, "r2", FLAT)
    return repo, older, newer


def test_refire_of_a_moved_plan_stages_it_with_one_halt_plan_path_and_the_newest_halt_sha(moved):
    repo, older, newer = moved

    manifest = stage(repo, run_id="r3", sizing_rel=REL)
    request = parse_marker((repo / manifest.marker_path).read_text(encoding="utf-8"))
    assert [r.id for r in manifest.rows] == ["A1"]
    assert request is not None and request.plan_path == FLAT

    first, second = (c.read_halt_record(repo, run, at_sha=sha) for run, sha in (("r1", older), ("r2", newer)))
    assert first is not None and second is not None
    assert first.plan_rel == second.plan_rel == FLAT

    verdict = gate(repo, REL)
    assert verdict.halt is None and verdict.resume_ledger is not None, verdict.halt
    assert verdict.resume_ledger["plan"] == FLAT
    assert verdict.resume_ledger[c.RESUME_LEDGER_HALT_SHA_FIELD] == newer != older
    assert verdict.resume_ledger["run_id"] == "r2"
