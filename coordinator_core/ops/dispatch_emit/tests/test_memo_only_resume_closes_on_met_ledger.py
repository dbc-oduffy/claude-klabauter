"""A memo-only run resumed after a ledger-check halt closes on its met ledger; an unlanded citation still refuses."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import land_run, terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


def _repo(tmp_path, declared):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    halt = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "memo.md").write_text("memo\n", encoding="utf-8")
    _git(tmp_path, "add", "memo.md")
    _git(tmp_path, "commit", "-q", "-m", "memo")
    memo = _git(tmp_path, "rev-parse", "HEAD")
    manifest = tmp_path / "manifest.json"
    rows = [{"id": "C1", "writes": declared}] if declared else []
    manifest.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return manifest, halt, memo


def _resume(halt):
    return {"run_id": "old", "halt_sha": halt, "blocking": ["R1", "R2"], "unlanded": ["C1"]}


def _verdict(met_by):
    return {"items": [
        {"id": "R1", "status": "met", "met_by": met_by},
        {"id": "R2", "status": "met", "met_by": f"memo {met_by}" if met_by else None},
    ]}


def _twin_a(root, resume, verdict, manifest):
    return tc._unchanged_since_halt_failures(root, resume, verdict, manifest)


def _twin_b(root, resume, verdict, manifest):
    return land_run._unchanged_since_halt(root, resume, verdict, manifest, None, {})


@pytest.mark.parametrize("twin", [_twin_a, _twin_b])
@pytest.mark.parametrize("declared", [[], ["prod.py"]])
def test_met_rows_naming_a_landed_memo_commit_close_the_resume(tmp_path, twin, declared):
    manifest, halt, memo = _repo(tmp_path, declared)
    assert twin(tmp_path, _resume(halt), _verdict(memo), manifest) == []


@pytest.mark.parametrize("twin", [_twin_a, _twin_b])
@pytest.mark.parametrize("declared", [[], ["prod.py"]])
def test_met_rows_naming_no_landed_commit_are_still_refused(tmp_path, twin, declared):
    manifest, halt, _memo = _repo(tmp_path, declared)
    unlanded = "0123456789abcdef0123456789abcdef01234567"
    for met_by in (unlanded, "verified by hand", None):
        got = twin(tmp_path, _resume(halt), _verdict(met_by), manifest)
        assert [r["id"] for r in got] == ["R1", "R2"]
        assert all(r["reason"] == UNCHANGED_SINCE_HALT_REASON for r in got)


@pytest.mark.parametrize("twin", [_twin_a, _twin_b])
def test_only_the_row_naming_a_landed_commit_is_credited(tmp_path, twin):
    manifest, halt, memo = _repo(tmp_path, [])
    verdict = {"items": [
        {"id": "R1", "status": "met", "met_by": memo},
        {"id": "R2", "status": "met", "met_by": "nothing landed"},
    ]}
    got = twin(tmp_path, _resume(halt), verdict, manifest)
    assert got == [{"id": "R2", "reason": UNCHANGED_SINCE_HALT_REASON}]
