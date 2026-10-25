"""Replay of a ledger-check halt whose work landed: out_of_scope revise -> refire -> terminal_commit ->
the route each refusal names -> the queued close legs run in-process, with the ruled-out row withheld."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import halted_record_message
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, resumable_plan
from coordinator_core.testing.git_write import write_seen_by_git
from coordinator_core.warp_fire import fire_command
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PARENT = "ask-parent-20261023T000000-abcdef"
RUN = "ask-20261023T121013-e7c4ae"
SUCCESSOR = "ask-20261023T121013-e4ad19"
PLAN = "docs/plans/2026-10-23-replay-plan.md"
SIZING = "state/sizings/2026-10-23-replay-sizing.yaml"
SUCCESSOR_SIZING = "state/sizings/2026-10-23-successor.yaml"
REVIEW_RECORD = ".coordinator-local/subagent-share/replay/replay-review.md"
WORDS = "R1 is out of scope per the APM"
ROUTE = "dispatch.record_halted_run_landed {run_id: " + RUN
ROWS = ("C1", "C2", "C3")
BASE = "a" * 40


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return done.stdout.strip()


def _put(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    write_seen_by_git(path, text, newline="\n")


def _commit(root: Path, message: str, *paths: str) -> str:
    _git(root, "add", "--", *paths)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t.example", "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _plan_text(base: str) -> str:
    rows = "".join(
        f"- id: {i}\n  title: row {i}\n  change_kind: code-edit\n  surface: src/{i}.py\n"
        f"  writes: [src/{i}.py]\n  disposition: open\n  register_rows: [\"R1\"]\n"
        for i in ROWS
    )
    return (
        "---\n"
        'title: "Replay plan"\ncreated: 2026-10-23\nauthor: test-fixture\nstatus: approved\n'
        'branch: "work/test/2026-10-23"\nplan_id: "pln-replay-000001"\n'
        'deliverable_id: "dlv-replay-000001"\n'
        f"sizing_object: {SIZING}\n"
        "prime_exit_criterion:\n  statement: The replay surface behaves as claimed.\n"
        f"  derived_from: {SIZING}\n  falsifier:\n    how: read the replay surface\n"
        f"    baseline_output: 'wrong'\n    baseline_ref: {base}\n    expected_when_true: 'right'\n"
        "---\n\n# Replay plan\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "```\n"
    )


def _sizing_doc() -> dict:
    return {
        "schema": "sizing-object", "intent": "replay the ledger-halt close", "name": "replay",
        "estimate": {"tshirt": "M", "provisional": True}, "route": "plan", "detents": [], "fork": None,
        "xl_exit": None, "status": "routed", "plan": PLAN,
        "premise": {"provenance": "read", "evidence": "test fixture, no real premise verified"},
        "deliverable_id": "dlv-replay-000001",
        "pm_verbatims": [{"turn": 1, "text": "lint the thing"}],
    }


def _review_record(sha: str) -> str:
    fm = {
        "prep": {"slice_files": [f"src/{i}.py" for i in ROWS], "product_files": 3, "run_base_sha": BASE},
        "delivery": {"verdict": "PASS"},
        "tests": {"status": "pass", "sidecar": None},
        "criterion": {"status": "met", "observation": "the replay surface behaves as claimed", "sidecar": None},
        "slices": 1, "unresolved": [],
    }
    return "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\nreview of " + sha + "\n"


def _sibling_run(rid: str, after=(), status=c.STATUS_READY, sizing=SIZING) -> c.SiblingRun:
    return c.SiblingRun(rid, rid, tuple(after), sizing, status)


def _build(root: Path) -> str:
    """The halted state: a parent record, the run's sibling status ``halted``, a plan and a bound sizing,
    the landed rows' commits, and the halt commit; returns the halt commit's sha."""
    _git(root, "init", "-q")
    _git(root, "config", "commit.gpgsign", "false")
    _put(root, "README.md", "init\n")
    base = _commit(root, "init", "README.md")
    (root / "archive").mkdir()
    _put(root, "archive/.keep", "")
    _put(root, SIZING, yaml.safe_dump(_sizing_doc(), sort_keys=False))
    _put(root, PLAN, _plan_text(base))
    _commit(root, "seed", SIZING, PLAN, "archive/.keep")
    for i in ROWS:
        _put(root, f"src/{i}.py", f"{i} landed\n")
    _commit(
        root, f"checkpoint(wave 1): 3 rows — {', '.join(ROWS)}\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base[:7]}",
        *[f"src/{i}.py" for i in ROWS],
    )
    rl.start(root, RUN)
    rl.bind(root, RUN, SIZING)
    runs = [_sibling_run(RUN), _sibling_run(SUCCESSOR, [RUN], c.STATUS_AFTER, SUCCESSOR_SIZING)]
    _put(root, c.parent_record_rel(PARENT), json.dumps(c.ParentRecord(PARENT, tuple(runs)).to_json()))
    for rid, status in ((RUN, "halted"), (SUCCESSOR, "after")):
        _put(root, c.status_rel(rid), json.dumps({"parent_id": PARENT, "status": status}))
    halted = {
        "status": "halted", "halted": "ledger-check", "blocking": ["R1"], "plan": PLAN,
        "sizing_rel": SIZING, "run_base_sha": BASE,
    }
    _put(root, f"state/warp/{RUN}/halted.json", json.dumps(halted))
    message = halted_record_message(RUN, halted) + f"\nLands-Chunks: {', '.join(ROWS)}"
    return _commit(root, message, f"state/warp/{RUN}/halted.json", f"state/warp/{RUN}/run.json")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    launches: list = []
    monkeypatch.setattr(op, "_launch", lambda root, run_id: launches.append(run_id) or True)
    root = tmp_path
    sha = _build(root)
    return root, sha, launches


def _register(root: Path) -> dict:
    return {r["id"]: r for r in rr.read_register((root / SIZING).read_text(encoding="utf-8")).rows}


def _run_job(root: Path) -> dict:
    return cb.run_background(cb.JobContext(root, root / ".git", RUN, (), None))


def _drive(root: Path, sha: str) -> dict:
    """Steps 1 to 4 of the replay; returns the op's reply."""
    revised = rl._handler({"run_id": RUN, "verbatim": WORDS, "out_of_scope": ["R1"]}, repo_root=root)
    assert revised["ok"] and revised["withheld"] == ["R1"]
    assert _register(root)["R1"]["status"] == "deferred"

    with pytest.raises(SizingFireRefused) as refire:
        resumable_plan(yaml.safe_load((root / SIZING).read_text(encoding="utf-8")), SIZING, root, "m_plus")
    (refusal,) = refire.value.fields
    assert "already ran" in refusal and ROUTE in refusal

    digest = root / "scratch" / "task-output.json"
    digest.parent.mkdir(parents=True, exist_ok=True)
    params = {"run_id": RUN, "note": "halted at ledger-check", "ledger_verdict": {"ok": False}}
    digest.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    refused = terminal_commit._handler({"task_output_path": str(digest)}, repo_root=root)
    assert refused["refused"] == "halted-run" and ROUTE in refused["error"]

    return op._handler({"run_id": RUN, "sha": sha}, root)


