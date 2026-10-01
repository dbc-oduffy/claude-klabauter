"""Tests for fast_red_census shape classification and first-pass clustering."""
from coordinator_core.ops.fast_red_census import (
    classify_summary, cluster_rows, main, render_table,
)

CAPTURE = """\
ERROR coordinator_core/tests/test_a.py - ImportError: cannot import name 'x'
ERROR coordinator_core/tests/test_b.py::test_setup - RuntimeError: boom
FAILED coordinator_core/tests/test_c.py::test_slow - Failed: Timeout >30s
FAILED coordinator_core/tests/test_d.py::test_eq - AssertionError: assert 1 == 2
FAILED coordinator_core/tests/test_e.py::test_t - TypeError: bad arg
FAILED coordinator_core/tests/test_f.py::test_p1 - AssertionError: no /tmp/pytest-1234/a.txt here
FAILED coordinator_core/tests/test_g.py::test_p2 - AssertionError: no /tmp/pytest-9876/a.txt here
ERROR coordinator_core/tests/test_h.py
"""


def _by_id():
    return {r.nodeid: r for r in classify_summary(CAPTURE)}


def test_shapes():
    r = _by_id()
    assert r["coordinator_core/tests/test_a.py"].shape == "collection-import"
    assert r["coordinator_core/tests/test_b.py::test_setup"].shape == "fixture-env"
    assert r["coordinator_core/tests/test_c.py::test_slow"].shape == "fixture-env"
    assert r["coordinator_core/tests/test_d.py::test_eq"].shape == "assertion"
    assert r["coordinator_core/tests/test_e.py::test_t"].shape == "unclassified"


def test_tail_parse_and_absent_tail():
    r = _by_id()
    a = r["coordinator_core/tests/test_a.py"]
    assert (a.exc_type, a.outcome) == ("ImportError", "ERROR")
    assert r["coordinator_core/tests/test_h.py"].exc_type == ""


def test_bare_importerror_tail():
    rows = classify_summary("ERROR tests/x.py - ImportError\n")
    assert rows[0].exc_type == "ImportError" and rows[0].shape == "collection-import"


def test_path_only_difference_clusters_together():
    clusters = cluster_rows(classify_summary(CAPTURE))
    sizes = sorted(len(v) for v in clusters.values())
    assert 2 in sizes
    pair = next(v for v in clusters.values() if len(v) == 2)
    assert {r.nodeid.split("::")[0] for r in pair} == {
        "coordinator_core/tests/test_f.py", "coordinator_core/tests/test_g.py"}


def test_slugs_stable():
    assert list(cluster_rows(classify_summary(CAPTURE))) == list(cluster_rows(classify_summary(CAPTURE)))


def test_cli_table(tmp_path, capsys):
    f = tmp_path / "cap.txt"
    f.write_text(CAPTURE, encoding="utf-8")
    assert main(["cluster", str(f)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("| cluster | shape | files | node ids | first message |")
    assert "test_d.py::test_eq" in out
    assert render_table({}).count("\n") == 1
