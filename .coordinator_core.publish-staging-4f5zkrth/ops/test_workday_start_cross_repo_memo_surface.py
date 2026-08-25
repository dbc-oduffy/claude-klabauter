"""
Tests for coordinator_core.ops.workday_start_cross_repo_memo_surface.

Port of: workday-start-cross-repo-memo-surface.sh (DoE b5a4192c, 2026-07-20).
Cases mirror the DoE bash test suite (workday-start-cross-repo-memo-surface.test.sh,
DoE 3a561713, 2026-07-22, 11 cases) plus additional edge/platform cases
exercised during the port.
"""

from __future__ import annotations

import inspect
import io
import os
import subprocess
from contextlib import redirect_stdout
from unittest.mock import MagicMock

import pytest

import coordinator_core.ops.workday_start_cross_repo_memo_surface as surface_mod
from coordinator_core.ops.workday_start_cross_repo_memo_surface import main

# Declared, not excused: `test_main_performs_no_archival_in_a_real_git_worktree`
# spawns a real git process because it deliberately regression-tests the
# git-root-resolved path (as opposed to the env-override fixture the rest of
# this file uses) against a REAL worktree, per its own docstring -- a prior
# defect only reproduced there. No mock stands in for real git-root
# resolution. This is the file's only spawn site, so it is left as its own
# self-isolated test rather than hoisted. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for this
# file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _write_memo(dirpath, fname, frontmatter):
    (dirpath / fname).write_text(f"---\n{frontmatter}\n---\n\nMemo body.\n")


def _run(env_overrides, monkeypatch):
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main([])
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# Test 1: Empty fixture dir -> silent, exit 0
# ---------------------------------------------------------------------------
def test_empty_dir_silent(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    rc, out = _run({"CROSS_REPO_INBOX_DIR": str(inbox)}, monkeypatch)
    assert rc == 0
    assert out == ""


# ---------------------------------------------------------------------------
# Test 2: open memo created today -> "(0 days old)", no stale flag
# ---------------------------------------------------------------------------
def test_open_memo_created_today(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2099-01-01-test.md",
        "title: Test Memo\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: open",
    )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-05-22"}, monkeypatch
    )
    assert rc == 0
    assert "Test Memo" in out
    assert "0 days old" in out
    assert "[STALE" not in out


# ---------------------------------------------------------------------------
# Test 3/4: open memo 10d / 20d old -> [STALE — awaiting your action]
# ---------------------------------------------------------------------------
def test_open_memo_10_days_old_stale(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2099-01-02-test.md",
        "title: Old Open Memo\nfrom: central-em\nto: holodeck-em\n"
        "created: 2026-05-22\nstatus: open",
    )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-06-01"}, monkeypatch
    )
    assert rc == 0
    assert "[STALE — awaiting your action]" in out
    assert "10 days old" in out


def test_open_memo_20_days_old_stale(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2099-01-03-test.md",
        "title: Long Stale Memo\nfrom: central-em\nto: dronesim-em\n"
        "created: 2026-05-22\nstatus: open",
    )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-06-11"}, monkeypatch
    )
    assert rc == 0
    assert "[STALE — awaiting your action]" in out
    assert "20 days old" in out


# ---------------------------------------------------------------------------
# Test 5: action_taken filtered out, open surfaced
# ---------------------------------------------------------------------------
def test_action_taken_filtered_open_surfaced(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2099-01-04-closed.md",
        "title: Already Done\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: action_taken\n"
        "action_taken_at: 2026-05-22\ndecision: accepted",
    )
    _write_memo(
        inbox,
        "2099-01-05-open.md",
        "title: Still Open\nfrom: central-em\nto: holodeck-em\n"
        "created: 2026-05-22\nstatus: open",
    )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-05-22"}, monkeypatch
    )
    assert rc == 0
    assert "Already Done" not in out
    assert "Still Open" in out
    # 1 qualifying memo line + 1 close-command footer line (printed once
    # whenever at least one memo qualifies — see the footer assertion below).
    assert len(out.rstrip("\n").splitlines()) == 2
    assert "archive-stamp-cli resolve-memo" in out


# ---------------------------------------------------------------------------
# Test 6: 10 open memos -> 8 lines + truncation line (9 total)
# ---------------------------------------------------------------------------
def test_truncation_at_8_entries(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for i in range(1, 11):
        _write_memo(
            inbox,
            f"2099-02-{i:02d}-memo.md",
            f"title: Memo {i}\nfrom: central-em\nto: project-rag-em\n"
            "created: 2026-05-22\nstatus: open",
        )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-05-22"}, monkeypatch
    )
    assert rc == 0
    lines = out.rstrip("\n").splitlines()
    # 8 entries + truncation line + 1 close-command footer line.
    assert len(lines) == 10
    assert "(2 more" in out
    assert "archive-stamp-cli resolve-memo" in out


