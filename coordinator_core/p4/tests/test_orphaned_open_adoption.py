"""
test_orphaned_open_adoption.py -- D4b's own coverage: adopting a file still
open in an EXITED session's changelist at the next push.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md
plan-spine row C9 (D4b).

Exercises: a file open in CL-A (standing in for an exited session) is
reopened into this session's CL before reconcile, its adopted count rides
`ShelveOutcome.adopted`; a file open by a DIFFERENT client/user (fstat shows
no `change`/`action` for THIS client) is never reopened; the no-orphan path
still spends exactly 4 p4 spawns; the negative that motivates the row -- with
the reopen removed, a successful shelve silently omits the file.
"""

from __future__ import annotations

import pytest

from coordinator_core.git.run import GitResult
from coordinator_core.p4 import runner, shelve, workspace


@pytest.fixture
def sdir(tmp_path):
    d = tmp_path / "session"
    d.mkdir()
    (d / "meta.json").write_text("{}", encoding="utf-8")
    return d


@pytest.fixture
def identity():
    return workspace.P4Identity(
        port="p4.example.com:1666", user="bob", client="bob-ws", client_root="/depot/root"
    )


def _git_ok(stdout: str = "") -> GitResult:
    return GitResult(returncode=0, timed_out=False, stdout=stdout, stderr="")


def _fstat_block(client_file: str, *, action: str = None, change: str = None) -> str:
    lines = [f"... clientFile {client_file}", "... haveRev 1"]
    if action:
        lines.append(f"... action {action}")
    if change:
        lines.append(f"... change {change}")
    return "\n".join(lines)


class TestOrphanedOpenIsAdoptedBeforeReconcile:
    def test_file_open_in_exited_sessions_cl_is_reopened_into_this_cl(
        self, monkeypatch, tmp_path, sdir, identity
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        orphan = repo_root / "orphan.uasset"
        orphan.write_text("open-in-cl-a\n", encoding="utf-8")

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_ok()
            if "log" in args:
                return _git_ok("orphan.uasset\n")
            if args[-1] == "HEAD":
                return _git_ok("newheadsha\n")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)

        spawned = []

        def fake_p4_run(port, user, client, args, *, spec_input=None):
            spawned.append(list(args))
            if "fstat" in args:
                # open in CL-A (an exited session's own CL), not effective_cl.
                return runner.P4Result(
                    ok=True,
                    stdout=_fstat_block("//bob-ws/orphan.uasset", action="edit", change="55"),
                )
            return runner.P4Result(ok=True, stdout="")

        monkeypatch.setattr(shelve.runner, "run", fake_p4_run)

        outcome = shelve.shelve_outstanding(
            str(repo_root), "sid-1", str(sdir), identity, cl=101, base_sha="basesha"
        )

        assert outcome.ok
        assert outcome.adopted == 1
        reopen_call = next(c for c in spawned if "reopen" in c)
        assert reopen_call[reopen_call.index("-c") + 1] == "101"
        assert "orphan.uasset" in reopen_call
        # adoption path: fstat, reopen, reconcile, revert, shelve -- 5 spawns.
        assert len(spawned) == 5
        # never restored to read-only: the file stays open (action present
        # at step 0), so it is excluded from D4a's restore set.
        assert outcome.restored == 0


class TestOtherClientOpenIsNeverReopened:
    def test_a_path_open_by_a_different_client_is_refused_not_reopened(
        self, monkeypatch, tmp_path, sdir, identity
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "peer.uasset").write_text("owned-elsewhere\n", encoding="utf-8")

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_ok()
            if "log" in args:
                return _git_ok("peer.uasset\n")
            if args[-1] == "HEAD":
                return _git_ok("newheadsha\n")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)

        spawned = []

        def fake_p4_run(port, user, client, args, *, spec_input=None):
            spawned.append(list(args))
            if "fstat" in args:
                # opened by ANOTHER client -- this client's own fstat record
                # carries no `change`/`action` at all (that info surfaces
                # under `otherOpen0`, which this module never reads).
                return runner.P4Result(
                    ok=True, stdout=_fstat_block("//other-ws/peer.uasset")
                )
            return runner.P4Result(ok=True, stdout="")

        monkeypatch.setattr(shelve.runner, "run", fake_p4_run)

        outcome = shelve.shelve_outstanding(
            str(repo_root), "sid-1", str(sdir), identity, cl=101, base_sha="basesha"
        )

        assert outcome.ok
        assert outcome.adopted == 0
        assert not any("reopen" in c for c in spawned)


class TestNoOrphanPathSpendsFourSpawns:
    def test_no_orphan_path_still_spends_exactly_four_p4_spawns(
        self, monkeypatch, tmp_path, sdir, identity
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "plain.uasset").write_text("edited\n", encoding="utf-8")

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_ok()
            if "log" in args:
                return _git_ok("plain.uasset\n")
            if args[-1] == "HEAD":
                return _git_ok("newheadsha\n")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)

        spawned = []

        def fake_p4_run(port, user, client, args, *, spec_input=None):
            spawned.append(list(args))
            if "fstat" in args:
                return runner.P4Result(
                    ok=True,
                    stdout=_fstat_block("//bob-ws/plain.uasset", action="edit", change="101"),
                )
            return runner.P4Result(ok=True, stdout="")

        monkeypatch.setattr(shelve.runner, "run", fake_p4_run)

        outcome = shelve.shelve_outstanding(
            str(repo_root), "sid-1", str(sdir), identity, cl=101, base_sha="basesha"
        )

        assert outcome.ok
        assert outcome.adopted == 0
        assert len(spawned) == 4

# Review: overengineering-reviewer F7 (integrator-applied) --
# `TestNegativeWithoutReopenShelveSilentlyOmitsTheFile` used to live here.
# It monkeypatched `_fstat_records` to a hand-stripped stub, so its
# assertion followed from the stub by construction and would have passed
# identically with the adoption branch deleted. Deleted; the real negative
# is `TestOtherClientOpenIsNeverReopened` above, which feeds real fstat
# parser output.
