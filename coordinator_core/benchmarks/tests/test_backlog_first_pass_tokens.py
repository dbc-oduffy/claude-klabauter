"""Benchmark: DoE resolves from the machine-local key, refuses naming it, and --baseline pins the base total."""

import json
from pathlib import Path

import pytest

from coordinator_core._claude_klabauter_root import clear_machine_local_cache
from coordinator_core.benchmarks import backlog_first_pass_tokens as bench


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setenv("COORDINATOR_INVOCATION_ORIGIN", "benchmark")
    clear_machine_local_cache()
    yield
    clear_machine_local_cache()


def test_missing_key_is_a_typed_refusal_naming_the_key(monkeypatch, capsys):
    monkeypatch.setattr("coordinator_core._claude_klabauter_root._machine_local_get", lambda key: None)
    with pytest.raises(bench.MissingRepoKey) as exc:
        bench.resolve_doe_repo()
    assert exc.value.key == "repos.doe_claude"
    assert "repos.doe_claude" in str(exc.value)
    assert bench.main([]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["refused"] is True and out["missing_key"] == "repos.doe_claude"


def test_key_naming_no_directory_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr("coordinator_core._claude_klabauter_root._machine_local_get", lambda key: str(tmp_path / "absent"))
    with pytest.raises(bench.MissingRepoKey):
        bench.resolve_doe_repo()


def test_key_resolves_to_directory(monkeypatch, tmp_path):
    monkeypatch.setattr("coordinator_core._claude_klabauter_root._machine_local_get", lambda key: str(tmp_path))
    assert bench.resolve_doe_repo() == tmp_path


def test_baseline_mode_pins_measured_total(monkeypatch, tmp_path, capsys):
    fake = {
        "total_tokens": 1234,
        "scoped_file_count": 7,
    }
    monkeypatch.setattr(bench, "measure_baseline", lambda repo: fake)
    target = tmp_path / "pinned.json"
    assert bench.main(["--baseline", "--repo", str(tmp_path), "--baseline-file", str(target)]) == 0
    pinned = json.loads(target.read_text(encoding="utf-8"))
    assert pinned["base"] == fake
    assert pinned["repo_key"] == "repos.doe_claude"
    assert pinned["bar"] == 1_000_000
    assert pinned["base_over_bar"] is False
    assert json.loads(capsys.readouterr().out)["total_tokens"] == 1234
    assert str(tmp_path) not in target.read_text(encoding="utf-8")


def test_baseline_over_bar_is_recorded():
    assert bench.baseline_record({"total_tokens": 1_000_001})["base_over_bar"] is True
    assert bench.baseline_record({"total_tokens": 1_000_000})["base_over_bar"] is False


def _result(**over):
    base = {"topics": 2, "weeks_walked": 5, "weeks_total": 5, "artifacts_opened": 3, "artifacts_cited": 7,
            "total_tokens": 999, "rag_gaps": [{"form": "doc_outline"}], "_topic_tokens": [100, 200]}
    return {**base, **over}


def _stub(monkeypatch, result, cpu_s=0.1, ms=50.0, spawns=0):
    monkeypatch.setattr(bench, "invoke_triage_pass", lambda src, inv: (result, cpu_s))
    monkeypatch.setattr(bench, "measure_in_process", lambda src: {"process_ms_warm": ms, "spawns": spawns})


def test_price_sums_digest_tokens_plus_per_topic_overhead():
    end = bench.price(_result())
    assert end["digest_tokens"] == 300
    assert end["total_tokens"] == 300 + 2 * bench.SYNTHESIS_OVERHEAD_TOKENS_PER_TOPIC
    assert end["artifacts_not_opened"] == 4 and end["rag_gap_forms"] == ["doc_outline"]


def test_default_mode_passes_and_records_end_beside_base(monkeypatch, tmp_path, capsys):
    _stub(monkeypatch, _result())
    target = tmp_path / "pinned.json"
    target.write_text(json.dumps({"base": {"total_tokens": 5}, "bar": 1}), encoding="utf-8")
    assert bench.main(["--repo", str(tmp_path), "--baseline-file", str(target)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] is True and out["bar"] == 1_000_000 and out["bar_source"] in ("vendored", "pinned-default")
    pinned = json.loads(target.read_text(encoding="utf-8"))
    assert pinned["base"] == {"total_tokens": 5}
    assert pinned["end"]["total_tokens"] == 3300 and pinned["end"]["spawns"] == 0 and pinned["end_passed"] is True


def test_over_bar_fails(monkeypatch, tmp_path):
    _stub(monkeypatch, _result(_topic_tokens=[1_000_000]))
    report = bench.run_default(tmp_path)
    assert report["checks"]["under_bar"] is False and report["passed"] is False


def test_unwalked_week_fails(monkeypatch, tmp_path):
    _stub(monkeypatch, _result(weeks_walked=4))
    report = bench.run_default(tmp_path)
    assert report["checks"]["every_week_walked"] is False and report["passed"] is False


def test_process_time_over_brightline_fails_and_at_it_passes(monkeypatch, tmp_path):
    _stub(monkeypatch, _result(), cpu_s=0.501)
    assert bench.run_default(tmp_path)["checks"]["process_time_within_brightline"] is False
    _stub(monkeypatch, _result(), cpu_s=0.5)
    assert bench.run_default(tmp_path)["checks"]["process_time_within_brightline"] is True


def test_process_time_falls_back_to_warm_in_process_without_rusage(monkeypatch, tmp_path):
    _stub(monkeypatch, _result(), cpu_s=None, ms=900.0)
    report = bench.run_default(tmp_path)
    assert report["end"]["process_time_source"] == "in-process warm" and report["passed"] is False


def test_failing_default_exits_nonzero(monkeypatch, tmp_path):
    _stub(monkeypatch, _result(weeks_walked=1))
    assert bench.main(["--repo", str(tmp_path), "--baseline-file", str(tmp_path / "absent.json")]) == 1


def test_spawn_counter_counts_popen():
    import subprocess
    import sys

    with bench._count_spawns() as spawns:
        subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert len(spawns) == 1


def test_pinned_baseline_file_is_well_formed():
    pinned = json.loads(bench.BASELINE_FILE.read_text(encoding="utf-8"))
    assert pinned["schema"] == bench.SCHEMA
    assert isinstance(pinned["base"]["total_tokens"], int) and pinned["base"]["total_tokens"] > 0
