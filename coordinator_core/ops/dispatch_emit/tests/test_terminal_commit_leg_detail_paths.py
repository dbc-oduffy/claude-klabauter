"""Inline close legs keep a failed leg's cause readable: path tokens are normalised, not withheld."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import run_close, terminal_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

PEER_PATH = "Z:/coordinator-content-repo/coordinator/x.md"  # abs-path-ok: literal token under test, a peer-repo spelling


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, **no_console_creationflags()
    )


@pytest.fixture
def base(tmp_path, monkeypatch):
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_fixture(parent)
    outbox = parent / "lessons-outbox"
    outbox.mkdir()
    noop = parent / "noop.py"
    noop.write_text("pass\n", encoding="utf-8")
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(terminal_commit, "CLOSE_JOB_SCRIPT", str(noop))
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], [])
    )
    return repo, script


def _fire(repo: Path, script: str) -> dict:
    return terminal_commit._handler(
        {
            "script_path": script,
            "incomplete_chunks": [],
            "session_id": driver.SESSION_ID,
            "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
        },
        repo_root=repo / ".git",
    )


def test_review_refusal_keeps_its_words_and_normalises_both_paths(base, monkeypatch):
    repo, script = base
    plan_abs = (repo / driver.PLAN_REL).as_posix()
    monkeypatch.setattr(
        terminal_commit,
        "_mint_review_stamp",
        lambda *a, **k: {
            "review_stamp": "refused",
            "review_stamp_refusal": f"stamp refused: plan {plan_abs} cites {PEER_PATH} unreadably",
        },
    )

    leg = _fire(repo, script)["close"]["legs"]["review"]

    assert leg["status"] == "failed"
    assert driver.PLAN_REL in leg["detail"] and plan_abs not in leg["detail"]
    assert "<path>" in leg["detail"] and PEER_PATH not in leg["detail"]
    assert "stamp refused" in leg["detail"] and "unreadably" in leg["detail"]


def test_plan_reconciliation_refusal_keeps_its_words_and_normalises_both_paths(base, monkeypatch):
    repo, script = base
    plan_abs = (repo / driver.PLAN_REL).as_posix()
    monkeypatch.setattr(
        terminal_commit,
        "_stamp_plan_implemented",
        lambda *a, **k: {
            "plan_status": "refused",
            "plan_status_reason": f"open spine rows in {plan_abs}; see {PEER_PATH}",
        },
    )

    leg = _fire(repo, script)["close"]["legs"]["plan-reconciliation"]

    assert leg["status"] == "failed"
    assert driver.PLAN_REL in leg["detail"] and plan_abs not in leg["detail"]
    assert "<path>" in leg["detail"] and PEER_PATH not in leg["detail"]
    assert "open spine rows" in leg["detail"]


def test_plan_citing_a_peer_repo_path_still_reconciles(base):
    repo, script = base
    plan = repo / driver.PLAN_REL
    plan.write_text(
        plan.read_text(encoding="utf-8")
        + f"\nSee {PEER_PATH} and C:\\coordinator-content-repo\\coordinator\\docs\\wiki\\x.md for the doctrine.\n",  # abs-path-ok: literal under test
        encoding="utf-8",
    )
    _git(repo, "add", "-A", "docs")
    _git(repo, "commit", "-q", "-m", "plan cites a peer repo")

    close = _fire(repo, script)["close"]

    assert close["legs"]["plan-reconciliation"]["status"] == "ran", close["legs"]["plan-reconciliation"]


def test_leg_normalises_and_result_passes_hygiene():
    leg = terminal_commit._leg("failed", "bad C:\\x\\y and /etc/z ok docs/p.md")

    run_close.check_path_hygiene(leg.detail)
    assert "docs/p.md" in leg.detail and "<path>" in leg.detail


def test_path_withheld_constant_is_gone():
    assert not hasattr(terminal_commit, "_PATH_WITHHELD")
