"""Caller attribution survives cold/warm routing without daemon leakage."""
import pytest

from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.mark.spawns_process
def test_real_git_hook_attributes_codex_preserves_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path
    from coordinator_core.win_portability import no_console_creationflags

    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ, COORDINATOR_AGENT_PROVIDER="codex", PYTHONPATH=str(root),
               GIT_AUTHOR_NAME="Human Operator", GIT_AUTHOR_EMAIL="human@example.com",
               GIT_COMMITTER_NAME="Human Operator", GIT_COMMITTER_EMAIL="human@example.com")
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True,
                              capture_output=True, text=True, **no_console_creationflags()).stdout

    git("init", "-q")
    git("config", "user.name", "Human Operator")
    git("config", "user.email", "human@example.com")
    # Exercise Git's actual prepare-commit-msg delivery with the shared hook body.
    hook = tmp_path / ".git/hooks/prepare-commit-msg"
    script = root / "coordinator/bin/coordinator-prepare-commit-msg.py"
    hook.write_text(f'#!/bin/sh\n"{Path(sys.executable).as_posix()}" "{script.as_posix()}" "$@"\n', encoding="utf-8", newline="\n")
    (tmp_path / "owned.txt").write_text("owned", encoding="utf-8")
    git("add", "--", "owned.txt")
    git("commit", "-m", "test: native Codex attribution")
    message = git("log", "-1", "--format=%B")
    assert "Co-Authored-By: Codex <noreply@openai.com>" in message
    assert "Co-Authored-By: Claude" not in message
    assert git("log", "-1", "--format=%an <%ae>").strip() == "Human Operator <human@example.com>"
