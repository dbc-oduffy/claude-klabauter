"""Runs every shape in fixtures/session_registry_key_variants.json through parse_row."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BIN))

from lib import session_registry  # noqa: E402

_FIXTURE = Path(__file__).parent / "fixtures" / "session_registry_key_variants.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _CASES, ids=[c["label"] for c in _CASES])
def test_parse_row_projection(case, tmp_path):
    path = tmp_path / "row.json"
    path.write_text(json.dumps(case["file"]), encoding="utf-8")
    row = session_registry.parse_row(path)
    if case["expected"] is None:
        assert row is None
        return
    assert row is not None
    projection = {k: getattr(row, k) for k in case["expected"]}
    assert projection == case["expected"]
    assert row.path == path


def test_unparseable_file_is_rejected(tmp_path):
    path = tmp_path / "row.json"
    path.write_text("{not json", encoding="utf-8")
    assert session_registry.parse_row(path) is None
