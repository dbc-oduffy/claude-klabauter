"""The halted-record landing stage commits only its own claimed paths, never a sibling run's."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import halted_record_commit, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _git,
    _landed,
    _marker_script,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIBLING_SIZING = "state/sizings/2026-10-20-sibling-slice.yaml"
SIBLING_FILE = "sibling_product.py"
B_RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-bbbbbb"
DONE = {"C1": "DONE: C1\n"}


def _sibling(root, *, halted: bool) -> None:
    """Run B declares SIBLING_FILE and SIBLING_SIZING; held by a live token or halted unresumed."""
    rdir = root / RECORD_DIR_ROOT / B_RUN
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [SIBLING_FILE]}], "doc_writes": []}), encoding="utf-8"
    )
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": SIBLING_SIZING}), encoding="utf-8")
    if halted:
        (rdir / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    else:
        run_token.mint(root, B_RUN, "b.mjs", session_id="5e1f0000-0000-4000-8000-0000000000b1")
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / SIBLING_SIZING).write_text("a: 1\n", encoding="utf-8")
    (root / SIBLING_FILE).write_text("b = 1\n", encoding="utf-8")


def _run_a(root) -> None:
    """Run A is a parent-level record whose parent.json names B's slice sizing: the leaked arm."""
    rec = _write_record(root, RUN)
    (rec / "parent.json").write_text(
        json.dumps({"runs": [{"run_id": B_RUN, "slice_sizing": SIBLING_SIZING}]}), encoding="utf-8"
    )
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    _marker_script(root, [("C1", ["a.py"])], DONE)


@pytest.mark.parametrize("halted", [False, True], ids=["live-token", "halted"])
def test_sibling_sizing_is_withheld_and_stays_dirty(repo, halted):
    _sibling(repo, halted=halted)
    _run_a(repo)

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert out["withheld"] == {SIBLING_SIZING: "sibling-claimed"}
    names = _landed(repo, out, "--name-only")
    assert "a.py" in names and SIBLING_SIZING not in names and SIBLING_FILE not in names
    status = _git(["status", "--porcelain", "-uall"], repo)
    assert SIBLING_SIZING in status and SIBLING_FILE in status
    assert SIBLING_SIZING not in _git(["log", "-1", "--format=%B"], repo)


@pytest.mark.parametrize("halted", [False, True], ids=["live-token", "halted"])
def test_commit_halt_withholds_sibling_path(repo, halted):
    _sibling(repo, halted=halted)
    _run_a(repo)

    out = _call(repo, {"halted": "commit", "reason": "review"})

    assert out["committed"] is True, out
    names = _landed(repo, out, "--name-only")
    assert SIBLING_SIZING not in names and SIBLING_FILE not in names
    assert out["withheld"][SIBLING_SIZING] == "sibling-claimed"


def test_halted_run_leaves_review_edits_unlanded_and_names_them(repo, monkeypatch):
    _sibling(repo, halted=True)
    _run_a(repo)
    (repo / "a_review.py").write_text("r = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        halted_record_commit, "_review_written",
        lambda *a, **k: {SIBLING_FILE: "agent-b", "a_review.py": "agent-a"},
    )

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert out["review_edits_unlanded"] == {"a_review.py": "halted-run", SIBLING_FILE: "halted-run"}
    names = _landed(repo, out, "--name-only")
    assert "a.py" in names and "a_review.py" not in names and SIBLING_FILE not in names
    status = _git(["status", "--porcelain", "-uall"], repo)
    assert "a_review.py" in status and SIBLING_FILE in status
    message = _git(["log", "-1", "--format=%B"], repo)
    assert "Review-Edit-Unlanded: a_review.py" in message


def test_own_declared_path_a_sibling_also_claims_still_lands(repo):
    _sibling(repo, halted=True)
    _run_a(repo)
    _marker_script(repo, [("C1", [SIBLING_FILE])], DONE)

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert SIBLING_FILE in _landed(repo, out, "--name-only")
