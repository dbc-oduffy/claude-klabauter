"""Tests for coordinator_core.learn_lessons_pipeline.grind_cost."""

from __future__ import annotations

import json

import pytest

from coordinator_core.learn_lessons_pipeline import grind_cost, mechanical_close
from coordinator_core.ops.dispatch_emit.tests.test_queue_emit import (
    _FIXTURE_PROFILE_DIR,
    _batches_const,
    _emit,
    _setup_repo,
)
from pathlib import Path


def test_measure_matches_the_emitted_script_batches(tmp_path: Path) -> None:
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


def test_measure_is_read_only(tmp_path: Path) -> None:
    repo_root, queue_dir, run_dir = _setup_repo(tmp_path, n_rows=3)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    grind_cost.measure(repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR)
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert before == after


def test_empty_queue_reports_zero_without_dividing(tmp_path: Path) -> None:
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path, n_rows=0)
    result = grind_cost.measure(
        repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR
    )
    assert result["admitted"] == 0
    assert result["triage_batches"] == 0
    assert result["spawns_per_lesson"] == 0.0


def test_measure_reports_the_routing_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    verdicts.append(mechanical_close.MechanicalVerdict("c", "retag", "", "scope", "", "project"))
    result = grind_cost.measure(
        repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR
    )
    assert result["mechanically_closed"] == 2
    assert result["llm_routed"] == 3


def test_tokens_after_drops_closed_rows_but_keeps_retags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path, n_rows=4)
    kw = dict(profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR)
    base = grind_cost.measure(repo_root, **kw)
    assert base["tokens_before"] == base["tokens_after"] > 0

    rows = sorted(p.relative_to(repo_root).as_posix() for p in queue_dir.glob("*.yaml"))
    V = mechanical_close.MechanicalVerdict
    verdicts = [
        V("a", "dedupe", "x.md", "dup", rows[0]),
        V("b", "discard", "", "empty", rows[1]),
        V("c", "retag", "", "scope", rows[2], "project"),
    ]
    monkeypatch.setattr(grind_cost, "close_candidates", lambda manifest, root: verdicts)
    result = grind_cost.measure(repo_root, **kw)
    sizes = {r: (repo_root / r).stat().st_size for r in rows}
    from coordinator_core.distill.prompt_tier import estimate_tokens

    closed = sum(estimate_tokens(sizes[r]) for r in rows[:2])
    assert result["tokens_before"] == base["tokens_before"]
    assert result["tokens_before"] - result["tokens_after"] == closed
    assert result["tokens_after"] < result["tokens_before"]


def test_routed_out_counts_only_schema_declines() -> None:
    assert grind_cost._is_routed_out("admission: row fails its schema and cannot be closed (x)")
    assert grind_cost._is_routed_out("admission: row carries a schema hand-back mark (x)")
    assert not grind_cost._is_routed_out("row carries a route-to-* hand-back mark ('route-to-x')")


def test_main_prints_json_with_the_four_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
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
        "tokens_before",
        "tokens_after",
        "projected_spawns",
        "max_triage_batch_tokens",
    } <= payload.keys()


def test_measure_reports_max_triage_batch_tokens_under_the_tier(tmp_path: Path) -> None:
    from coordinator_core.distill.prompt_tier import LONG_PROMPT_TIER_TOKENS

    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path, n_rows=5)
    result = grind_cost.measure(
        repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR
    )
    assert 0 < result["max_triage_batch_tokens"] <= LONG_PROMPT_TIER_TOKENS


def test_measure_splits_oversize_batches_so_the_max_stays_under_the_tier(tmp_path: Path) -> None:
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


def test_measure_reports_the_budget_before_and_after_narrowing(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit.tests.test_queue_emit_budget import _BATCH, _ROWS, _repo

    repo, queue, pdir = _repo(tmp_path)
    result = grind_cost.measure(repo, profile="lessons", queue=queue, profile_dir=pdir)

    assert result["refused"] is False
    assert result["budget"]["cap"] == {"agents": 10, "tokens": 200000, "minutes": 15}
    assert all(result["budget"]["within"].values())
    assert not result["budget_before"]["within"]["tokens"]
    assert 0 < result["fitted_rows"] < _ROWS
    assert result["fitted_rows"] % _BATCH == 0, "whole batches only"
    assert result["deferred"] == _ROWS - result["fitted_rows"]
    assert result["admitted"] == _ROWS


def _calibration(tmp_path, rate):
    path = tmp_path / "prevet_calibration.json"
    path.write_text(json.dumps({grind_cost.PREVET_RATE_KEY: rate}), encoding="utf-8")
    return path


def test_a_prevet_spawn_is_priced_at_the_prevet_role_rate(tmp_path: Path) -> None:
    rates = grind_cost.role_rates(True, _calibration(tmp_path, 5_000))
    assert rates["prevet"] == 5_000 != grind_cost.ROLE_TOKENS["triage"]
    spawns = {"prevet": 10, "triage": 3, "verify": 3}
    cost = grind_cost.projected_cost(grind_cost.UnitCost(minutes=1), 3, spawns, rates)
    assert cost.agents == 16
    assert cost.tokens == 10 * 5_000 + 6 * grind_cost.ROLE_TOKENS["triage"]
    assert cost.tokens < 16 * grind_cost.ROLE_TOKENS["triage"]


def test_batch_unit_cost_prices_each_role(tmp_path: Path) -> None:
    rates = grind_cost.role_rates(True, _calibration(tmp_path, 5_000))
    unit = grind_cost.batch_unit_cost(True, 25, 50, rates)
    assert unit.agents == 4
    assert unit.tokens == 2 * 5_000 + 2 * grind_cost.ROLE_TOKENS["triage"]


def test_a_declared_prevet_without_calibration_refuses_naming_the_file(tmp_path: Path) -> None:
    missing = tmp_path / "prevet_calibration.json"
    with pytest.raises(grind_cost.PrevetCalibrationError, match="prevet_calibration.json"):
        grind_cost.role_rates(True, missing)
    missing.write_text("{}", encoding="utf-8")
    with pytest.raises(grind_cost.PrevetCalibrationError, match=grind_cost.PREVET_RATE_KEY):
        grind_cost.role_rates(True, missing)


def test_a_profile_without_prevet_never_reads_the_calibration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grind_cost, "PREVET_CALIBRATION", tmp_path / "absent.json")
    assert grind_cost.role_rates(False)["prevet"] == 0
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path, n_rows=3)
    result = grind_cost.measure(
        repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR
    )
    assert result["spawns_by_role"]["prevet"] == 0
    assert result["projected_spawns"] == sum(result["spawns_by_role"].values())


def test_measure_does_not_narrow_a_profile_outside_the_mechanical_close_set(tmp_path: Path) -> None:
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path, n_rows=5)
    result = grind_cost.measure(
        repo_root, profile="fixture", queue=queue_dir, profile_dir=_FIXTURE_PROFILE_DIR
    )
    assert result["deferred"] == 0
    assert result["fitted_rows"] == result["admitted"] == 5
