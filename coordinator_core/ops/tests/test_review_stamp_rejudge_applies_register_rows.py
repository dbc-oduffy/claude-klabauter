"""A recorded met rejudge applies its register_rows to the plan's sizing, claimed or not, so sizing.ship passes."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import review_stamp as m
from coordinator_core.ops.requirement_register import read_register
from coordinator_core.ops.tests.test_sizing_ship import _init_repo, _run

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_ID = "pln-fixture-rejudge-rows-000001"
PLAN_REL = "docs/plans/2026-11/p.md"
SIZING_REL = "state/sizings/2026-11/s.yaml"
VERDICT_REL = "state/delivery-verdicts/2026-11/p.rejudge.20261124T192748072369Z.md"
VERDICT_SHA = "18acd40fe3962351ef2381cabefe179374a62047"


def _row(rid: str, status: str, claimed: list) -> dict:
    return {"id": rid, "source_text": f"text {rid}", "anchor": "relayed", "surface": "internal",
            "status": status, "claimed_by": claimed, "wired": False}


def _repo(tmp_path: Path, criterion: str) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = {
        "schema": "sizing-object", "intent": "Test intent.", "estimate": {"tshirt": "M", "provisional": True},
        "route": "spec-dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "routed",
        "premise": {"provenance": "read", "evidence": "fixture"},
        "requirement_register": {"rows": [_row("R1", "partial", [PLAN_REL]), _row("R2", "open", [])]},
    }
    (repo / SIZING_REL).parent.mkdir(parents=True)
    (repo / SIZING_REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    plan = repo / PLAN_REL
    plan.parent.mkdir(parents=True)
    plan.write_text(
        f"---\ntitle: T\nstatus: executing\nplan_id: {PLAN_ID}\nsizing_object: {SIZING_REL}\n"
        "review_stamp:\n  terminal_commit_sha: abc\n  terminal_tree_sha: def\n"
        f"  criterion:\n    status: {criterion}\n    observation: o\n    sidecar: null\n"
        "  stamped_at: '2026-11-20T20:53:59Z'\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return repo


def _verdict(repo: Path, rows: list) -> None:
    fm = {"kind": "criterion-rejudge", "plan_id": PLAN_ID, "head_sha": VERDICT_SHA,
          "recorded_at": "2026-11-24T19:27:48.072369Z",
          "criterion": {"status": "met", "observation": "judged", "sidecar": None, "register_rows": rows}}
    path = repo / VERDICT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\n" + yaml.safe_dump(fm) + "---\n", encoding="utf-8")


def _met(rid: str, **over) -> dict:
    row = {"id": rid, "claim": "this-plan", "status": "met", "surface": "internal", "wired": True,
           "observed_ref": f"gen_install_docs.py --check {rid}"}
    row.update(over)
    return row


def _rows(repo: Path) -> dict:
    register = read_register((repo / SIZING_REL).read_text(encoding="utf-8"))
    return {r["id"]: r for r in register.rows}


@pytest.mark.parametrize("criterion", ["met", "not_met"])
def test_rejudge_stamps_claimed_and_unclaimed_rows_met_and_ship_passes(tmp_path, criterion):
    repo = _repo(tmp_path, criterion)
    _verdict(repo, [_met("R1"), _met("R2")])

    out = m.rejudge(repo / PLAN_REL, repo)

    assert out["status"] == "rejudged", out
    rows = _rows(repo)
    for rid in ("R1", "R2"):
        assert rows[rid]["status"] == "met" and rows[rid]["wired"] is True
        assert rows[rid]["met_by"] == [f"{VERDICT_REL}@{VERDICT_SHA}"]
        assert PLAN_REL in rows[rid]["claimed_by"]
    shipped = _run({"sizing_path": SIZING_REL}, repo_root=repo / ".git")
    assert shipped["exit_code"] == 0 and shipped["applied"] is True, shipped


def test_a_repeat_rejudge_writes_nothing(tmp_path):
    repo = _repo(tmp_path, "met")
    _verdict(repo, [_met("R1"), _met("R2")])
    m.rejudge(repo / PLAN_REL, repo)
    before = (repo / SIZING_REL).read_bytes()

    out = m.rejudge(repo / PLAN_REL, repo)

    assert out["status"] == "unchanged"
    assert (repo / SIZING_REL).read_bytes() == before


@pytest.mark.parametrize("rows", [[_met("R1")], [_met("R1"), _met("R2", status="partial")],
                                  [_met("R1"), _met("R2", observed_ref="")]])
def test_a_row_the_verdict_does_not_judge_met_stays_owed_and_ship_refuses(tmp_path, rows):
    repo = _repo(tmp_path, "met")
    _verdict(repo, rows)
    before = (repo / SIZING_REL).read_bytes()

    out = m.rejudge(repo / PLAN_REL, repo)

    assert out["status"] == "unchanged"
    assert (repo / SIZING_REL).read_bytes() == before
    refused = _run({"sizing_path": SIZING_REL}, repo_root=repo / ".git")
    assert refused["exit_code"] == 1 and "requirement register is not met" in refused["error"]
    assert "stays owed" in refused["error"]


# DoE sizing 183aca replay: the real sizing, plan and recorded rejudge verdict, copied (read-only)
# into a tmp_path repo; skipped on a box without the DoE checkout.
def _content_root() -> Path:
    from coordinator_core.benchmarks.backlog_first_pass_tokens import MissingRepoKey, resolve_doe_repo

    try:
        return resolve_doe_repo()
    except MissingRepoKey:
        return Path("__no_doe_checkout__")


_DOE = _content_root()
_DOE_SIZING = "state/sizings/2026-11/2026-11-20-doe-c0-c4-stamp31-recovery-183aca.yaml"
_DOE_PLAN = "docs/plans/2026-11/2026-11-20-hnd-doe-c0-c4-stamp31-recovery-2c6463.md"
_DOE_VERDICT = (
    "state/delivery-verdicts/2026-11/pln-recover-doe-c0-c4-status-inter-e03cc0."
    "rejudge.20261124T192748072369Z.md"
)
_needs_doe = pytest.mark.skipif(
    not all((_DOE / p).is_file() for p in (_DOE_SIZING, _DOE_PLAN, _DOE_VERDICT)),
    reason="coordinator-content-repo checkout with the 183aca sizing and verdict not present",
)


def _doe_sizing_text() -> str:
    """The real sizing with both rows back at their pre-verdict `partial`, as DoE holds them."""
    return (_DOE / _DOE_SIZING).read_text(encoding="utf-8")


@_needs_doe
def test_doe_183aca_replay_recorded_rejudge_leaves_r1_r2_met_and_ship_succeeds(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    for rel in (_DOE_SIZING, _DOE_PLAN, _DOE_VERDICT):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_DOE / rel, repo / rel)
    sizing = repo / _DOE_SIZING
    before = read_register(sizing.read_text(encoding="utf-8"))
    assert {r["id"]: r["status"] for r in before.rows} == {"R1": "partial", "R2": "partial"}

    out = m.rejudge(repo / _DOE_PLAN, repo)

    assert out["status"] == "rejudged", out
    rows = {r["id"]: r for r in read_register(sizing.read_text(encoding="utf-8")).rows}
    for rid in ("R1", "R2"):
        assert rows[rid]["status"] == "met" and rows[rid]["wired"] is True
        assert rows[rid]["met_by"] == [f"{_DOE_VERDICT}@18acd40fe3962351ef2381cabefe179374a62047"]
    shipped = _run({"sizing_path": _DOE_SIZING}, repo_root=repo / ".git")
    assert shipped["exit_code"] == 0 and shipped["applied"] is True, shipped


@_needs_doe
def test_doe_183aca_replay_landing_run_reconcile_leaves_r1_r2_met_and_ship_succeeds(tmp_path):
    from coordinator_core.ops.dispatch_emit.tests import test_terminal_commit_reconciles_sizing_rows as c3

    repo = tmp_path / "repo"
    (repo / "state" / "sizings").mkdir(parents=True)
    (repo / "docs" / "plans").mkdir(parents=True)
    c3._git(["init", "-q"], repo)
    c3._git(["config", "user.email", "t@example.com"], repo)
    c3._git(["config", "user.name", "t"], repo)
    text = _doe_sizing_text().replace(_DOE_PLAN, c3._PLAN)
    (repo / c3._SIZING).write_text(text, encoding="utf-8")
    (repo / c3._PLAN).write_text(c3._PLAN_TEXT, encoding="utf-8")
    c3._git(["add", "."], repo)
    c3._git(["commit", "-q", "-m", "seed"], repo)

    out = c3._fire(repo, [c3._judged("R1"), c3._judged("R2")])

    assert out["committed"] is True, out
    rows = c3._rows(repo)
    for rid in ("R1", "R2"):
        assert rows[rid]["status"] == "met" and rows[rid]["wired"] is True
        assert f"{c3._PLAN}@{out['sha']}" in rows[rid]["met_by"]
    assert out.get("sizing_shipped") == c3._SIZING, out
