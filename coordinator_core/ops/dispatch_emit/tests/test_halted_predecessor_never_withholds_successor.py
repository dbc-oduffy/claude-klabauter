"""dispatch.land_run: a halted run of the landing run's own lineage (same sizing, or a `resumed_from` link) never
withholds the landing run's paths; a halted run of another sizing still does."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.tests.test_land_run_declared_write_left_dirty import (  # noqa: F401
    CODE, DOC, SIZING, _git, _land, _write, repo,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

OTHER = "pkg/other.py"
FOREIGN_SIZING = "state/sizings/20260101-other.yaml"


def _halted_holder(repo, sizing: str, kept_text: str = "a = 0\nb = 7\nc = 0\n") -> str:
    """A recent halted run on `sizing` whose halt record withholds CODE and a still-dirty untracked OTHER."""
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
    _write(repo, f"{rec}/run.json", json.dumps({"sizing_rel": sizing}))
    _write(repo, f"{rec}/halted.json", json.dumps({
        "status": "halted", "sizing_rel": sizing, "run_base_sha": _git(repo, "rev-parse", "HEAD").strip(),
        "run_artifacts": {"withheld": {CODE: "halt-record-only", OTHER: "halt-record-only"}},
    }))
    _write(repo, f"{rec}/kept.json", json.dumps({"chunks": {"X1": {"paths": {CODE: blob, OTHER: other_blob}}}, "plan_gate": None}))
    return halted


def _withheld_by(out: dict) -> list:
    return [p for p, why in (out.get("withheld") or {}).items() if "withheld by" in str(why)]


def test_a_successor_of_the_same_sizing_lands_its_paths_over_a_halted_predecessor(repo) -> None:
    halted = _halted_holder(repo, SIZING)

    out = _land(repo, hand_commit=False)

    assert out["committed"] is True, out
    assert not _withheld_by(out), (halted, out)
    landed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert CODE in landed and DOC in landed, (landed, out)
    assert halted not in json.dumps(out.get("withheld") or {}), out


def _spine_declaring_sibling(repo, monkeypatch, sizing: str) -> str:
    """A sibling run on `sizing` whose halt marker is not yet written (a fresh token, a live owner) and
    whose plan spine declares CODE and DOC: the co-declared hold, not a halt record, names it."""
    sibling = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 300))}-bbbbbb"
    _write(repo, "state/sizings/b.yaml", 'plan: "docs/plans/b.md"\n')
    _write(
        repo, "docs/plans/b.md",
        f"# B\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n    - {CODE}\n    - {DOC}\n```\n",
    )
    if sizing == SIZING:
        _write(repo, SIZING, (repo / SIZING).read_text(encoding="utf-8") + 'plan: "docs/plans/b.md"\n')
    _write(repo, f"state/warp/{sibling}/run.json", json.dumps({"sizing_rel": sizing}))
    run_token.mint(repo, sibling, "other.mjs", session_id="5e1f0000-0000-4000-8000-0000000000b2")
    monkeypatch.setattr("coordinator_core.session.touch_record.session_live", lambda sid, cwd=None: True)
    return sibling


def test_a_predecessor_of_the_same_sizing_named_by_the_co_declared_hold_never_withholds(repo, monkeypatch) -> None:
    sibling = _spine_declaring_sibling(repo, monkeypatch, SIZING)

    out = _land(repo, hand_commit=False)

    assert out["committed"] is True, out
    assert not _withheld_by(out), (sibling, out)
    assert {CODE, DOC} <= set(_git(repo, "show", "--name-only", "--format=", "HEAD").split()), out


def test_a_sibling_of_another_sizing_named_by_the_co_declared_hold_still_withholds(repo, monkeypatch) -> None:
    sibling = _spine_declaring_sibling(repo, monkeypatch, "state/sizings/b.yaml")

    out = _land(repo, hand_commit=False)

    assert out["committed"] is True, out
    assert (out.get("withheld") or {}).get(CODE) == f"withheld by {sibling}", out
    assert CODE not in _git(repo, "show", "--name-only", "--format=", "HEAD").split(), out


def test_a_halted_run_of_another_sizing_still_withholds(repo) -> None:
    halted = _halted_holder(repo, FOREIGN_SIZING)

    out = _land(repo, hand_commit=False)

    assert out["committed"] is True, out
    assert (out.get("withheld") or {}).get(CODE) == f"withheld by {halted}", out
    assert CODE not in _git(repo, "show", "--name-only", "--format=", "HEAD").split(), out
