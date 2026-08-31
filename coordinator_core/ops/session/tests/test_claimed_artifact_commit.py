"""
coordinator_core.ops.session.tests.test_claimed_artifact_commit

C4 (2026-08-20 the-close-ceremony-commits-what-the-session-wrote plan) --
hardening tests for `safe_commit_offer.commit_session_offer_async`'s three
named hardenings: (a) degraded/indeterminate claim reads commit NOTHING,
(c) a claimed path also present in this call's own `ownership["peer"]`
bucket fails closed, plus AC6 (peer isolation, same directory) and AC9 (the
structured `outcome` a caller can render).

(b) post-stage verify (`git diff --cached --name-only` after staging,
compared against the expected claim set) is deferred per the brief's own
"do not reach into `ceremony.scoped_git_commit`'s internals" instruction --
staging is that op's own internal, not observable from this module without
reaching in. No test for it here; see `safe_commit_offer.CommitOutcome`'s
own docstring for the follow-up-chunk note.

Spec backlink: coordinator_core/ops/session/safe_commit_offer.py
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit import CommitRefused
from coordinator_core.session import claim_index, core, scope
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

# Real git spawn is load-bearing, same rationale as test_safe_commit_offer.py.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


class TestAC6PeerIsolationSameDirectory:
    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). Red at HEAD before this workstream
    # touched anything, and left unmarked by the change that suspended the op --
    # marked here because an unexplained red is a worklist entry nobody can
    # action. NOT the attribution kill: that was rebuilt and
    # `_MECHANISM_DISABLED` is gone. Re-greens when the op leaves the roster.
    @pytest.mark.designed_red
    def test_peer_claimed_artifact_beside_own_survives_uncommitted(self, tmp_path):
        """AC6: place a peer-claimed artifact beside this session's own in
        the SAME directory, run the op, assert the peer's file is still
        untracked and the commit pathspec never named it."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("peer", cwd=str(repo))
        (repo / "state").mkdir()
        (repo / "state" / "mine.txt").write_text("mine")
        (repo / "state" / "peer.txt").write_text("peer")
        scope.touch("mine", "state/mine.txt", cwd=str(repo))
        scope.touch("peer", "state/peer.txt", cwd=str(repo))

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))

        assert len(report["groups"]) == 1
        assert report["groups"][0]["paths"] == ["state/mine.txt"]
        assert report["outcome"]["status"] == "committed"
        assert report["outcome"]["committed_paths"] == ["state/mine.txt"]

        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        , **no_console_creationflags()).stdout
        assert "state/peer.txt" in status  # still untracked, never committed
        assert "state/mine.txt" not in status  # ours landed


class TestDegradedOrIndeterminateCommitsNothing:
    def test_indeterminate_read_skips_the_whole_call(self, tmp_path, monkeypatch):
        """(a): an unreadable peer touched.txt makes this call's own claim
        reads indeterminate call-wide -- even though `mine.py` itself is
        genuinely this session's own uncontested file, nothing is committed
        because attribution for the WHOLE call cannot be trusted."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "mine.py").write_text("m")
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "mine.py", cwd=str(repo))

        # The read seam moved with the 2026-08-21 rebuild: `compute_offer`
        # reads claims through `claim_index`, whose reader reports
        # unreadability as a `(lines, ok)` pair rather than by raising and
        # does not go through `pathlib.Path.read_text` at all. Patching the
        # old seam leaves the precondition unestablished and takes the test
        # red for a reason unrelated to the code under test.
        scope.touch("other", "shared.py", cwd=str(repo))
        # The seam moved AGAIN after the comment above was written:
        # `_read_lines_discard_torn_tail` was deleted outright by the
        # 2026-08-21 rebuild, so patching it raised AttributeError and took
        # this test red for exactly the reason that comment warns about. The
        # reader is now `claim_index._read_stream_claims(sink) ->
        # (claims, content_read_ok)`. Blind on the peer's session DIRECTORY,
        # not a `touched.txt` filename -- the sink is `touch-record.jsonl`
        # now, and filename matching would patch nothing silently.
        other_dir = os.path.normcase(core.session_dir("other", cwd=str(repo)))
        real_reader = claim_index._read_stream_claims

        def _unreadable(sink_path):
            if os.path.normcase(str(sink_path)).startswith(other_dir):
                return {}, False
            return real_reader(sink_path)

        monkeypatch.setattr(claim_index, "_read_stream_claims", _unreadable)

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))

        assert report["groups"] == []
        assert report["failed_groups"] == []
        assert report["outcome"]["status"] == "skipped_indeterminate"
        assert report["outcome"]["committed_paths"] == []

        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        , **no_console_creationflags()).stdout
        assert "mine.py" in status  # never committed, even though uncontested

    def test_degraded_ownership_read_skips_the_whole_call(self, tmp_path, monkeypatch):
        """Same underlying signal, read via `ownership["degraded"]` instead
        of `offer["indeterminate"]` directly -- both must gate identically
        per the brief: "Either true means commit NOTHING"."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        core.init("other", cwd=str(repo))
        (repo / "mine.py").write_text("m")
        (repo / "shared.py").write_text("s")
        scope.touch("mine", "mine.py", cwd=str(repo))

        # The read seam moved with the 2026-08-21 rebuild: `compute_offer`
        # reads claims through `claim_index`, whose reader reports
        # unreadability as a `(lines, ok)` pair rather than by raising and
        # does not go through `pathlib.Path.read_text` at all. Patching the
        # old seam leaves the precondition unestablished and takes the test
        # red for a reason unrelated to the code under test.
        scope.touch("other", "shared.py", cwd=str(repo))
        # The seam moved AGAIN after the comment above was written:
        # `_read_lines_discard_torn_tail` was deleted outright by the
        # 2026-08-21 rebuild, so patching it raised AttributeError and took
        # this test red for exactly the reason that comment warns about. The
        # reader is now `claim_index._read_stream_claims(sink) ->
        # (claims, content_read_ok)`. Blind on the peer's session DIRECTORY,
        # not a `touched.txt` filename -- the sink is `touch-record.jsonl`
        # now, and filename matching would patch nothing silently.
        other_dir = os.path.normcase(core.session_dir("other", cwd=str(repo)))
        real_reader = claim_index._read_stream_claims

        def _unreadable(sink_path):
            if os.path.normcase(str(sink_path)).startswith(other_dir):
                return {}, False
            return real_reader(sink_path)

        monkeypatch.setattr(claim_index, "_read_stream_claims", _unreadable)

        offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert offer["ownership"]["degraded"] is True  # precondition for this test

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))
        assert report["outcome"]["status"] in ("skipped_indeterminate", "skipped_degraded")
        assert report["groups"] == []


