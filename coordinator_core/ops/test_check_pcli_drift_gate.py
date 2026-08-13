"""Tests for coordinator_core.ops.check_pcli_drift_gate.

Fixtures are synthetic, written into tmp_path — the boundary tests must not
depend on the live coordinator-claude clone. See that module's docstring for the
granularity/staleness-window reasoning these tests hold to a fixed shape.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

import pytest

from coordinator_core.ops import check_pcli_drift_gate as gate


# ---------------------------------------------------------------------------
# Leg 1 — contract-vs-capture drift (pure predicate)
# ---------------------------------------------------------------------------

_MATCHING_CONTRACT_PROPS = set(gate._MIRRORED.keys()) | gate._CONTRACT_ONLY
_MATCHING_CAPTURE_OPTS = set(gate._MIRRORED.values()) | gate._CAPTURE_ONLY


def test_mirrored_contract_only_capture_only_literal_content():
    # Ground truth captured by hand this session — not derived from
    # gate._MIRRORED/_CONTRACT_ONLY/_CAPTURE_ONLY, unlike the fixtures
    # above. A wrong edit to those tables must fail this test even though
    # it would leave the derived fixtures and their assertions self-
    # consistent (review-integrator finding #3, coordinatorcode-reviewer-e234de67.md).
    assert gate._MIRRORED == {
        "label": "label",
        "agent_type": "agentType",
        "model": "model",
        "effort": "effort",
        "schema_ref": "schema",
        "phase": "phase",
    }
    assert gate._CONTRACT_ONLY == {"brief_ref", "gate_kind", "write_files", "est_min"}
    assert gate._CAPTURE_ONLY == {"isolation"}


def test_drift_clean_on_matching_fixture():
    reasons = gate.compute_contract_capture_drift(_MATCHING_CONTRACT_PROPS, _MATCHING_CAPTURE_OPTS)
    assert reasons == []


def test_drift_fires_on_new_undeclared_capture_key():
    capture_opts = set(_MATCHING_CAPTURE_OPTS) | {"newLiveOption"}
    reasons = gate.compute_contract_capture_drift(_MATCHING_CONTRACT_PROPS, capture_opts)
    assert any("newLiveOption" in r for r in reasons)


def test_drift_fires_on_new_undeclared_contract_key():
    contract_props = set(_MATCHING_CONTRACT_PROPS) | {"new_contract_field"}
    reasons = gate.compute_contract_capture_drift(contract_props, _MATCHING_CAPTURE_OPTS)
    assert any("new_contract_field" in r for r in reasons)


def test_drift_fires_on_missing_mirrored_contract_key():
    contract_props = _MATCHING_CONTRACT_PROPS - {"label"}
    reasons = gate.compute_contract_capture_drift(contract_props, _MATCHING_CAPTURE_OPTS)
    assert any("label" in r and "missing from dispatch_feed.properties" in r for r in reasons)


def test_drift_fires_on_missing_mirrored_capture_key():
    capture_opts = _MATCHING_CAPTURE_OPTS - {"agentType"}
    reasons = gate.compute_contract_capture_drift(_MATCHING_CONTRACT_PROPS, capture_opts)
    assert any("agentType" in r and "missing from capture opts_fields" in r for r in reasons)


# ---------------------------------------------------------------------------
# Leg 2 — staleness (pure predicate, boundary-tested)
# ---------------------------------------------------------------------------


def test_staleness_zero_at_13_days():
    result = gate.compute_staleness(
        "2026-08-01", "workflow-tool-api-capture.2026-08-01.json", today=date(2026, 8, 14)
    )
    assert result["verdict"] == "FRESH"
    assert result["reasons"] == []


def test_staleness_nonzero_at_15_days():
    result = gate.compute_staleness(
        "2026-08-01", "workflow-tool-api-capture.2026-08-01.json", today=date(2026, 8, 16)
    )
    assert result["verdict"] == "STALE"
    assert result["reasons"]


def test_staleness_message_names_applied_threshold():
    result = gate.compute_staleness(
        "2026-08-01", "workflow-tool-api-capture.2026-08-01.json", today=date(2026, 8, 16)
    )
    assert result["threshold_days"] == 14
    assert any("threshold_days=14" in r for r in result["reasons"])


def test_staleness_filename_captured_at_mismatch_fails():
    result = gate.compute_staleness(
        "2026-08-01", "workflow-tool-api-capture.2026-08-02.json", today=date(2026, 8, 5)
    )
    assert result["reasons"]
    assert any("disagrees with captured_at" in r for r in result["reasons"])


def test_staleness_max_age_days_shortens_window():
    # 7-day capture override: fresh at 7, fires at 8.
    fresh = gate.compute_staleness(
        "2026-08-01",
        "workflow-tool-api-capture.2026-08-01.json",
        max_age_days_capture=7,
        today=date(2026, 8, 8),
    )
    assert fresh["verdict"] == "FRESH"
    assert fresh["threshold_days"] == 7

    stale = gate.compute_staleness(
        "2026-08-01",
        "workflow-tool-api-capture.2026-08-01.json",
        max_age_days_capture=7,
        today=date(2026, 8, 9),
    )
    assert stale["verdict"] == "STALE"
    assert stale["threshold_days"] == 7


def test_staleness_max_age_days_lengthening_is_ignored():
    # 30-day capture override is ignored — still fires at 15 (14-day authority).
    result = gate.compute_staleness(
        "2026-08-01",
        "workflow-tool-api-capture.2026-08-01.json",
        max_age_days_capture=30,
        today=date(2026, 8, 16),
    )
    assert result["verdict"] == "STALE"
    assert result["threshold_days"] == 14


# ---------------------------------------------------------------------------
# Leg 3 — C7 hash drift
# ---------------------------------------------------------------------------


def test_hash_drift_clean_on_matching_files(tmp_path):
    (tmp_path / "sub").mkdir()
    target = tmp_path / "sub" / "file.md"
    target.write_text("hello world", encoding="utf-8")
    digest = hashlib.sha256(b"hello world").hexdigest()
    reasons = gate.compute_hash_drift(tmp_path, "sha256", {"sub/file.md": digest})
    assert reasons == []


def test_hash_drift_fires_on_mismatch(tmp_path):
    target = tmp_path / "file.md"
    target.write_text("hello world", encoding="utf-8")
    reasons = gate.compute_hash_drift(tmp_path, "sha256", {"file.md": "0" * 64})
    assert any("hash mismatch" in r for r in reasons)


def test_hash_drift_raises_gate_error_on_unsupported_algorithm(tmp_path):
    with pytest.raises(gate.GateError):
        gate.compute_hash_drift(tmp_path, "not-a-real-algorithm", {})


# ---------------------------------------------------------------------------
# run_gate — full integration over a synthetic coordinator-claude-clone-shaped tmp_path tree
# ---------------------------------------------------------------------------


def _write_contract(schemas_dir: Path, extra_props: dict | None = None) -> None:
    props = {key: {"type": "string"} for key in gate._MIRRORED}
    for key in gate._CONTRACT_ONLY:
        props[key] = {"type": "string"}
    if extra_props:
        props.update(extra_props)
    contract = {
        "properties": {
            "dispatch_feed": {
                "type": ["object", "null"],
                "properties": props,
            }
        }
    }
    (schemas_dir / "run-report.schema.json").write_text(json.dumps(contract), encoding="utf-8")


def _write_capture(
    schemas_dir: Path,
    *,
    captured_at: str = "2026-08-01",
    filename_date: str = "2026-08-01",
    extra_opts: dict | None = None,
    max_age_days: int | None = None,
) -> None:
    opts_fields = {value: f"prose for {value}" for value in gate._MIRRORED.values()}
    for key in gate._CAPTURE_ONLY:
        opts_fields[key] = f"prose for {key}"
    if extra_opts:
        opts_fields.update(extra_opts)
    capture = {
        "captured_at": captured_at,
        "script_globals": {"agent": {"opts_fields": opts_fields}},
    }
    if max_age_days is not None:
        capture["max_age_days"] = max_age_days
    (schemas_dir / f"workflow-tool-api-capture.{filename_date}.json").write_text(
        json.dumps(capture), encoding="utf-8"
    )


def _write_resolution(doe_root: Path, schemas_dir: Path, source_hashes: dict[str, str] | None = None) -> None:
    if source_hashes is None:
        tracked = doe_root / "coordinator" / "tracked.md"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("tracked content", encoding="utf-8")
        source_hashes = {
            "coordinator/tracked.md": hashlib.sha256(b"tracked content").hexdigest()
        }
    resolution = {"hash_algorithm": "sha256", "source_hashes": source_hashes}
    (schemas_dir / "subagent-catering-resolution.json").write_text(
        json.dumps(resolution), encoding="utf-8"
    )


def _doe_root(tmp_path: Path) -> tuple[Path, Path]:
    doe_root = tmp_path / "coordinator-claude"
    schemas_dir = doe_root / "coordinator" / "schemas"
    schemas_dir.mkdir(parents=True)
    return doe_root, schemas_dir


def test_run_gate_clean_on_matching_fixture(tmp_path):
    doe_root, schemas_dir = _doe_root(tmp_path)
    _write_contract(schemas_dir)
    _write_capture(schemas_dir, captured_at="2026-08-05", filename_date="2026-08-05")
    _write_resolution(doe_root, schemas_dir)

    lines = gate.run_gate(doe_root, today=date(2026, 8, 10))
    assert lines == []


def test_run_gate_nonzero_on_drifted_capture(tmp_path):
    doe_root, schemas_dir = _doe_root(tmp_path)
    _write_contract(schemas_dir)
    _write_capture(
        schemas_dir,
        captured_at="2026-08-05",
        filename_date="2026-08-05",
        extra_opts={"brandNewOption": "prose"},
    )
    _write_resolution(doe_root, schemas_dir)

    lines = gate.run_gate(doe_root, today=date(2026, 8, 10))
    assert any("LEG 1" in line for line in lines)
    assert any("brandNewOption" in line for line in lines)


def test_run_gate_nonzero_on_filename_captured_at_mismatch(tmp_path):
    doe_root, schemas_dir = _doe_root(tmp_path)
    _write_contract(schemas_dir)
    _write_capture(schemas_dir, captured_at="2026-08-05", filename_date="2026-08-06")
    _write_resolution(doe_root, schemas_dir)

    lines = gate.run_gate(doe_root, today=date(2026, 8, 10))
    assert any("LEG 2" in line for line in lines)
    assert any("disagrees with captured_at" in line for line in lines)


def test_run_gate_nonzero_on_source_hash_mismatch(tmp_path):
    doe_root, schemas_dir = _doe_root(tmp_path)
    _write_contract(schemas_dir)
    _write_capture(schemas_dir, captured_at="2026-08-05", filename_date="2026-08-05")
    _write_resolution(doe_root, schemas_dir, source_hashes={"coordinator/missing.md": "0" * 64})

    lines = gate.run_gate(doe_root, today=date(2026, 8, 10))
    assert any("LEG 3" in line for line in lines)


def test_run_gate_clock_ignores_backdated_mtime(tmp_path):
    """Backdating the capture file's mtime must not change the verdict — the
    gate clocks off captured_at, never mtime."""
    doe_root, schemas_dir = _doe_root(tmp_path)
    _write_contract(schemas_dir)
    _write_capture(schemas_dir, captured_at="2026-08-05", filename_date="2026-08-05")
    _write_resolution(doe_root, schemas_dir)

    capture_path = schemas_dir / "workflow-tool-api-capture.2026-08-05.json"
    old_time = date(2000, 1, 1).toordinal() * 86400
    os.utime(capture_path, (old_time, old_time))

    lines = gate.run_gate(doe_root, today=date(2026, 8, 10))
    assert lines == []


def test_run_gate_raises_gate_error_on_missing_source_hashes(tmp_path):
    doe_root, schemas_dir = _doe_root(tmp_path)
    _write_contract(schemas_dir)
    _write_capture(schemas_dir, captured_at="2026-08-05", filename_date="2026-08-05")
    resolution = {"hash_algorithm": "sha256"}
    (schemas_dir / "subagent-catering-resolution.json").write_text(
        json.dumps(resolution), encoding="utf-8"
    )

    with pytest.raises(gate.GateError):
        gate.run_gate(doe_root, today=date(2026, 8, 10))


def test_run_gate_raises_gate_error_on_empty_source_hashes(tmp_path):
    doe_root, schemas_dir = _doe_root(tmp_path)
    _write_contract(schemas_dir)
    _write_capture(schemas_dir, captured_at="2026-08-05", filename_date="2026-08-05")
    _write_resolution(doe_root, schemas_dir, source_hashes={})

    with pytest.raises(gate.GateError):
        gate.run_gate(doe_root, today=date(2026, 8, 10))


def test_run_gate_raises_gate_error_on_non_mapping_source_hashes(tmp_path):
    doe_root, schemas_dir = _doe_root(tmp_path)
    _write_contract(schemas_dir)
    _write_capture(schemas_dir, captured_at="2026-08-05", filename_date="2026-08-05")
    resolution = {"hash_algorithm": "sha256", "source_hashes": ["not", "a", "mapping"]}
    (schemas_dir / "subagent-catering-resolution.json").write_text(
        json.dumps(resolution), encoding="utf-8"
    )

    with pytest.raises(gate.GateError):
        gate.run_gate(doe_root, today=date(2026, 8, 10))


def test_hash_drift_raises_gate_error_on_path_traversal(tmp_path):
    (tmp_path / "sub").mkdir()
    outside = tmp_path.parent / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    digest = hashlib.sha256(b"secret").hexdigest()
    with pytest.raises(gate.GateError):
        gate.compute_hash_drift(tmp_path / "sub", "sha256", {"../../outside.md": digest})


def test_load_json_raises_gate_error_on_non_utf8(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(gate.GateError):
        gate._load_json(bad)


def test_run_gate_missing_dispatch_feed_schema_raises_gate_error(tmp_path):
    doe_root, schemas_dir = _doe_root(tmp_path)
    _write_capture(schemas_dir)
    _write_resolution(doe_root, schemas_dir)

    with pytest.raises(gate.GateError):
        gate.run_gate(doe_root, today=date(2026, 8, 10))
