"""The submit slow-disk profile charges repo-root I/O and names the sites before the answer."""

from __future__ import annotations

import os

import pytest

from coordinator_core.benchmarks import submit_slow_disk_profile as prof


def test_charger_counts_repo_io_and_skips_outside(tmp_path):
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    charger = prof.Charger(root, 2.0)
    with charger.active():
        (root / "sub" / "a.txt").write_text("a", encoding="utf-8")
        (root / "sub" / "a.txt").read_text(encoding="utf-8")
        with os.scandir(root):
            pass
        outside.read_text(encoding="utf-8")
    kinds = sorted(s["kind"] for s in charger.report())
    assert kinds == ["read", "scandir", "write"]
    assert charger.waited_ms == 3 * 2000.0
    assert [s["bulk"] for s in charger.report() if s["kind"] == "scandir"] == [True]


def test_charger_restores_patched_seams(tmp_path):
    import builtins

    real = (builtins.open, os.scandir)
    with prof.Charger(tmp_path, 1.0).active():
        assert builtins.open is not real[0]
    assert (builtins.open, os.scandir) == real


@pytest.mark.spawns_process
def test_default_ask_reports_answer_run_id_and_ordered_sites():
    result = prof.measure(2.0)
    assert result["answer_kind"] == "allow", result["reason"]
    assert result["run_id"]
    assert result["injected_wait_ms"] == 2000.0 * sum(s["count"] for s in result["charged_sites"])
    assert result["pre_answer_ms"] == round(result["injected_wait_ms"] + result["process_ms"], 1)
    # The premint answer writes one run-token record; the state/warp directory walk is off the pre-answer path.
    assert not any(s["kind"] == "scandir" for s in result["charged_sites"])
    assert any(s["kind"] == "write" for s in result["charged_sites"])
    assert result["bulk_charged"] == 0
    assert "[BULK]" not in prof.render(result)