def _stage_review(root: Path, sha: str) -> None:
    """The review sidecar and the run outcome naming it, in place before the op queues the close:
    the review-stamp leg is queued only when the op resolves a review record."""
    _put(root, REVIEW_RECORD, _review_record(sha))
    _put(
        root, f"state/warp/{RUN}/run-outcome.json",
        json.dumps({"inline_review": {"integration": {"sidecar": REVIEW_RECORD}}}),
    )


def _coded(root: Path) -> list:
    text = (root / PLAN).read_text(encoding="utf-8")
    block = text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return [(r["id"], r["disposition"]) for r in yaml.safe_load(block)]


def test_halt_to_named_route_to_closed_with_r1_withheld(repo):
    root, sha, launches = repo
    _stage_review(root, sha)
    out = _drive(root, sha)
    assert out["ok"] and out["written"] is True
    assert out["close"]["job"] == f".coordinator-local/close-jobs/{RUN}.json" and out["close"]["launched"] is True
    assert out["blocking_rows"] == ["R1"] and launches == [RUN]
    assert out["siblings"]["released"] == [fire_command(SUCCESSOR_SIZING)]

    legs = _run_job(root)

    assert legs["coded-stamp"].status == "ran", legs["coded-stamp"].detail
    assert _coded(root) == [(i, "coded") for i in ROWS]
    assert legs["review-stamp"].status == "ran", legs["review-stamp"].detail
    assert legs["plan-reconciliation"].status == "ran", legs["plan-reconciliation"].detail
    assert "status: implemented" in (root / PLAN).read_text(encoding="utf-8")
    assert legs["sizing-ship"].status == "ran", legs["sizing-ship"].detail
    assert "deliverable-cascade" in legs
    sizing = yaml.safe_load((root / SIZING).read_text(encoding="utf-8"))
    assert sizing["status"] == "shipped" and _register(root)["R1"]["status"] == "deferred"
    marker = json.loads((root / f"state/warp/{RUN}/halted.json").read_text(encoding="utf-8"))
    assert marker["status"] == "landed" and marker["sha"] == sha
    assert legs["close-commit"].status == "ran", legs["close-commit"].detail
    assert json.loads((root / c.status_rel(RUN)).read_text(encoding="utf-8"))["status"] == "landed"


def test_without_a_review_record_the_stamp_and_flip_fail_while_the_rest_lands(repo):
    root, sha, launches = repo
    out = _drive(root, sha)
    assert out["blocking_rows"] == ["R1"] and out["siblings"]["released"] == [fire_command(SUCCESSOR_SIZING)]
    legs = _run_job(root)
    assert "review-stamp" not in out["close"]["queued"] and "review-stamp" not in legs
    job = json.loads((root / ".coordinator-local" / "close-jobs" / f"{RUN}.json").read_text(encoding="utf-8"))
    assert job["settled"]["review-stamp"]["status"] == "not-applicable"
    assert legs["plan-reconciliation"].status == "failed"
    assert "status: implemented" not in (root / PLAN).read_text(encoding="utf-8")
    assert legs["coded-stamp"].status == "ran" and _coded(root) == [(i, "coded") for i in ROWS]
    assert legs["sizing-ship"].status == "ran"
    assert _register(root)["R1"]["status"] == "deferred"
    marker = json.loads((root / f"state/warp/{RUN}/halted.json").read_text(encoding="utf-8"))
    assert marker["status"] == "landed"
