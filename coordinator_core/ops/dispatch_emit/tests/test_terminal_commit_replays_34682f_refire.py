"""Replay of run 34682f's refire: the commit leg credits pre-halt commits named only in the run's live notes."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, UNCHANGED_SINCE_HALT_REASON
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import RUN2, halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_pre_halt_credit_satisfies_since_halt import (  # noqa: F401
    _close_only_run,
    _terminal,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


def _setup(root):
    """Three pre-halt ancestor commits, a halt commit, N1 only in the run dir's notes.jsonl."""
    token = _close_only_run(root)
    shas = []
    for i in range(3):
        _git(root, "commit", "-q", "--allow-empty", "-m", f"pre-halt {i}")
        shas.append(_git(root, "rev-parse", "HEAD"))
    _git(root, "commit", "-q", "--allow-empty", "-m", "halt")
    halt = _git(root, "rev-parse", "HEAD")
    note = (
        f"R1 is met by pre-halt commits: the code is {shas[0][:8]}; run df5e1f closed through it at "
        f"{shas[1][:8]} and run 5f687f at {shas[2][:10]}. Nothing is left to execute."
    )
    (root / RUN_DIR_ROOT / RUN2 / "notes.jsonl").write_text(json.dumps({"note": note, "at": "t"}) + "\n", encoding="utf-8")
    resume = {"run_id": "old", "halt_sha": halt, "blocking": ["R1"]}
    return token, shas, halt, resume


def _verdict(shas):
    met_by = [shas[0][:8], shas[1][:8], shas[2][:10]]
    return {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": met_by}]}


def test_ledger_gate_credits_sha_named_only_in_live_notes(halted):
    _, shas, _, resume = _setup(halted)
    run_dir = halted / RUN_DIR_ROOT / RUN2
    refusal = tc._ledger_gate(
        halted, run_dir, _verdict(shas), run_dir / "manifest.json", resume_ledger=resume
    )[0]
    assert refusal is None, refusal


def test_terminal_commit_lands_close_only_paths(halted):
    token, shas, halt, resume = _setup(halted)
    out = _terminal(halted, token, resume, _verdict(shas))
    assert out["committed"] is True, out
    landed = _git(halted, "diff", "--name-only", f"{halt}..HEAD").splitlines()
    assert landed and "a.py" not in landed


def test_met_by_naming_an_unnamed_sha_is_refused(halted):
    _, shas, _, resume = _setup(halted)
    run_dir = halted / RUN_DIR_ROOT / RUN2
    verdict = {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": "abcdef1234"}]}
    out = tc._ledger_gate(halted, run_dir, verdict, run_dir / "manifest.json", resume_ledger=resume)[0]
    assert out["refused"] == "ledger-unmet"
    assert out["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
