"""Round-trip and rejection pins for the grind-row undo path token."""
from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from coordinator_core.contract import grind_vocab as gv


def test_round_trip_awkward_paths():
    p = ["a/it's.py", "dir with space/b.py", "100%/c.py", "é/ü.py", "a/it's.py"]
    token = gv.encode_undo_paths(p)
    assert "'" not in token
    assert gv.decode_undo_paths(token) == sorted(set(p))


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        json.dumps({}),
        json.dumps({"paths": ["a"], "extra": 1}),
        json.dumps({"paths": []}),
        json.dumps({"paths": "a"}),
        json.dumps({"paths": [""]}),
        json.dumps({"paths": [1]}),
    ],
)
def test_decode_rejects(raw):
    with pytest.raises(ValueError):
        gv.decode_undo_paths(quote(raw, safe=""))
