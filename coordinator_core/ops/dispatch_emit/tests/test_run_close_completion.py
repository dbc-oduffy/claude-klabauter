"""Tests for the inline completion-entry leg."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import run_close_completion as rcc

PLAN = "docs/plans/p.md"


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN).write_text('---\ntitle: "Shipped the thing"\n---\n\nbody\n', encoding="utf-8")

    def _no_spawn(*a, **k):
        raise AssertionError("process spawned")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    return tmp_path


def _entries(root):
    return list((root / "archive").rglob("*.md")) if (root / "archive").exists() else []


def test_no_summary_fails_and_writes_nothing(root):
    result, paths = rcc.completion_entry_leg(root, "r1", PLAN, {"status": "met"})
    assert result.status == "failed" and result.detail.startswith("no judge summary; ")
    assert paths == [] and _entries(root) == []


@pytest.mark.parametrize("crit", [None, {}, {"status": "indeterminate"}])
def test_unmet_detail_names_entry_path_and_runnable_cli(root, crit):
    from coordinator_core.ops.ceremony.completion_entry import _completion_entry_rel_path

    result, paths = rcc.completion_entry_leg(root, "r1", PLAN, crit)
    entry = _completion_entry_rel_path("Shipped the thing").as_posix()
    assert result.status == "failed" and paths == []
    assert entry in result.detail
    assert "coordinator/bin/coordinator-complete-entry.py" in result.detail
    assert "/" not in result.detail.split(";")[0] and "\n" not in result.detail
    assert _entries(root) == []


def test_met_observation_is_the_prose_when_no_summary(root):
    result, paths = rcc.completion_entry_leg(
        root, "r1", PLAN, {"status": "met", "observation": "Now true on disk."}
    )
    assert result.status == "ran" and len(paths) == 1
    assert "Now true on disk." in (root / paths[0]).read_text(encoding="utf-8")


def test_summary_wins_over_observation(root):
    crit = {"status": "met", "completion_summary": "The summary.", "observation": "The obs."}
    _, paths = rcc.completion_entry_leg(root, "r1", PLAN, crit)
    text = (root / paths[0]).read_text(encoding="utf-8")
    assert "The summary." in text and "The obs." not in text


def test_not_met_with_observation_still_fails(root):
    crit = {"status": "not_met", "observation": "nope"}
    result, paths = rcc.completion_entry_leg(root, "r1", PLAN, crit)
    assert result.status == "failed" and result.detail.startswith("criterion not_met; ")
    assert paths == [] and _entries(root) == []


def test_indeterminate_criterion_with_met_ledger_writes_entry(root):
    result, paths = rcc.completion_entry_leg(
        root, "r1", PLAN, {"status": "indeterminate"}, ledger_met="ledger-met: R1"
    )
    assert result.status == "ran" and len(paths) == 1
    assert "ledger-met: R1" in (root / paths[0]).read_text(encoding="utf-8")


def test_not_met_criterion_with_met_ledger_still_fails(root):
    result, paths = rcc.completion_entry_leg(
        root, "r1", PLAN, {"status": "not_met"}, ledger_met="ledger-met: R1"
    )
    assert result.status == "failed" and result.detail.startswith("criterion not_met; ")
    assert paths == [] and _entries(root) == []


def test_indeterminate_criterion_without_ledger_fails(root):
    result, paths = rcc.completion_entry_leg(root, "r1", PLAN, {"status": "indeterminate"})
    assert result.status == "failed" and result.detail.startswith("criterion indeterminate; ")
    assert paths == [] and _entries(root) == []


def test_not_met_fails_and_writes_nothing(root):
    crit = {"status": "not_met", "completion_summary": "It shipped."}
    result, paths = rcc.completion_entry_leg(root, "r1", PLAN, crit)
    assert result.status == "failed" and result.detail.startswith("criterion not_met; ")
    assert paths == [] and _entries(root) == []


def test_ran_writes_summary_verbatim_with_provenance(root):
    crit = {"status": "met", "completion_summary": "Shipped X. It matters."}
    result, paths = rcc.completion_entry_leg(root, "r1", PLAN, crit)
    assert result.status == "ran"
    assert len(paths) == 1 and paths[0].startswith("archive/completed/") and "\\" not in paths[0]
    assert paths[0] in result.detail
    text = (root / paths[0]).read_text(encoding="utf-8")
    assert "Shipped X. It matters." in text
    assert "authored_by: terminal-judge" in text
    assert "\nrun_id: r1\n" in text
    assert "ONE paragraph" not in text


def test_second_call_same_run_is_discharged(root):
    crit = {"status": "met", "completion_summary": "Shipped X."}
    first, paths = rcc.completion_entry_leg(root, "r1", PLAN, crit)
    again, again_paths = rcc.completion_entry_leg(root, "r1", PLAN, crit)
    assert again.status == "discharged" and paths[0] in again.detail
    assert again_paths == [] and len(_entries(root)) == 1


def test_other_run_with_taken_path_fails_without_clobber(root):
    crit = {"status": "met", "completion_summary": "Shipped X."}
    _, paths = rcc.completion_entry_leg(root, "r1", PLAN, crit)
    before = (root / paths[0]).read_text(encoding="utf-8")
    result, written = rcc.completion_entry_leg(root, "r2", PLAN, crit)
    assert result.status == "failed" and written == []
    assert (root / paths[0]).read_text(encoding="utf-8") == before
