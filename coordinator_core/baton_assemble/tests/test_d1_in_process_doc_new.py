"""d1 (`coordinator-doc-new`) runs in-process on the warm engine and reports the
path it actually wrote (the month-sharded one), with no child process."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.baton_assemble import apply as ba_apply


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "state" / "handoffs").mkdir(parents=True)
    return repo


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"child process spawned: {a!r}")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def _d1_args(out: str) -> list[str]:
    return [
        "--type=handoff",
        "--deliverable-id=dlv-in-process-probe-a1b2c3",
        "--title=In process probe",
        "--branch=work/test/2026-11-03",
        f"--out={out}",
    ]


def test_d1_scaffolds_in_process_and_reports_sharded_path(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _no_spawn(monkeypatch)
    cwd_before, argv_before = os.getcwd(), list(sys.argv)

    handler = ba_apply._CLI_DISPATCH["coordinator-doc-new"]
    detail = handler(_d1_args("state/handoffs/2026-11-03-in-process-probe.md"), repo)

    reported = detail["output_path"]
    assert reported == "state/handoffs/2026-11/2026-11-03-in-process-probe.md"
    assert (repo / reported).is_file()
    assert not (repo / "state" / "handoffs" / "2026-11-03-in-process-probe.md").exists()
    assert os.getcwd() == cwd_before
    assert sys.argv == argv_before
    module = ba_apply._load_doc_new_module()
    assert module._resolve_state_root is not ba_apply._state_root_in_process


def test_state_root_in_process_degrades_to_none(monkeypatch):
    import coordinator_core.state_root as sr

    def refuse(**_kw):
        raise sr.StateRootError("no root")

    monkeypatch.setattr(sr, "coordinator_state_root", refuse)
    assert ba_apply._state_root_in_process(central=True) is None


def test_d1_failure_raises_and_restores_process_state(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _no_spawn(monkeypatch)
    cwd_before, argv_before = os.getcwd(), list(sys.argv)

    args = _d1_args("state/handoffs/2026-11-03-collide.md")
    ba_apply._dispatch_coordinator_doc_new(args, repo)
    with pytest.raises(RuntimeError, match="coordinator-doc-new"):
        ba_apply._dispatch_coordinator_doc_new(args, repo)

    assert os.getcwd() == cwd_before
    assert sys.argv == argv_before
