"""Tests for the sizing and warp run-record evidence readers."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.baton_close_evidence_records import run_record_evidence, sizing_evidence

BATON = "state/handoffs/2026-10-01-a.md"
SIZING = "state/sizings/2026-10-01-a.yaml"


def _sizing(root: Path, status: str, archived: bool = False) -> None:
    d = root / ("archive/sizings/2026-10" if archived else "state/sizings")
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-10-01-a.yaml").write_text(f"status: {status}\n", encoding="utf-8")


def _run(root: Path, name: str, run: dict | None, files: tuple[str, ...] = (), baton: str | None = None):
    d = root / "state" / "warp" / name
    d.mkdir(parents=True)
    if run is not None:
        (d / "run.json").write_text(json.dumps(run), encoding="utf-8")
    for f in files:
        (d / f).write_text("{}", encoding="utf-8")
    if baton:
        (d / "run-baton.json").write_text(json.dumps({"baton": baton}), encoding="utf-8")


def _kinds(ev: dict, key: str = BATON) -> list[str]:
    return [e["kind"] for e in ev.get(key, [])]


def test_sizing_shipped_is_strong_terminal(tmp_path):
    _sizing(tmp_path, "shipped")
    ev = sizing_evidence(tmp_path, {BATON: {"sizing_object": SIZING}})[BATON][0]
    assert (ev["kind"], ev["reads"], ev["strength"], ev["proposes"]) == (
        "SIZING_TERMINAL", "terminal", "strong", "shipped")
    assert ev["source"] == SIZING


def test_sizing_declined_is_weak_abandoned(tmp_path):
    _sizing(tmp_path, "declined")
    ev = sizing_evidence(tmp_path, {BATON: {"sizing_object": SIZING}})[BATON][0]
    assert (ev["kind"], ev["strength"], ev["proposes"]) == ("SIZING_DECLINED", "weak", "abandoned")


def test_sizing_open_status_yields_nothing(tmp_path):
    _sizing(tmp_path, "draft")
    assert sizing_evidence(tmp_path, {BATON: {"sizing_object": SIZING}}) == {}


def test_sizing_missing(tmp_path):
    ev = sizing_evidence(tmp_path, {BATON: {"sizing_object": SIZING}})[BATON][0]
    assert (ev["kind"], ev["reads"], ev["proposes"]) == ("SIZING_MISSING", "live", None)


def test_sizing_archive_fallback(tmp_path):
    _sizing(tmp_path, "shipped", archived=True)
    ev = sizing_evidence(tmp_path, {BATON: {"sizing_object": SIZING}})[BATON][0]
    assert ev["kind"] == "SIZING_TERMINAL"
    assert ev["source"] == "archive/sizings/2026-10/2026-10-01-a.yaml"


def test_backslash_sizing_object_normalises(tmp_path):
    _sizing(tmp_path, "shipped")
    ev = sizing_evidence(tmp_path, {BATON: {"sizing_object": "state\\sizings\\2026-10-01-a.yaml"}})
    assert _kinds(ev) == ["SIZING_TERMINAL"]
    assert "\\" not in ev[BATON][0]["source"]


def test_baton_without_sizing_object_is_skipped(tmp_path):
    assert sizing_evidence(tmp_path, {BATON: {}}) == {}


def test_run_committed_via_run_baton(tmp_path):
    _run(tmp_path, "r1", {}, ("run-outcome.json",), baton=BATON)
    ev = run_record_evidence(tmp_path, {BATON: {}})[BATON][0]
    assert (ev["kind"], ev["reads"], ev["strength"], ev["proposes"]) == (
        "RUN_COMMITTED", "terminal", "strong", "shipped")
    assert ev["source"] == "state/warp/r1/run.json"


def test_run_halted_wins_over_outcome(tmp_path):
    _run(tmp_path, "r1", {}, ("run-outcome.json", "halted.json"), baton=BATON)
    ev = run_record_evidence(tmp_path, {BATON: {}})[BATON]
    assert [e["kind"] for e in ev] == ["RUN_HALTED"]
    assert (ev[0]["reads"], ev[0]["strength"], ev[0]["proposes"]) == ("live", "strong", None)


def test_run_named_by_sizing_rel(tmp_path):
    _run(tmp_path, "r1", {"sizing_rel": SIZING}, ("run-outcome.json",))
    assert _kinds(run_record_evidence(tmp_path, {BATON: {"sizing_object": SIZING}})) == ["RUN_COMMITTED"]


def test_run_in_flight_contributes_nothing(tmp_path):
    _run(tmp_path, "r1", {"sizing_rel": SIZING})
    assert run_record_evidence(tmp_path, {BATON: {"sizing_object": SIZING}}) == {}


def test_malformed_run_json_is_skipped(tmp_path):
    _run(tmp_path, "r1", None, ("run-outcome.json",))
    (tmp_path / "state/warp/r1/run.json").write_text("{not json", encoding="utf-8")
    assert run_record_evidence(tmp_path, {BATON: {"sizing_object": SIZING}}) == {}


def test_run_naming_another_baton_contributes_nothing(tmp_path):
    _run(tmp_path, "r1", {"sizing_rel": "state/sizings/other.yaml"}, ("run-outcome.json",),
         baton="state/handoffs/other.md")
    assert run_record_evidence(tmp_path, {BATON: {"sizing_object": SIZING}}) == {}


def test_no_warp_home(tmp_path):
    assert run_record_evidence(tmp_path, {BATON: {}}) == {}