# ---------------------------------------------------------------------------
# Test 7: pre-cutoff memo (created 2026-05-21) grandfathered -> silent
# ---------------------------------------------------------------------------
def test_precutoff_memo_grandfathered_silent(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2026-05-21-legacy.md",
        "title: Legacy Pre-Lifecycle Memo\nfrom: central-em\nto: holodeck-em\n"
        "created: 2026-05-21\nstatus: open",
    )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-05-22"}, monkeypatch
    )
    assert rc == 0
    assert out == ""


# ---------------------------------------------------------------------------
# Test 8: mixed kinds -- ask+consult surface above fyi; fyi carries marker
# ---------------------------------------------------------------------------
def test_kind_banding_ask_consult_before_fyi(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2099-03-01-fyi.md",
        "title: FYI Notification\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: open\nkind: fyi",
    )
    _write_memo(
        inbox,
        "2099-03-02-consult.md",
        "title: Consult Request\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: open\nkind: consult",
    )
    _write_memo(
        inbox,
        "2099-03-03-ask.md",
        "title: Action Request\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: open\nkind: ask",
    )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-05-22"}, monkeypatch
    )
    assert rc == 0
    lines = out.rstrip("\n").splitlines()
    idx_ask = next(i for i, l in enumerate(lines) if "Action Request" in l)
    idx_consult = next(i for i, l in enumerate(lines) if "Consult Request" in l)
    idx_fyi = next(i for i, l in enumerate(lines) if "FYI Notification" in l)
    assert idx_ask < idx_fyi
    assert idx_consult < idx_fyi
    assert "[fyi]" in lines[idx_fyi]
    assert "[fyi]" not in lines[idx_ask]
    assert "[fyi]" not in lines[idx_consult]


# ---------------------------------------------------------------------------
# Test 9: missing kind field defaults to ask (urgent band)
# ---------------------------------------------------------------------------
def test_missing_kind_defaults_to_ask(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2099-04-01-fyi.md",
        "title: FYI Only\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: open\nkind: fyi",
    )
    _write_memo(
        inbox,
        "2099-04-02-nokind.md",
        "title: No Kind Memo\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: open",
    )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-05-22"}, monkeypatch
    )
    assert rc == 0
    lines = out.rstrip("\n").splitlines()
    idx_nokind = next(i for i, l in enumerate(lines) if "No Kind Memo" in l)
    idx_fyi = next(i for i, l in enumerate(lines) if "FYI Only" in l)
    assert idx_nokind < idx_fyi
    assert "[fyi]" not in lines[idx_nokind]


# ---------------------------------------------------------------------------
# Test 10: pipe character in title sanitized to en-dash, kind still parses
# ---------------------------------------------------------------------------
def test_pipe_in_title_sanitized(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2099-05-01-pipe-title.md",
        "title: Memo With | Pipe In Title\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: open\nkind: fyi",
    )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-05-22"}, monkeypatch
    )
    assert rc == 0
    assert "Memo With" in out
    line = next(l for l in out.splitlines() if "Memo With" in l)
    assert "[fyi]" in line
    # Literal pipe must not survive raw -- it is replaced by an en dash.
    assert "| Pipe" not in line
    assert "–" in line


# ---------------------------------------------------------------------------
# Test 11: proposal kind bands as urgent (before fyi), no [fyi] marker
# ---------------------------------------------------------------------------
def test_proposal_kind_bands_urgent(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2099-06-01-fyi.md",
        "title: FYI Background Note\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: open\nkind: fyi",
    )
    _write_memo(
        inbox,
        "2099-06-02-proposal.md",
        "title: Proposal For Review\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: open\nkind: proposal",
    )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-05-22"}, monkeypatch
    )
    assert rc == 0
    lines = out.rstrip("\n").splitlines()
    idx_proposal = next(i for i, l in enumerate(lines) if "Proposal For Review" in l)
    idx_fyi = next(i for i, l in enumerate(lines) if "FYI Background Note" in l)
    assert idx_proposal < idx_fyi
    assert "[fyi]" not in lines[idx_proposal]


# ---------------------------------------------------------------------------
# Additional edge/platform cases (this port)
# ---------------------------------------------------------------------------
def test_in_progress_surfaced_with_claimed_tag_no_stale_flag(tmp_path, monkeypatch):
    """status: in_progress is surfaced (not hidden) with a [CLAIMED by ...]
    tag, and the stale flag is suppressed even when the memo is >7 days old
    -- claimed work is not "awaiting your action". Spec backlink:
    docs/plans/2026-06-21-memo-pickup-claim-lock-and-routed-plan-reconcile.md § C4
    """
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2099-07-01-claimed.md",
        "title: Claimed Memo\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: in_progress\npicked_up_by: holodeck-em",
    )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-06-11"}, monkeypatch
    )
    assert rc == 0
    assert "[CLAIMED by holodeck-em]" in out
    assert "[STALE" not in out


