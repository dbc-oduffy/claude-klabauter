"""Tests for coordinator_core.learn_lessons_pipeline.grind_cost."""

from __future__ import annotations

import json

from coordinator_core.learn_lessons_pipeline import grind_cost, mechanical_close
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


def test_measure_reports_the_routing_split(tmp_path, monkeypatch):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path, n_rows=5)
    base = grind_cost.measure(
        repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR
    )
    assert base["mechanically_closed"] == 0
    assert base["llm_routed"] == base["admitted"] == 5
    assert base["routed_out"] == 0
    assert base["projected_spawns"] >= base["triage_batches"]

    verdicts = [
        mechanical_close.MechanicalVerdict("a", "dedupe", "x.md", "dup"),
        mechanical_close.MechanicalVerdict("b", "dedupe", "y.md", "covered"),
    ]
    monkeypatch.setattr(grind_cost, "close_candidates", lambda manifest, root: verdicts)
    result = grind_cost.measure(
        repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR
    )
    assert result["mechanically_closed"] == 2
    assert result["llm_routed"] == 3


def test_routed_out_counts_only_schema_declines():
    assert grind_cost._is_routed_out("admission: row fails its schema and cannot be closed (x)")
    assert grind_cost._is_routed_out("admission: row carries a schema hand-back mark (x)")
    assert not grind_cost._is_routed_out("row carries a route-to-* hand-back mark ('route-to-x')")


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
    assert {
        "admitted",
        "declined",
        "triage_batches",
        "spawns_per_lesson",
        "llm_routed",
        "mechanically_closed",
        "routed_out",
        "projected_spawns",
        "max_triage_batch_tokens",
    } <= payload.keys()


def test_measure_reports_max_triage_batch_tokens_under_the_tier(tmp_path):
    from coordinator_core.distill.prompt_tier import LONG_PROMPT_TIER_TOKENS

    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path, n_rows=5)
    result = grind_cost.measure(
        repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR
    )
    assert 0 < result["max_triage_batch_tokens"] <= LONG_PROMPT_TIER_TOKENS


def test_measure_splits_oversize_batches_so_the_max_stays_under_the_tier(tmp_path):
    from coordinator_core.distill.prompt_tier import LONG_PROMPT_TIER_TOKENS

    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path, n_rows=5)
    for row in sorted(queue_dir.glob("*.yaml"))[:4]:
        with row.open("a") as fh:
            fh.write("# " + "x" * (LONG_PROMPT_TIER_TOKENS * 4 // 2) + "\n")
    result = grind_cost.measure(
        repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR
    )
    assert result["triage_batches"] >= 2
    assert result["max_triage_batch_tokens"] <= LONG_PROMPT_TIER_TOKENS
