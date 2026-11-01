"""`land_gate_reach_profile`: the `--bar-ms` exit logic and the cProfile attribution helper, on synthetic
rows (no clone)."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import land_gate_reach_profile as lgrp


def _rows(measured_ms: float, land_ms: float, added: int = 0) -> list:
    return [
        lgrp.Row(lgrp.MEASURED_REV, measured_ms),
        lgrp.Row(lgrp.LAND_RUN, land_ms, added_git_spawns=added, attribution={"reach_module_present": True}),
    ]


def test_default_bar_is_unchanged():
    assert lgrp.BAR_MS == 500.0
    assert lgrp.exit_code(_rows(265.6, 390.6)) == 0


def test_bar_ms_flag_tightens_the_exit_and_the_print():
    rows = _rows(265.6, 390.6)
    assert lgrp.exit_code(rows, 200.0) == 1
    assert "OVER" in lgrp.render(rows, 200.0)
    assert "OVER" not in lgrp.render(rows)
    assert lgrp.exit_code(_rows(150.0, 190.0), 200.0) == 0


def test_added_git_spawn_or_missing_reach_module_fails():
    assert lgrp.exit_code(_rows(10.0, 10.0, added=1), 200.0) == 1
    rows = _rows(10.0, 10.0)
    rows[1].attribution["reach_module_present"] = False
    assert lgrp.exit_code(rows) == 1


def test_bar_ms_parses_in_main(monkeypatch):
    seen = {}

    def fake_run(source, workdir, overlay=()):
        return _rows(100.0, 300.0)

    monkeypatch.setattr(lgrp, "run", fake_run)
    seen["default"] = lgrp.main([])
    seen["tight"] = lgrp.main(["--bar-ms", "200"])
    seen["loose"] = lgrp.main(["--bar-ms", "400"])
    assert seen == {"default": 0, "tight": 1, "loose": 0}


def test_profiled_attribution_splits_named_legs():
    def inner_a():
        return sum(range(1000))

    def outer():
        inner_a()

    out = lgrp._profiled_attribution(outer, {"a": ("inner_a",), "missing": ("nope",)})
    assert out["profiled"] is True
    assert out["legs"]["a"] > 0.0
    assert out["legs"]["missing"] == 0.0


def test_profiled_attribution_reports_a_raised_call():
    def boom():
        raise ValueError("x")

    assert lgrp._profiled_attribution(boom)["error"] == "ValueError"


def test_apply_overlay_copies_source_files_into_the_clone(tmp_path):
    src, clone = tmp_path / "src", tmp_path / "clone"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("new\n")
    (clone / "pkg").mkdir(parents=True)
    (clone / "pkg" / "a.py").write_text("old\n")
    lgrp.apply_overlay(src, clone, ["pkg/a.py"])
    assert (clone / "pkg" / "a.py").read_text() == "new\n"
    with pytest.raises(FileNotFoundError):
        lgrp.apply_overlay(src, clone, ["pkg/missing.py"])


def test_overlay_flag_reaches_run(monkeypatch):
    seen = {}

    def fake_run(source, workdir, overlay=()):
        seen["overlay"] = overlay
        return _rows(100.0, 100.0)

    monkeypatch.setattr(lgrp, "run", fake_run)
    assert lgrp.main(["--overlay", "a/b.py", "--overlay", "c.py"]) == 0
    assert seen["overlay"] == ("a/b.py", "c.py")
