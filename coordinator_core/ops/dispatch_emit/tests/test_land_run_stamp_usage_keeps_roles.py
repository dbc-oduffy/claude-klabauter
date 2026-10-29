"""land_run._stamp_usage updates only as_of in an existing usage.json."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "ask-20261029T000000-abc123"


def _read(root):
    return json.loads((root / RECORD_DIR_ROOT / RUN / "usage.json").read_text(encoding="utf-8"))


def test_existing_keys_survive_and_as_of_is_stamped(tmp_path):
    d = tmp_path / RECORD_DIR_ROOT / RUN
    d.mkdir(parents=True)
    seeded = {"as_of": "scan", "by_role": {"work": {"total_tokens": 7}}, "unsettled": ["c1"], "agents": []}
    (d / "usage.json").write_text(json.dumps(seeded), encoding="utf-8")
    land_run._stamp_usage(tmp_path, RUN)
    body = _read(tmp_path)
    assert body["as_of"] == "terminal-commit"
    assert body["by_role"] == {"work": {"total_tokens": 7}}
    assert body["unsettled"] == ["c1"] and body["agents"] == []


def test_absent_file_gets_minimal_stamp(tmp_path):
    land_run._stamp_usage(tmp_path, RUN)
    assert _read(tmp_path) == {"as_of": "terminal-commit", "unsettled": []}


def test_corrupt_file_is_replaced_by_minimal_stamp(tmp_path):
    d = tmp_path / RECORD_DIR_ROOT / RUN
    d.mkdir(parents=True)
    (d / "usage.json").write_text("{not json", encoding="utf-8")
    land_run._stamp_usage(tmp_path, RUN)
    assert _read(tmp_path) == {"as_of": "terminal-commit", "unsettled": []}
