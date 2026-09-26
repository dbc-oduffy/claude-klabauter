"""C4 (docs/plans/2026-08-26-the-destructive-core-learns-the-shell-it-guards.md,
Bucket B): the ADVISORY_REWRITE-band entries widened from
``matchers=("Bash",)`` to ``matchers=COMMAND_TOOL_NAMES`` in this chunk and
still live -- ``validate-commit``, ``reap-stale-git-lock``,
``git-no-optional-locks``, ``block-dev-repo-sentinel-removal-advisory``.
(A fifth such entry was deleted -- see
docs/plans/2026-08-21-the-advisory-band-gets-smaller-cheaper-and-honest.md
C5 -- and its own dialect-parity test class along with it.)

None of these four guards' own detection bodies read ``tool_name`` (three
match a foreign binary's argv/text spelled identically under both dialects;
the fourth, ``block-dev-repo-sentinel-removal-advisory``, was ALREADY fully
dialect-aware via ``dialect_from_tool_name`` before this chunk -- see that
entry's own registration comment in ``dispatch.py``). The only thing this
chunk's widening changes is whether the chain-entry ``matchers`` gate lets a
``PowerShell``-tagged payload reach the guard's ``fn`` at all -- so every
test below builds the LIVE chain via ``dispatch._build_guard_chain`` (same
seam ``test_tool_name_membership.py``'s own ``_dummy_chain`` uses) for both
``tool_name`` values on IDENTICAL git argv, and asserts (a) the named entry's
own declared ``matchers`` includes both dialects, and (b) invoking its `fn`
fires the same way under both.

Spec backlink: state/dispatch-briefs/2026-08-26-the-destructive-core-learns-
the-shell-it-guards/C4.md
"""
from __future__ import annotations

import time

import pytest

from coordinator_core.bash_guards import dispatch


def _chain(cmd, session_id, cwd, tool_name, extra_payload=None):
    payload = {
        "tool_name": tool_name,
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
    }
    if extra_payload:
        payload.update(extra_payload)
    return dispatch._build_guard_chain(
        cmd=cmd,
        session_id=session_id,
        cwd=cwd,
        payload=payload,
        policy_file=None,
        host_is_windows=None,
    )


def _entry(chain, name):
    for entry in chain:
        if entry.name == name:
            return entry
    raise AssertionError("no chain entry named %r" % name)


class TestGitNoOptionalLocksBothDialects:
    NAME = "git-no-optional-locks"

    def test_matchers_declare_both_dialects(self, tmp_path):
        chain = _chain("git status", "sess-a", str(tmp_path), "Bash")
        entry = _entry(chain, self.NAME)
        assert "Bash" in entry.matchers
        assert "PowerShell" in entry.matchers

    def test_rewrite_fires_identically_under_both_dialects(self, tmp_path):
        bash_entry = _entry(_chain("git status", "sess-a", str(tmp_path), "Bash"), self.NAME)
        ps_entry = _entry(_chain("git status", "sess-b", str(tmp_path), "PowerShell"), self.NAME)

        bash_out = bash_entry.fn()
        ps_out = ps_entry.fn()

        assert bash_out is not None
        assert ps_out is not None
        bash_cmd = bash_out["hookSpecificOutput"]["updatedInput"]["command"]
        ps_cmd = ps_out["hookSpecificOutput"]["updatedInput"]["command"]
        assert bash_cmd == "git --no-optional-locks status"
        assert ps_cmd == bash_cmd


class TestReapStaleGitLockBothDialects:
    NAME = "reap-stale-git-lock"

    def _aged_lock_repo(self, tmp_path, name):
        root = tmp_path / name
        git_dir = root / ".git"
        git_dir.mkdir(parents=True)
        lock = git_dir / "index.lock"
        lock.write_text("")
        old = time.time() - 999
        import os

        os.utime(lock, (old, old))
        return str(root), lock

    def test_matchers_declare_both_dialects(self, tmp_path):
        root, _ = self._aged_lock_repo(tmp_path, "repo0")
        chain = _chain("git commit -m x", "sess-a", root, "Bash")
        entry = _entry(chain, self.NAME)
        assert "Bash" in entry.matchers
        assert "PowerShell" in entry.matchers

    def test_reap_side_effect_fires_identically_under_both_dialects(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_NO_SLEEP", "1")
        monkeypatch.setenv("COORDINATOR_LOCK_REAP_AGE_SEC", "10")

        bash_root, bash_lock = self._aged_lock_repo(tmp_path, "repo-bash")
        ps_root, ps_lock = self._aged_lock_repo(tmp_path, "repo-ps")

        bash_entry = _entry(
            _chain("git commit -m x", "sess-a", bash_root, "Bash"), self.NAME
        )
        ps_entry = _entry(
            _chain("git commit -m x", "sess-b", ps_root, "PowerShell"), self.NAME
        )

        assert bash_lock.exists()
        assert ps_lock.exists()

        assert bash_entry.fn() is None
        assert ps_entry.fn() is None

        assert not bash_lock.exists(), "Bash-dialect call did not reap the aged lock"
        assert not ps_lock.exists(), "PowerShell-dialect call did not reap the aged lock"


class TestBlockDevRepoSentinelRemovalAdvisoryBothDialects:
    NAME = "block-dev-repo-sentinel-removal-advisory"
    SENTINEL = ".coordinator-dev-repo"

    def test_matchers_declare_both_dialects(self, tmp_path):
        chain = _chain(
            "git rm %s" % self.SENTINEL, "sess-a", str(tmp_path), "Bash"
        )
        entry = _entry(chain, self.NAME)
        assert "Bash" in entry.matchers
        assert "PowerShell" in entry.matchers

    def test_git_rm_sentinel_advises_identically_under_both_dialects(self, tmp_path):
        cmd = "git rm %s" % self.SENTINEL
        bash_entry = _entry(_chain(cmd, "sess-a", str(tmp_path), "Bash"), self.NAME)
        ps_entry = _entry(_chain(cmd, "sess-b", str(tmp_path), "PowerShell"), self.NAME)

        bash_out = bash_entry.fn()
        ps_out = ps_entry.fn()

        assert bash_out is not None, "expected an advisory envelope (Bash)"
        assert ps_out is not None, "expected an advisory envelope (PowerShell)"
        assert bash_out["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert ps_out["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "additionalContext" in bash_out["hookSpecificOutput"]
        assert "additionalContext" in ps_out["hookSpecificOutput"]
