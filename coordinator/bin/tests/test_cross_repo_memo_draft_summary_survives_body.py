"""`cross-repo-memo draft --body-file` keeps the explicit --summary, and a body-derived summary fits send's cap."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.frontmatter import schema_validate

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN = Path(__file__).resolve().parents[1]
_ENGINE_ROOT = Path(__file__).resolve().parents[3]


def _draft(tmp_path: Path, body: str, summary: str | None) -> str:
    sender = tmp_path / "sender_repo"
    sender.mkdir()
    receiver = tmp_path / "receiver_repo"
    receiver.mkdir()
    subprocess.run(["git", "init", "-q", str(sender)], check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    home = tmp_path / "home"
    (home / "machine-local").mkdir(parents=True)
    (home / "machine-local" / "registry.toml").write_text(
        f'"repos.sender_repo" = "{sender.as_posix()}"\n"repos.receiver_repo" = "{receiver.as_posix()}"\n'
        '"coordinator.feature.cross_repo_memos" = "on"\n',
        encoding="utf-8",
    )
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")
    env = {
        **os.environ,
        "CLAUDE_HOME": str(home),
        "COORDINATOR_SETTINGS_HOME": str(home),
        "COORDINATOR_ENGINE_ROOT": str(_ENGINE_ROOT),
        "COORDINATOR_WARM": "0",
    }
    argv = [sys.executable, str(_BIN / "cross-repo-memo.py"), "draft", "summary-survives",
            "--to", "receiver_repo-em", "--title", "T", "--kind", "ask",
            "--body-file", str(body_file)]
    if summary is not None:
        argv += ["--summary", summary]
    proc = subprocess.run(argv, cwd=sender, env=env, capture_output=True, text=True, timeout=120,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stderr
    return Path(proc.stdout.strip().splitlines()[-1]).read_text(encoding="utf-8")


def _summary_of(text: str) -> str:
    m = re.search(r"^summary: (.*)$", text, re.MULTILINE)
    assert m, text
    raw = m.group(1).strip()
    if raw.startswith('"'):
        raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return raw


def test_explicit_summary_survives_body_file(tmp_path):
    text = _draft(tmp_path, "Body sentence that must not become the summary. More.\n", "Short explicit tldr")
    assert _summary_of(text) == "Short explicit tldr"


def test_derived_summary_passes_send_cap(tmp_path):
    body = "Word " * 60 + "ends here. Tail sentence.\n"
    text = _draft(tmp_path, body, None)
    summary = _summary_of(text)
    assert 0 < len(summary) <= 240
    assert schema_validate._memo_cf_summary_length_cap({"summary": summary}) is None
