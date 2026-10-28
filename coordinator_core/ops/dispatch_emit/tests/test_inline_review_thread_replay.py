"""Replay of the review-floor notes of runs 41bbaf (scratch-only xs), 87d761 (code-writing xs) and a non-commit-halt refire under a new run id."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import RESUMED_RUN_ID_KEY, ledger_halt_path, write_run_outcome
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest
from coordinator_core.ops.dispatch_emit.tests import test_ask_commit_halt_refire_e2e as refire
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_commit_halt_refire_e2e import (  # noqa: F401
    halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SCRATCH = "scratch/warp/probe.py"
CODE = "coordinator_core/mod.py"


def _digest(run_id: str) -> dict:
    """An xs digest as 41bbaf and 87d761 emitted it: zero-slice `review.wave`, no prep/delivery/integration."""
    return {"arm": "xs", "run_id": run_id, "review": {"wave": []}}


def _floor(root, path: str, review: dict) -> dict | None:
    """The floor note `dispatch.terminal_commit` records (`_review_floor_note`) for an xs run declaring ``path``."""
    request = CommitRequest(chunks=(ChunkCommit(id="X1", title="t", paths=(path,)),), deliverable_id="dlv-x-aaaaaa", plan_path="docs/plans/p.md")
    return refusals._review_floor_note(request, review, False, root)


def _xs_script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-87d761", session_id=None,
        wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", script_path="x.mjs", **REVIEW_KW,
    )


def test_a_scratch_only_xs_run_with_a_zero_slice_review_passes_the_floor(tmp_path):
    review = refusals._xs_zero_slice_review(_digest("41bbaf"))
    assert review is not None and review["wave_reviews"] == []

    assert _floor(tmp_path, SCRATCH, review) is None
    refused = _floor(tmp_path, CODE, review)
    assert refused is not None and "0 verdicts" in refused


def test_a_code_writing_xs_run_composes_a_review_wave_before_commit(tmp_path, monkeypatch):
    script = _xs_script(monkeypatch)
    # Wiring pins on the composed JS (no end-to-end xs commit is driven here); they guard the review-wave gate text only.
    assert "((_gate.arm !== 'xs' && !_xsTail) || _codeReviewOwed)" in script
    assert "const _codeReviewOwed = (_manifest?.review_code_paths ?? []).length > 0;" in script

    sidecar = tmp_path / "scratch" / "reviews" / "r0.md"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("verdict: APPROVED\n", encoding="utf-8")
    composed = {"wave_reviews": [{"verdict": "APPROVED", "sidecar_path": "scratch/reviews/r0.md"}]}
    assert _floor(tmp_path, CODE, composed) is None
    zero = refusals._xs_zero_slice_review(_digest("87d761"))
    assert "0 verdicts" in _floor(tmp_path, CODE, zero)
    # The composed digest of a code-writing xs run must not synthesise the zero-slice form.
    assert refusals._xs_zero_slice_review({**_digest("87d761"), "review": {"wave": [{"verdict": "APPROVED"}]}}) is None
    assert "review_code_paths" in ask_compose._MANIFEST_SCHEMA["properties"]


def test_a_non_commit_halt_refire_under_a_new_run_id_still_sees_its_review(halted):  # noqa: F811
    root = halted
    sidecar = "scratch/reviews/pred.md"
    refire._write(root, sidecar, "verdict: PASS\n")
    review = {**refire._REVIEWED, "wave_reviews": [{"verdict": "PASS", "sidecar_path": sidecar}]}
    pointer = ledger_halt_path(root, refire.PLAN)
    data = json.loads(pointer.read_text(encoding="utf-8"))
    data["halted"] = "ledger-check"
    pointer.write_text(json.dumps(data), encoding="utf-8")
    write_run_outcome(root, refire.RUN, {"inline_review": review, "incomplete_chunks": [], "landed_chunks": ["C1"]})

    verdict = gate(root, refire.SIZING)
    assert verdict.resume_ledger is not None, verdict.halt
    threaded = verdict.resume_ledger.get("inline_review")
    assert threaded == {**review, RESUMED_RUN_ID_KEY: refire.RUN}

    out = refire._commit(root, refire._second_run(root), threaded)
    assert out["committed"] is True, out
    assert refusals.review_floor.verdict_count(threaded, root) >= 1
