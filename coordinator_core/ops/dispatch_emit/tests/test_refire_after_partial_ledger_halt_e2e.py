"""A run halted at the ledger check that landed one chunk and left another in the tree, an em-discharged
row, and a later refusal halt that unlinked the pointer: the same sizing refires as resume_ledger, plans
and executes nothing, and a new run's terminal commit lands the withheld chunk and codes the plan."""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_compose import _GATE_SCHEMA  # noqa: F401
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    ManifestRow,
    OwedRow,
    StageManifest,
    ledger_halt_path,
    read_ledger_halt,
)
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_ledger_resume import _script
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    DLV,
    PLAN,
    RUN,
    SIZING,
    _git,
    _write,
    repo,
)
from coordinator_core.ops.plan_tasks_mutate import _handler as mutate_handler
from coordinator_core.ops.plan_tasks_mutate import read_em_discharges

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REFUSED_RUN = "run-refused"
RUN2 = "run2"
SCRIPT2 = "run2.mjs"
_SID = "11111111-2222-3333-4444-555555555555"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_HALT = {"halted": "ledger-check", "plan": PLAN, "items": [{"id": "R1"}]}
_MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}
_DISCHARGE = "verified V1 by hand"


def _plan_text() -> str:
    return (
        f"---\ntitle: p\ndeliverable_id: {DLV}\nsizing_object: {SIZING}\nprime_exit_criterion:\n"
        "  statement: done\n  falsifier:\n    how: pytest tests/test_a.py::test_x -q\n"
        "    expected_when_true: passes\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: first\n  change_kind: code-edit\n  surface: a.py\n  queue_scope: project\n"
        "  writes:\n    - a.py\n  disposition: open\n  body: |\n    b\n"
        "- id: C2\n  title: second\n  change_kind: code-edit\n  surface: b.py\n  queue_scope: project\n"
        "  writes:\n    - b.py\n  disposition: open\n  body: |\n    b\n"
        "- id: V1\n  title: em step\n  change_kind: script-edit\n  surface: b.py\n  queue_scope: project\n"
        "  performer: em\n  writes: []\n  disposition: open\n  body: |\n    by hand\n```\n"
    )


def _record(root, run: str) -> None:
    rec = root / RECORD_DIR_ROOT / run
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING}) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{run}",
        rows=tuple(
            ManifestRow(id=i, agent_type="coordinator:executor", model="m", brief_path=f"b/{i}.md", writes=(w,), wave=0)
            for i, w in (("C1", "a.py"), ("C2", "b.py"))
        ),
        review_declared_paths=(), marker_path=f"{RUN_DIR_ROOT}/{run}/commit-request.txt",
        owed=(OwedRow(id="V1", reason="em-performed", detail="by hand"),),
    )
    (rec / "manifest.json").write_text(json.dumps(manifest.to_json()) + "\n", encoding="utf-8")


@pytest.fixture
def halted(repo, monkeypatch):  # noqa: F811
    """RUN staged C1, C2 and owed V1, halted at R1 with C1 landed by the checkpoint and b.py left modified."""
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID)
    doc = yaml.safe_load((repo / SIZING).read_text(encoding="utf-8"))
    doc.update(
        name="partial fixture", plan=PLAN, deliverable_id=DLV, interaction_mode="pm", route="spec-dispatch",
        status="sized", exit_criterion={"statement": "done", "accepted": None},
    )
    (repo / SIZING).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    _write(repo, PLAN, _plan_text())
    _git(repo, "add", "a.py", SIZING)
    _git(
        repo, "commit", "-q", "-m", "checkpoint(wave 1): 1 row — C1",
        "-m", f"Checkpoint-Plan: {PLAN}\nCheckpoint-Base: abc1234",
    )
    _write(repo, "b.py", "b\n")
    _record(repo, RUN)
    out = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: _HALT}, repo_root=repo
    )
    assert out["committed"] is True, out
    assert read_ledger_halt(ledger_halt_path(repo, PLAN)).blocking == ("R1",)
    assert "b.py" in _git(repo, "status", "--porcelain")
    return repo


