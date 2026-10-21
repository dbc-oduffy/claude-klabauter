"""#60: product a halt withheld stays dirty with the halt's own bytes; a refire counts it as work to land."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, land_run, land_run_halt
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    UNCHANGED_SINCE_HALT_REASON,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261020T000000-aaaaaa"
REFUSED = [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _commit(root: Path, text: str, msg: str) -> str:
    _write(root, "prod.py", text)
    _git(root, "add", "prod.py")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def _init(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")


@pytest.fixture
def repo(tmp_path):
    """HEAD reverted prod.py after the halt commit; the worktree holds the halt commit's bytes."""
    _init(tmp_path)
    _commit(tmp_path, "x = 1\n", "base")
    halt = _commit(tmp_path, "x = 2\n", "halt")
    _commit(tmp_path, "x = 1\n", "revert")
    _write(tmp_path, "prod.py", "x = 2\n")
    return tmp_path, halt


def _resume(**extra) -> dict:
    return {"run_id": RUN, "blocking": ["R1"], "unlanded": ["C1"], **extra}


def _record_halt(root: Path, withheld: dict) -> None:
    record_dir = root / RECORD_DIR_ROOT / RUN
    record_dir.mkdir(parents=True)
    halted_run.write_halt_marker(record_dir, halted="ledger-check", sizing_rel=None)
    halted_run.record_withheld(record_dir, withheld)


def _guard(root, halt, declared=("prod.py",), detail=None, **resume_extra):
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "R1", "writes": list(declared)}]}), encoding="utf-8")
    resume = _resume(halt_sha=halt, **resume_extra)
    verdict = {"items": [{"id": "R1", "status": "met"}]}
    return land_run._unchanged_since_halt(root, resume, verdict, manifest, None, {} if detail is None else detail)


def _failures(root, halt, **resume_extra):
    return _guard(root, halt, **resume_extra)


def test_dirty_path_the_halt_withheld_counts_as_work_to_land(repo):
    root, halt = repo
    _record_halt(root, {"prod.py": "halt-record-only"})
    assert land_run._work_since_halt(root, halt, ["prod.py"], {"prod.py": "x"}) == "withheld-pending"
    assert _failures(root, halt) == []


def test_withheld_path_carried_on_the_resume_counts_too(repo):
    root, halt = repo
    assert _failures(root, halt, run_artifacts={"withheld": {"prod.py": "halt-record-only"}}) == []


def test_same_dirty_path_without_a_withheld_record_still_refuses(repo):
    root, halt = repo
    detail: dict = {}
    got = _guard(root, halt, detail=detail)
    assert got == REFUSED
    assert detail["base"] == halt and detail["outcome"] == "unchanged"


def test_withheld_path_that_is_clean_still_refuses(repo):
    root, halt = repo
    _git(root, "reset", "-q", "--hard", halt)
    _record_halt(root, {"prod.py": "halt-record-only"})
    assert _failures(root, halt) == REFUSED


def test_a_declared_path_differing_from_the_halt_commit_is_work(repo):
    root, halt = repo
    _write(root, "prod.py", "x = 3\n")
    assert land_run._work_since_halt(root, halt, ["prod.py"], {}) == "changed"
    assert _failures(root, halt) == []


def test_exemptions_and_unresolved_inputs(repo):
    root, halt = repo
    proof = {"R1": {"command": "pytest -q", "observed": "3 passed"}}
    assert _failures(root, halt, verified_only=proof) == []
    assert _failures(root, halt, verified_only={"R1": {"command": "pytest -q"}}) == REFUSED
    assert _failures(root, halt, unlanded=[]) == []
    detail: dict = {}
    assert _guard(root, None, detail=detail) == REFUSED
    assert detail["outcome"] == "no-base"
    assert _guard(root, halt, declared=(), detail=detail) == REFUSED
    assert detail["outcome"] == "no-declared-paths"
    assert _guard(root, "0" * 40, detail=detail) == REFUSED
    assert detail["outcome"] == "tree-unreadable"


def test_halt_commit_withholds_product_then_refire_counts_it(tmp_path):
    """The #60 repro end to end: a real halt commit withholds the finished chunk's product, which
    stays dirty with the halt's bytes; the refire's guard counts it. Once the product is committed
    as it stands, nothing remains and the refire refuses."""
    _init(tmp_path)
    _commit(tmp_path, "x = 1\n", "base")
    _write(tmp_path, "prod.py", "x = 2\n")
    _write(tmp_path, "reports/C1.md", "DONE: C1\n")
    marker = render_marker(CommitRequest(
        chunks=(ChunkCommit(id="C1", title="C1", paths=("prod.py",), report="reports/C1.md"),),
        session_id=None, repo_root=None, plan_path=None,
    ))
    _write(tmp_path, "run.mjs", f"{marker}\n")
    _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/ask.md", "ask\n")
    params = {
        "script_path": "run.mjs", "run_id": RUN,
        HALTED_RECORD_PARAM: {"halted": "ledger-check", "reason": "row R1 open"},
    }

    out = land_run_halt.land(tmp_path, tmp_path, params)

    assert out["committed"] is True, out
    assert out["withheld"] == {"prod.py": land_run_halt.HALT_RECORD_ONLY}
    assert "prod.py" not in _git(tmp_path, "show", "--name-only", "--format=", out["sha"]).split()
    assert halted_run.halted_withheld(tmp_path / RECORD_DIR_ROOT / RUN) == {"prod.py": land_run_halt.HALT_RECORD_ONLY}
    assert _failures(tmp_path, out["sha"]) == []

    _git(tmp_path, "add", "prod.py")
    _git(tmp_path, "commit", "-q", "-m", "refire lands the product")
    assert _failures(tmp_path, out["sha"]) == []  # differs from the halt commit's tree
    assert land_run._work_since_halt(tmp_path, _git(tmp_path, "rev-parse", "HEAD"), ["prod.py"], {"prod.py": "x"}) == "unchanged"
