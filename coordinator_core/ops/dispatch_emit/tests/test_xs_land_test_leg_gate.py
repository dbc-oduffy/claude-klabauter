"""An XS land with a test file in scope runs its scoped test leg, and a not_run tests leg never commits."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "ask-xstl"
SCRIPT = f"scratch/warp/{RUN_ID}.workflow.mjs"


def _git(root, *a):
    return subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


def _request(paths):
    return CommitRequest(chunks=(ChunkCommit(id="X1", title="t", paths=tuple(paths)),), plan_path=None, version=1)


def _review(tests):
    return {"integration_stem": f"{RUN_ID}.rw", "slices": 0, "tests": tests}


def test_xs_test_target_predicate_is_in_the_emitted_test_guard():
    assert ask_compose.XS_TEST_TARGET_JS in ask_compose.__dict__["XS_TEST_TARGET_JS"]
    src = open(ask_compose.__file__, encoding="utf-8").read()
    assert "|| {XS_TEST_TARGET_JS})" in src


def test_not_run_tests_with_declared_test_file_refused(tmp_path):
    reply = tc._tests_not_run_refusal(
        tmp_path, {"arm": "xs"}, _request(["pkg/tests/test_a.py"]), _review({"status": "not_run"})
    )
    assert reply is not None and reply["refused"] == tc.REFUSED_TESTS_NOT_RUN


def test_passed_tests_not_refused(tmp_path):
    assert tc._tests_not_run_refusal(
        tmp_path, {"arm": "xs"}, _request(["pkg/tests/test_a.py"]), _review({"status": "passed"})
    ) is None


def test_no_test_file_in_scope_not_refused(tmp_path):
    assert tc._tests_not_run_refusal(
        tmp_path, {"arm": "xs"}, _request(["pkg/a.py"]), _review({"status": "not_run"})
    ) is None


def test_other_arm_not_refused(tmp_path):
    assert tc._tests_not_run_refusal(
        tmp_path, {"arm": "s"}, _request(["pkg/tests/test_a.py"]), _review({"status": "not_run"})
    ) is None


def test_met_item_on_not_run_evidence_refused_replay_of_b1f64a(tmp_path):
    params = {
        "arm": "xs",
        "ledger_verdict": {"items": [
            {"id": "R1", "status": "met", "met_by": "tests added in test_runner_killed_cell.py (not run in this phase)"}
        ]},
    }
    reply = tc._tests_not_run_refusal(
        tmp_path, params, _request(["tests/test_runner_killed_cell.py"]), _review({"status": "passed"})
    )
    assert reply is not None and reply["refused"] == tc.REFUSED_TESTS_NOT_RUN


def test_met_item_on_ran_evidence_not_refused(tmp_path):
    params = {"arm": "xs", "ledger_verdict": {"items": [{"id": "R1", "status": "met", "met_by": "21 passed"}]}}
    assert tc._tests_not_run_refusal(
        tmp_path, params, _request(["tests/test_x.py"]), _review({"status": "passed"})
    ) is None


def test_terminal_commit_entry_refuses_not_run(tmp_path):
    root = tmp_path
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    (root / "keep.txt").write_text("k\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    script = root / SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(render_marker(_request(["tests/test_a.py"])) + "\n", encoding="utf-8")
    params = {
        "script_path": SCRIPT,
        "incomplete_chunks": [],
        "run_id": RUN_ID,
        "run_token": run_token.mint(root / ".git", RUN_ID, SCRIPT),
        "run_base_sha": _git(root, "rev-parse", "HEAD"),
        "arm": "xs",
        "inline_review": {
            "integration_stem": f"{RUN_ID}.rw", "slices": 0, "fixes": 0, "wave_sidecar_paths": [],
            "wave_reviews": [], "prep": None, "delivery": None, "wave_not_run": [],
            "tests": {"status": "not_run"},
        },
    }
    head = _git(root, "rev-parse", "HEAD")
    reply = tc._terminal_commit(params, root / ".git", {}, {})
    assert "refused" in str(reply) and tc.REFUSED_TESTS_NOT_RUN in str(reply)
    assert _git(root, "rev-parse", "HEAD") == head