class TestEmptyClaimSetIsCleanNoop:
    def test_empty_safe_paths_reports_empty_outcome(self, tmp_path):
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))
        assert report["groups"] == []
        assert report["outcome"]["status"] == "empty"
        assert report["outcome"]["committed_paths"] == []
        assert report["outcome"]["conflicted_paths"] == []


class TestGitFailureReturnsNonBlocking:
    def test_unregistered_handler_is_non_blocking_and_reports_failed_group(
        self, tmp_path, monkeypatch
    ):
        """A git-level commit failure must never raise out of
        `commit_session_offer_async` -- it comes back as a structured
        `failed_groups` entry, and the call itself completes normally.

        The failure is injected at `commit_paths`, which is what
        `_commit_group` actually calls (C4 repoint, docs/plans/2026-08-29-
        the-push-subsystem-leaves-and-then-the-pipeline-can-go.md, off the
        killed `run_commit_pipeline`). It previously patched
        `coordinator_core.ipc.get_op_handler` to return None, simulating an
        unregistered `ceremony.scoped_git_commit`; that op was DELETED
        2026-08-23 and `_commit_group` was rewired on 2026-08-26 to call the
        pipeline DIRECTLY, "never re-resolved by op name" (its own docstring).
        So the patch stopped intercepting anything, the commit succeeded, and
        `failed_groups` came back empty -- the test failed while the behaviour
        it guards was fine. Patch the seam the code actually uses."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "a.py").write_text("a")
        scope.touch("mine", "a.py", cwd=str(repo))

        # A git-level commit failure is an EXCEPTION from `commit_paths`
        # (`CommitRefused`), not a return-value shape -- `_commit_group`
        # catches it and maps it onto `GroupResult`.
        def _failed_commit(*args, **kwargs):
            raise CommitRefused("simulated git-level commit failure")

        monkeypatch.setattr(safe_commit_offer, "commit_paths", _failed_commit)

        report = safe_commit_offer.commit_session_offer("mine", cwd=str(repo))

        assert len(report["failed_groups"]) == 1
        assert report["failed_groups"][0]["commit_failed"] is True
        assert report["outcome"]["status"] == "empty"  # nothing landed


class TestStagedSetMismatchDeferred:
    def test_no_post_stage_verify_implemented_this_chunk(self):
        """(b) post-stage verify is deferred: `ceremony.scoped_git_commit`
        owns staging internally, and this chunk's brief forbids reaching
        into that op's internals. Documented here (not implemented) per the
        brief's own instruction -- see `CommitOutcome`'s docstring for the
        named follow-up chunk against `scoped_git_commit` itself."""
        assert "follow-up chunk" in (safe_commit_offer.CommitOutcome.__doc__ or "")


class TestDirtyPathAlreadyDirtyFromAnotherWriterFailsClosed:
    # designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
    # (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
    # max 150021ms against a 2000ms bar). Red at HEAD before this workstream
    # touched anything, and left unmarked by the change that suspended the op --
    # marked here because an unexplained red is a worklist entry nobody can
    # action. NOT the attribution kill: that was rebuilt and
    # `_MECHANISM_DISABLED` is gone. Re-greens when the op leaves the roster.
    @pytest.mark.designed_red
    def test_claimed_path_also_seen_as_peer_claim_is_withheld(self, tmp_path):
        """(c): a defensive check -- if a path this session claims as
        "mine" is ALSO present in this same call's own
        `ownership["peer"]` bucket, it must be withheld from every commit
        group rather than committed. `mine`/`peer` are mutually exclusive
        by construction in `_compute_ownership` today (see that function's
        own docstring), so this test drives the check directly against
        `commit_session_offer_async`'s own filtering logic by monkeypatching
        `compute_offer` to hand back a conflicting shape -- proving the
        withhold fires on the ownership signal itself, not merely on
        `compute_scope`'s current invariant holding true forever."""
        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "contested.py").write_text("c")
        (repo / "clean.py").write_text("k")
        scope.touch("mine", "contested.py", cwd=str(repo))
        scope.touch("mine", "clean.py", cwd=str(repo))

        real_offer = safe_commit_offer.compute_offer("mine", cwd=str(repo))
        assert set(real_offer["safe_paths"]) == {"contested.py", "clean.py"}

        rigged_offer = dict(real_offer)
        rigged_offer["ownership"] = dict(real_offer["ownership"])
        rigged_offer["ownership"]["peer"] = [
            {
                "path": "contested.py",
                "owner": "peer",
                "liveness": "live",
                "claim_source": "session",
            }
        ]

        def _fake_compute_offer(session_id, cwd=None, *, extra_candidates=None):
            return rigged_offer

        import asyncio

        orig = safe_commit_offer.compute_offer
        safe_commit_offer.compute_offer = _fake_compute_offer
        try:
            report = asyncio.run(
                safe_commit_offer.commit_session_offer_async("mine", cwd=str(repo))
            )
        finally:
            safe_commit_offer.compute_offer = orig

        assert report["outcome"]["status"] == "dirty_conflict_skipped"
        assert report["outcome"]["conflicted_paths"] == ["contested.py"]
        committed = {p for g in report["groups"] for p in g["paths"] if g["committed"]}
        assert "contested.py" not in committed
        assert "clean.py" in committed  # the uncontested path still lands

        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        , **no_console_creationflags()).stdout
        assert "contested.py" in status  # withheld, never committed


