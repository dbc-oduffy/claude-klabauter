"""Tests for bug_blitz.mint: one row per file, severity, idempotence, reuse of an open tf row."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.bug_blitz.contract import FailingTest, run_tag
from coordinator_core.bug_blitz.mint import mint_tf_rows


@pytest.fixture(autouse=True)
def _no_output_override(monkeypatch):
    monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)


def _rows(root: Path) -> dict[str, dict]:
    out = {}
    for p in sorted((root / "state" / "bug-backlog").glob("*.yaml")):
        out[p.stem] = yaml.safe_load(p.read_text(encoding="utf-8"))
    return out


def _tests():
    return (
        FailingTest(file="a/test_x.py", name="test_one", message="AssertionError: boom\nmore"),
        FailingTest(file="a/test_x.py", name="test_two", message="ValueError"),
        FailingTest(file="b/test_y.py", name="test_three", message="Segmentation fault: SIGSEGV"),
    )


def test_one_row_per_file_with_fields(tmp_path):
    ids = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="redset.txt")
    rows = _rows(tmp_path)
    assert len(ids) == 2 and set(ids) == set(rows)
    by_surface = {r["surface"]: r for r in rows.values()}
    a, b = by_surface["a/test_x.py"], by_surface["b/test_y.py"]
    assert a["title"] == "TF: a/test_x.py (2 failing)"
    assert a["severity"] == "P1" and b["severity"] == "P0"
    assert a["tags"] == ["tf", run_tag("r1")]
    assert a["evidence"] == "redset.txt"
    assert a["status"] == "open"
    assert "test_one :: AssertionError: boom" in a["body"]
    assert "more" not in a["body"]


def test_rerun_same_run_is_idempotent(tmp_path):
    first = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    before = _rows(tmp_path)
    second = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    assert second == first
    assert _rows(tmp_path) == before


def test_new_run_reuses_open_row_and_adds_tag(tmp_path):
    first = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    second = mint_tf_rows(tmp_path, "r2", _tests(), target_ref="t")
    rows = _rows(tmp_path)
    assert len(rows) == 2 and set(second) == set(rows)
    for row in rows.values():
        assert row["tags"][:2] == ["tf", run_tag("r1")] and run_tag("r2") in row["tags"]
    assert not set(first) & set(rows) or set(first) == set(second)


def test_closed_row_is_not_reused(tmp_path):
    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    path = tmp_path / "state" / "bug-backlog" / f"{ids[0]}.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("status: open", "status: closed"), encoding="utf-8")
    again = mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    assert again != ids
    assert len(_rows(tmp_path)) == 2


def test_empty_tests_mints_nothing(tmp_path):
    assert mint_tf_rows(tmp_path, "r1", (), target_ref="t") == []
