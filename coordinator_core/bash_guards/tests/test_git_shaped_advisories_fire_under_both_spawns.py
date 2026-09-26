from __future__ import annotations

import time

import pytest

from coordinator_core.bash_guards import dispatch

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.bash_guards.tests.test_git_shaped_advisories_fire_under_both import (
    _chain,
    _entry,
)


@pytest.mark.spawns_process
@pytest.mark.cadence
class TestValidateCommitBothDialects:
    NAME = "validate-commit"

    def test_matchers_declare_both_dialects(self, tmp_path):
        chain = _chain("git commit -m x", "sess-a", str(tmp_path), "Bash")
        entry = _entry(chain, self.NAME)
        assert "Bash" in entry.matchers
        assert "PowerShell" in entry.matchers

    def _init_repo_with_no_staged_changes(self, tmp_path):
        import subprocess

        from coordinator_core.win_portability import no_console_creationflags

        root = str(tmp_path)

        def _git(*args):
            subprocess.run(
                ["git", *args],
                cwd=root,
                check=True,
                capture_output=True,
                **no_console_creationflags(),
            )

        _git("init", "-q")
        _git("config", "user.email", "t@example.com")
        _git("config", "user.name", "Test")
        (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
        _git("add", "README.md")
        _git("commit", "-q", "-m", "init")
        return root

    def test_no_staged_changes_declines_identically_under_both_dialects(
        self, tmp_path
    ):
        root = self._init_repo_with_no_staged_changes(tmp_path)

        bash_entry = _entry(_chain("git commit -m x", "sess-a", root, "Bash"), self.NAME)
        ps_entry = _entry(_chain("git commit -m x", "sess-b", root, "PowerShell"), self.NAME)

        assert bash_entry.fn() is None
        assert ps_entry.fn() is None
