"""Tests of the update-docs ops budget benchmark: output-key pin, fixture shape, export, rtt summary."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.benchmarks import update_docs_ops_budget as bench
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

OP_KEYS = {"route", "reply_code", "process_ms", "spawn_count", "dominant_leg", "wall_ms"}
DOOR_KEYS = {
    "callers", "door_rtt_p50_ms_idle", "door_rtt_p95_ms_idle", "door_rtt_p50_ms_load",
    "door_rtt_p95_ms_load", "failed_idle", "failed_load", "unavailable",
}


def test_measure_op_reports_pinned_keys_through_the_real_door_handler(tmp_path: Path) -> None:
    repo = bench.ensure_ops_fixture(tmp_path, wiki_pages=12)
    result = bench.measure_op("check-citation-integrity", bench.repo_root(), repo, tmp_path / "scratch")
    assert set(result) == OP_KEYS
    assert result["route"] in {"warm", "cold"}
    assert isinstance(result["reply_code"], int) and result["reply_code"] != bench._ERROR_CODE
    assert result["process_ms"] >= 0 and isinstance(result["spawn_count"], int)
    assert result["dominant_leg"]
    assert subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo,
                          capture_output=True, text=True).stdout.strip() == ""
