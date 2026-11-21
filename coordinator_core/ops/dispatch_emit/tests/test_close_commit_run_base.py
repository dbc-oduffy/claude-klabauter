"""The closure trailer names the run's recorded base, not a later halt-record sha the marker carries."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_contract, ask_fanout_contract, halted_run
from coordinator_core.ops.dispatch_emit import halted_run_close_commit as cc

RUN = "warp-x-20261121T000000-abc123"
BASE = "1" * 40
LATER = "2" * 40


def test_closed_trailer_prefers_recorded_run_base_over_marker(tmp_path: Path, monkeypatch) -> None:
    record = tmp_path / ask_contract.RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(record, halted=RUN, sizing_rel=None, run_base_sha=LATER)
    base_file = tmp_path / ask_fanout_contract.run_base_rel(RUN)
    base_file.parent.mkdir(parents=True, exist_ok=True)
    base_file.write_text(BASE, encoding="utf-8")
    seen = {}

    def fake_closing_bases(root, run_id, params, **_):
        seen.update(params)
        return [params["run_base_sha"]]

    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.landing_integrity.closing_bases", fake_closing_bases)
    tail = cc._closed_trailers(tmp_path, RUN, record)
    assert seen["run_base_sha"] == BASE
    assert f"Checkpoint-Run-Closed: {BASE}" in tail and LATER not in tail
