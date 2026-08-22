"""
Tests for coordinator_core.ops.workday_complete_step2_5_dirty_tree.

Golden-fixture parity check (Review: code-reviewer F3, 2026-07-17): the
port was ORIGINALLY authored against DoE-claude's bash oracle (Port of:
workday-complete-step2_5-dirty-tree.sh, DoE b5a4192c, 2026-07-20), and
byte-parity was verified at port-time (see git history for the retired
574-line bash body). That trampoline file has since been overwritten IN
PLACE with a Python re-exec shim over this same port module, so a live
"run the bash oracle" comparison would silently compare the port against
itself — a tautology, not independent verification. The
`test_golden_fixture_parity`
scenarios below instead assert against frozen expected (rc, commit-count,
verdict-token) triples captured from the port's own behavior at the time
this test was converted (mirrors the golden-file pattern in
coordinator_core/install/test_gen_settings_hooks.py's
`test_golden_output_matches_oracle_fixture`) — a future regression in this
module's classification logic will change one of these values and fail
the test, whereas the old self-comparison could not have caught it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from coordinator_core.ops.workday_complete_step2_5_dirty_tree import main as port_main
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope

# Declared, not excused: this file spawns a real git process because the port
# under test classifies and auto-commits real dirty-tree state (renames,
# untracked/tracked gitignore transitions, review-trail auto-commit, peer
# claim/session disposition against real commits) that no mock stands in
# for. Each test mutates its own repo (commits, renames, rm --cached), so
# `_make_repo` is not hoisted to module scope -- per-test isolation. The
# spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "--", ".gitkeep"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _run_port(repo, args, capsys):
    cwd_before = os.getcwd()
    os.chdir(repo)
    try:
        rc = port_main(list(args))
    finally:
        os.chdir(cwd_before)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _rev_count(repo):
    res = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return int(res.stdout.strip())


# ---------------------------------------------------------------------------
# Port-only shape tests (always run).
# ---------------------------------------------------------------------------


def test_empty_repo_ok(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    rc, out, _err = _run_port(repo, [], capsys)
    assert rc == 0
    assert "[step2.5] OK" in out


def test_eol_phantom_skipped(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "eol-test.txt").write_text("hello\n")
    subprocess.run(["git", "add", "--", "eol-test.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=repo, check=True)
    (repo / "eol-test.txt").write_text("hello world\n")
    subprocess.run(["git", "add", "--", "eol-test.txt"], cwd=repo, check=True)
    rc, out, _err = _run_port(repo, [], capsys)
    assert rc == 0
    assert "EOL-phantom skipped: 1" in out


def test_auto_commit_review_trail(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "state" / "review-trail").mkdir(parents=True)
    (repo / "state" / "review-trail" / "r.json").write_text('{"status":"done"}\n')
    before = _rev_count(repo)
    rc, _out, _err = _run_port(repo, [], capsys)
    assert rc == 0
    after = _rev_count(repo)
    assert after == before + 1
    show = subprocess.run(
        ["git", "show", "--stat", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert "r.json" in show

    # second run: idempotent no-op
    rc2, _out2, _err2 = _run_port(repo, [], capsys)
    assert rc2 == 0
    assert _rev_count(repo) == after


def test_auto_gitignore_untracked_log(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "logs").mkdir()
    (repo / "logs" / "foo.log").write_text("some log\n")
    before = _rev_count(repo)
    rc, _out, _err = _run_port(repo, [], capsys)
    assert rc == 0
    gi = (repo / ".gitignore").read_text() if (repo / ".gitignore").exists() else ""
    assert "logs/" in gi.splitlines() or "*.log" in gi.splitlines()
    assert _rev_count(repo) == before + 1

    # second run: idempotent no-op
    rc2, _out2, _err2 = _run_port(repo, [], capsys)
    assert rc2 == 0
    assert _rev_count(repo) == before + 1


def test_auto_gitignore_tracked_log_rm_cached(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "logs").mkdir()
    (repo / "logs" / "foo.log").write_text("tracked log content\n")
    subprocess.run(["git", "add", "--", "logs/foo.log"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "accidentally track"], cwd=repo, check=True)
    with open(repo / "logs" / "foo.log", "a", encoding="utf-8") as fh:
        fh.write("more log content\n")
    before = _rev_count(repo)
    rc, _out, _err = _run_port(repo, [], capsys)
    assert rc == 0
    assert _rev_count(repo) == before + 1
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", "logs/foo.log"], cwd=repo, capture_output=True
    )
    assert tracked.returncode != 0
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert status.strip() == ""


def test_auto_gitignore_mixed_tracked_and_untracked_batch(tmp_path, capsys):
    """Multi-item angle on the batched `_act_gitignore` ls-files/rm --cached
    pair -- a single-tracked-file fixture (test_auto_gitignore_tracked_log_
    rm_cached above) would pass identically whether or not the batch call
    correctly attributes each path to its own tracked/untracked status (the
    same gap that shipped a wrong batched `_own_frozen_diff_shas` on
    2026-08-19). Two tracked .log files under distinct dirs plus one
    never-tracked .log file in the SAME gitignore-classified batch --
    exercises that `git ls-files` correctly reports membership per-path
    across a multi-path pathspec, and that `git rm --cached` untracks BOTH
    previously-tracked paths, not just the first."""
    repo = _make_repo(tmp_path)
    (repo / "logs").mkdir()
    (repo / "logs" / "a.log").write_text("tracked a\n")
    (repo / "logs" / "b.log").write_text("tracked b\n")
    subprocess.run(["git", "add", "--", "logs/a.log", "logs/b.log"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "accidentally track two"], cwd=repo, check=True)
    with open(repo / "logs" / "a.log", "a", encoding="utf-8") as fh:
        fh.write("more a\n")
    with open(repo / "logs" / "b.log", "a", encoding="utf-8") as fh:
        fh.write("more b\n")
    (repo / "logs" / "c.log").write_text("never tracked c\n")

    before = _rev_count(repo)
    rc, _out, _err = _run_port(repo, [], capsys)
    assert rc == 0
    assert _rev_count(repo) == before + 1

    for path in ("logs/a.log", "logs/b.log", "logs/c.log"):
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path], cwd=repo, capture_output=True
        )
        assert tracked.returncode != 0, f"{path} still tracked after batched rm --cached"

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert status.strip() == ""


def test_source_tree_needs_pm(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "bin").mkdir()
    (repo / "bin" / "foo.sh").write_text("#!/usr/bin/env bash\necho hello\n")
    subprocess.run(["git", "add", "--", "bin/foo.sh"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add foo.sh"], cwd=repo, check=True)
    (repo / "bin" / "foo.sh").write_text("#!/usr/bin/env bash\necho goodbye\n")
    rc, out, err = _run_port(repo, [], capsys)
    assert rc == 2
    assert "[step2.5] source-tree: bin/foo.sh" in err
    assert "[step2.5] NEEDS-PM" in out


def test_orphan_tmp_listed_not_deleted(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "foo.tmp.12345.67890").write_text("crash artifact\n")
    rc, out, err = _run_port(repo, [], capsys)
    assert rc == 0
    assert "[step2.5] orphan-tmp: foo.tmp.12345.67890" in err
    assert (repo / "foo.tmp.12345.67890").exists()
    assert "orphan-tmp listed (no action): 1" in out


def test_dry_run_no_commit(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    (repo / "state" / "review-trail").mkdir(parents=True)
    (repo / "state" / "review-trail" / "dryrun.json").write_text('{"x":1}\n')
    before = _rev_count(repo)
    rc, out, err = _run_port(repo, ["--dry-run"], capsys)
    assert rc == 0
    assert _rev_count(repo) == before
    assert "DRY-RUN: would commit" in err
    assert "auto-commit" in out


def test_rename_fold_bug1_no_abort(tmp_path, capsys):
    """Reported bug: a staged rename whose source is folded via AUTO-COMMIT
    must NOT abort the AUTO-COMMIT act block (vanished-pathspec regression)."""
    repo = _make_repo(tmp_path)
    (repo / "cross-repo" / "inbox").mkdir(parents=True)
    (repo / "cross-repo" / "archive").mkdir(parents=True)
    (repo / "cross-repo" / "inbox" / "memo.md").write_text("memo content\n")
    subprocess.run(["git", "add", "--", "cross-repo/inbox/memo.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add memo"], cwd=repo, check=True)
    subprocess.run(
        ["git", "mv", "cross-repo/inbox/memo.md", "cross-repo/archive/memo.md"], cwd=repo, check=True
    )
    rc, _out, _err = _run_port(repo, [], capsys)
    assert rc == 0
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert status.strip() == ""


def test_rename_fold_pure_source_dest_source_tree(tmp_path, capsys):
    """Pure rename-source fold (zero real add paths): source in an
    AUTO-COMMIT root, destination in SOURCE-TREE -> exit 2 NEEDS-PM, source
    still folds into the auto-commit."""
    repo = _make_repo(tmp_path)
    (repo / "cross-repo" / "inbox").mkdir(parents=True)
    (repo / "bin").mkdir()
    (repo / "cross-repo" / "inbox" / "x.md").write_text("x content\n")
    subprocess.run(["git", "add", "--", "cross-repo/inbox/x.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add x"], cwd=repo, check=True)
    subprocess.run(["git", "mv", "cross-repo/inbox/x.md", "bin/x.md"], cwd=repo, check=True)
    rc, out, _err = _run_port(repo, [], capsys)
    assert rc == 2
    assert "NEEDS-PM" in out
    name_status = subprocess.run(
        ["git", "log", "-1", "--name-status", "--pretty=format:"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "cross-repo/inbox/x.md" in name_status


def test_rename_fold_rd_status_degrades_to_deletion(tmp_path, capsys):
    """RD porcelain (rename dest deleted post-stage) must not abort; the
    destination must never be `git add`ed, so the rename degrades to a
    plain deletion-of-source commit."""
    repo = _make_repo(tmp_path)
    (repo / "cross-repo" / "inbox").mkdir(parents=True)
    (repo / "cross-repo" / "archive").mkdir(parents=True)
    (repo / "cross-repo" / "inbox" / "memo.md").write_text("memo content\n")
    subprocess.run(["git", "add", "--", "cross-repo/inbox/memo.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add memo"], cwd=repo, check=True)
    subprocess.run(
        ["git", "mv", "cross-repo/inbox/memo.md", "cross-repo/archive/memo.md"], cwd=repo, check=True
    )
    os.remove(repo / "cross-repo" / "archive" / "memo.md")
    rc, _out, _err = _run_port(repo, [], capsys)
    assert rc == 0
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert status.strip() == ""
    name_status = subprocess.run(
        ["git", "log", "-1", "--name-status", "--pretty=format:"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "D" in name_status and "cross-repo/inbox/memo.md" in name_status


def test_unknown_arg_errors(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    rc, _out, err = _run_port(repo, ["--bogus"], capsys)
    assert rc == 1
    assert "unknown argument" in err


def test_not_a_git_repo(tmp_path, capsys):
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    rc, _out, err = _run_port(non_repo, [], capsys)
    assert rc == 1
    assert "not inside a git repo" in err


# ---------------------------------------------------------------------------
# Golden-fixture parity tests — frozen (rc, commit-count, verdict-token)
# expectations captured from the port's own behavior at conversion time
# (Review: code-reviewer F3). Always run; no oracle-availability skip,
# since there is no longer an independent oracle to be unavailable.
# ---------------------------------------------------------------------------

# scenario -> (expected_rc, expected_rev_count, expected_verdict_token)
_GOLDEN = {
    "empty": (0, 1, "[step2.5] OK"),
    "eol_phantom": (0, 2, "[step2.5] OK"),
    "auto_commit": (0, 2, "[step2.5] OK"),
    "auto_gitignore": (0, 2, "[step2.5] OK"),
    "source_tree": (2, 2, "[step2.5] NEEDS-PM"),
    "orphan_tmp": (0, 1, "[step2.5] OK"),
}


# ---------------------------------------------------------------------------
# C6 — CLAIM-aware branch (docs/plans/2026-08-05-in-process-writers-declare-
# their-writes.md). Tests drive tier-1 (`COORDINATOR_SESSION_ID`) directly
# rather than the tier-4 sentinel file -- the cheapest deterministic override
# `resolve_session_id` itself documents.
# ---------------------------------------------------------------------------


def _clear_session_env(monkeypatch):
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)


def test_two_live_sessions_commits_own_reports_peer_never_commits(
    tmp_path, capsys, monkeypatch
):
    """AC8 / two-live-session concurrency (required by dispatch): dirty
    state/subagent-share/ content under BOTH a live peer's session AND the
    closing session's own -- the closing session commits only its own and
    reports (never commits) the peer's."""
    repo = _make_repo(tmp_path)
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "mine")

    session_core.init("mine", cwd=str(repo))
    session_core.init("livepeer", cwd=str(repo))
    monkeypatch.setattr(
        session_scope.liveness,
        "live_session_ids",
        lambda cwd=None: frozenset({"mine", "livepeer"}),
    )

    mine_dir = repo / "state" / "subagent-share" / "mine"
    peer_dir = repo / "state" / "subagent-share" / "livepeer"
    mine_dir.mkdir(parents=True)
    peer_dir.mkdir(parents=True)
    (mine_dir / "minefile.md").write_text("mine content\n")
    (peer_dir / "peerfile.md").write_text("peer content\n")
    session_scope.touch("mine", "state/subagent-share/mine/minefile.md", cwd=str(repo))
    session_scope.touch(
        "livepeer", "state/subagent-share/livepeer/peerfile.md", cwd=str(repo)
    )

    rc, out, err = _run_port(repo, [], capsys)

    assert rc == 2
    assert "[step2.5] NEEDS-PM" in out
    assert (
        "[step2.5] peer-claim: state/subagent-share/livepeer/peerfile.md "
        "owner=livepeer liveness=live claim_source=session" in err
    )

    name_status = subprocess.run(
        ["git", "log", "-1", "--name-status", "--pretty=format:"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "state/subagent-share/mine/minefile.md" in name_status
    assert "state/subagent-share/livepeer/peerfile.md" not in name_status

    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert "state/subagent-share/livepeer/peerfile.md" in status
    assert "state/subagent-share/mine/minefile.md" not in status


def test_peer_claim_perturbation_resolvable_then_broken_never_commits(
    tmp_path, capsys, monkeypatch
):
    """Perturbation proof (AC5/AC8): with the peer's claim resolvable, the
    peer's file is named and never committed. Breaking that resolution (an
    unreadable peer touched.txt -> `ScopeResult.indeterminate=True` ->
    `ownership["degraded"]=True`) must degrade the SAME path to AMBIGUOUS,
    not to a false peer-attribution and never to a commit -- proving the
    peer-protection is load-bearing, not a coincidence of one code path."""
    repo = _make_repo(tmp_path)
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "mine")

    session_core.init("mine", cwd=str(repo))
    session_core.init("livepeer", cwd=str(repo))
    monkeypatch.setattr(
        session_scope.liveness,
        "live_session_ids",
        lambda cwd=None: frozenset({"mine", "livepeer"}),
    )

    peer_dir = repo / "state" / "subagent-share" / "livepeer"
    peer_dir.mkdir(parents=True)
    (peer_dir / "peerfile.md").write_text("peer content\n")
    session_scope.touch(
        "livepeer", "state/subagent-share/livepeer/peerfile.md", cwd=str(repo)
    )

    # -- resolvable leg: peer's claim resolves -- named, never committed --
    rc, _out, err = _run_port(repo, [], capsys)
    assert rc == 2
    assert "[step2.5] peer-claim: state/subagent-share/livepeer/peerfile.md" in err
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "state/subagent-share/livepeer/peerfile.md" in status

    # -- broken leg: livepeer's touched.txt becomes unreadable --
    # The read seam moved with the 2026-08-21 rebuild: ownership now comes
    # from `claim_index`, whose reader reports unreadability as a
    # `(lines, ok)` pair and never goes through `pathlib.Path.read_text`.
    # Patched at the old seam this leg established no degradation at all and
    # asserted the wrong half of its own contrast.
    peer_touched = os.path.join(
        session_core.session_dir("livepeer", cwd=str(repo)), "touched.txt"
    )
    real_reader = claim_index._read_lines_discard_torn_tail

    def _unreadable(path):
        if os.path.normcase(str(path)) == os.path.normcase(peer_touched):
            return [], False
        return real_reader(path)

    monkeypatch.setattr(claim_index, "_read_lines_discard_torn_tail", _unreadable)

    rc2, out2, err2 = _run_port(repo, [], capsys)
    assert rc2 == 2
    assert "[step2.5] NEEDS-PM" in out2
    assert "[step2.5] ambiguous: state/subagent-share/livepeer/peerfile.md" in err2
    assert "peer-claim: state/subagent-share/livepeer/peerfile.md" not in err2

    status2 = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "state/subagent-share/livepeer/peerfile.md" in status2


def test_claimed_source_tree_path_still_classifies_source_tree_never_commits(
    tmp_path, capsys, monkeypatch
):
    """PM-caught defect (2026-08-05, fixed before landing): CLAIM must NOT
    override SOURCE-TREE. A claim proves WHO wrote a path, never that it is
    safe for an unattended ceremony to commit unreviewed -- SOURCE-TREE
    exists precisely to force human review of source changes, and a
    session's own claimed edit under one of its roots still needs that
    review. A `bin/` path provably claimed by the closing session must
    still classify SOURCE-TREE (branch 7): listed on stderr, needs_pm set,
    and NEVER committed."""
    repo = _make_repo(tmp_path)
    _clear_session_env(monkeypatch)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "mine")

    session_core.init("mine", cwd=str(repo))
    monkeypatch.setattr(
        session_scope.liveness, "live_session_ids", lambda cwd=None: frozenset({"mine"})
    )

    (repo / "bin").mkdir()
    (repo / "bin" / "foo.sh").write_text("#!/usr/bin/env bash\necho hello\n")
    subprocess.run(["git", "add", "--", "bin/foo.sh"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add foo.sh"], cwd=repo, check=True)
    (repo / "bin" / "foo.sh").write_text("#!/usr/bin/env bash\necho goodbye\n")
    session_scope.touch("mine", "bin/foo.sh", cwd=str(repo))

    before = _rev_count(repo)
    rc, out, err = _run_port(repo, [], capsys)

    assert rc == 2
    assert "[step2.5] source-tree: bin/foo.sh" in err
    assert "[step2.5] NEEDS-PM" in out
    assert "claim-commit" not in out
    assert _rev_count(repo) == before  # this run made NO commit
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert "bin/foo.sh" in status  # still dirty, uncommitted


def test_unresolvable_session_id_degrades_to_pre_c6_ambiguous(tmp_path, capsys, monkeypatch):
    """AC8 negative half: with no resolvable session id (env vars unset, no
    tier-4 sentinel), the CLAIM branch is skipped entirely and a
    state/subagent-share/ path reaches AMBIGUOUS exactly as it did before
    this chunk -- never a wider commit (`_resolve_claim_context` returns
    `None`)."""
    repo = _make_repo(tmp_path)
    _clear_session_env(monkeypatch)

    orphan_dir = repo / "state" / "subagent-share" / "someone"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "file.md").write_text("orphaned content\n")

    rc, out, err = _run_port(repo, [], capsys)
    assert rc == 2
    assert "[step2.5] ambiguous: state/subagent-share/someone/file.md" in err
    assert "[step2.5] NEEDS-PM" in out
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "state/subagent-share/someone/file.md" in status


@pytest.mark.parametrize("scenario", sorted(_GOLDEN))
def test_golden_fixture_parity(tmp_path, capsys, scenario):
    repo = _make_repo(tmp_path, "port")

    def _seed(repo):
        if scenario == "empty":
            return
        if scenario == "eol_phantom":
            (repo / "eol-test.txt").write_text("hello\n")
            subprocess.run(["git", "add", "--", "eol-test.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=repo, check=True)
            (repo / "eol-test.txt").write_text("hello world\n")
            subprocess.run(["git", "add", "--", "eol-test.txt"], cwd=repo, check=True)
        elif scenario == "auto_commit":
            (repo / "state" / "review-trail").mkdir(parents=True)
            (repo / "state" / "review-trail" / "r.json").write_text('{"status":"done"}\n')
        elif scenario == "auto_gitignore":
            (repo / "logs").mkdir()
            (repo / "logs" / "foo.log").write_text("some log\n")
        elif scenario == "source_tree":
            (repo / "bin").mkdir()
            (repo / "bin" / "foo.sh").write_text("echo hi\n")
            subprocess.run(["git", "add", "--", "bin/foo.sh"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=repo, check=True)
            (repo / "bin" / "foo.sh").write_text("echo bye\n")
        elif scenario == "orphan_tmp":
            (repo / "foo.tmp.111.222").write_text("x\n")

    _seed(repo)

    expected_rc, expected_rev, expected_token = _GOLDEN[scenario]

    port_rc, port_out, _port_err = _run_port(repo, [], capsys)
    assert port_rc == expected_rc, f"rc mismatch for {scenario}: expected={expected_rc} got={port_rc}"

    port_rev = _rev_count(repo)
    assert port_rev == expected_rev, (
        f"commit-count mismatch for {scenario}: expected={expected_rev} got={port_rev}"
    )

    assert expected_token in port_out, (
        f"verdict-line mismatch for {scenario}: expected {expected_token!r} in port_out={port_out!r}"
    )
    other_token = "[step2.5] NEEDS-PM" if expected_token == "[step2.5] OK" else "[step2.5] OK"
    assert other_token not in port_out, (
        f"unexpected verdict-line for {scenario}: {other_token!r} present in port_out={port_out!r}"
    )
