"""`halted_run.bound_runs`: the shared sizing -> run dirs index, written through by the run.json and halted.json writers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

SIZING = "state/sizings/2026-11/2026-11-18-target.yaml"
OTHER = "state/sizings/2026-11/2026-11-18-other.yaml"
STEM = "2026-11-18-target"


def _ask(root: Path, n: int) -> Path:
    return root / RECORD_DIR_ROOT / f"ask-20261120T{n // 3600 % 24:02d}{n // 60 % 60:02d}{n % 60:02d}-{n:06x}"


def _warp(root: Path, stamp: str = "20261120T010000-aaaaaa") -> Path:
    return root / RECORD_DIR_ROOT / f"warp-{STEM}-{stamp}"


def _raw(d: Path, name: str, sizing: str, **extra: object) -> None:
    """A record file written straight to disk, as a run that predates the index would hold it."""
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"sizing_rel": sizing, **extra}), encoding="utf-8")


def _unrelated(root: Path, count: int) -> None:
    for n in range(count):
        _raw(_ask(root, n), "run.json", OTHER)
        if n % 2:
            _raw(_ask(root, n), hr.HALTED_FILE, OTHER, status="halted", halted="x")


def _opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Names of every `run.json` / `halted.json` read through `Path.read_text`."""
    seen: list[str] = []
    real = Path.read_text

    def spy(self: Path, *a: object, **k: object) -> str:
        if self.name in ("run.json", hr.HALTED_FILE):
            seen.append(f"{self.parent.name}/{self.name}")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    return seen


def test_a_run_bound_by_run_json_and_a_run_bound_only_by_halted_json_both_appear(tmp_path: Path) -> None:
    by_run, by_marker = _warp(tmp_path), _ask(tmp_path, 7)
    rl._write_run(by_run, {"sizing_rel": SIZING})
    hr.write_halt_marker(by_marker, halted="ledger-check", sizing_rel=SIZING)
    got = hr.bound_runs(tmp_path, SIZING)
    assert set(got) == {by_run.name, by_marker.name}
    assert got[by_run.name] == {}
    assert got[by_marker.name]["halted"] == "ledger-check"
    assert got[by_marker.name]["status"] == hr.STATUS_HALTED


def test_a_run_of_another_sizing_does_not_appear(tmp_path: Path) -> None:
    rl._write_run(_ask(tmp_path, 1), {"sizing_rel": OTHER})
    hr.write_halt_marker(_ask(tmp_path, 2), halted="x", sizing_rel=OTHER)
    rl._write_run(_warp(tmp_path), {"sizing_rel": SIZING})
    assert set(hr.bound_runs(tmp_path, SIZING)) == {_warp(tmp_path).name}


def test_a_filled_index_opens_no_run_record_of_an_unrelated_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _unrelated(tmp_path, 300)
    rl._write_run(_warp(tmp_path), {"sizing_rel": SIZING})
    halted = _ask(tmp_path, 900)
    hr.write_halt_marker(halted, halted="ledger-check", sizing_rel=SIZING)
    hr.bound_runs(tmp_path, SIZING)  # first call back-fills
    opens = _opens(monkeypatch)
    got = hr.bound_runs(tmp_path, SIZING)
    assert set(got) == {_warp(tmp_path).name, halted.name}
    assert opens == []


def test_runs_that_predate_the_index_are_found_once_and_then_served_from_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unrelated(tmp_path, 40)
    old_run, old_halt = _warp(tmp_path), _ask(tmp_path, 500)
    _raw(old_run, "run.json", SIZING)
    _raw(old_halt, hr.HALTED_FILE, SIZING, status="halted", halted="ledger-check", ignored="not indexed")
    assert not (tmp_path / hr.LOOKUP_INDEX_DIR).exists()
    got = hr.bound_runs(tmp_path, SIZING)
    assert set(got) == {old_run.name, old_halt.name}
    assert got[old_halt.name] == {"status": "halted", "halted": "ledger-check"}
    opens = _opens(monkeypatch)
    assert set(hr.bound_runs(tmp_path, SIZING)) == {old_run.name, old_halt.name}
    assert opens == []


