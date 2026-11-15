"""Clustering pins: one cluster per normalised signature, deterministic, non-test rows by surface file."""

from __future__ import annotations

import itertools

import pytest

from coordinator_core.grind_triage.cluster import cluster_rows, normalise_signature, row_signature
from coordinator_core.grind_triage.contract import Cluster, TriageRow


_DRIVE = "".join(["C", ":"])  # a Windows drive prefix as fixture text, not a path this code reads


def _test_row(key: str, text: str, target: str = "a/test_x.py::test_one") -> TriageRow:
    return TriageRow(key=key, title=f"TF: {target}", is_test=True, target=target, signature_text=text)


def _surface_row(key: str, target: str) -> TriageRow:
    return TriageRow(key=key, title=key, is_test=False, target=target)


@pytest.mark.parametrize(
    "a, b",
    [
        ("AssertionError: expected 3 got 4", "AssertionError: expected 17 got 902"),
        (f"FileNotFoundError: {_DRIVE}\\x\\repo\\state\\a.yaml", "FileNotFoundError: /tmp/pytest-1/b.yaml"),
        ("KeyError: 'id' at coordinator_core/a.py:34", "KeyError: 'id' at coordinator_core/other/b.py:7"),
        ("bad ref 3f9a1c7e2b", "bad ref deadbeef01"),
        ("mismatch 0x7ffd1234", "mismatch 0xAB12"),
        ("row 123e4567-e89b-12d3-a456-426614174000 gone", "row 00000000-0000-0000-0000-000000000001 gone"),
    ],
)
def test_signature_masks_paths_ids_and_numbers(a, b):
    assert normalise_signature(a) == normalise_signature(b)
    assert normalise_signature(a) != ""


def test_signature_keeps_the_failure_kind():
    assert normalise_signature("AssertionError: 1 != 2") != normalise_signature("ValueError: 1 != 2")


def test_signature_uses_only_the_first_nonblank_line():
    assert normalise_signature("\n  \nValueError: x\ntraceback 99") == "ValueError: x"
    assert normalise_signature("") == ""


def test_same_signature_rows_share_one_cluster():
    rows = [
        _test_row("r1", "AssertionError: expected 3 got 4", "a/test_x.py::t1"),
        _test_row("r2", "AssertionError: expected 8 got 9", "b/test_y.py::t2"),
        _test_row("r3", "ValueError: nope", "b/test_y.py::t3"),
    ]
    clusters = cluster_rows(rows)
    assert len(clusters) == 2
    by_keys = {c.row_keys for c in clusters}
    assert by_keys == {("r1", "r2"), ("r3",)}
    assert all(isinstance(c, Cluster) for c in clusters)


def test_non_test_rows_cluster_by_surface_file_ignoring_anchor():
    rows = [
        _surface_row("d1", "coordinator_core/a.py:12"),
        _surface_row("d2", "coordinator_core/a.py#some_fn"),
        _surface_row("d3", "coordinator_core/b.py:12"),
    ]
    clusters = {c.signature: c.row_keys for c in cluster_rows(rows)}
    assert clusters == {
        "surface:coordinator_core/a.py": ("d1", "d2"),
        "surface:coordinator_core/b.py": ("d3",),
    }


def test_windows_drive_surface_keeps_its_drive_colon():
    assert row_signature(_surface_row("d", f"{_DRIVE}\\repo\\a.py:3")) == f"surface:{_DRIVE}/repo/a.py"


def test_test_and_non_test_rows_never_share_a_cluster():
    rows = [_test_row("t", "boom"), _surface_row("s", "boom")]
    assert len(cluster_rows(rows)) == 2


def test_no_failure_text_clusters_by_test_file_only():
    rows = [
        _test_row("r1", "", "a/test_x.py::t1"),
        _test_row("r2", "  \n", "a/test_x.py::t2"),
        _test_row("r3", "", "b/test_y.py::t3"),
    ]
    assert sorted(c.row_keys for c in cluster_rows(rows)) == [("r1", "r2"), ("r3",)]


def test_order_is_deterministic_under_any_input_order():
    rows = [
        _test_row("r2", "ValueError: a", "a/test_x.py::t2"),
        _test_row("r1", "AssertionError: 1", "a/test_x.py::t1"),
        _surface_row("s1", "z/a.py"),
        _test_row("r3", "AssertionError: 2", "a/test_x.py::t3"),
    ]
    expected = cluster_rows(rows)
    assert [c.signature for c in expected] == sorted(c.signature for c in expected)
    for perm in itertools.permutations(rows):
        assert cluster_rows(perm) == expected


def test_duplicate_keys_collapse_and_empty_input_yields_nothing():
    row = _test_row("r1", "boom")
    assert cluster_rows([row, row]) == (Cluster(signature="test:boom", row_keys=("r1",)),)
    assert cluster_rows([]) == ()


def test_each_row_lands_in_exactly_one_cluster():
    rows = [_test_row(f"r{i}", f"Err {i % 2}: x{i}", f"a/test_{i}.py::t") for i in range(9)]
    keys = [k for c in cluster_rows(rows) for k in c.row_keys]
    assert sorted(keys) == sorted(r.key for r in rows)
