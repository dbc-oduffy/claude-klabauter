"""The terminal commit never lands over an unresolved REQUIRES_CHANGES verdict or an unrun tests leg on a
diff declaring a test file, at any arm, however the verdict is carried; a resolved verdict still lands."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.terminal_commit_refusals import REFUSED_REVIEW_OWED
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "ask-rcgate"
SCRIPT = f"scratch/warp/{RUN_ID}.workflow.mjs"
SIDECAR = "reviewer.md"


def _git(root, *a):
    return subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


@pytest.fixture
def root(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "keep.txt").write_text("k\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=("tests/test_a.py",)),), plan_path=None, version=1
    )
    script = tmp_path / SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(render_marker(request) + "\n", encoding="utf-8")
    return tmp_path


def _sidecar(root, rows, verdict="REQUIRES_CHANGES"):
    (root / SIDECAR).write_text(
        f"---\nverdict: {verdict}\nfindings_ledger: {{rows: {len(rows)}, verified_at: 2026-11-10T00:00:00Z}}\n---\n\n"
        f"## Findings Ledger\n\n```json\n{json.dumps(rows)}\n```\n",
        encoding="utf-8", newline="",
    )


def _review(**extra):
    return {
        "integration_stem": f"{RUN_ID}.rw", "slices": 1, "fixes": 0, "wave_sidecar_paths": [],
        "wave_reviews": [], "prep": None, "delivery": None, "wave_not_run": [],
        "tests": {"status": "passed"}, **extra,
    }


def _fire(root, review, arm="s"):
    params = {
        "script_path": SCRIPT, "incomplete_chunks": [], "run_id": RUN_ID, "arm": arm,
        "run_token": run_token.mint(root / ".git", RUN_ID, SCRIPT),
        "run_base_sha": _git(root, "rev-parse", "HEAD"), "inline_review": review,
    }
    head = _git(root, "rev-parse", "HEAD")
    reply = tc._terminal_commit(params, root / ".git", {}, {})
    return reply, head


def _refused(root, review, code, arm="s"):
    reply, head = _fire(root, review, arm)
    assert reply.get("refused") == code, reply
    assert reply.get("committed") is not True
    assert _git(root, "rev-parse", "HEAD") == head
    assert "re-fire" in reply["error"]
    return reply


UNAPPLIED = [{"id": "F1", "file": "tests/test_a.py", "before": "pass", "after": "assert True", "status": "applied", "verified": True}]


def test_standing_verdict_with_an_unapplied_finding_refuses(root):
    _sidecar(root, UNAPPLIED)
    _refused(root, _review(wave_sidecar_paths=[SIDECAR]), REFUSED_REVIEW_OWED)


def test_standing_verdict_with_no_ledger_rows_refuses(root):
    _sidecar(root, [])
    reply = _refused(root, _review(wave_sidecar_paths=[SIDECAR]), REFUSED_REVIEW_OWED)
    assert "no-ledger-rows" in reply["error"]


def test_standing_verdict_claimed_with_no_wave_sidecar_refuses(root):
    review = _review(wave_reviews=[{"agent_type": "coordinator:reviewer", "verdict": "REQUIRES_CHANGES"}])
    reply = _refused(root, review, REFUSED_REVIEW_OWED)
    assert "no-sidecar" in reply["error"]


def test_standing_verdict_whose_sidecar_is_unreadable_refuses(root):
    review = _review(
        wave_sidecar_paths=["gone.md"],
        wave_reviews=[{"agent_type": "coordinator:reviewer", "verdict": "REQUIRES_CHANGES", "sidecar_path": "gone.md"}],
    )
    reply = _refused(root, review, REFUSED_REVIEW_OWED)
    assert "sidecar-unreadable" in reply["error"]


def test_claimed_standing_verdict_with_verdictless_sidecar_and_no_rows_refuses(root):
    (root / SIDECAR).write_text("no frontmatter\n", encoding="utf-8")
    review = _review(
        wave_sidecar_paths=[SIDECAR],
        wave_reviews=[{"agent_type": "coordinator:reviewer", "verdict": "REQUIRES_CHANGES", "sidecar_path": SIDECAR}],
    )
    reply = _refused(root, review, REFUSED_REVIEW_OWED)
    assert "no-ledger-rows" in reply["error"]


@pytest.mark.parametrize("arm", ["xs", "s", "m", "l"])
def test_not_run_tests_on_a_diff_declaring_a_test_file_refuse_at_every_arm(root, arm):
    _refused(root, _review(tests={"status": "not_run"}), tc.REFUSED_TESTS_NOT_RUN, arm)


def test_an_approved_verdict_with_passed_tests_lands(root):
    _sidecar(root, [], verdict="APPROVED")
    reply, _ = _fire(root, _review(wave_sidecar_paths=[SIDECAR]))
    assert reply.get("committed") is True, reply


def test_a_standing_verdict_whose_findings_are_all_in_the_tree_lands(root):
    rows = [{"id": "F1", "file": "tests/test_a.py", "before": "zzz", "after": "pass", "status": "applied", "verified": True}]
    _sidecar(root, rows)
    reply, _ = _fire(root, _review(wave_sidecar_paths=[SIDECAR]))
    assert reply.get("committed") is True, reply
