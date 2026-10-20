"""A ledger-halted run whose owed work lands before the refire (out of band, or by the resume's remaining-work
leg): the same sizing re-fires as resume_ledger, a new run's terminal commit lands and codes the plan, and a
still-unmet row re-halts at the ledger gate."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
    ledger_halt_path,
    read_ledger_halt,
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
_SID = "11111111-2222-3333-4444-555555555555"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_HALT = {"halted": "ledger-check", "plan": PLAN, "items": [{"id": "R1"}]}


@pytest.fixture
def halted(repo, monkeypatch):  # noqa: F811
    """The product-commit repo with every plan row checkpoint-landed and run `RUN` halted at R1."""
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID)
    doc = yaml.safe_load((repo / SIZING).read_text(encoding="utf-8"))
    doc.update(
        name="resume fixture", plan=PLAN, deliverable_id=DLV, interaction_mode="pm", route="spec-dispatch",
        status="sized", exit_criterion={"statement": "done", "accepted": None},
    )
    (repo / SIZING).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    _git(repo, "add", "a.py", SIZING)
    _git(
        repo, "commit", "-q", "-m", "checkpoint(wave 1): 1 row — C1",
        "-m", f"Checkpoint-Plan: {PLAN}\nCheckpoint-Base: abc1234",
    )
    rec = repo / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING}) + "\n", encoding="utf-8")
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: _HALT}, repo_root=repo
    )
    assert out["committed"] is True, out
    assert read_ledger_halt(ledger_halt_path(repo, PLAN)).blocking == ("R1",)
    return repo


def _sizings(root) -> list:
    return sorted(p.name for p in (root / "state" / "sizings").iterdir())


def _met_out_of_band(root) -> None:
    rl.apply_verdict(
        root, SIZING, {"items": [{"id": "R1", "status": "met", "wired": True}]}, run_id="oob", sha="b" * 7
    )


def _second_run(root) -> str:
    """Run `RUN2`'s ledger, stage manifest, commit request (the plan row's landed file plus a docs
    note) and script, with a freshly minted run token returned."""
    _write(root, "docs/note.md", "note\n")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t", paths=("a.py",)),
            ChunkCommit(id="docs", title="run docs", paths=("docs/note.md",)),
        ),
        deliverable_id=DLV, plan_path=PLAN,
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/commit-request.txt", render_marker(request) + "\n")
    # rows=() models a stage that staged no executable row; the composed script's own stage call is not run here.
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN2}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN2}/commit-request.txt",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, SCRIPT2, f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN2}/manifest.json\n")
    rl.start(root, RUN2, all_of_pm_ask=False)  # after the scratch run dir, as the emit does: the ledger lands beside it
    rl.bind(root, RUN2, SIZING)
    return run_token.mint(root, RUN2, SCRIPT2, session_id=_SID, sizing_rel=SIZING)


def _commit(root, token: str, verdict: dict) -> dict:
    return terminal_commit._handler(
        {
            "script_path": SCRIPT2, "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
            "inline_review": _REVIEWED, "session_id": _SID, "ledger_verdict": verdict,
        },
        repo_root=root / ".git",
    )


def test_row_met_out_of_band_resumes_the_same_sizing_and_the_new_run_commits(halted):
    before = _sizings(halted)
    _met_out_of_band(halted)

    verdict = gate(halted, SIZING)
    assert verdict.halt is None and verdict.resume_ledger is not None, verdict.halt
    assert verdict.resume_ledger["plan"] == PLAN and verdict.resume_ledger["blocking"] == ["R1"]
    assert _sizings(halted) == before

    token = _second_run(halted)
    out = _commit(halted, token, {"items": [{"id": "R1", "status": "met", "wired": True}]})
    assert out["committed"] is True, out
    assert out["rows_coded"] == {PLAN: ["C1"]}, out.get("rows_coded")
    assert "disposition: coded" in (halted / PLAN).read_text(encoding="utf-8")

    later = gate(halted, SIZING)
    # The closing commit ships the sizing (`sizing.ship`), and a shipped sizing is held by the status gate in
    # `sizing_fire.collect_fire_refusals`; the closed-plan "already ran" refusal is pinned by test_ask_gate_resume.py.
    assert later.arm is None and later.resume_ledger is None
    assert later.halt["kind"] == "refusal" and "'shipped'" in later.halt["reason"]


def test_row_still_not_met_refuses_the_commit_at_the_ledger_gate(halted):
    rows = {r["id"]: r for r in rr.read_register((halted / SIZING).read_text(encoding="utf-8")).rows}
    assert rows["R1"]["status"] != "met"
    token = _second_run(halted)
    head = _git(halted, "rev-parse", "HEAD")

    verdict = {"items": [{"id": "R1", "status": "not_met", "essential": True, "remaining": ["the test is red"]}]}
    out = _commit(halted, token, verdict)

    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert [f["id"] for f in out["ledger_failures"]] == ["R1"]
    assert _git(halted, "rev-parse", "HEAD") == head
    assert read_ledger_halt(ledger_halt_path(halted, PLAN)) is not None
