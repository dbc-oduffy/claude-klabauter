"""publish_path_split: parse both line kinds, gate at 600s, timeout and log modes."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.benchmarks import percolate_round_ab as ab
from coordinator_core.benchmarks import publish_path_split as pps


@pytest.fixture(autouse=True)
def _restore_origin_env(monkeypatch):
    # main() declares benchmark origin in os.environ; monkeypatch restores it.
    from coordinator_core.telemetry import op_latency

    monkeypatch.setenv(op_latency.ORIGIN_ENV, "placeholder")
    monkeypatch.delenv(op_latency.ORIGIN_ENV)


_PUBLISHED = "\nPublished: pushed to /d.\n"


def _fixture(total: float, *, materialise: float = 12.0) -> str:
    return "\n".join(
        [
            f"  [timing] round1: step:commit_clone_materialise: wall={materialise}s cpu=1.0s",
            "  [timing] row-a: stage: 3.5s wall / 2.0s cpu",
            "  [timing] row-a: transform: 4.5s wall / 1.0s cpu",
            "  [timing] round1: step:real_run: wall=40.0s cpu=5.0s",
            "  [timing] round1: step:engine_import_probe: wall=9.0s cpu=1.0s",
            "  [timing] round1: step:push: wall=3.0s cpu=0.5s",
            f"  [timing] round1: step:total: wall={total}s cpu=9.0s",
        ]
    )


def test_parses_both_line_kinds_without_double_counting() -> None:
    parsed = pps.parse_timing_text(_fixture(100.0))
    assert [s["step"] for s in parsed["steps"]] == [
        "commit_clone_materialise", "real_run", "engine_import_probe", "push", "total",
    ]
    assert [p["phase"] for p in parsed["phases"]] == ["stage", "transform"]
    assert parsed["phases"][0]["wall_s"] == 3.5 and parsed["phases"][0]["cpu_s"] == 2.0


def test_gate_pass_at_599_fail_at_601() -> None:
    assert pps.evaluate(pps.parse_timing_text(_fixture(599.0)))["ten_minute_gate"] == "pass"
    assert pps.evaluate(pps.parse_timing_text(_fixture(601.0)))["ten_minute_gate"] == "fail"


def test_total_includes_materialise_when_step_total_excludes_it() -> None:
    parsed = pps.parse_timing_text(_fixture(560.0, materialise=100.0))
    # top-level sum = 100 + 40 + 3 = 143 < 560, so declared total governs
    assert pps.total_wall_s(parsed) == 560.0
    text = (
        "  [timing] r: step:commit_clone_materialise: wall=300.0s cpu=1.0s\n"
        "  [timing] r: step:real_run: wall=250.0s cpu=1.0s\n"
        "  [timing] r: step:push: wall=60.0s cpu=1.0s\n"
        "  [timing] r: step:total: wall=320.0s cpu=1.0s\n"
    )
    assert pps.total_wall_s(pps.parse_timing_text(text)) == 610.0
    assert pps.evaluate(pps.parse_timing_text(text))["ten_minute_gate"] == "fail"


def test_no_step_lines_is_unmeasured_fail() -> None:
    result = pps.evaluate(pps.parse_timing_text("nothing here"))
    assert result["total_wall_s"] is None
    assert result["ten_minute_gate"] == "fail"


def test_nonzero_rc_fails_even_under_budget() -> None:
    parsed = pps.parse_timing_text(_fixture(100.0))
    assert pps.evaluate(parsed, rc=1)["ten_minute_gate"] == "fail"
    assert pps.evaluate(parsed, rc=0)["ten_minute_gate"] == "pass"


def test_from_log_gates_and_main_exit_code(tmp_path, capsys) -> None:
    log = tmp_path / "round.log"
    log.write_text(_fixture(590.0) + _PUBLISHED, encoding="utf-8")
    assert pps.main(["--from-log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "ten_minute_gate: pass" in out and "step:push" in out
    log.write_text(_fixture(700.0) + _PUBLISHED, encoding="utf-8")
    assert pps.main(["--from-log", str(log)]) == 1
    assert "ten_minute_gate: fail" in capsys.readouterr().out


def test_from_log_fails_a_round_that_did_not_publish(tmp_path) -> None:
    log = tmp_path / "round.log"
    log.write_text(_fixture(100.0), encoding="utf-8")
    assert pps.run_from_log(log)["ten_minute_gate"] == "fail"
    log.write_text(
        _fixture(100.0) + "\npercolate-round: push failed:\nrejected\n", encoding="utf-8"
    )
    assert pps.run_from_log(log)["ten_minute_gate"] == "fail"
    log.write_text(_fixture(100.0) + _PUBLISHED, encoding="utf-8")
    assert pps.run_from_log(log)["ten_minute_gate"] == "pass"


def test_timeout_reports_fail_with_legs_seen(monkeypatch) -> None:
    def boom(target, *, scratch_root=None):
        raise subprocess.TimeoutExpired(
            cmd="x", timeout=1800,
            output=b"  [timing] r: step:real_run: wall=40.0s cpu=1.0s\n",
            stderr=b"  [timing] r: step:commit_clone_materialise: wall=2.0s cpu=0.1s\n",
        )

    monkeypatch.setattr(ab, "capture_real_round_phase_breakdown", boom)
    result = pps.run_disposable("t")
    assert result["ten_minute_gate"] == "fail"
    assert result["timed_out"] is True
    assert result["total_wall_s"] >= ab._REAL_ROUND_TIMEOUT_S
    assert {s["step"] for s in result["steps"]} == {"real_run", "commit_clone_materialise"}


def test_disposable_parses_stderr_as_well_as_stdout(monkeypatch) -> None:
    def fake(target, *, scratch_root=None):
        return {
            "rc": 0,
            "stdout": "  [timing] r: step:real_run: wall=40.0s cpu=1.0s\n",
            "stderr": "  [timing] r: step:commit_clone_materialise: wall=2.0s cpu=0.1s\n",
            "dest": "d",
        }

    monkeypatch.setattr(ab, "capture_real_round_phase_breakdown", fake)
    result = pps.run_disposable("t")
    assert result["total_wall_s"] == 42.0
    assert result["ten_minute_gate"] == "pass"


def test_disposable_default_target_is_the_ab_default(monkeypatch, capsys) -> None:
    seen = {}

    def fake(target, *, scratch_root=None):
        seen["target"] = target
        return {"rc": 0, "stdout": "", "stderr": "", "dest": "d"}

    monkeypatch.setattr(ab, "capture_real_round_phase_breakdown", fake)
    assert pps.main([]) == 1
    assert seen["target"] == ab._DEFAULT_TARGET
