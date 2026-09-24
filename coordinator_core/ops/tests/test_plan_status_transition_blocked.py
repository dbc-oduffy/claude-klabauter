"""Characterization tests for the stamp-blocked / stamp-unblocked verbs of
coordinator_core.ops.plan_status_transition (C1, docs/plans/2026-09-23-
plan-blocked-state.md).

Real git, like test_plan_status_transition.py — the writer-side commit
ownership and locked_rmw machinery this exercises reads actual git state,
so a mocked git would validate the fixture harness, not the verb's
behaviour.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.frontmatter import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops.plan_status_transition import main
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GIT_ENV_KEYS = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _ensure_git_repo(tmp_path: Path) -> None:
    if (tmp_path / ".git").exists():
        return
    env = {**os.environ, **_GIT_ENV_KEYS}
    subprocess.run(
        ["git", "init"], cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", ".gitkeep"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )


def _track(tmp_path: Path, p: Path) -> None:
    env = {**os.environ, **_GIT_ENV_KEYS}
    rel = p.relative_to(tmp_path)
    subprocess.run(
        ["git", "add", "--", str(rel)],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "commit", "-m", f"seed {rel}"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )


def _write(tmp_path: Path, name: str, body: str) -> Path:
    _ensure_git_repo(tmp_path)
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    _track(tmp_path, p)
    return p


# ---------------------------------------------------------------------------
# AC1: approved -> blocked
# ---------------------------------------------------------------------------

def test_stamp_blocked_happy_path(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "---\ntitle: T\nstatus: approved\n---\n\nBody.\n")
    t0 = time.process_time()
    rc = main(["stamp-blocked", "--plan", str(p), "--reason", "waiting on DoE schema"])
    elapsed_ms = (time.process_time() - t0) * 1000
    assert rc == 0
    assert elapsed_ms < 200, f"stamp-blocked process_time {elapsed_ms:.1f}ms over the 200ms bar"
    out = capsys.readouterr().out
    assert 'status "approved" → blocked' in out
    text = p.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    assert split is not None
    assert read_fm_field_unquoted(split.fm_text, "status") == "blocked"
    assert read_fm_field_unquoted(split.fm_text, "status_reason") == "waiting on DoE schema"


def test_stamp_blocked_inserts_status_reason_after_status_key(tmp_path):
    p = _write(tmp_path, "p.md", "---\ntitle: T\nstatus: approved\nowner: x\n---\n\nBody.\n")
    rc = main(["stamp-blocked", "--plan", str(p), "--reason", "r"])
    assert rc == 0
    text = p.read_text(encoding="utf-8")
    assert text == (
        "---\ntitle: T\nstatus: blocked\nstatus_reason: r\nowner: x\n---\n\nBody.\n"
    )


# ---------------------------------------------------------------------------
# AC2: refusals -- writes nothing
# ---------------------------------------------------------------------------

def test_stamp_blocked_requires_reason(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: approved\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-blocked", "--plan", str(p)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "requires --reason" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_blocked_rejects_blank_reason(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: approved\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-blocked", "--plan", str(p), "--reason", "   "])
    assert rc == 1
    err = capsys.readouterr().err
    assert "requires --reason" in err
    assert p.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "status", ["draft", "reviewed", "executing", "landed", "implemented", "superseded",
               "abandoned", "deferred"]
)
def test_stamp_blocked_refuses_non_approved_source(tmp_path, capsys, status):
    original = f"---\ntitle: T\nstatus: {status}\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-blocked", "--plan", str(p), "--reason", "r"])
    assert rc == 1
    err = capsys.readouterr().err
    assert f'status "{status}"' in err
    assert "expected: approved" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_blocked_rejects_by_flag(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: approved\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-blocked", "--plan", str(p), "--reason", "r", "--by", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not accept --by" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_blocked_rejects_override_reason_flag(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: approved\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-blocked", "--plan", str(p), "--reason", "r", "--override-reason", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not accept --override-reason" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_blocked_rejects_findings_flag(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: approved\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-blocked", "--plan", str(p), "--reason", "r", "--findings", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not accept --findings" in err
    assert p.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# AC2 (continued): re-running stamp-blocked on an already-blocked plan
# ---------------------------------------------------------------------------

def test_stamp_blocked_same_reason_is_byte_identical_noop(tmp_path, capsys):
    body = "---\ntitle: T\nstatus: blocked\nstatus_reason: r\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", body)
    rc = main(["stamp-blocked", "--plan", str(p), "--reason", "r"])
    assert rc == 0
    assert p.read_text(encoding="utf-8") == body
    out = capsys.readouterr().out
    assert "no-op" in out


def test_stamp_blocked_different_reason_replaces_status_reason_only(tmp_path):
    body = "---\ntitle: T\nstatus: blocked\nstatus_reason: old reason\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", body)
    rc = main(["stamp-blocked", "--plan", str(p), "--reason", "new reason"])
    assert rc == 0
    text = p.read_text(encoding="utf-8")
    assert text == "---\ntitle: T\nstatus: blocked\nstatus_reason: new reason\n---\n\nBody.\n"


# ---------------------------------------------------------------------------
# AC3: blocked -> approved (stamp-unblocked)
# ---------------------------------------------------------------------------

def test_stamp_unblocked_happy_path(tmp_path, capsys):
    p = _write(
        tmp_path, "p.md",
        "---\ntitle: T\nstatus: blocked\nstatus_reason: waiting\n---\n\nBody.\n",
    )
    t0 = time.process_time()
    rc = main(["stamp-unblocked", "--plan", str(p)])
    elapsed_ms = (time.process_time() - t0) * 1000
    assert rc == 0
    assert elapsed_ms < 200, f"stamp-unblocked process_time {elapsed_ms:.1f}ms over the 200ms bar"
    out = capsys.readouterr().out
    assert 'status "blocked" → approved' in out
    text = p.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    assert split is not None
    assert read_fm_field_unquoted(split.fm_text, "status") == "approved"
    assert read_fm_field_unquoted(split.fm_text, "status_reason") is None
    assert "status_reason" not in text


@pytest.mark.parametrize(
    "status", ["draft", "reviewed", "approved", "executing", "landed", "implemented",
               "superseded", "abandoned", "deferred"]
)
def test_stamp_unblocked_refuses_non_blocked_source(tmp_path, capsys, status):
    original = f"---\ntitle: T\nstatus: {status}\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-unblocked", "--plan", str(p)])
    assert rc == 1
    err = capsys.readouterr().err
    assert f'status "{status}"' in err
    assert "expected: blocked" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_unblocked_rejects_reason_flag(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: blocked\nstatus_reason: r\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-unblocked", "--plan", str(p), "--reason", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not accept --reason" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_unblocked_rejects_by_flag(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: blocked\nstatus_reason: r\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-unblocked", "--plan", str(p), "--by", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not accept --by" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_unblocked_rejects_override_reason_flag(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: blocked\nstatus_reason: r\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-unblocked", "--plan", str(p), "--override-reason", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not accept --override-reason" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_unblocked_rejects_findings_flag(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: blocked\nstatus_reason: r\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-unblocked", "--plan", str(p), "--findings", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not accept --findings" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_unblocked_missing_plan_flag(capsys):
    rc = main(["stamp-unblocked"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "requires --plan" in err


# ---------------------------------------------------------------------------
# AC4: every other stamp verb refuses a blocked plan, byte-identical, and
# claim_plan(for_execution=True) refuses too, leaving no claim.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "argv_tail",
    [
        ["stamp-implemented"],
        ["stamp-superseded", "--by", "docs/plans/successor.md"],
        ["stamp-reviewed"],
        ["stamp-approved"],
        ["stamp-executing"],
    ],
)
def test_every_other_verb_refuses_a_blocked_plan(tmp_path, capsys, argv_tail):
    original = "---\ntitle: T\nstatus: blocked\nstatus_reason: r\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main([*argv_tail, "--plan", str(p)])
    assert rc != 0
    assert p.read_text(encoding="utf-8") == original


def test_claim_plan_for_execution_refuses_a_blocked_plan_and_leaves_no_claim(
    tmp_path, monkeypatch
):
    from coordinator_core.session import claims
    from coordinator_core.session.tests.test_claims import _make_repo, _set_me

    repo = _make_repo(tmp_path)
    _set_me(monkeypatch)
    plan = repo / "docs" / "plans" / "blocked-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "---\nstatus: blocked\nstatus_reason: r\n---\n# Blocked Plan\n", encoding="utf-8"
    )
    original = plan.read_text(encoding="utf-8")

    t0 = time.process_time()
    result = claims.claim_plan("blocked-plan", cwd=str(repo), for_execution=True)
    elapsed_ms = (time.process_time() - t0) * 1000

    assert result is False
    assert elapsed_ms < 200, f"claim_plan(for_execution) process_time {elapsed_ms:.1f}ms over the 200ms bar"
    assert plan.read_text(encoding="utf-8") == original
    claim_dir = repo / ".git" / "coordinator-sessions" / "plan-claims" / "blocked-plan"
    assert not claim_dir.is_dir()


# ---------------------------------------------------------------------------
# AC5: set-membership assertions
# ---------------------------------------------------------------------------

def test_blocked_is_in_neither_frozen_nor_flippable_status_sets():
    from coordinator_core import lifecycle_constants
    from coordinator_core.ops.plan_status_transition import (
        _FLIPPABLE_STATUSES,
        _FROZEN_STATUSES,
    )

    assert "blocked" not in _FROZEN_STATUSES
    assert "blocked" not in _FLIPPABLE_STATUSES
    assert "blocked" not in lifecycle_constants.PLAN_ARCHIVABLE_STATUS
    assert "blocked" not in lifecycle_constants.PLAN_ORPHAN_TERMINAL_STATUS


# ---------------------------------------------------------------------------
# Unknown-verb message lists both new verbs
# ---------------------------------------------------------------------------

def test_unknown_verb_message_lists_stamp_blocked_and_unblocked(capsys):
    rc = main(["not-a-real-verb"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "stamp-blocked" in err
    assert "stamp-unblocked" in err
