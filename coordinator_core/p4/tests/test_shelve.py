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
from coordinator_core.p4 import runner, session_change, shelve, workspace

# Review: overengineering-reviewer F1 (integrator-applied) -- the package
# used to re-export `workspace.session_change` under this same attribute
# name, shadowing this submodule on a plain import; that facade is gone,
# so a plain submodule import is unambiguous now (mirrors
# test_session_change.py).


def _spawn_with_verb(spawned, verb):
    """The one spawn whose p4 VERB is `verb`.

    The verb is the first token that is not a global option or its value —
    `runner.run` argv may lead with `-d <dir>`, and more globals could be
    added. Asserts exactly one match so a duplicated spawn fails loudly
    rather than silently resolving to the first."""
    matches = []
    for argv in spawned:
        i = 0
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in ("-d", "-p", "-u", "-c") else 1
        if i < len(argv) and argv[i] == verb:
            matches.append(argv)
    assert len(matches) == 1, f"expected exactly one {verb!r} spawn, got {len(matches)}: {spawned}"
    return matches[0]


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
        # Asserted by MEANING, never by index — at BOTH levels. Within an
        # argv, because the leading global `-d <repo_root>` shifts every
        # position (p4 ignores subprocess cwd for relative path resolution).
        # Across the spawn list, because C9 adds probe spawns around this
        # sequence: an earlier version of this assertion read `spawned[0]`
        # and broke the moment the orphaned-open probe landed in front of
        # reconcile — failing for a reason unrelated to the behaviour under
        # test. Find each verb's spawn by its verb.
        reconcile = _spawn_with_verb(spawned, "reconcile")
        assert reconcile[:2] == ["-d", repo_root]
        assert reconcile[reconcile.index("-c") + 1] == "101"
        assert {"-e", "-a", "-d"} <= set(reconcile)
        # No end-of-options token: real `p4 reconcile` refuses `--`.
        assert "--" not in reconcile
        revert = _spawn_with_verb(spawned, "revert")
        assert revert[revert.index("-c") + 1] == "101"
        assert "-a" in revert
        shelve_spawn = _spawn_with_verb(spawned, "shelve")
        assert shelve_spawn[shelve_spawn.index("-c") + 1] == "101"
        assert "-r" in shelve_spawn
        # Ordering is the invariant that matters, not absolute position.
        assert spawned.index(reconcile) < spawned.index(revert) < spawned.index(shelve_spawn)

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


class TestP4ignoreAbsentDropsDashA:
    """F3 (overengineering-reviewer, integrator-applied) -- `_p4ignore_absent`
    drives whether `reconcile` carries `-a`; the two arms are exercised
    directly here by monkeypatching `_p4ignore_absent` itself, which keeps
    this test independent of the registry/repo_key plumbing that function
    reads (that plumbing is `session_change`'s own coverage)."""

    def _run(self, monkeypatch, tmp_path, sdir, identity, *, p4ignore_absent):
        repo_root = str(tmp_path)
        spawned = []

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_ok()
            if "log" in args:
                return _git_ok("a.uasset\n")
            if args[-1] == "HEAD":
                return _git_ok("newheadsha\n")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)
        monkeypatch.setattr(shelve, "_p4ignore_absent", lambda repo_root: p4ignore_absent)

        def fake_p4_run(port, user, client, args, *, spec_input=None):
            spawned.append(list(args))
            return runner.P4Result(ok=True, stdout="")

        monkeypatch.setattr(shelve.runner, "run", fake_p4_run)

        outcome = shelve.shelve_outstanding(
            repo_root, "sid-1", str(sdir), identity, cl=101, base_sha="basesha"
        )
        assert outcome.ok
        return _spawn_with_verb(spawned, "reconcile")

    def test_absent_drops_dash_a(self, monkeypatch, tmp_path, sdir, identity):
        reconcile = self._run(monkeypatch, tmp_path, sdir, identity, p4ignore_absent=True)
        assert "-a" not in reconcile
        assert {"-e", "-d"} <= set(reconcile)

    def test_present_keeps_dash_a(self, monkeypatch, tmp_path, sdir, identity):
        reconcile = self._run(monkeypatch, tmp_path, sdir, identity, p4ignore_absent=False)
        assert {"-e", "-a", "-d"} <= set(reconcile)


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
        # The re-minted CL, not the original: every p4 spawn in this leg must
        # carry 202. Found by verb rather than by position — C9's probes sit
        # around this sequence.
        reconcile = _spawn_with_verb(spawned, "reconcile")
        assert reconcile[reconcile.index("-c") + 1] == "202"

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


