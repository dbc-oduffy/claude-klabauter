"""Tests for coordinator_core.learn_lessons_pipeline.grind_cost."""

from __future__ import annotations

import json

from coordinator_core.learn_lessons_pipeline import grind_cost
from coordinator_core.ops.dispatch_emit.tests.test_queue_emit import (
    _FIXTURE_PROFILE_DIR,
    _batches_const,
    _emit,
    _setup_repo,
)


def test_measure_matches_the_emitted_script_batches(tmp_path):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path, n_rows=5)
    result = grind_cost.measure(
        repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR
    )
    (tmp_path / "emit").mkdir()
    emission = _emit(tmp_path / "emit", n_rows=5)

    assert result["admitted"] == 5
    assert result["declined"] == 0
    assert result["triage_batches"] == len(_batches_const(emission.script))
    assert result["spawns_per_lesson"] == result["triage_batches"] / 5


def test_measure_is_read_only(tmp_path):
    repo_root, queue_dir, run_dir = _setup_repo(tmp_path, n_rows=3)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    grind_cost.measure(repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR)
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert before == after


def test_empty_queue_reports_zero_without_dividing(tmp_path):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path, n_rows=0)
    result = grind_cost.measure(
        repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR
    )
    assert result["admitted"] == 0
    assert result["triage_batches"] == 0
    assert result["spawns_per_lesson"] == 0.0


def test_main_prints_json_with_the_four_keys(tmp_path, monkeypatch, capsys):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path, n_rows=2)
    monkeypatch.chdir(repo_root)
    real = grind_cost.measure
    monkeypatch.setattr(
        grind_cost,
        "measure",
        lambda root: real(root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR),
    )
    assert grind_cost.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"admitted", "declined", "triage_batches", "spawns_per_lesson"} <= payload.keys()
