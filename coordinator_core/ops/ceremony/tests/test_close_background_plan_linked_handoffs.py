"""The close job's deliverable-cascade leg ships handoffs that link the run's plan, on the close
job and the halted-run-landed launch params alike.

Run (from repo root):
    python -m pytest coordinator_core/ops/ceremony/tests/test_close_background_plan_linked_handoffs.py -q
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import validate
from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import _git, _init_repo, _seed_handoff

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "run-plan-linked"
_DID = "dlv-run-own-000000"
_PLAN = "docs/plans/2026-01-01-p.md"
_PLAN_Q = "docs/plans/2026-01-01-q.md"
_SHA = "a" * 40
_ENTRY_PARAMS = {
    "close job": {},
    "halted-run-landed launch": {"claimed_batons": [], "source_memo": None},
}


def _plan(repo: Path, rel: str, plan_id: str, status: str, did: str = "") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    extra = f"deliverable_id: {did}\n" if did else ""
    p.write_text(f"---\nplan_id: {plan_id}\nstatus: {status}\n{extra}---\n\n# Plan\n", encoding="utf-8")


def _link(path: Path, **extra) -> None:
    text = path.read_text(encoding="utf-8")
    add = "".join(f"{k}: {v}\n" for k, v in extra.items())
    path.write_text(text.replace("\n---\n\n# Handoff", f"\n{add}---\n\n# Handoff", 1), encoding="utf-8")


def _fm(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])


def _pay(repo: Path, params: dict) -> cb.LegResult:
    run_close.write_close_job(
        repo, RUN, ("deliverable-cascade",), datetime.now().astimezone() + timedelta(minutes=10),
        plan_rel=_PLAN, terminal_sha=_SHA,
    )
    ctx = cb._context(repo, repo / ".git", {**params, "run_id": RUN})
    return cb.run_background(ctx, {"deliverable-cascade": cb._deliverable_cascade})["deliverable-cascade"]


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(cb, "_not_opted_in", lambda ctx: False)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _plan(repo, _PLAN, "plan-p", "implemented", _DID)
    return repo


@pytest.mark.parametrize("params", _ENTRY_PARAMS.values(), ids=_ENTRY_PARAMS.keys())
def test_foreign_deliverable_handoff_linked_to_shipped_plan_closes_and_validates(tmp_path, params):
    repo = _repo(tmp_path)
    h = _seed_handoff(repo, "h.md", deliverable_id="dlv-other-000000")
    _link(h, governing_plan=_PLAN)

    result = _pay(repo, params)

    assert result.status == "ran", result.detail
    fm = _fm(h)
    assert fm["deployment_state"] == "shipped", fm
    verdict = validate("handoff", {k: str(v) if hasattr(v, "isoformat") else v for k, v in fm.items()})
    assert verdict["ok"], verdict


@pytest.mark.parametrize("params", _ENTRY_PARAMS.values(), ids=_ENTRY_PARAMS.keys())
def test_handoff_linked_to_an_unimplemented_second_plan_stays_open(tmp_path, params):
    repo = _repo(tmp_path)
    _plan(repo, _PLAN_Q, "plan-q", "in_progress")
    h = _seed_handoff(repo, "h.md", deliverable_id="dlv-other-000000")
    _link(h, plan_ids="[plan-p, plan-q]")

    _pay(repo, params)

    assert _fm(h)["deployment_state"] == "ready_to_fire"


def test_unlinked_foreign_handoff_stays_open(tmp_path):
    repo = _repo(tmp_path)
    h = _seed_handoff(repo, "h.md", deliverable_id="dlv-other-000000")

    _pay(repo, {})

    assert _fm(h)["deployment_state"] == "ready_to_fire"


def test_leg_process_time_under_bar_with_three_linked_handoffs_over_1100_records(tmp_path):
    repo = _repo(tmp_path)
    for i in range(1097):
        _seed_handoff(repo, f"fill-{i:04d}.md", deliverable_id=f"dlv-fill-{i:06d}")
    linked = [_seed_handoff(repo, f"link-{i}.md", deliverable_id=f"dlv-other-{i:06d}") for i in range(3)]
    for h in linked:
        _link(h, governing_plan=_PLAN)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed corpus")

    warm = _repo(tmp_path / "warm")  # first-use import cost is not the leg's
    _link(_seed_handoff(warm, "w.md", deliverable_id="dlv-other-w00000"), governing_plan=_PLAN)
    _pay(warm, {})

    ctx = cb.JobContext(repo, repo / ".git", RUN, (), None, _SHA, {"plan_rel": _PLAN})
    start = time.process_time()
    result = cb._deliverable_cascade(ctx)
    elapsed_ms = (time.process_time() - start) * 1000

    assert result.status == "ran", result.detail
    assert all(_fm(h)["deployment_state"] == "shipped" for h in linked)
    assert elapsed_ms < 500, f"{elapsed_ms:.0f}ms"
