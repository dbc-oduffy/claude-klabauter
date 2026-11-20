"""The manifest's test_targets are the union of every row's declared test files, whatever the naming shape."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.ask_stage import _test_scope, _wave_test_scopes
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_D = "coordinator_core/ops/dispatch_emit/tests"
_SEVEN = [
    f"{_D}/test_a.py",
    f"{_D}/test_b.py",
    f"{_D}/test_c.py",
    f"{_D}/c_test.py",
    f"{_D}/test_d.py",
    f"{_D}/d_test.py",
    f"{_D}/test_e.py",
]


def _row(rid: str, writes, depends=()) -> WaveRow:
    return WaveRow(rid, rid, "", writes, [], list(depends))


def test_a_seven_file_plan_yields_seven_targets_through_the_default_scope(tmp_path: Path) -> None:
    waves = [
        [_row("C1", ["pkg/one.py", *_SEVEN[:3]]), _row("C2", ["pkg/two.py", *_SEVEN[3:5]])],
        [_row("C3", _SEVEN[5:], depends=["C1"])],
    ]

    targets, no_target = _test_scope(waves, tmp_path)

    assert no_target is None
    assert set(targets) == set(_SEVEN)
    assert len(targets) == 7


def test_a_suffix_named_test_file_is_its_own_target(tmp_path: Path) -> None:
    targets, no_target = _test_scope([[_row("C1", [f"{_D}/checks_test.py"])]], tmp_path)

    assert (targets, no_target) == ((f"{_D}/checks_test.py",), None)


def test_a_suffix_named_test_file_is_not_derived_a_stem_test(tmp_path: Path) -> None:
    targets, _ = _test_scope([[_row("C1", [f"{_D}/checks_test.py", f"{_D}/test_z.py"])]], tmp_path)

    assert targets == (f"{_D}/checks_test.py", f"{_D}/test_z.py")


def test_a_non_test_module_still_maps_to_no_target_when_nothing_covers_it(tmp_path: Path) -> None:
    targets, no_target = _test_scope([[_row("C1", ["pkg/helper.py"])]], tmp_path)

    assert targets == ()
    assert no_target is not None and "pkg/helper.py" in no_target["unmapped_paths"]


def test_the_wave_scopes_agree_with_the_terminal_scope_on_a_suffix_named_test(tmp_path: Path) -> None:
    waves = [[_row("C1", [f"{_D}/x_test.py"])], [_row("C2", [f"{_D}/test_y.py"], depends=["C1"])]]

    scope = _wave_test_scopes(waves, tmp_path)
    assert list(scope.values())[0] == (f"{_D}/x_test.py",)
    assert _test_scope(waves, tmp_path)[0] == (f"{_D}/x_test.py", f"{_D}/test_y.py")