class TestUnresolvedGitRootFailsClosed:
    """committer-P0 (2026-08-31): `worktree_root` was
    `core.git_root(cwd) or cwd or "."`.

    `_commit_group` classifies a declared path as DELETED whenever
    `(Path(worktree_root) / p).exists()` is False, so a root that is not the
    repo root makes EVERY claimed path probe False -- and this close path
    commits a mass deletion of the session's own work under the session's own
    message. `commit_paths`' phantom-deletion refusal cannot rescue it: it is
    handed the same bad root, resolves the same absent paths, and never
    fires.

    Root cause and both signatures:
    `state/audits/2026-08-31-committer-p0-root-cause-cwd-probe-becomes-
    deletion.md`.
    """

    @staticmethod
    def _core_without_git_root(value):
        """A stand-in for THIS module's `core` binding only.

        `core.git_root` is read by half the session package; patching the
        function on the shared module would degrade `compute_offer`'s own
        claim reads and the call would short-circuit as
        `skipped_indeterminate` -- green for the wrong reason. Swapping the
        module reference `safe_commit_offer` itself holds keeps the blast
        radius at the one call site under test.
        """

        class _Shim:
            def __getattr__(self, name):
                return getattr(core, name)

            def git_root(self, *args, **kwargs):
                return value

        return _Shim()

    @pytest.mark.parametrize("unresolved", [None, ""])
    def test_unresolved_root_commits_nothing_and_leaves_head_unmoved(
        self, tmp_path, monkeypatch, unresolved
    ):
        import asyncio

        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "mine.py").write_text("m")
        scope.touch("mine", "mine.py", cwd=str(repo))

        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout.strip()

        monkeypatch.setattr(
            safe_commit_offer, "core", self._core_without_git_root(unresolved)
        )
        report = asyncio.run(
            safe_commit_offer.commit_session_offer_async("mine", cwd=str(repo))
        )

        assert report["outcome"]["status"] == "skipped_unresolved_root"
        assert report["outcome"]["committed_paths"] == []
        assert report["reconciliation"]["reconciled"] is False
        assert report["groups"] == []
        assert report["failed_groups"] == []

        # The return value is not the evidence that matters -- a commit that
        # deleted the file would still report an empty `committed_paths` on
        # some other status. Ask git.
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout.strip()
        assert head_after == head_before

        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout
        assert "mine.py" in status  # never committed, and never deleted
        assert (repo / "mine.py").exists()

    def test_a_resolvable_root_still_commits(self, tmp_path):
        """The early return must fire on an unresolved root and nothing
        else -- a guard that also fires on the healthy path silently stops
        every close ceremony from committing."""
        import asyncio

        repo = _make_repo(tmp_path)
        core.init("mine", cwd=str(repo))
        (repo / "mine.py").write_text("m")
        scope.touch("mine", "mine.py", cwd=str(repo))

        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout.strip()

        report = asyncio.run(
            safe_commit_offer.commit_session_offer_async("mine", cwd=str(repo))
        )

        assert report["outcome"]["status"] == "committed"
        assert report["outcome"]["committed_paths"] == ["mine.py"]

        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
            **no_console_creationflags(),
        ).stdout.strip()
        assert head_after != head_before
