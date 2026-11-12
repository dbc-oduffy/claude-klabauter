"""Census over the checked-in transcript excerpt: phase attribution, dedupe, dispatches, CLI shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks import ceremony_token_census as census_mod

FIXTURE = Path(__file__).parent / "fixtures" / "ceremony_transcript_excerpt.jsonl"


def _by_phase(result: dict) -> dict:
    return {p["phase"]: p for p in result["phases"]}


def test_phases_attributed_and_sidechain_summed():
    result = census_mod.census(FIXTURE)
    phases = _by_phase(result)
    assert list(phases) == ["command:update-docs", "Phase 2", "skill:distill"]
    first = phases["command:update-docs"]
    # msg_1 counted once despite the repeated line; the sidechain line is summed in.
    assert (first["input_tokens"], first["output_tokens"]) == (15, 150)
    assert (first["cache_read_tokens"], first["cache_creation_tokens"]) == (1200, 500)
    assert first["dispatches"] == 1
    assert phases["Phase 2"]["dispatches"] == 1
    assert phases["skill:distill"]["dispatches"] == 1  # Skill counts, Bash does not
    assert phases["skill:distill"]["output_tokens"] == 700


def test_totals_equal_phase_sums():
    result = census_mod.census(FIXTURE)
    assert result["ceremony"] == "update-docs"
    assert result["totals"]["input_tokens"] == 105
    assert result["totals"]["dispatches"] == 3
    for field in ("input_tokens", "output_tokens", "cache_read_tokens", "dispatches"):
        assert result["totals"][field] == sum(p[field] for p in result["phases"])


def test_subagent_files_land_in_the_phase_holding_their_timestamp(tmp_path):
    sub = tmp_path / "subagents"
    sub.mkdir()
    line = {
        "type": "assistant",
        "timestamp": "2026-11-12T10:01:30.000Z",
        "message": {"id": "msg_sub", "usage": {"input_tokens": 7, "output_tokens": 70}, "content": []},
    }
    (sub / "agent-a.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    phases = _by_phase(census_mod.census(FIXTURE, sub))
    assert phases["Phase 2"]["input_tokens"] == 27
    assert phases["command:update-docs"]["input_tokens"] == 15


def test_cli_prints_contract_shape(capsys):
    assert census_mod.main(["--transcript", str(FIXTURE)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"ceremony", "phases", "totals"}
    assert set(out["phases"][0]) >= {"phase", "input_tokens", "output_tokens", "cache_read_tokens", "dispatches"}


def test_transcript_is_required():
    with pytest.raises(SystemExit):
        census_mod.main([])


def test_missing_transcript_refused(tmp_path, capsys):
    assert census_mod.main(["--transcript", str(tmp_path / "nope.jsonl")]) == 2
    assert "transcript not found" in capsys.readouterr().err
