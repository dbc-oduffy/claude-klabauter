"""A review-only request names DONE-unlanded plan chunks and they reach the spine; `--rows` repeats and
splits on commas; the test-runner brief names its paths literally."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.emit import _test_runner_clause
from coordinator_core.ops.dispatch_emit.tests.test_inventory_mint_done_unlanded import (
    _fixture,
    _mint,
    _report,
)
from coordinator_core.ops.dispatch_emit.tests.test_review_close_routes import _drive

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _ids(rows) -> set:
    return {r["id"] for r in rows}


def test_done_unlanded_chunk_is_not_minted_without_a_review_request(tmp_path):
    inv = _fixture(tmp_path)
    for mid in ("A.C3", "B"):
        _report(tmp_path, mid)

    assert not {"A.C3", "A.C4"} & _ids(_mint(inv))


def test_review_request_mints_the_named_done_unlanded_chunk_and_its_held_dependent(tmp_path):
    inv = _fixture(tmp_path)
    for mid in ("A.C3", "B"):
        _report(tmp_path, mid)

    rows = _mint(inv, review_rows=frozenset({"A.C3", "A.C4"}))

    assert {"A.C3", "A.C4"} <= _ids(rows)
    c4 = next(r for r in rows if r["id"] == "A.C4")
    assert [e["chunk"] for e in c4["depends_on"]] == ["A.C3"]


def test_review_request_leaves_an_unnamed_done_unlanded_chunk_out(tmp_path):
    inv = _fixture(tmp_path)
    for mid in ("A.C1", "A.C3"):
        _report(tmp_path, mid)

    rows = _mint(inv, review_rows=frozenset({"A.C3"}))

    assert "A.C3" in _ids(rows)
    assert "A.C1" not in _ids(rows)


def test_rows_flag_repeats_and_splits_on_commas(tmp_path, monkeypatch):
    params = _drive(tmp_path, monkeypatch, ["--review-only", "--rows", "C1", "--rows", "C2,C3"])

    assert params["review_only_rows"] == ["C1", "C2", "C3"]


@pytest.mark.parametrize("root", [None, "repo"])
def test_test_runner_clause_names_paths_literally(tmp_path, root):
    clause = _test_runner_clause(tmp_path if root else None)

    assert "literally" in clause and "never through a shell variable" in clause


def test_resolution_defect_clause_runs_no_tests_and_adds_no_path_rule(tmp_path):
    (tmp_path / "coordinator.local.md").write_text(
        '---\ntargeted_test_python: "mypy3.99"\n---\n', encoding="utf-8"
    )

    clause = _test_runner_clause(tmp_path)

    assert "Run no tests" in clause
    assert "never through a shell variable" not in clause
