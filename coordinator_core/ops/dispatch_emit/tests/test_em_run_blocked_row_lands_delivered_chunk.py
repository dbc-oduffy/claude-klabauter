"""A verified delivered chunk lands while an em_run row blocked externally (an uncleared external_gate, or
needs_slot) stays open as owed: `_ledger_gate`, called as `terminal_commit` calls it, lists the register row
it withholds open and never refuses it, while a row with any other gap still refuses `ledger-unmet`.

The manifest rows come from a real `read_spine` of a plan holding a delivered code row C3 and an em_run row C4.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, OwedRow, StageManifest
from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import gated_rows, slot_withheld_rows
from coordinator_core.ops.dispatch_emit.spine_read import EM_OWNED_REASONS
from coordinator_core.ops.dispatch_emit.terminal_commit_ledger import _ledger_gate
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

DLV = "dlv-alpha-aaaaaa"
RUN = "run1"
SIZING = "state/sizings/20260101-a.yaml"
PLAN = "docs/plans/p.md"

_GATE = "  external_gate:\n    - owner_repo: other\n      requires: landed-work\n"
_C4 = "- id: C4\n  title: measure on the box\n  em_run: true\n  needs_slot: true\n{gate}  disposition: open\n  body: |\n    run it\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _plan(gated: bool) -> str:
    return (
        f"---\ntitle: p\ndeliverable_id: {DLV}\nsizing_object: {SIZING}\nprime_exit_criterion:\n"
        "  statement: done\n  falsifier:\n    how: pytest t.py::test_x -q\n    expected_when_true: passes\n---\n\n"
        "## Tasks\n\n```yaml plan-tasks\n"
        "- id: C3\n  title: delivered code\n  writes: [a.py]\n  register_rows: [R1]\n  disposition: open\n  body: |\n    b\n"
        + _C4.format(gate=_GATE if gated else "")
        + "```\n"
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _write(
        root, SIZING,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: ship a.py\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _write(root, "a.py", "a\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING)
    return root


def _manifest(root: Path, plan_text: str) -> StageManifest:
    """The gated/owed split `ask_stage.stage` derives from the plan's real spine read."""
    parse = ask_stage._parse_plan(root, root / PLAN, plan_text)
    owed = tuple(
        OwedRow(id=e["id"], reason=e["reason"], detail=e.get("detail") or "")
        for e in parse.exclusions
        if e.get("reason") in EM_OWNED_REASONS
    )
    return StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
        gated=tuple(gated_rows(parse.exclusions, parse.raw_by_id)), owed=owed,
        slot_withheld=tuple(slot_withheld_rows(parse.raw_by_id)),
    )


def _gate(root: Path, plan_text: str, verdict: dict):
    """`_ledger_gate` with `gated` and `operator` built the way `terminal_commit` builds them."""
    manifest = _manifest(root, plan_text)
    return _gate_with(root, plan_text, verdict, manifest)


def _gate_with(root: Path, plan_text: str, verdict: dict, manifest: StageManifest):
    return _ledger_gate(
        root, rl.run_dir(root, RUN), verdict, None,
        [g.id for g in manifest.gated], rl.owed_rows(manifest.owed), plan_text,
    )


def _r1(**link) -> dict:
    return {"id": "R1", "status": "not_met", "remaining": ["C4: EM measures on the box"], **link}


def _open_ids(open_rows: list) -> list:
    return [r["id"] for r in open_rows]


@pytest.mark.parametrize("gated", [True, False], ids=["external_gate", "needs_slot_only"])
def test_the_em_run_row_reaches_the_manifest_as_gated_or_owed(repo, gated):
    manifest = _manifest(repo, _plan(gated))
    assert [g.id for g in manifest.gated] == (["C4"] if gated else [])
    assert [o.id for o in manifest.owed] == ([] if gated else ["C4"])
    assert [s["id"] for s in manifest.slot_withheld] == ["C4"]


@pytest.mark.parametrize("gated", [True, False], ids=["external_gate", "needs_slot_only"])
def test_a_row_withheld_by_the_blocked_em_run_row_lands_open(repo, gated):
    verdict = {"items": [_r1(withheld_by="C4")]}
    refusal, _ledger, open_rows = _gate(repo, _plan(gated), verdict)
    assert refusal is None, refusal
    assert _open_ids(open_rows) == ["R1"] and "C4" in open_rows[0]["reason"]


@pytest.mark.parametrize("gated", [True, False], ids=["external_gate", "needs_slot_only"])
def test_a_row_owed_by_the_blocked_em_run_row_lands_open(repo, gated):
    verdict = {"items": [_r1(owed_by=["C4"])]}
    refusal, _ledger, open_rows = _gate(repo, _plan(gated), verdict)
    assert refusal is None, refusal
    assert _open_ids(open_rows) == ["R1"] and "C4" in open_rows[0]["reason"]
    label = "owed-external" if gated else "owed-after-publish"
    assert open_rows[0]["reason"] == f"{label} C4"


@pytest.mark.parametrize("gated", [True, False], ids=["external_gate", "needs_slot_only"])
@pytest.mark.parametrize("link", [{"withheld_by": "C4"}, {"owed_by": ["C4"]}], ids=["withheld_by", "owed_by"])
def test_a_second_row_with_a_gap_beyond_the_owed_row_still_refuses(repo, gated, link):
    rl.revise(repo, RUN, verbatim="and the second requirement")
    verdict = {"items": [
        _r1(**link),
        {"id": "R2", "status": "not_met", "remaining": ["add the missing handler"]},
    ]}
    refusal, _ledger, open_rows = _gate(repo, _plan(gated), verdict)
    assert refusal is not None and refusal["refused"] == "ledger-unmet"
    assert "R2" in [f["id"] for f in refusal["ledger_failures"]]
    assert open_rows == []


def test_a_row_withheld_by_a_row_the_manifest_never_holds_still_refuses(repo):
    verdict = {"items": [_r1(withheld_by="C9")]}
    refusal, _ledger, _open = _gate(repo, _plan(True), verdict)
    assert refusal is not None and refusal["refused"] == "ledger-unmet"