def _discharge_v1(root) -> None:
    out = asyncio.run(
        mutate_handler(
            {"verb": "em-discharge", "plan_path": str(root / PLAN), "id": "V1", "text": _DISCHARGE},
            repo_root=root / ".git",
        )
    )
    assert out["exit_code"] == 0 and out["applied"] is True, out


def _refusal_halt_unlinks_the_pointer(root) -> None:
    _record(root, REFUSED_RUN)
    out = terminal_commit._handler(
        {
            "script_path": "run.mjs", "run_id": REFUSED_RUN,
            HALTED_RECORD_PARAM: {"halted": "refusal", "plan": PLAN, "reason": "refused"},
        },
        repo_root=root,
    )
    assert out["committed"] is True, out
    assert not ledger_halt_path(root, PLAN).exists()


def _snapshot(root) -> dict:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for base in ("state/sizings", "docs/plans")
        for p in sorted((root / base).rglob("*"))
        if p.is_file()
    }


def _second_run(root) -> str:
    """Run `RUN2`: its re-staged commit request carries the withheld chunk C2 and a docs note."""
    _write(root, "docs/note.md", "note\n")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C2", title="second", paths=("b.py",)),
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
    rl.start(root, RUN2, all_of_pm_ask=False)
    rl.bind(root, RUN2, SIZING)
    return run_token.mint(root, RUN2, SCRIPT2, session_id=_SID, sizing_rel=SIZING)


def _commit(root, token: str) -> dict:
    return terminal_commit._handler(
        {
            "script_path": SCRIPT2, "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
            "inline_review": _REVIEWED, "session_id": _SID, "ledger_verdict": _MET,
        },
        repo_root=root / ".git",
    )


def test_refire_resumes_at_the_ledger_plans_nothing_and_the_withheld_chunk_lands(halted, monkeypatch):
    _discharge_v1(halted)
    _refusal_halt_unlinks_the_pointer(halted)
    rl.apply_verdict(halted, SIZING, _MET, run_id="oob", sha="b" * 7)
    rows = {r["id"]: r for r in rr.read_register((halted / SIZING).read_text(encoding="utf-8")).rows}
    assert rows["R1"]["status"] == "met"
    assert read_em_discharges(halted / PLAN)["V1"]["text"] == _DISCHARGE
    before = _snapshot(halted)

    verdict = gate(halted, SIZING)
    assert verdict.halt is None, verdict.halt
    assert verdict.resume_ledger is not None, verdict.to_json()
    assert verdict.resume_ledger["plan"] == PLAN and verdict.resume_ledger["blocking"] == ["R1"]
    assert verdict.resume_ledger["em_discharged"] == {"V1": _DISCHARGE}
    assert verdict.resume_plan is None and verdict.resume_rows is None and verdict.spine_plan is None
    assert _snapshot(halted) == before

    script = _script(monkeypatch)
    resume = script.index("const _resumeLedger = !_halted && !!_gate.resume_ledger;")
    assert resume < script.index("label: 'verify'")
    assert resume < script.index("label: 'plan'")
    assert "if (!_halted && !_resumeLedger) {\n  phase('Execute');" in script
    assert "_gate.resume_ledger.em_discharged" in script

    token = _second_run(halted)
    out = _commit(halted, token)
    assert out["committed"] is True, out
    assert "C2" in out["rows_coded"][PLAN], out.get("rows_coded")
    assert "b.py" in _git(halted, "show", "--name-only", "--format=", "HEAD~1") + _git(
        halted, "show", "--name-only", "--format=", "HEAD"
    )
    assert "disposition: coded" in (halted / PLAN).read_text(encoding="utf-8")

    later = gate(halted, SIZING)
    assert later.arm is None and later.halt is not None and later.resume_ledger is None
