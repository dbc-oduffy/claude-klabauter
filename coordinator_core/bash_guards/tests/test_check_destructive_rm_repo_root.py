"""Regression tests for repo-ROOT targets in
``coordinator_core.bash_guards.dispatch_checks.check_destructive_rm``.

Subject: the 2026-07-31 ``~/.claude`` destruction. A probe-cleanup
``rm -rf ~/.Claude`` — believed to name a throwaway case-variant directory —
deleted the live ``~/.claude`` install, because APFS is case-insensitive and
the two spellings are one inode. The guard that exists precisely to stop this
ran and ALLOWED it.

Two independent defects, both fixed here, both pinned below.

  1. RESOLUTION (the root cause). The dirty-work branch resolved the owning
     repository from ``os.path.dirname(tgt_abs)`` — the target's PARENT —
     and ``continue``d when that came back non-zero. A checkout's parent is
     normally not itself a repo (``~`` for ``~/.claude``; the containing
     projects directory for an ordinary clone), so the branch silently
     skipped every repository ROOT in the fleet while correctly denying
     their SUBdirectories. The protection was not missing; it never ran on
     the paths with the most to lose. ``TestRepoRootIsDenied`` is the proof.

  2. IDENTITY. Root-ness must be decided on ``st_dev``/``st_ino``
     (``_is_same_dir``), never a string compare. ``os.path.normcase`` is an
     identity function off Windows, so on the very filesystem that caused
     this incident it reports ``.Claude`` and ``.claude`` as different paths
     — the guard would resolve the toplevel correctly and then fail to
     recognize that the target IS it. ``TestCaseVariantAliasing`` pins the
     aliasing case directly and is skipped on case-sensitive filesystems,
     where the alias cannot be constructed.

The deny is on IDENTITY, not dirty-state: ``TestCleanRepoRootStillDenied``
pins that a pristine worktree does not soften it, because deleting a root
takes the ``.git`` store (unpushed commits, stashes, reflog) and every
gitignored file — ``settings.json``, session state, credentials — none of
which any ``git status --porcelain`` row would have reported.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.bash_guards.dispatch_checks import (
    _is_same_dir,
    check_destructive_rm,
)


def _git(*args: str, cwd: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )


def _make_repo(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "t@t.invalid", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    with open(os.path.join(path, "tracked.txt"), "w", encoding="utf-8") as fh:
        fh.write("committed\n")
    _git("add", "tracked.txt", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    return path


def _denied(cmd: str) -> bool:
    return check_destructive_rm(cmd) is not None


def _reason(cmd: str) -> str:
    out = check_destructive_rm(cmd)
    assert out is not None, f"expected a deny for: {cmd}"
    return out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.fixture()
def repo_outside_any_repo(tmp_path):
    """A repo whose PARENT is not itself a repository.

    This is the shape the guard used to skip, and the shape of every real
    checkout — do not "simplify" this fixture by nesting the repo inside
    another one, which is precisely the case that always worked.
    """
    parent = tmp_path / "home"
    parent.mkdir()
    return _make_repo(str(parent / "checkout"))


class TestRepoRootIsDenied:
    def test_root_denied_though_parent_is_not_a_repo(self, repo_outside_any_repo):
        assert _denied(f"rm -rf {repo_outside_any_repo}")

    def test_deny_names_root_ness_and_the_store(self, repo_outside_any_repo):
        reason = _reason(f"rm -rf {repo_outside_any_repo}")
        assert "ROOT of a git repository" in reason
        assert ".git store" in reason

    def test_subdirectory_of_same_repo_still_denied(self, repo_outside_any_repo):
        """The pre-existing dirty-work path must not regress."""
        sub = os.path.join(repo_outside_any_repo, "sub")
        os.makedirs(sub)
        with open(os.path.join(sub, "untracked.txt"), "w", encoding="utf-8") as fh:
            fh.write("uncommitted\n")
        assert _denied(f"rm -rf {sub}")

    def test_plain_non_repo_directory_is_not_denied(self, tmp_path):
        """The widening must not turn every rm -rf into a deny."""
        plain = tmp_path / "scratch"
        plain.mkdir()
        (plain / "junk.txt").write_text("junk\n")
        assert not _denied(f"rm -rf {plain}")


class TestCleanRepoRootStillDenied:
    def test_pristine_worktree_does_not_soften_the_deny(self, repo_outside_any_repo):
        """No porcelain rows exist here — the old branch would have allowed.

        A clean worktree says nothing about the store: unpushed commits,
        stashes, reflog, and every gitignored file are invisible to
        `git status --porcelain` and are destroyed all the same.
        """
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_outside_any_repo,
            capture_output=True,
            text=True,
        )
        assert out.stdout.strip() == "", "fixture must be clean for this to mean anything"
        assert _denied(f"rm -rf {repo_outside_any_repo}")


def _fs_is_case_insensitive(tmp_dir: str) -> bool:
    probe = os.path.join(tmp_dir, "CaseProbe")
    os.makedirs(probe, exist_ok=True)
    return os.path.isdir(os.path.join(tmp_dir, "caseprobe"))


class TestCaseVariantAliasing:
    """The 2026-07-31 incident shape, reproduced end to end."""

    def test_case_variant_spelling_of_a_repo_root_is_denied(self, tmp_path):
        if not _fs_is_case_insensitive(str(tmp_path)):
            pytest.skip("case-sensitive filesystem — the alias cannot exist here")
        parent = tmp_path / "home"
        parent.mkdir()
        _make_repo(str(parent / ".claude"))
        variant = parent / ".Claude"  # same inode, different spelling
        assert _denied(f"rm -rf {variant}")

    def test_deny_warns_about_case_insensitive_filesystems(self, tmp_path):
        if not _fs_is_case_insensitive(str(tmp_path)):
            pytest.skip("case-sensitive filesystem — the alias cannot exist here")
        parent = tmp_path / "home"
        parent.mkdir()
        _make_repo(str(parent / ".claude"))
        reason = _reason(f"rm -rf {parent / '.Claude'}")
        assert "case-insensitive" in reason


class TestTildeAndHomeVarTargets:
    """The literal 2026-07-31 incident command, pinned through the public
    entrypoint. Pre-fix, `os.path.exists("~/.claude")` is False (tilde is
    never expanded before the existence gate), so the token was skipped
    before ANY guard leg ran -- the widened root-resolution/identity fix
    landed in the same diff did not by itself close this hole.
    """

    def test_tilde_spelled_root_is_denied(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        _make_repo(str(home / "checkout"))
        monkeypatch.setenv("HOME", str(home))
        assert _denied("rm -rf ~/checkout")

    def test_home_var_spelled_root_is_denied(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        _make_repo(str(home / "checkout"))
        monkeypatch.setenv("HOME", str(home))
        assert _denied("rm -rf $HOME/checkout")

    def test_braced_home_var_spelled_root_is_denied(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        _make_repo(str(home / "checkout"))
        monkeypatch.setenv("HOME", str(home))
        assert _denied("rm -rf ${HOME}/checkout")

    def test_unresolvable_var_target_still_skipped(self, monkeypatch, tmp_path):
        """Finding 8 (glob/unknown-var carve-out) must not regress: a
        target the hook genuinely cannot resolve stays skipped, not denied
        and not (silently) allowed by crashing."""
        home = tmp_path / "home"
        home.mkdir()
        _make_repo(str(home / "checkout"))
        monkeypatch.setenv("HOME", str(home))
        # $FOO is not $HOME -- must remain unresolved and therefore skipped,
        # i.e. not denied (the check has nothing to probe).
        assert not _denied("rm -rf $FOO/checkout")

    def test_glob_target_still_skipped(self, tmp_path):
        """Finding 8: unchanged pre-existing carve-out, pinned directly."""
        repo = _make_repo(str(tmp_path / "checkout"))
        assert not _denied(f"rm -rf {os.path.dirname(repo)}/*")


class TestBareRepoRoot:
    """Finding 1: `git rev-parse --show-toplevel` fails inside a bare repo
    (no working tree), so neither the root-deny branch nor the dirty-work
    fallback (which has no dirty-state check for a bare repo at all) used
    to fire -- a bare repo target sailed through completely undenied.
    """

    def _make_bare_repo(self, path: str) -> str:
        os.makedirs(path, exist_ok=True)
        _git("init", "-q", "--bare", cwd=path)
        return path

    def test_bare_repo_root_is_denied(self, tmp_path):
        bare = self._make_bare_repo(str(tmp_path / "repo.git"))
        assert _denied(f"rm -rf {bare}")

    def test_bare_repo_deny_names_root_ness(self, tmp_path):
        bare = self._make_bare_repo(str(tmp_path / "repo.git"))
        reason = _reason(f"rm -rf {bare}")
        assert "ROOT of a git repository" in reason


class TestLinkedWorktreeRoot:
    """Finding 2: a LINKED `git worktree add` checkout's actual `.git` store
    lives in the MAIN repo (`.git/worktrees/<name>`) -- deleting the
    worktree directory loses only its own local uncommitted work, not the
    repo's full history. The deny message must reflect that, not the
    full-history-loss wording used for an ordinary repo root."""

    def _make_worktree(self, repo: str, worktree_path: str, branch: str) -> str:
        _git("worktree", "add", worktree_path, "-b", branch, cwd=repo)
        return worktree_path

    def test_linked_worktree_root_is_denied(self, tmp_path):
        repo = _make_repo(str(tmp_path / "repo"))
        worktree = self._make_worktree(repo, str(tmp_path / "wt"), "wt-branch")
        assert _denied(f"rm -rf {worktree}")

    def test_linked_worktree_deny_uses_worktree_specific_wording(self, tmp_path):
        repo = _make_repo(str(tmp_path / "repo"))
        worktree = self._make_worktree(repo, str(tmp_path / "wt"), "wt-branch")
        reason = _reason(f"rm -rf {worktree}")
        assert "this worktree's own uncommitted work" in reason
        assert "deletes the .git store along with the worktree" not in reason


class TestRmOverride:
    """Finding 3: the override escape hatch (`COORDINATOR_ALLOW_RM=1`) was
    untested on the new root-deny branch specifically."""

    def test_override_allows_repo_root_delete(self, monkeypatch, repo_outside_any_repo):
        monkeypatch.setenv("COORDINATOR_ALLOW_RM", "1")
        assert not _denied(f"rm -rf {repo_outside_any_repo}")


class TestIsSameDir:
    def test_distinct_directories_are_not_the_same(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert not _is_same_dir(str(a), str(b))

    def test_symlink_alias_resolves_to_the_same_dir(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        try:
            os.symlink(str(real), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable (Windows without developer mode)")
        assert _is_same_dir(str(link), str(real))

    def test_missing_path_returns_false_rather_than_raising(self, tmp_path):
        """Callers ARM a deny from this — an unprovable identity must not assert one."""
        assert not _is_same_dir(str(tmp_path / "absent"), str(tmp_path))

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX device-node path")
    def test_unstattable_operand_returns_false(self):
        assert not _is_same_dir("\x00invalid", "/")
