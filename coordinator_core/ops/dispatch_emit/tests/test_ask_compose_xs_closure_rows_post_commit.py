"""An XS verify-close ledger whose only unmet legs are the landing-owned lifecycle acts (baton_archive,
plan_implemented) passes the ledger check as post_commit and reaches the terminal commit; a refire after a
note revise (a resume_ledger gate reply) reaches the ledger check instead of the no-folded-manifest refusal.
The composed script runs under node with every agent leg stubbed by label."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_ledger_resume_restage import (  # noqa: F401
    _DISK,
    _PLAN,
    _SIZING,
    _TOKEN,
    _labels,
    _run,
    _script,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    WORKFLOW_FAKE_JS,
    readers_cfg,
    stage_reply,
    stage_root,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REFIRE_RESUME = {"run_id": "ask-prior", "plan": _PLAN, "blocking": ["R1"], "em_discharged": {}}


def _verdict(remaining: list[str]) -> dict:
    return {"items": [{"id": "R1", "status": "not_met", "essential": True, "remaining": remaining}]}


def _refire_gate() -> dict:
    return {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": _REFIRE_RESUME, "tshirt": "XS",
        "route": "spec-dispatch", "stage_reply": stage_reply(_DISK),
    }


def test_only_lifecycle_legs_unmet_passes_the_ledger_check_and_commits(monkeypatch):
    out = _run(
        _script(monkeypatch), gate=_refire_gate(), verify=_verdict(["baton_archive", "plan_implemented"]),
    )
    labels = _labels(out)
    assert out["result"]["halted_by"] is None, out["result"]["halted_by"]
    assert labels.index("verify") < labels.index("commit")
    assert "halt-commit" not in labels


def test_a_plain_words_gap_beside_the_lifecycle_legs_still_halts_at_the_ledger_check(monkeypatch):
    out = _run(
        _script(monkeypatch), gate=_refire_gate(),
        verify=_verdict(["plan_implemented", "typecheck not re-run"]),
    )
    labels = _labels(out)
    assert out["result"]["halted_by"]["halted"] == "ledger-check"
    assert "commit" not in labels and "halt-commit" in labels


def test_a_refire_after_a_note_revise_reaches_the_ledger_check_not_the_no_manifest_refusal(monkeypatch):
    out = _run(_script(monkeypatch), gate=_refire_gate(), verify=_verdict(["baton_archive"]))
    halted = out["result"]["halted_by"]
    assert halted is None or not str(halted.get("reason", "")).startswith("no folded stage manifest")
    assert "verify" in _labels(out)
