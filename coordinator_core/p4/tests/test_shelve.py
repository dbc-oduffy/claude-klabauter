"""
test_shelve.py — pytest coverage for coordinator_core.p4.shelve.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § C3, § D4.

Exercises: the reconcile/revert/shelve sequence against a session's own
commits, the healthy-base happy path, the unreachable-base loud re-mint
(never reading it as an empty path set), a typed refusal returned rather
than raised, and the zero-spawn empty-path-set success.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.git.run import GitResult
from coordinator_core.p4 import runner, shelve, workspace
import coordinator_core.p4.session_change as session_change

# NOTE: `coordinator_core.p4.__init__` re-exports `workspace.session_change`
# (the D9 reader function) under the package attribute name `session_change`,
# shadowing this submodule on a plain `from coordinator_core.p4 import
# session_change`. The dotted-module import above binds the submodule
# directly and is unaffected by that re-export (mirrors test_session_change.py).


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


def _git_fail() -> GitResult:
    return GitResult(returncode=1, timed_out=False, stdout="", stderr="fatal: bad object")


class TestHealthyBaseShelves:
    def test_reconcile_revert_shelve_sequence(self, monkeypatch, tmp_path, sdir, identity):
        repo_root = str(tmp_path)
        spawned = []

        def fake_run_git(args, **kw):
            if args[:2] == ["-C", repo_root] and "cat-file" in args:
                return _git_ok()
            if "log" in args:
                return _git_ok("a/b.uasset\nc/d.uasset\n")
            if args[-1] == "HEAD":
                return _git_ok("newheadsha\n")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)

        def fake_p4_run(port, user, client, args, *, spec_input=None):
            spawned.append(list(args))
            return runner.P4Result(ok=True, stdout="")

        monkeypatch.setattr(shelve.runner, "run", fake_p4_run)

        outcome = shelve.shelve_outstanding(
            repo_root, "sid-1", str(sdir), identity, cl=101, base_sha="basesha"
        )

        assert outcome.ok
        assert outcome.paths == ["a/b.uasset", "c/d.uasset"]
        assert outcome.shelved_sha == "newheadsha"
        assert not outcome.remint
        # Asserted by MEANING, not by index: the argv carries a leading
        # global `-d <repo_root>` (p4 ignores subprocess cwd for relative
        # path resolution), so anything keyed on position shifts whenever
        # that prefix changes and fails for a reason unrelated to the
        # behaviour under test.
        reconcile = spawned[0]
        assert reconcile[:2] == ["-d", repo_root]
        assert reconcile[2] == "reconcile"
        assert reconcile[reconcile.index("-c") + 1] == "101"
        assert {"-e", "-a", "-d"} <= set(reconcile)
        # No end-of-options token: real `p4 reconcile` refuses `--`.
        assert "--" not in reconcile
        assert spawned[1] == ["revert", "-a", "-c", "101"]
        assert spawned[2] == ["shelve", "-r", "-c", "101"]

        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["p4_shelved_sha"] == "newheadsha"
        assert meta["p4_shelved_at"]

    def test_empty_path_set_is_a_success_with_zero_p4_spawns(
        self, monkeypatch, tmp_path, sdir, identity
    ):
        repo_root = str(tmp_path)

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_ok()
            if "log" in args:
                return _git_ok("")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)

        def fail_run(*a, **kw):
            raise AssertionError("p4 runner must not be called on an empty path set")

        monkeypatch.setattr(shelve.runner, "run", fail_run)

        outcome = shelve.shelve_outstanding(
            repo_root, "sid-1", str(sdir), identity, cl=101, base_sha="basesha"
        )

        assert outcome.ok
        assert outcome.paths == []
        assert outcome.shelved_sha is None


class TestUnreachableBaseReMints:
    def test_unreachable_base_falls_back_to_merge_base_and_remints(
        self, monkeypatch, tmp_path, sdir, identity
    ):
        repo_root = str(tmp_path)

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_fail()  # base unreachable
            if "merge-base" in args:
                return _git_ok("mergebasesha\n")
            if "log" in args:
                assert "mergebasesha" in args[3]
                return _git_ok("a/b.uasset\n")
            if args[-1] == "HEAD":
                return _git_ok("newheadsha\n")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)
        monkeypatch.setattr(
            session_change, "remint_session_change", lambda repo_root, sid: 202
        )

        spawned = []

        def fake_p4_run(port, user, client, args, *, spec_input=None):
            spawned.append(list(args))
            return runner.P4Result(ok=True, stdout="")

        monkeypatch.setattr(shelve.runner, "run", fake_p4_run)

        outcome = shelve.shelve_outstanding(
            repo_root, "sid-1", str(sdir), identity, cl=101, base_sha="gonesha"
        )

        assert outcome.ok
        assert outcome.remint is True
        assert outcome.cl == 202
        # never reads the unreachable base as an empty path set
        assert outcome.paths == ["a/b.uasset"]
        assert spawned[0][spawned[0].index("-c") + 1] == "202"

    def test_unreachable_base_with_no_upstream_merge_base_is_a_typed_refusal(
        self, monkeypatch, tmp_path, sdir, identity
    ):
        repo_root = str(tmp_path)

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_fail()
            if "merge-base" in args:
                return _git_fail()
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)

        def fail_run(*a, **kw):
            raise AssertionError("p4 runner must not be called when no base can be established")

        monkeypatch.setattr(shelve.runner, "run", fail_run)

        outcome = shelve.shelve_outstanding(
            repo_root, "sid-1", str(sdir), identity, cl=101, base_sha="gonesha"
        )

        assert not outcome.ok
        assert outcome.error is not None
        assert outcome.error.kind == "refused"


class TestTypedRefusalNeverRaises:
    def test_reconcile_refusal_returns_typed_outcome(self, monkeypatch, tmp_path, sdir, identity):
        repo_root = str(tmp_path)

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_ok()
            if "log" in args:
                return _git_ok("a/b.uasset\n")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)
        monkeypatch.setattr(
            shelve.runner,
            "run",
            lambda *a, **kw: runner.P4Result(
                ok=False, stdout="", error=runner.P4Error(kind="lock_held", holder="peer@ws")
            ),
        )

        outcome = shelve.shelve_outstanding(
            repo_root, "sid-1", str(sdir), identity, cl=101, base_sha="basesha"
        )

        assert not outcome.ok
        assert outcome.error is not None
        assert outcome.error.kind == "lock_held"
        meta = json.loads((sdir / "meta.json").read_text())
        assert "p4_shelved_sha" not in meta
