"""plan.merge contract and handler param/guard arms: pure tmp_path tests."""

from __future__ import annotations

import pytest

from coordinator_core.ops._plan_merge_contract import (
    CHECK_KEYS,
    MergeReport,
    Refusal,
    RowAccount,
    normalize_newlines,
)
from coordinator_core.ops.plan_merge import _handler


def test_normalize_newlines():
    assert normalize_newlines("a\r\nb\rc\nd") == "a\nb\nc\nd"


def _report():
    rows = (
        RowAccount("A", "s1", "untouched"),
        RowAccount("B", "s1", "amended", ("fields", "note")),
        RowAccount("N", "delta", "new"),
    )
    return MergeReport(rows, ("A", "B", "C"), ("Old",), {k: True for k in CHECK_KEYS})


def test_counts_arithmetic():
    assert _report().counts() == {
        "source_rows": 3, "untouched": 1, "amended": 1, "unaccounted": 1,
        "new": 1, "out": 3, "dropped_sections": 1,
    }
    assert _report().to_dict()["counts"]["out"] == 3


def test_checks_keys_enforced():
    with pytest.raises(ValueError):
        MergeReport((), (), (), {"row_partition": True})


def test_refusal_code_enforced():
    with pytest.raises(ValueError):
        Refusal("NOPE", "m")


def _setup(tmp_path):
    (tmp_path / ".git").mkdir()
    for n in ("a.md", "b.md", "d.yaml"):
        (tmp_path / n).write_text("x", encoding="utf-8")


def test_output_is_source_refused_even_with_force(tmp_path):
    _setup(tmp_path)
    for force in (False, True):
        res = _handler({"sources": ["a.md", "b.md"], "delta": "d.yaml", "out": "./a.md", "force": force}, tmp_path)
        assert res["refusal"]["code"] == "OUTPUT_IS_SOURCE"
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "x"


def test_output_exists_without_force(tmp_path):
    _setup(tmp_path)
    (tmp_path / "o.md").write_text("keep", encoding="utf-8")
    res = _handler({"sources": ["a.md", "b.md"], "delta": "d.yaml", "out": "o.md"}, tmp_path)
    assert res["refusal"]["code"] == "OUTPUT_EXISTS"
    assert (tmp_path / "o.md").read_text(encoding="utf-8") == "keep"


def test_unparseable_sources_refuse_spine_malformed(tmp_path):
    _setup(tmp_path)
    res = _handler({"sources": ["a.md", "b.md"], "delta": "d.yaml", "out": "o.md"}, tmp_path)
    assert res["ok"] is False and res["refusal"]["code"] == "SPINE_MALFORMED"
    assert not (tmp_path / "o.md").exists()


def test_param_validation(tmp_path):
    with pytest.raises(ValueError):
        _handler({"sources": ["a.md"], "delta": "d", "out": "o"}, tmp_path)
    with pytest.raises(ValueError):
        _handler({"sources": ["a.md", "b.md"], "delta": "../d", "out": "o"}, tmp_path)
    with pytest.raises(ValueError):
        _handler({"sources": ["a.md", "b.md"], "delta": "d", "out": "o"}, None)