class TestFstatRecordsAreKeyedByNameNeverByPosition:
    """Review: code-reviewer F1. Three paths, two returned fstat records --
    `zip()` would silently misalign the second record onto the third path.
    Keying by `clientFile` must either resolve correctly or refuse; it must
    never produce a misaligned `ok=True`."""

    def test_fewer_records_than_paths_resolves_correctly_never_misaligned(
        self, monkeypatch, tmp_path, sdir, identity
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        for name in ("a.uasset", "b.uasset", "c.uasset"):
            (repo_root / name).write_text("x\n", encoding="utf-8")

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_ok()
            if "log" in args:
                return _git_ok("a.uasset\nb.uasset\nc.uasset\n")
            if args[-1] == "HEAD":
                return _git_ok("newheadsha\n")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)

        spawned = []

        def fake_p4_run(port, user, client, args, *, spec_input=None):
            spawned.append(list(args))
            if "fstat" in args:
                # b.uasset is not yet in the depot -- p4 emits no block for
                # it. Only a.uasset and c.uasset get records. A positional
                # zip would pair c.uasset's path with b's absent slot's
                # neighbor, i.e. misalign entirely.
                return runner.P4Result(
                    ok=True,
                    stdout=(
                        "... clientFile //bob-ws/a.uasset\n"
                        "... haveRev 1\n"
                        "... change 101\n"
                        "\n"
                        "... clientFile //bob-ws/c.uasset\n"
                        "... haveRev 1\n"
                        "... action edit\n"
                    ),
                )
            return runner.P4Result(ok=True, stdout="")

        monkeypatch.setattr(shelve.runner, "run", fake_p4_run)

        outcome = shelve.shelve_outstanding(
            str(repo_root), "sid-1", str(sdir), identity, cl=101, base_sha="basesha"
        )

        # a.uasset's record carries `change 101` == effective_cl -- not an
        # orphan; it also carries `haveRev` with no `action`, so D4a
        # correctly restores it. c.uasset carries `action edit` -- not a
        # restore candidate. b.uasset has no record at all -- absent,
        # never misassigned another path's data (a positional zip would
        # have paired c.uasset's `action edit` record onto b.uasset here).
        assert outcome.ok
        assert outcome.adopted == 0
        assert outcome.restored == 1

    def test_record_naming_an_unrequested_path_is_a_typed_refusal(
        self, monkeypatch, tmp_path, sdir, identity
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "a.uasset").write_text("x\n", encoding="utf-8")

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_ok()
            if "log" in args:
                return _git_ok("a.uasset\n")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)

        def fake_p4_run(port, user, client, args, *, spec_input=None):
            if "fstat" in args:
                # p4 reports a path this call never asked about -- the
                # record/path correspondence cannot be trusted by name.
                return runner.P4Result(
                    ok=True,
                    stdout="... clientFile //bob-ws/unrelated.uasset\n... haveRev 1\n",
                )
            raise AssertionError("must not proceed past an unreconciled fstat record")

        monkeypatch.setattr(shelve.runner, "run", fake_p4_run)

        outcome = shelve.shelve_outstanding(
            str(repo_root), "sid-1", str(sdir), identity, cl=101, base_sha="basesha"
        )

        assert not outcome.ok
        assert outcome.error is not None
        assert outcome.error.kind == "refused"
