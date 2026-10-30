"""`cross-repo-memo draft --body "<text>"` (argparse's abbreviation of --body-file) names the path-or-stdin form."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN = Path(__file__).resolve().parents[1]
_ENGINE_ROOT = Path(__file__).resolve().parents[3]


def test_inline_body_text_refusal_names_file_path_or_stdin(tmp_path):
    sender = tmp_path / "sender_repo"
    sender.mkdir()
    subprocess.run(["git", "init", "-q", str(sender)], check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    home = tmp_path / "home"
    (home / "machine-local").mkdir(parents=True)
    (home / "machine-local" / "registry.toml").write_text(
        f'"repos.sender_repo" = "{sender.as_posix()}"\n"coordinator.feature.cross_repo_memos" = "on"\n',
        encoding="utf-8",
    )
    env = {**os.environ, "CLAUDE_HOME": str(home), "COORDINATOR_SETTINGS_HOME": str(home),
           "COORDINATOR_ENGINE_ROOT": str(_ENGINE_ROOT), "COORDINATOR_WARM": "0"}
    argv = [sys.executable, str(_BIN / "cross-repo-memo.py"), "draft", "inline-body",
            "--to", "sender_repo-em", "--title", "T", "--kind", "ask",
            "--body", "Fixed: the gate now <passes> | see sha 3493dae"]
    proc = subprocess.run(argv, cwd=sender, env=env, capture_output=True, text=True, timeout=120,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 2
    assert "takes a file path, or - for stdin" in proc.stderr
    assert "Errno" not in proc.stderr
