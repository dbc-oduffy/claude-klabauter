"""An all-landed run halted at the commit stage re-fires as resume_ledger with its recorded review verdict,
and the new run's terminal commit lands the residual reviewer edit and codes the plan."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import review_edits, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RESUMED_RUN_ID_KEY,
    RUN_DIR_ROOT,
    StageManifest,
    ledger_halt_path,
    read_ledger_halt,
    read_run_outcome,
)
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
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

RUN2 = "run2"
SCRIPT2 = "run2.mjs"
RESIDUE = "b.py"
HALTED_STARTED = 1000.0
_SID = "11111111-2222-3333-4444-555555555555"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 1}
_MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}
_HALT = {"halted": "commit", "plan": PLAN, "sizing": SIZING, "refused": "since-base-overlap"}


@pytest.fixture
def halted(repo, monkeypatch):  # noqa: F811
    """Every plan row checkpoint-landed (a.py, b.py) and run `RUN` halted at commit with its review verdict."""
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID)
    doc = yaml.safe_load((repo / SIZING).read_text(encoding="utf-8"))
    doc.update(
        name="refire fixture", plan=PLAN, deliverable_id=DLV, interaction_mode="pm", route="spec-dispatch",
        status="sized", exit_criterion={"statement": "done", "accepted": None},
    )
    (repo / SIZING).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    _write(repo, RESIDUE, "b = 1\n")
    _git(repo, "add", "a.py", RESIDUE, SIZING)
    _git(
        repo, "commit", "-q", "-m", "checkpoint(wave 1): 1 row — C1",
        "-m", f"Checkpoint-Plan: {PLAN}\nCheckpoint-Base: abc1234",
    )
    rec = repo / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING}) + "\n", encoding="utf-8")
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/submission.json", json.dumps({"submitted_at": HALTED_STARTED}))
    outcome = {"inline_review": _REVIEWED, "incomplete_chunks": [], "landed_chunks": ["C1"]}
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: _HALT, "run_outcome": outcome},
        repo_root=repo,
    )
    assert out["committed"] is True, out
    return repo


def _second_run(root) -> str:
    """Run `RUN2`'s ledger, stage manifest, commit request (the plan row's landed file plus a docs note)
    and script, with a freshly minted run token returned. The residue file is declared by nothing."""
    _write(root, "docs/note.md", "note\n")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t", paths=("a.py",)),
            ChunkCommit(id="docs", title="run docs", paths=("docs/note.md",)),
        ),
        deliverable_id=DLV, plan_path=PLAN,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/commit-request.txt", render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN2}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN2}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, SCRIPT2, f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN2}/manifest.json\n")
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/submission.json", json.dumps({"submitted_at": 5000.0}))
    rl.start(root, RUN2, all_of_pm_ask=False)
    rl.bind(root, RUN2, SIZING)
    return run_token.mint(root, RUN2, SCRIPT2, session_id=_SID, sizing_rel=SIZING)


def _commit(root, token: str, inline_review: dict) -> dict:
    return terminal_commit._handler(
        {
            "script_path": SCRIPT2, "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
            "inline_review": inline_review, "session_id": _SID, "ledger_verdict": _MET,
        },
        repo_root=root / ".git",
    )


def test_commit_halted_run_refires_and_the_new_run_lands_the_residual_reviewer_edit(halted, monkeypatch):
    pointer = read_ledger_halt(ledger_halt_path(halted, PLAN))
    assert (pointer.run_id, pointer.halted, pointer.blocking) == (RUN, "commit", ())
    assert read_run_outcome(halted, RUN)["inline_review"] == _REVIEWED

    _write(halted, RESIDUE, "b = 2  # reworded by the reviewer\n")

    verdict = gate(halted, SIZING)
    assert verdict.halt is None and verdict.resume_ledger is not None, verdict.halt
    inline_review = verdict.resume_ledger["inline_review"]
    assert inline_review == {**_REVIEWED, RESUMED_RUN_ID_KEY: RUN}
    assert verdict.resume_rows is None and verdict.resume_plan is None

    calls: list = []

    def review_wrote(root, sid, since):
        calls.append(since)
        return {RESIDUE: "reviewer"}

    monkeypatch.setattr(review_edits, "review_writes", review_wrote)
    token = _second_run(halted)
    out = _commit(halted, token, inline_review)

    assert out["committed"] is True, out
    assert calls == [HALTED_STARTED]
    assert out["rows_coded"] == {PLAN: ["C1"]}, out.get("rows_coded")
    assert "disposition: coded" in (halted / PLAN).read_text(encoding="utf-8")
    shas = [out["sha"]] + ([out["process_sha"]] if out.get("process_sha") else [])
    landed = {p for sha in shas for p in _git(halted, "show", "--name-only", "--format=", sha).split()}
    assert RESIDUE in landed
    assert _git(halted, "status", "--porcelain", "--", RESIDUE).strip() == ""
    assert "revise_and_refire" not in json.dumps(out)


def test_a_closed_run_does_not_resume_again(halted, monkeypatch):
    _write(halted, RESIDUE, "b = 2  # reworded by the reviewer\n")
    inline_review = gate(halted, SIZING).resume_ledger["inline_review"]
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {RESIDUE: "reviewer"})
    assert _commit(halted, _second_run(halted), inline_review)["committed"] is True

    later = gate(halted, SIZING)

    assert later.arm is None and "already ran" in later.halt["reason"]
