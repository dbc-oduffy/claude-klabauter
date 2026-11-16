"""A family-stage row's declared writes it changed land in kept.json, as an executor row's do."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import kept_work, row_returns, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261114T133823-06aacf"
KEPT_REL = f"{RECORD_DIR_ROOT}/{RUN}/{kept_work.KEPT_FILE}"
EVIDENCE = f"state/run-evidence/{RUN}/evidence.md"
UNDECLARED = f"state/run-evidence/{RUN}/family-stage-result.json"
FAMILY = "coordinator:family-stage-runner"
EXECUTOR = "coordinator:executor"
FAMILY_DONE = 'DONE: C1 via codex {"stage_role":"write","attempts":[{"family":"codex","verdict":"OK"}]}'


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, "seed.txt", "seed\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/run.json", "run.json\n")
    return tmp_path


def _stage_row(root: Path, agent_type: str, *, report_written: bool = False) -> None:
    """Row C1 writes EVIDENCE; the file and an undeclared sibling are dirty, the baseline holds null."""
    report = "reports/C1.md"
    if report_written:
        _write(root, report, "DONE: C1\n")
    marker = render_marker(CommitRequest(
        chunks=(ChunkCommit(id="C1", title="C1", paths=(EVIDENCE,), report=report),),
        session_id=None, repo_root=None, plan_path=None,
    ))
    _write(root, "run.mjs", f"{marker}\n")
    _write(root, EVIDENCE, "evidence\n")
    _write(root, UNDECLARED, "{}\n")
    row = {"id": "C1", "agent_type": agent_type, "family": "codex", "writes": [EVIDENCE]}
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/manifest.json", json.dumps({"rows": [row], "write_baseline": {EVIDENCE: None}}))


def _halt(root: Path, returned: str) -> dict:
    params = {
        "script_path": "run.mjs", "run_id": RUN,
        HALTED_RECORD_PARAM: {"halted": "refusal", "reason": "review prep returned no slices"},
        row_returns.CONSUMED_RETURNS_PARAM: {"C1": returned},
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def _kept_paths(root: Path) -> dict:
    kept_file = root / KEPT_REL
    if not kept_file.is_file():
        return {}
    return json.loads(kept_file.read_text(encoding="utf-8"))["chunks"].get("C1", {}).get("paths", {})


def test_family_row_changed_declared_write_is_recorded_with_its_fingerprint(repo):
    _stage_row(repo, FAMILY)

    out = _halt(repo, FAMILY_DONE)

    assert out["committed"] is True, out
    assert _kept_paths(repo) == {EVIDENCE: kept_work.fingerprint(repo, EVIDENCE)}


def test_family_row_undeclared_dirty_path_stays_unrecorded(repo):
    _stage_row(repo, FAMILY)

    _halt(repo, FAMILY_DONE)

    assert UNDECLARED not in _kept_paths(repo)


def test_family_row_whose_return_is_not_done_records_no_path(repo):
    _stage_row(repo, FAMILY)

    _halt(repo, 'BLOCKED: C1 via codex {"verdict":"FAILED"}')

    assert _kept_paths(repo) == {}


def test_family_row_unchanged_since_baseline_records_no_path(repo):
    _stage_row(repo, FAMILY)
    (repo / EVIDENCE).unlink()

    _halt(repo, FAMILY_DONE)

    assert _kept_paths(repo) == {}


def test_executor_row_without_a_delivered_report_stays_unrecorded(repo):
    _stage_row(repo, EXECUTOR)

    _halt(repo, "DONE: C1")

    assert _kept_paths(repo) == {}


def test_executor_row_with_a_delivered_report_is_recorded(repo):
    _stage_row(repo, EXECUTOR, report_written=True)

    _halt(repo, "DONE: C1")

    assert _kept_paths(repo) == {EVIDENCE: kept_work.fingerprint(repo, EVIDENCE)}
