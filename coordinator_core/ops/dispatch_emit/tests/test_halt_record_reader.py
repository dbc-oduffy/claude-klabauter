"""halted_run_id and read_halt_record read a ledger-check halt's committed run record."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit.ask_contract import (
    halted_record_message,
    halted_run_id,
    read_halt_record,
)

SIZING = "state/sizings/s.yaml"


def _record(root, run_id, halted=None, manifest=None):
    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True)
    if halted is not None:
        (d / "halted.json").write_text(json.dumps(halted), encoding="utf-8")
    if manifest is not None:
        (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


LEDGER = {"status": "halted", "halted": "ledger-check", "sizing_rel": SIZING}
MANIFEST = {"rows": [{"id": "C1"}], "owed": [{"id": "V1"}], "gated": [{"id": "G1"}]}


def test_subject_parses_with_and_without_process_files_suffix():
    subject = halted_record_message("run-1", {"halted": "x"}).splitlines()[0]
    assert halted_run_id(subject) == "run-1"
    assert halted_run_id(subject + " (process files)") == "run-1"
    assert halted_run_id("C1: something else") is None
    assert halted_run_id("warp: record halted ../x run artifacts") is None


def test_staged_covers_rows_owed_and_gated(tmp_path):
    _record(tmp_path, "run-1", {**LEDGER, "plan": "docs/plans/p.md", "blocking": ["R1"]}, MANIFEST)
    rec = read_halt_record(tmp_path, "run-1")
    assert rec.staged == {"C1", "V1", "G1"}
    assert rec.plan_rel == "docs/plans/p.md" and rec.sizing_rel == SIZING
    assert rec.blocking == ("R1",)


def test_blocking_and_plan_none_when_halted_json_lacks_them(tmp_path):
    _record(tmp_path, "run-1", LEDGER, MANIFEST)
    rec = read_halt_record(tmp_path, "run-1")
    assert rec.blocking is None and rec.plan_rel is None


def test_empty_owed_and_gated_lists_still_read(tmp_path):
    _record(tmp_path, "run-1", LEDGER, {"rows": [{"id": "C1"}], "owed": [], "gated": []})
    assert read_halt_record(tmp_path, "run-1").staged == {"C1"}


def test_missing_manifest_answers_none(tmp_path):
    _record(tmp_path, "run-1", LEDGER)
    assert read_halt_record(tmp_path, "run-1") is None


def test_non_ledger_halt_answers_none(tmp_path):
    _record(tmp_path, "run-1", {**LEDGER, "halted": "refusal"}, MANIFEST)
    assert read_halt_record(tmp_path, "run-1") is None


def test_unsafe_run_id_answers_none(tmp_path):
    assert read_halt_record(tmp_path, "../escape") is None


def test_malformed_blocking_is_none(tmp_path):
    _record(tmp_path, "run-1", {**LEDGER, "blocking": [1]}, MANIFEST)
    assert read_halt_record(tmp_path, "run-1").blocking is None
