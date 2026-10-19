"""A ledger resume whose staged rows all landed before the halt (landed-writes gate or credit) owes no since-halt diff."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, manifest, _git(tmp_path, "rev-parse", "HEAD")


def _resume(halt, **extra):
    return {"run_id": "old", "halt_sha": halt, "blocking": ["R1"], **extra}


def test_empty_unlanded_exempts_met_rows_without_a_git_spawn(repo, monkeypatch):
    root, manifest, halt = repo

    def boom(*a, **k):
        raise AssertionError("git spawned")

    monkeypatch.setattr(tc, "run_git", boom)
    assert tc._unchanged_since_halt_failures(root, _resume(halt, unlanded=[]), MET, manifest) == []


@pytest.mark.parametrize("extra", [{"unlanded": ["C1"]}, {}, {"unlanded": "none"}])
def test_row_without_landing_evidence_still_refuses(repo, extra):
    root, manifest, halt = repo
    got = tc._unchanged_since_halt_failures(root, _resume(halt, **extra), MET, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_changed_path_still_passes_with_unlanded_rows(repo):
    root, manifest, halt = repo
    (root / "prod.py").write_text("x = 2\n", encoding="utf-8")
    assert tc._unchanged_since_halt_failures(root, _resume(halt, unlanded=["C1"]), MET, manifest) == []
