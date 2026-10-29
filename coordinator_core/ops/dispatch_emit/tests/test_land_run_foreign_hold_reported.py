"""dispatch.land_run: a declared path a halted foreign run holds is named, with its reason, in the reply's
``withheld`` and ``run_artifacts.withheld``; the rest of the landing still commits."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests.test_land_run_declared_write_left_dirty import (  # noqa: F401
    CODE, DOC, SIZING, _git, _land, _write, repo,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

OTHER = "pkg/other.py"
HOLDER = "warp-2026-10-28-halted-holder-ee4ba7"
HOLDER_SIZING = "state/sizings/20260101-halted-holder.yaml"  # a shared sizing makes the landing run the holder's successor


def test_a_path_held_by_a_halted_foreign_run_is_named_in_both_withheld_maps(repo, monkeypatch):
    monkeypatch.setattr(
        land_run, "foreign_halted_withheld",
        lambda root, run_id, plan_rel, paths: {p: HOLDER for p in paths if p == CODE},
    )
    monkeypatch.setattr(land_run, "_halted_only_release", lambda *a, **k: None)

    out = _land(repo, hand_commit=False)

    assert out["committed"] is True, out
    reason = f"withheld by {HOLDER}"
    assert out["withheld"] == {CODE: reason}, out
    assert out["run_artifacts"]["withheld"].get(CODE) == reason, out
    landed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert CODE not in landed and DOC in landed, landed
    assert out["criterion_status"] == "not_met", out


def test_no_foreign_hold_adds_no_withheld_entry(repo):
    out = _land(repo, hand_commit=False)

    assert out["committed"] is True, out
    assert "withheld" not in out
    assert CODE not in out["run_artifacts"]["withheld"]


def _halted_holder(repo, kept_text: str) -> str:
    """A recent halted run that withholds CODE (kept blob `kept_text`) and a still-dirty untracked OTHER.

    OTHER keeps the run itself unreleased, so only the per-path release can free CODE.
    """
    halted = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 300))}-aaaaaa"
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=kept_text, capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout.strip()
    _write(repo, OTHER, "x = 1\n")
    other_blob = subprocess.run(
        ["git", "hash-object", "-w", OTHER], cwd=repo, capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()
    rec = f"state/warp/{halted}"
    _write(repo, f"{rec}/run.json", json.dumps({"sizing_rel": HOLDER_SIZING}))
    _write(repo, f"{rec}/halted.json", json.dumps({
        "status": "halted", "sizing_rel": HOLDER_SIZING, "run_base_sha": _git(repo, "rev-parse", "HEAD").strip(),
        "run_artifacts": {"withheld": {CODE: "halt-record-only", OTHER: "halt-record-only"}},
    }))
    _write(repo, f"{rec}/kept.json", json.dumps({"chunks": {"X1": {"paths": {CODE: blob, OTHER: other_blob}}}, "plan_gate": None}))
    return halted


def test_land_run_lands_a_path_whose_halted_holders_kept_hunks_were_hand_committed(repo):
    """The hold frees through the real leg: the holder's kept hunk is at HEAD after the hand commit."""
    halted = _halted_holder(repo, "a = 0\nb = 0\nc = 9\n")

    out = _land(repo, hand_commit=True)

    assert out["committed"] is True, out
    assert CODE not in (out.get("withheld") or {}), (halted, out)
    assert CODE in _git(repo, "show", "--name-only", "--format=", "HEAD").split(), out


@pytest.mark.parametrize(
    "kept_text, lands", [("a = 0\nb = 0\nc = 9\n", True), ("a = 0\nb = 7\nc = 0\n", False)],
)
def test_a_declared_path_of_a_halted_run_lands_with_its_hunks_or_is_withheld_with_a_reason(repo, kept_text, lands):
    """Run A halts holding CODE; CODE is hand-committed; run B declares CODE and lands: never neither."""
    halted = _halted_holder(repo, kept_text)

    out = _land(repo, hand_commit=True)

    assert out["committed"] is True, out
    withheld = out.get("withheld") or {}
    landed = CODE in _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert landed is lands, out
    if landed:
        assert CODE not in withheld, out
        assert "a = 1" in _git(repo, "show", f"HEAD:{CODE}"), out
    else:
        assert withheld.get(CODE) == f"withheld by {halted}", out
        assert out["run_artifacts"]["withheld"].get(CODE) == withheld[CODE], out
        assert out["criterion_status"] == "not_met", out
        assert _git(repo, "status", "--porcelain", "--", CODE).strip(), out


def test_land_run_still_withholds_a_path_whose_halted_holders_kept_hunks_are_not_at_head(repo):
    halted = _halted_holder(repo, "a = 0\nb = 7\nc = 0\n")

    out = _land(repo, hand_commit=True)

    assert out["committed"] is True, out
    assert out["withheld"] == {CODE: f"withheld by {halted}"}, out
    assert out["criterion_status"] == "not_met", out
