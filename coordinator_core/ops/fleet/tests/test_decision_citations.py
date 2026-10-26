"""Leaf cases for fleet._decision_citations.live_citations."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.fleet import _decision_citations as dc
from coordinator_core.ops.fleet._decision_citations import CitationTarget, live_citations
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_A = "docs" + "/wiki/a.md"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _repo(tmp_path: Path, files: dict) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(
            ["git", *args], cwd=str(repo), env=_GIT_ENV, check=True,
            stdin=subprocess.DEVNULL, **no_console_creationflags(),
        )
    return repo


_OLD = "docs/decisions/DR-5-old.md"
_NEW = "docs/decisions/DR-6-new.md"


def test_own_file_is_exempt(tmp_path):
    repo = _repo(tmp_path, {_OLD: "# DR-5\nDR-5 says x\n"})
    assert live_citations(repo, [CitationTarget(_OLD, 5, None)]) == {}


def test_successor_file_and_successor_line_are_exempt(tmp_path):
    repo = _repo(tmp_path, {
        _OLD: "# DR-5\n",
        _NEW: "# DR-6\nSupersedes DR-5\n",
        _A: "see DR-5 now DR-6\n",
    })
    assert live_citations(repo, [CitationTarget(_OLD, 5, "DR-6")]) == {}


def test_other_file_hit_names_path_and_line(tmp_path):
    repo = _repo(tmp_path, {
        _OLD: "# DR-5\n",
        _A: "x\nsee DR-5\n",
        "state/h.md": "DR-5 in history\n",
    })
    got = live_citations(repo, [CitationTarget(_OLD, 5, "DR-6")])
    assert got == {_OLD: [f"{_A}:2"]}


def test_git_grep_failure_returns_empty(tmp_path):
    failed = subprocess.CompletedProcess([], 2, stdout=b"", stderr=b"fatal: boom")
    with patch.object(dc.subprocess, "run", return_value=failed):
        assert live_citations(tmp_path, [CitationTarget(_OLD, 5, None)]) == {}


def test_empty_targets_spawns_nothing():
    with patch.object(dc.subprocess, "run") as run:
        assert live_citations(Path("."), []) == {}
    run.assert_not_called()
