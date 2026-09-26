"""SC-DR-023 (CEPEF-B6): four whole-tree revert forms deny from the command
string ALONE, with no `git status` probe -- see
``docs/plans/2026-09-26-commit-emit-plane-engine-findings.md`` DD3 and
``_check_destructive_git_revert_full``'s own module docstring.

The four forms: pathspec-less `git stash`/`git stash push`, `git checkout
.`/`git checkout -- .`, `git restore .`, and bare `git reset --hard` (no
ref). Each denies unconditionally -- even against a perfectly clean tree --
because the classification never runs `_run_git` at all for these shapes;
that is the behavior under test here (asserted via a stubbed `_run_git`
that fails the test if called).

Everything this row leaves untouched gets its own negative-case coverage
here too: `git stash list/show/pop/apply/drop`, a pathspec-scoped stash,
and `git checkout <branch>` (not `git checkout .`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.bash_guards.dispatch_checks import check_destructive_git_revert
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence]


def _deny_reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


class _NeverCalled:
    """Stand-in for `_run_git` that fails the test if invoked -- proves the
    classification never spawns `git status`/`git rev-parse` for a
    whole-tree form."""

    def __call__(self, *args, **kwargs):
        raise AssertionError(
            "whole-tree form spawned _run_git (%r, %r) -- SC-DR-023 "
            "classifies from the command string alone" % (args, kwargs)
        )


@pytest.fixture()
def no_git_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch_checks, "_run_git", _NeverCalled())


class TestWholeTreeFormsDenyWithoutAProbe:

    @pytest.mark.parametrize(
        "cmd",
        [
            "git stash",
            "git stash push",
            'git stash push -m "wip"',
            "git stash save",
            "git checkout .",
            "git checkout -- .",
            "git restore .",
            "git reset --hard",
        ],
    )
    def test_denies_with_no_run_git_call(self, no_git_spawn: None, cmd: str) -> None:
        result = check_destructive_git_revert(cmd)
        assert result is not None, "%r must deny (SC-DR-023 whole-tree form)" % cmd
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "SC-DR-023" in hso["permissionDecisionReason"]

    @pytest.mark.spawns_process
    def test_denies_over_a_clean_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "clean"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
        (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())

        for tail in ("stash", "reset --hard", "checkout .", "restore ."):
            result = check_destructive_git_revert("git -C %s %s" % (repo, tail))
            assert result is not None, "%r on a clean tree must still deny" % tail


class TestScopedFormsUnaffected:

    def test_scoped_stash_is_allowed(self, no_git_spawn: None) -> None:
        assert check_destructive_git_revert("git stash push -- some/path.py") is None

    @pytest.mark.parametrize("sub", ["list", "show", "pop", "apply", "drop"])
    def test_non_sweep_stash_subcommands_are_allowed(self, no_git_spawn: None, sub: str) -> None:
        assert check_destructive_git_revert("git stash %s" % sub) is None

    def test_checkout_branch_is_not_whole_tree(self, no_git_spawn: None) -> None:
        assert check_destructive_git_revert("git checkout main") is None


class TestResetHardWithRefIsUnchanged:
    """`git reset --hard <ref>` still names a target -- it is the orphan
    check's own case (out of this row's scope) and keeps today's
    probe-then-deny-on-risk behavior, not the new command-string-alone
    deny."""

    def test_reset_hard_with_ref_still_probes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: List[Tuple[tuple, dict]] = []

        def _spy(args, cwd: Optional[str] = None, **kwargs):
            calls.append((tuple(args), kwargs))
            return (0, "")

        monkeypatch.setattr(dispatch_checks, "_run_git", _spy)
        check_destructive_git_revert("git reset --hard HEAD~1")
        assert calls, "git reset --hard <ref> must still consult git status (not command-string-alone)"


class TestPowerShellLeg:

    def test_start_process_bare_stash_denies(self, no_git_spawn: None) -> None:
        cmd = "Start-Process git -ArgumentList 'stash'"
        payload = {"tool_name": "PowerShell"}
        result = dispatch_checks._check_destructive_git_revert_full(cmd, hook_payload=payload)[0]
        assert result is not None, "PowerShell Start-Process bare stash must deny too"
        assert "SC-DR-023" in result["hookSpecificOutput"]["permissionDecisionReason"]
