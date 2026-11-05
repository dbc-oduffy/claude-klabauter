"""Ratchet: a halt commit lists only the run's record dir and its run-doc classes under ``run_artifacts.landed``.

Replays the 420b2c53c2 shape through the default ``land_run_halt.land`` call: a done chunk declaring
product files the run never wrote, on a ledger-check halt that carries a passing verdict.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run_halt, run_artifacts
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_MINT = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
RUN = f"ask-{_MINT}-bbbbbb"
PRED = "ask-20261103T120000-aaaaaa"
SIZING = f"{run_artifacts._SIZINGS_REL}/2026-11-04-s.yaml"
PRODUCT = ("coordinator_core/x/fallback.py", "tests/test_fallback.py")
LEDGER = {"halted": "ledger-check", "reason": "row C2 open"}
PASSING = {"targeted_tests": {"status": "pass", "tests_failed": 0, "baseline": None}}


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
    _write(tmp_path, "README.md", "seed\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    _write(tmp_path, SIZING, "plan: none\n")
    _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/ask.md", "ask\n")
    _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"sizing_rel": SIZING}))
    return tmp_path


def _chunk_declaring(root: Path, paths) -> None:
    _write(root, "reports/C1.md", "DONE: C1\n")
    for p in paths:
        _write(root, p, f"{p} = 1\n")
    chunk = ChunkCommit(id="C1", title="C1", paths=tuple(paths), report="reports/C1.md")
    marker = render_marker(CommitRequest(chunks=(chunk,), session_id=None, repo_root=None, plan_path=None))
    _write(root, "run.mjs", f"{marker}\n")


def _call(root: Path, **extra) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: LEDGER, **PASSING, **extra}
    return land_run_halt.land(root, root, params)


def _only_run_docs(root: Path, landed) -> bool:
    record = f"{RECORD_DIR_ROOT}/{RUN}/"
    return all(p.startswith(record) or p == SIZING for p in landed)


def test_a_done_chunks_declared_product_never_lands_in_a_halt_commit(repo):
    _chunk_declaring(repo, PRODUCT)

    out = _call(repo)

    assert out["committed"] is True, out
    landed = out["run_artifacts"]["landed"]
    assert landed and _only_run_docs(repo, landed), landed
    assert SIZING in landed
    names = _git(repo, "show", "--name-only", "--format=", out["sha"]).split()
    assert not set(PRODUCT) & set(names) and _only_run_docs(repo, names), names
    for p in PRODUCT:
        assert out["withheld"][p] == land_run_halt.HALT_RECORD_ONLY
        assert p in _git(repo, "status", "--porcelain", "-uall")
    assert "Lands-Chunks" not in _git(repo, "log", "-1", "--format=%B")


def test_a_superseded_record_dir_and_a_docs_stage_path_never_land(repo):
    pred_file = f"{RECORD_DIR_ROOT}/{PRED}/run.json"
    _write(repo, pred_file, json.dumps({"sizing_rel": SIZING}))
    _write(repo, "docs/notes/n.md", "note\n")
    _chunk_declaring(repo, PRODUCT[:1])

    out = _call(repo, **{run_artifacts.DOC_PATHS_PARAM: ["docs/notes/n.md"]})

    assert out["committed"] is True, out
    landed = out["run_artifacts"]["landed"]
    assert pred_file not in landed and "docs/notes/n.md" not in landed, landed
    assert _only_run_docs(repo, landed), landed
    assert out["withheld"][pred_file] == land_run_halt.HALT_RECORD_ONLY


def test_halt_landable_classes_name_only_run_docs():
    assert run_artifacts.HALT_LANDABLE_CLASSES <= set(run_artifacts.ARTIFACT_CLASSES)
    assert not run_artifacts.HALT_LANDABLE_CLASSES & {"superseded-record", "docs-stage", "ask-dir", "slice-sizing"}
