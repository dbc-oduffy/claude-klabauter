"""
test_readonly_restore.py -- D4a's own coverage: the read-only-bit restore
folded into `coordinator_core.p4.shelve.shelve_outstanding`'s step-0 fstat.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md
plan-spine row C9 (D4a).

Exercises: a mode-only-drift fixture (content matching depot, nothing open,
read-only bit cleared) is restored and its count rides `ShelveOutcome.restored`;
the no-orphan/no-drift path spends exactly 4 p4 spawns (fstat, reconcile,
revert, shelve) -- never `p4 clean`, `reconcile -w`, or `sync -f`.

Review: overengineering-reviewer F8 (integrator-applied) -- this file used
to carry its own `TestEmptyPathSetStillZeroSpawns`, a verbatim duplicate of
`test_shelve.py::test_empty_path_set_is_a_success_with_zero_p4_spawns`
(same fixture shape, same `fail_run` spy, same assertion). Deleted here;
that original already fails if the D4a/D4b step-0 hoist breaks the
zero-spawn guarantee this file's docstring used to also claim.
"""

from __future__ import annotations

import json
import os
import stat

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


def _fstat_block(client_file: str, *, have_rev: bool, action: str = None) -> str:
    lines = [f"... clientFile {client_file}"]
    if have_rev:
        lines.append("... haveRev 1")
    if action:
        lines.append(f"... action {action}")
        lines.append("... change 101")
    return "\n".join(lines)


class TestModeOnlyDriftIsRestored:
    def test_synced_unopened_path_gets_read_only_bit_restored(
        self, monkeypatch, tmp_path, sdir, identity
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        drifted = repo_root / "a.uasset"
        drifted.write_text("unchanged\n", encoding="utf-8")
        drifted.chmod(0o644)  # writable -- the drift this row restores

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_ok()
            if "log" in args:
                return _git_ok("a.uasset\n")
            if args[-1] == "HEAD":
                return _git_ok("newheadsha\n")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)

        spawned = []

        def fake_p4_run(port, user, client, args, *, spec_input=None):
            spawned.append(list(args))
            if "fstat" in args:
                return runner.P4Result(
                    ok=True, stdout=_fstat_block("//bob-ws/a.uasset", have_rev=True)
                )
            return runner.P4Result(ok=True, stdout="")

        monkeypatch.setattr(shelve.runner, "run", fake_p4_run)

        outcome = shelve.shelve_outstanding(
            str(repo_root), "sid-1", str(sdir), identity, cl=101, base_sha="basesha"
        )

        assert outcome.ok
        assert outcome.restored == 1
        assert not (os.stat(drifted).st_mode & stat.S_IWRITE)
        # exactly 4 p4 spawns on the no-orphan happy path: fstat, reconcile,
        # revert, shelve -- never p4 clean/reconcile -w/sync -f.
        assert len(spawned) == 4
        verbs = [seg for call in spawned for seg in call if seg in ("fstat", "reconcile", "revert", "shelve")]
        assert verbs == ["fstat", "reconcile", "revert", "shelve"]
        assert not any("clean" in call for call in spawned)
        assert not any("-w" in call for call in spawned)
        assert not any("-f" in call and "sync" in call for call in spawned)


class TestOpenPathIsNeverRestored:
    def test_already_open_path_is_excluded_from_restore(
        self, monkeypatch, tmp_path, sdir, identity
    ):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        opened = repo_root / "b.uasset"
        opened.write_text("changed\n", encoding="utf-8")
        opened.chmod(0o644)

        def fake_run_git(args, **kw):
            if "cat-file" in args:
                return _git_ok()
            if "log" in args:
                return _git_ok("b.uasset\n")
            if args[-1] == "HEAD":
                return _git_ok("newheadsha\n")
            raise AssertionError(f"unexpected git args: {args}")

        monkeypatch.setattr(shelve, "run_git", fake_run_git)

        def fake_p4_run(port, user, client, args, *, spec_input=None):
            if "fstat" in args:
                return runner.P4Result(
                    ok=True,
                    stdout=_fstat_block("//bob-ws/b.uasset", have_rev=True, action="edit"),
                )
            return runner.P4Result(ok=True, stdout="")

        monkeypatch.setattr(shelve.runner, "run", fake_p4_run)

        outcome = shelve.shelve_outstanding(
            str(repo_root), "sid-1", str(sdir), identity, cl=101, base_sha="basesha"
        )

        assert outcome.ok
        assert outcome.restored == 0
        # already-writable (open for edit) target left untouched.
        assert os.stat(opened).st_mode & stat.S_IWRITE
