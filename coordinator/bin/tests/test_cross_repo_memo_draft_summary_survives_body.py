"""`cross-repo-memo draft --body-file` keeps the explicit --summary, and a body-derived summary fits send's cap.

Dispatches to the stamped published engine (a claude-klabauter tree is never an engine root).
"""

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


def _draft(tmp_path: Path, engine_root: str, body: str, summary: str | None) -> str:
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
        "COORDINATOR_ENGINE_ROOT": engine_root,
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


def test_explicit_summary_survives_body_file(tmp_path, stamped_engine_env):
    text = _draft(tmp_path, stamped_engine_env, "Body sentence that must not become the summary. More.\n", "Short explicit tldr")
    assert _summary_of(text) == "Short explicit tldr"


def test_derived_summary_passes_send_cap(tmp_path, stamped_engine_env):
    body = "Word " * 60 + "ends here. Tail sentence.\n"
    text = _draft(tmp_path, stamped_engine_env, body, None)
    summary = _summary_of(text)
    assert 0 < len(summary) <= 240
    assert schema_validate._memo_cf_summary_length_cap({"summary": summary}) is None


def test_derived_summary_with_quotes_and_backticks_passes_send_cap(tmp_path, stamped_engine_env):
    body = ('`cross-repo-memo draft x --title "..." --body-file "a.md"` derived ' + 'word ' * 40
            + 'and "quoted" ends here. Tail sentence.\n')
    text = _draft(tmp_path, stamped_engine_env, body, None)
    raw = re.search(r"^summary: \"(.*)\"$", text, re.MULTILINE).group(1)
    assert 0 < len(raw) <= 240
    assert schema_validate._memo_cf_summary_length_cap({"summary": raw}) is None


def test_lib_derive_summary_measures_escaped_form():
    sys.path.insert(0, str(_BIN / "lib"))
    try:
        from memo_compose import _derive_summary
    finally:
        sys.path.remove(str(_BIN / "lib"))
    derived = _derive_summary('"' * 200 + " tail")
    assert len(derived) + derived.count('"') + derived.count("\\") <= 240
    assert derived.endswith("…")
    assert _derive_summary("short line") == "short line"
