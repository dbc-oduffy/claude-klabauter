"""dispatch.terminal_commit's shipped-claim gate agrees with the landing set: a claim on a file a halted peer
run withholds is peer-owned and lands, a claim on a stranded executor-PARTIAL row's file refuses
partial-row-grade-owed naming the credit call, and a plain unbacked claim names its source and reason."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index, run_credit, run_token, shipped_claims, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import DELIVERY_NOTE_DIR, RECORD_DIR_ROOT, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    _REVIEWED,
    _SID,
    RUN,
    _git,
    _verdict,
    _write,
    repo,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NOTE = f"{DELIVERY_NOTE_DIR}/{RUN}.md"
SIBLING = "ask-20261024T100000-aaaaaa"


def _note(*paths: str) -> str:
    return "# Delivery\n\n## What shipped\n\n" + "".join(f"- `{p}`\n" for p in paths)


def _call(root, incomplete=("C9",), token=None) -> dict:
    params = {
        "script_path": "run.mjs", "incomplete_chunks": list(incomplete), "inline_review": _REVIEWED,
        "session_id": _SID, "ledger_verdict": _verdict({"essential": False}), "run_id": RUN,
    }
    if token:
        params["run_token"] = token
    return terminal_commit._handler(params, repo_root=root / ".git")


def _strand_c3(root, report: str) -> None:
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t", paths=("a.py",)),
            ChunkCommit(id="C3", title="wire peer gate", paths=("y.py",), report="reports/C3.md"),
        ),
        deliverable_id="dlv-alpha-aaaaaa", plan_path="docs/plans/p.md",
    )
    _write(root, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    _write(root, "reports/C3.md", report)
    _write(root, "y.py", "y = 1\n")
    _write(root, "a.py", "a = 1\n")


def test_a_claim_on_a_halted_peers_withheld_file_is_peer_owned_and_lands(repo, monkeypatch):  # noqa: F811
    _write(repo, "a.py", "a = 1\n")
    _write(repo, "x.py", "x = 1\n")
    _write(repo, NOTE, _note("a.py", "x.py"))
    halted_withheld_index.add_run(repo, SIBLING, ["x.py"])
    real_gate = terminal_commit.shipped_gate

    def gate(*args, owners=None, **kwargs):
        def no_spawn(*a, **k):
            raise AssertionError("owners started a process")

        def guarded(paths):
            with monkeypatch.context() as m:
                m.setattr(subprocess, "Popen", no_spawn)
                return owners(paths)

        return real_gate(*args, owners=guarded, **kwargs)

    monkeypatch.setattr(terminal_commit, "shipped_gate", gate)

    out = _call(repo)

    assert out["committed"] is True, out
    assert out["shipped_claims"]["peer_owned"] == [{"path": "x.py", "owner": SIBLING}]
    assert "x.py" not in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()


def test_a_claim_on_this_runs_own_halted_hold_is_not_peer_owned(repo):  # noqa: F811
    _write(repo, "a.py", "a = 1\n")
    _write(repo, "x.py", "x = 1\n")
    _write(repo, NOTE, _note("a.py", "x.py"))
    halted_withheld_index.add_run(repo, RUN, ["x.py"])

    out = _call(repo)

    assert out["committed"] is False
    assert out["refused"] == shipped_claims.SHIPPED_CLAIM_REFUSED
    assert "peer_owned" not in out["shipped_claims"]


def test_a_claim_on_a_stranded_partial_rows_file_refuses_row_grade_owed_until_credited(repo):  # noqa: F811
    _strand_c3(repo, "PARTIAL: edits done\nNot done: `rebuild-plugin` rc=0 AC, EM must run it\n")
    _write(repo, NOTE, _note("a.py", "y.py"))

    out = _call(repo, incomplete=("C3",))

    assert out["committed"] is False, out
    assert out["refused"] == shipped_claims.ROW_GRADE_OWED
    assert "C3" in out["error"] and "rebuild-plugin" in out["error"]
    assert "y.py" in out["error"] and "credit_chunks" in out["error"]
    assert out["shipped_claims"]["row_grade_owed"][0]["chunk"] == "C3"

    run_credit.record(repo / RECORD_DIR_ROOT / RUN, [{"chunk": "C3", "evidence": "tests pass"}], "t")
    again = _call(repo, incomplete=("C3",))

    assert again["committed"] is True, again
    assert "y.py" in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()


def test_a_claim_on_an_em_withheld_declared_file_refuses_naming_the_withhold_reason(repo):  # noqa: F811
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py", "b.py")),),
        deliverable_id="dlv-alpha-aaaaaa", plan_path="docs/plans/p.md",
    )
    _write(repo, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    _write(repo, "a.py", "a = 1\n")
    _write(repo, "b.py", "b = 1\n")
    _write(repo, NOTE, _note("a.py", "b.py"))
    _write(
        repo, f"state/warp/{RUN}/withhold.jsonl",
        json.dumps({"path": "b.py", "reason": "peer owns this", "at": "t"}) + "\n",
    )

    out = _call(repo)

    assert out["committed"] is False, out
    assert out["refused"] == shipped_claims.SHIPPED_CLAIM_REFUSED
    assert "b.py" in out["error"] and "peer owns this" in out["error"]


def test_a_plain_unbacked_claim_still_refuses_naming_source_and_reason(repo):  # noqa: F811
    _write(repo, "a.py", "a = 1\n")
    _write(repo, "ghost.py", "x = 1\n")
    _write(repo, NOTE, _note("ghost.py"))
    token = run_token.mint(repo, RUN, "run.mjs")

    out = _call(repo, token=token)

    assert out["committed"] is False
    assert out["refused"] == shipped_claims.SHIPPED_CLAIM_REFUSED
    assert "What shipped" in out["error"] and "ghost.py" in out["error"]
    assert "no row of this run declares it" in out["error"]