def test_footer_names_close_command_once_not_per_memo(tmp_path, monkeypatch):
    """The close-command footer line names archive-stamp-cli resolve-memo
    exactly ONCE regardless of how many memos qualify -- never one line per
    memo (that would be noise on a boot-hot-path surface). Absent entirely
    when there are zero qualifying memos (nothing to close)."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for i in range(1, 4):
        _write_memo(
            inbox,
            f"2099-08-{i:02d}-memo.md",
            f"title: Footer Memo {i}\nfrom: central-em\nto: project-rag-em\n"
            "created: 2026-05-22\nstatus: open",
        )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-05-22"}, monkeypatch
    )
    assert rc == 0
    assert out.count("archive-stamp-cli resolve-memo") == 1


def test_no_footer_when_no_qualifying_memos(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    rc, out = _run({"CROSS_REPO_INBOX_DIR": str(inbox)}, monkeypatch)
    assert rc == 0
    assert out == ""
    assert "archive-stamp-cli" not in out


def test_absent_inbox_dir_silent(tmp_path, monkeypatch):
    absent = tmp_path / "does-not-exist"
    rc, out = _run({"CROSS_REPO_INBOX_DIR": str(absent)}, monkeypatch)
    assert rc == 0
    assert out == ""


def test_unparseable_created_date_yields_zero_age(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2099-08-01-badtate.md",
        "title: Bad Date Memo\nfrom: central-em\nto: project-rag-em\n"
        "created: not-a-date\nstatus: open",
    )
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-06-11"}, monkeypatch
    )
    assert rc == 0
    assert "0 days old" in out
    assert "[STALE" not in out


def test_env_override_wins_over_git_root(tmp_path, monkeypatch):
    """CROSS_REPO_INBOX_DIR takes priority over git-root resolution even
    when cwd is inside an unrelated git repo (Windows-parity: no reliance
    on TMPDIR/POSIX-only assumptions in the override path)."""
    inbox = tmp_path / "explicit-inbox"
    inbox.mkdir()
    _write_memo(
        inbox,
        "2099-09-01-explicit.md",
        "title: Explicit Override\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: open",
    )
    monkeypatch.chdir(tmp_path)
    rc, out = _run(
        {"CROSS_REPO_INBOX_DIR": str(inbox), "MOCK_TODAY": "2026-05-22"}, monkeypatch
    )
    assert rc == 0
    assert "Explicit Override" in out


# ---------------------------------------------------------------------------
# C14 (docs/plans/2026-07-23-wsc-tail-slim-down.md): the duplicate
# actioned-memo archival sweep this op used to run before surfacing, plus its
# `except Exception: return` silent swallow, are deleted outright rather than
# patched -- session.boot_sweep is the sole memo-archival occasion, and its
# failures surface via the shared housekeeping-failures log (C17a/C17b), not
# via a second swallow here.
# ---------------------------------------------------------------------------
def test_dead_sweep_helper_and_silent_swallow_are_gone():
    """The module-level symbol and its bare `except Exception` swallow must
    not merely be unreferenced -- they must not exist at all, since a
    reintroduced-but-uncalled stub would still carry the same silent-swallow
    liability the next maintainer could accidentally wire back up."""
    assert not hasattr(surface_mod, "_run_actioned_memo_sweep")
    assert "archive_actioned_memos_internal" not in inspect.getsource(
        surface_mod.main
    )
    assert "except Exception" not in "\n".join(
        inspect.getsource(fn) for fn in vars(surface_mod).values() if inspect.isfunction(fn)
    )


def test_main_performs_no_archival_in_a_real_git_worktree(tmp_path, monkeypatch):
    """Prior to C14, `main()` unconditionally invoked the actioned-memo
    archival sweep (git mv + commit) whenever CROSS_REPO_INBOX_DIR was unset
    -- exactly the surfacing path exercised here. Run against a REAL git
    worktree (not the env-override fixture used by the other tests in this
    module) so a reintroduced archival call would be caught even if it only
    fires on the git-root-resolved path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    inbox = repo / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (repo / "README.md").write_text("placeholder\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    _write_memo(
        inbox,
        "2099-10-01-actioned.md",
        "title: Actioned Memo\nfrom: central-em\nto: project-rag-em\n"
        "created: 2026-05-22\nstatus: actioned",
    )

    from coordinator_core.ops.fleet import archive_actioned_memos as archive_mod

    mock = MagicMock(
        side_effect=AssertionError(
            "archival must never run from this read-only surface op (C14)"
        )
    )
    monkeypatch.setattr(archive_mod, "archive_actioned_memos_internal", mock)
    monkeypatch.delenv("CROSS_REPO_INBOX_DIR", raising=False)
    monkeypatch.chdir(repo)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main([])

    assert rc == 0
    mock.assert_not_called()

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().splitlines()) == 1
