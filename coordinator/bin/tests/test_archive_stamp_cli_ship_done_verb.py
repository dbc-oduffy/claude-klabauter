"""End to end: `archive-stamp-cli ship-handoff <path> --sha <sha>` is the whole done verb.

Runs `main` against the real `archive_stamp` module in a tmp_path git repo.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import claims
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_SID = "22222222-2222-2222-2222-222222222222"
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_ship_done_verb_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader("archive_stamp_cli_ship_done_verb_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    args_list = list(args)
    if args_list[:2] == ["commit", "-m"] and "Session-Id:" not in args_list[2]:
        args_list[2] = f"{args_list[2]}\n\nSession-Id: {_SID}"
    return subprocess.run(
        ["git", "-C", str(repo), *args_list],
        capture_output=True, text=True, env=_GIT_ENV, timeout=30,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )


def test_bare_sha_form_stamps_releases_archives_and_prints_both_lines(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    name = "2026-10-22-e2e-baton.md"
    hp = repo / "state" / "handoffs" / name
    hp.parent.mkdir(parents=True)
    hp.write_text(
        "---\n"
        'title: "E2E baton"\n'
        "created: 2026-10-22\n"
        "branch: work/test/2026-10-22\n"
        "status: claimed\n"
        "category: infra\n"
        'summary: "E2E baton"\n'
        'predecessor: "none"\n'
        "deployment_state: in_flight\n"
        f"scope:\n  - state/handoffs/{name}\n"
        "---\n\n# Handoff\n",
        encoding="utf-8",
    )
    _git(repo, "add", str(hp.relative_to(repo)))
    _git(repo, "commit", "-m", "add baton")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert claims.claim_artifact("handoff", name, cwd=str(repo)) is True
    status_line = next(ln for ln in hp.read_text(encoding="utf-8").splitlines() if ln.startswith("status:"))

    rc = _cli.main(["ship-handoff", str(hp), "--sha", sha])

    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert [ln for ln in out if ln.startswith("release=")] == ["release=released"]
    assert [ln for ln in out if ln.startswith("plan-terminality=")] == [
        "plan-terminality=no-plan-linked"
    ]
    assert not (repo / ".git" / "coordinator-sessions" / "handoff-claims" / name).exists()
    assert not hp.exists()
    (twin,) = (repo / "archive" / "handoffs").rglob(name)
    text = twin.read_text(encoding="utf-8")
    assert status_line in text.splitlines()
    assert "deployment_state: shipped" in text and "shipped_in:" in text
