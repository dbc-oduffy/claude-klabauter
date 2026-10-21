"""A halted run stamps the baton it recorded with status_reason and lands it, leaving it open."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_DEPENDENTS_UNSTARTED,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    write_run_baton,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261015T000000-aaaaaa"
BATON = "state/handoffs/2026-10-21-run-baton.md"
BATON_TEXT = (
    "---\ntitle: t\nstatus: open\ndeployment_state: ready_to_fire\n---\n\n# Baton\n"
)


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    return root


def _baton(root: Path, text: str = BATON_TEXT) -> Path:
    path = root / BATON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _call(root: Path, halted: dict) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: halted}
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_halt_stamps_status_reason_keeps_status_and_lands_baton(repo):
    baton = _baton(repo)
    write_run_baton(repo, RUN, BATON)

    out = _call(repo, {"halted": "refusal", "reason": "ignored for this kind"})

    assert out["committed"] is True, out
    text = baton.read_text(encoding="utf-8")
    assert "status: open\n" in text
    assert "deployment_state: ready_to_fire\n" in text
    assert f"status_reason: 'run {RUN} halted: refusal'\n" in text
    assert text.endswith("# Baton\n")
    assert BATON in out["paths"]
    assert BATON in _git(["show", "--name-only", "--format=", "HEAD"], repo).split()


def test_second_landing_writes_no_new_bytes(repo):
    baton = _baton(repo)
    write_run_baton(repo, RUN, BATON)
    _call(repo, {"halted": "refusal"})
    before = baton.read_bytes()
    mtime = baton.stat().st_mtime_ns

    again = _call(repo, {"halted": "refusal"})

    assert baton.read_bytes() == before
    assert baton.stat().st_mtime_ns == mtime
    assert again.get("committed") is not True or BATON not in again["paths"]


def test_existing_status_reason_is_replaced(repo):
    baton = _baton(repo, BATON_TEXT.replace("---\n\n# Baton", "status_reason: old\n---\n\n# Baton"))
    write_run_baton(repo, RUN, BATON)

    _call(repo, {"halted": "refusal"})

    text = baton.read_text(encoding="utf-8")
    assert text.count("status_reason:") == 1
    assert f"run {RUN} halted: refusal" in text


def test_dependents_unstarted_reason_rides_the_stamp(repo):
    baton = _baton(repo)
    write_run_baton(repo, RUN, BATON)

    _call(repo, {"halted": HALT_DEPENDENTS_UNSTARTED, "reason": "C2 waits on C1"})

    assert "C2 waits on C1" in (repo / RECORD_DIR_ROOT / RUN / "halted.json").read_text(encoding="utf-8")
    assert f"run {RUN} halted: {HALT_DEPENDENTS_UNSTARTED}: C2 waits on C1" in baton.read_text(encoding="utf-8")


def test_other_kind_stamp_carries_no_reason(repo):
    baton = _baton(repo)
    write_run_baton(repo, RUN, BATON)

    _call(repo, {"halted": "refusal", "reason": "secret detail"})

    assert "secret detail" not in baton.read_text(encoding="utf-8")


def test_no_run_baton_record_leaves_baton_untouched(repo):
    baton = _baton(repo)

    out = _call(repo, {"halted": "refusal"})

    assert out["committed"] is True
    assert baton.read_text(encoding="utf-8") == BATON_TEXT
    assert BATON not in out["paths"]


def test_baton_outside_handoffs_home_is_ignored(repo):
    other = repo / "docs" / "elsewhere.md"
    other.parent.mkdir()
    other.write_text(BATON_TEXT, encoding="utf-8")
    write_run_baton(repo, RUN, "docs/elsewhere.md")

    out = _call(repo, {"halted": "refusal"})

    assert other.read_text(encoding="utf-8") == BATON_TEXT
    assert "docs/elsewhere.md" not in out["paths"]


def test_unreadable_baton_does_not_fail_the_commit(repo):
    write_run_baton(repo, RUN, BATON)

    out = _call(repo, {"halted": "refusal"})

    assert out["committed"] is True
    assert BATON not in out["paths"]
