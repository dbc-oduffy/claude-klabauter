"""A met ledger-check refire's terminal commit lands the product paths its own run declared and changed
since the halt, instead of refusing `ledger-unmet` and leaving them to a halted-record commit that
withholds them as `halt-record-only`."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RUN_DIR_ROOT,
    UNCHANGED_SINCE_HALT_REASON,
    ManifestRow,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    RUN2,
    _met_out_of_band,
    _second_run,
    halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    DLV,
    PLAN,
    RUN,
    SIZING,
    _git,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-3333-4444-555555555555"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}


B = "README.md"


def _refire(root, *, edit: bool = True) -> dict:
    """Stage run `RUN2` as a refire of the halted `RUN`: the manifest declares `a.py` and `B` (both
    tracked before the halt) and stamps the staging HEAD; the refire then checkpoint-commits `a.py`
    and leaves `B` dirty."""
    token = _second_run(root)
    _write(
        root, f"{RUN_DIR_ROOT}/{RUN2}/commit-request.txt",
        render_marker(CommitRequest(
            chunks=(ChunkCommit(id="C1", title="t", paths=("a.py", B)),), deliverable_id=DLV, plan_path=PLAN
        )) + "\n",
    )
    row = ManifestRow(
        id="C1", agent_type="x", model="m", brief_path="briefs/C1.md", writes=("a.py", B), wave=0
    )
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN2}", rows=(row,), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN2}/commit-request.txt",
    ).to_json()
    manifest["head_sha"] = _git(root, "rev-parse", "HEAD").strip()
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/manifest.json", json.dumps(manifest))
    if edit:
        _write(root, "a.py", "a = 2\n")
        _git(root, "add", "a.py")
        _git(
            root, "commit", "-q", "-m", "checkpoint(wave 0): 1 row — C1",
            "-m", f"Checkpoint-Plan: {PLAN}\nCheckpoint-Base: {manifest['head_sha']}",
        )
        _write(root, B, "b = 2\n")
    return {"token": token, "head_sha": manifest["head_sha"]}


def _terminal(root, token: str, resume: dict) -> dict:
    return terminal_commit._handler(
        {
            "script_path": "run2.mjs", "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
            "inline_review": _REVIEWED, "session_id": _SID, "ledger_verdict": _MET,
            "resume_ledger": resume,
        },
        repo_root=root / ".git",
    )


def _composed(root, *, null_halt_sha: bool = False) -> dict:
    """The terminal param as `ask_compose` composes it: `{run_id, halt_sha, blocking}` and never a
    `plan`, so the since-halt check cannot look the halt commit up from the param alone."""
    _met_out_of_band(root)
    verdict = gate(root, SIZING)
    assert verdict.halt is None and verdict.resume_ledger is not None, verdict.halt
    halt_sha = None if null_halt_sha else verdict.resume_ledger.get("halt_sha")
    return {"run_id": RUN2, "halt_sha": halt_sha, "blocking": list(verdict.resume_ledger["blocking"])}


@pytest.mark.parametrize("null_halt_sha", [False, True])
def test_met_refire_lands_its_declared_product_and_withholds_nothing_as_halt_record_only(
    halted, null_halt_sha  # noqa: F811
):
    resume = _composed(halted, null_halt_sha=null_halt_sha)
    refire = _refire(halted)
    out = _terminal(halted, refire["token"], resume)

    assert out["committed"] is True, out
    assert "refused" not in out, out
    assert "halt-record-only" not in json.dumps(out.get("withheld", [])), out
    assert _git(halted, "show", f"{out['sha']}:a.py") == "a = 2\n"
    assert _git(halted, "show", f"{out['sha']}:{B}") == "b = 2\n"
    landed = set(_git(halted, "diff", "--name-only", refire["head_sha"], out["sha"]).split())
    assert {"a.py", B} <= landed
    assert _git(halted, "status", "--porcelain", "--", "a.py", B) == ""


def test_met_refire_that_changed_nothing_is_still_refused_and_names_the_since_halt_base(halted):  # noqa: F811
    resume = _composed(halted)
    refire = _refire(halted, edit=False)
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal(halted, refire["token"], resume)

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert out["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
    assert out["since_halt"]["outcome"] == "unchanged", out
    assert out["since_halt"]["base"] and out["since_halt"]["base_source"] in {"param", "resolved", "stage-head"}
    assert _git(halted, "rev-parse", "HEAD") == head
