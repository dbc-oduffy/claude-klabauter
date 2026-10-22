"""Replay of the a9760d refire shape through land_run: the resume param carries no revise_notes, the
revise note naming the pre-halt commits lives only in the run dir's notes.jsonl, and the ledger gate
still credits the row met by those commits."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, UNCHANGED_SINCE_HALT_REASON
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_met_refire_lands_its_own_product_changes import (
    B,
    RUN2,
    _composed,
    _git,
    _refire,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-3333-4444-555555555555"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
REFUSED = [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def _verdict(met_by) -> dict:
    return {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": met_by}]}


def _shas(root) -> tuple[str, str]:
    full = _git(root, "rev-parse", "HEAD").strip()
    return full[:8], full[:10]


def _note_in_run_dir(root, short: str) -> None:
    path = root / RUN_DIR_ROOT / RUN2 / "notes.jsonl"
    note = f"R1 is met by pre-halt commits: the code is {short}. Nothing is left to execute."
    path.write_text(json.dumps({"note": note, "at": "2026-10-22T10:38:18Z"}) + "\n", encoding="utf-8")


def _terminal(root, token, resume, verdict) -> dict:
    return land_run._handler(
        {
            "script_path": "run2.mjs", "run_id": "run2", "run_token": token, "incomplete_chunks": [],
            "inline_review": _REVIEWED, "session_id": _SID, "ledger_verdict": verdict, "resume_ledger": resume,
        },
        repo_root=root / ".git",
    )


def test_a_note_only_in_the_run_dir_credits_the_row_and_the_close_lands(halted):  # noqa: F811
    short, longer = _shas(halted)
    resume = _composed(halted)
    assert "revise_notes" not in resume
    refire = _refire(halted, edit=False)
    _note_in_run_dir(halted, short)

    out = _terminal(halted, refire["token"], resume, _verdict([longer]))

    assert out.get("refused") is None, out
    assert out["committed"] is True, out
    landed = set(_git(halted, "show", "--name-only", "--format=", out["sha"]).split())
    assert not landed & {"a.py", B}, landed


def test_a_met_by_sha_no_note_names_is_refused_unchanged_since_halt(halted):  # noqa: F811
    short, _ = _shas(halted)
    resume = _composed(halted)
    refire = _refire(halted, edit=False)
    _note_in_run_dir(halted, short)
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal(halted, refire["token"], resume, _verdict(["abcdef1234"]))

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert out["ledger_failures"] == REFUSED
    assert _git(halted, "rev-parse", "HEAD") == head
