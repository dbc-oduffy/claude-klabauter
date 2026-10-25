"""Tests for diff_test_select: pure `select` cases; the real-git `main()` cases live in test_diff_test_select_spawns."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import diff_test_select as d

SURFACE = ["pkg"]
GREP = "pkg/tests/test_a.py:from pkg.a import x\npkg/tests/test_b.py:import pkg.b\n"


def _sel(edited, passed_at=None, unchanged=lambda s: False):
    return d.select(edited, SURFACE, GREP, passed_at or {}, unchanged)


def test_test_file_maps_to_itself():
    assert _sel(["pkg/tests/test_z.py"]).tests == ("pkg/tests/test_z.py",)


def test_source_maps_to_referencers():
    assert _sel(["pkg/a.py"]).tests == ("pkg/tests/test_a.py",)


def test_out_of_surface_dropped():
    s = _sel(["other/a.py", "other/tests/test_q.py"])
    assert s.tests == () and s.skipped == ()


def test_passed_and_unchanged_skipped():
    s = _sel(["pkg/a.py", "pkg/b.py"], {"pkg/tests/test_a.py": "abc"}, lambda sha: sha == "abc")
    assert s.tests == ("pkg/tests/test_b.py",)
    assert s.skipped == ("pkg/tests/test_a.py",)


def test_passed_but_changed_still_runs():
    s = _sel(["pkg/a.py"], {"pkg/tests/test_a.py": "abc"}, lambda sha: False)
    assert s.tests == ("pkg/tests/test_a.py",) and s.skipped == ()


@pytest.mark.parametrize("bad,reason", [("not json", "not valid JSON"), ("[1]", "must be a JSON object")])
def test_main_refuses_malformed_passed_at(tmp_path, capsys, bad, reason):
    rc = d.main(["--base", "HEAD", "--surface", "pkg", "--passed-at", bad, "--repo-root", str(tmp_path)])
    cap = capsys.readouterr()
    assert rc == 2
    assert cap.out == ""
    assert reason in cap.err
