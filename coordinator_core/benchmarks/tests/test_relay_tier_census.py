import json
from pathlib import Path

from coordinator_core.benchmarks import relay_tier_census as rtc
from coordinator_core.distill.prompt_tier import LONG_PROMPT_TIER_TOKENS


def test_row_flags_over_tier_on_both_sides():
    over = rtc._row({"load": 3}, LONG_PROMPT_TIER_TOKENS + 1)
    at = rtc._row({"load": 3}, LONG_PROMPT_TIER_TOKENS)
    assert over["over_tier"] is True and at["over_tier"] is False
    assert over["relays_total"] == 3


def test_measure_reports_each_surface_and_isolates_a_failure(monkeypatch):
    fake = {s: (lambda repo, s=s: rtc._row({"p": 1}, 10)) for s in rtc.SURFACES}

    def boom(repo):
        raise RuntimeError("nope")

    fake["update-docs"] = boom
    monkeypatch.setattr(rtc, "_CENSUS", fake)
    out = rtc.measure(Path("."))
    assert list(out) == list(rtc.SURFACES)
    assert out["update-docs"] == {"error": "RuntimeError: nope"}
    for s in ("distill", "architecture-atlas", "learn-lessons"):
        assert out[s]["relays_by_phase"] == {"p": 1} and out[s]["max_prompt_tokens"] == 10


def test_atlas_census_on_default_gate_config_reports_estimate_analysis_tokens(tmp_path, monkeypatch):
    cfg = tmp_path / rtc.ATLAS_GATE_CONFIG
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"census_buckets": [{"bucketId": "a", "dirs": ["a"]}]}), encoding="utf-8")
    from coordinator_core.architecture_survey import tree_census

    monkeypatch.setattr(tree_census, "build", lambda repo, buckets: {"counts": {"bucketed": 7}})
    row = rtc.census_atlas(tmp_path)
    assert row["file_count"] == 7 and row["bucket_count"] == 1
    assert row["max_prompt_tokens"] == row["per_agent_tokens"]["analysis"] == LONG_PROMPT_TIER_TOKENS
    assert row["over_tier"] is False and row["label"] == "estimate"


def test_atlas_census_reports_the_measured_largest_projected_prompt(tmp_path, monkeypatch):
    cfg = tmp_path / rtc.ATLAS_GATE_CONFIG
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"census_buckets": [{"bucketId": "a", "dirs": ["a"]}]}), encoding="utf-8")
    from coordinator_core.architecture_survey import tree_census

    built = {"counts": {"bucketed": 7}, "censusShapedResults": [{"bucket_id": "a", "projected_tokens": 30_000}]}
    monkeypatch.setattr(tree_census, "build", lambda repo, buckets: built)
    assert rtc.census_atlas(tmp_path)["max_prompt_tokens"] == 30_000


def test_distill_census_measures_the_inlined_path_and_keeps_the_shard_mode_count(tmp_path, monkeypatch):
    from coordinator_core.benchmarks import distill_run_cost as drc

    (tmp_path / "a.md").write_bytes(b"x" * 400)
    (tmp_path / "b.md").write_bytes(b"x" * 400)
    payload = {
        "runId": "r", "repoRoot": str(tmp_path), "wikiDirs": ["docs/wiki"], "wikiSlugs": {},
        "wikiSlugCount": 0, "batch_count": 2, "total_file_count": 2,
        "batches": [
            {"batchId": f"batch-{i}", "files": [f], "description": "d", "formatHints": {}}
            for i, f in ((1, "a.md"), (2, "b.md"))
        ],
    }
    monkeypatch.setattr(drc, "run_scope", lambda repo: {"workflow_input_path": "unused"})
    monkeypatch.setattr(drc, "load_workflow_input", lambda repo, path: payload)

    row = rtc.census_distill(tmp_path)

    assert row["relays_by_phase"]["load"] <= 1
    assert row["load_shard_mode"] == 1 + 2
    assert row["scan_batches"] == 2 and row["max_prompt_tokens"] == 100
