"""Replay 1e2761 / b61521: minted-only dirs never shadow a halted run; a resumer that died unstaged leaves it halted."""

import json

from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

SIZING = "state/sizings/2026-10-25-foo.yaml"
STEM = "2026-10-25-foo"


def _dir(root, stamp):
    return root / RECORD_DIR_ROOT / f"warp-{STEM}-{stamp}"


def _shell(root, stamp, stage="minted"):
    d = _dir(root, stamp)
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    (d / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")
    return d


def test_minted_only_newer_dirs_do_not_shadow_a_halted_run(tmp_path):
    halted = _dir(tmp_path, "20261025T120000-aaaaaa")
    hr.write_halt_marker(halted, halted="x", sizing_rel=SIZING)
    _shell(tmp_path, "20261025T125615-1e2761")
    _shell(tmp_path, "20261025T125809-b61521")
    assert hr.halted_run_for(tmp_path, SIZING) == halted.name


def test_refire_over_shell_dirs_resolves_the_halted_run_and_mints_nothing(tmp_path):
    halted = _dir(tmp_path, "20261025T120000-aaaaaa")
    hr.write_halt_marker(halted, halted="x", sizing_rel=SIZING)
    _shell(tmp_path, "20261025T125615-1e2761")
    before = sorted(p.name for p in (tmp_path / RECORD_DIR_ROOT).iterdir())
    assert hr.halted_run_for(tmp_path, SIZING) == halted.name
    assert sorted(p.name for p in (tmp_path / RECORD_DIR_ROOT).iterdir()) == before


def test_a_newer_staged_dir_still_supersedes(tmp_path):
    hr.write_halt_marker(_dir(tmp_path, "20261025T120000-aaaaaa"), halted="x", sizing_rel=SIZING)
    _shell(tmp_path, "20261025T125615-1e2761", stage="staged")
    assert hr.halted_run_for(tmp_path, SIZING) is None


def test_resumed_with_a_dead_unstaged_resumer_is_halted_again(tmp_path):
    halted = _dir(tmp_path, "20261025T120000-aaaaaa")
    hr.write_halt_marker(halted, halted="x", sizing_rel=SIZING)
    hr.mark_resumed(halted)
    _shell(tmp_path, "20261025T125615-1e2761")
    assert hr.halted_run_for(tmp_path, SIZING) == halted.name
    assert json.loads((halted / hr.HALTED_FILE).read_text())["status"] == hr.STATUS_HALTED


def test_resumed_without_a_newer_dir_or_with_a_staged_one_stays_resumed(tmp_path):
    halted = _dir(tmp_path, "20261025T120000-aaaaaa")
    hr.write_halt_marker(halted, halted="x", sizing_rel=SIZING)
    hr.mark_resumed(halted)
    assert hr.halted_run_for(tmp_path, SIZING) is None
    _shell(tmp_path, "20261025T125615-1e2761", stage="staged")
    assert hr.halted_run_for(tmp_path, SIZING) is None
    assert json.loads((halted / hr.HALTED_FILE).read_text())["status"] == hr.STATUS_RESUMED


def test_landed_is_never_reopened(tmp_path):
    d = _dir(tmp_path, "20261025T120000-aaaaaa")
    hr.write_halt_marker(d, halted="x", sizing_rel=SIZING)
    hr.mark_landed(d, "a" * 40)
    _shell(tmp_path, "20261025T125615-1e2761")
    assert hr.halted_run_for(tmp_path, SIZING) is None