def test_the_back_fill_finds_exactly_the_bound_candidates(tmp_path: Path) -> None:
    _unrelated(tmp_path, 30)
    _raw(_warp(tmp_path), "run.json", SIZING)
    _raw(_ask(tmp_path, 600), hr.HALTED_FILE, SIZING, status="halted", halted="x")
    _raw(_ask(tmp_path, 601), "run.json", SIZING)
    # An ask run stamped before the sizing's date is never a candidate.
    early = tmp_path / RECORD_DIR_ROOT / "ask-20261101T010000-bbbbbb"
    _raw(early, "run.json", SIZING)
    assert set(hr.bound_runs(tmp_path, SIZING)) == {_warp(tmp_path).name, _ask(tmp_path, 600).name, _ask(tmp_path, 601).name}


def test_halting_then_releasing_a_run_updates_its_halt_fields(tmp_path: Path) -> None:
    d = _ask(tmp_path, 3)
    rl._write_run(d, {"sizing_rel": SIZING})
    assert hr.bound_runs(tmp_path, SIZING)[d.name] == {}
    hr.write_halt_marker(d, halted="ledger-check", sizing_rel=SIZING)
    assert hr.bound_runs(tmp_path, SIZING)[d.name]["status"] == hr.STATUS_HALTED
    assert hr.record_released(d, "a" * 40, "landed elsewhere")
    got = hr.bound_runs(tmp_path, SIZING)[d.name]
    assert got["released"] == {"reason": "landed elsewhere", "sha": "a" * 40}
    assert hr.mark_closed(d, "gone", "2026-11-22T00:00:00Z")
    assert hr.bound_runs(tmp_path, SIZING)[d.name]["status"] == hr.STATUS_CLOSED


def test_a_run_rebound_to_another_sizing_leaves_the_first_key(tmp_path: Path) -> None:
    d = _warp(tmp_path)
    rl._write_run(d, {"sizing_rel": SIZING})
    assert d.name in hr.bound_runs(tmp_path, SIZING)
    rl._write_run(d, {"sizing_rel": OTHER}, was=SIZING)
    assert d.name not in hr.bound_runs(tmp_path, SIZING)
    assert d.name not in hr.bound_runs(tmp_path, OTHER)  # a warp dir of another stem is not that sizing's candidate


def test_a_rebound_run_whose_marker_still_binds_the_first_sizing_stays_under_it(tmp_path: Path) -> None:
    d = _ask(tmp_path, 4)
    hr.write_halt_marker(d, halted="x", sizing_rel=SIZING)
    rl._write_run(d, {"sizing_rel": SIZING})
    rl._write_run(d, {"sizing_rel": OTHER}, was=SIZING)
    assert d.name in hr.bound_runs(tmp_path, SIZING)
    assert d.name in hr.bound_runs(tmp_path, OTHER)


def test_the_halted_lookup_still_answers_the_halted_runs_alone(tmp_path: Path) -> None:
    halted, plain = _ask(tmp_path, 5), _ask(tmp_path, 6)
    hr.write_halt_marker(halted, halted="x", sizing_rel=SIZING)
    rl._write_run(plain, {"sizing_rel": SIZING})
    assert hr._indexed_runs(tmp_path, SIZING) == {halted.name}
    assert set(hr.bound_runs(tmp_path, SIZING)) == {halted.name, plain.name}


def test_an_undated_sizing_answers_nothing(tmp_path: Path) -> None:
    rl._write_run(_ask(tmp_path, 1), {"sizing_rel": "state/sizings/undated.yaml"})
    assert hr.bound_runs(tmp_path, "state/sizings/undated.yaml") == {}


def test_a_deleted_run_dir_drops_out_of_the_answer(tmp_path: Path) -> None:
    import shutil

    d = _warp(tmp_path)
    rl._write_run(d, {"sizing_rel": SIZING})
    assert d.name in hr.bound_runs(tmp_path, SIZING)
    shutil.rmtree(d)
    assert hr.bound_runs(tmp_path, SIZING) == {}
