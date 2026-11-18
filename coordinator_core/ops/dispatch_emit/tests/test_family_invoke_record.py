from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import family_invoke_record as fir
from coordinator_core.ops.dispatch_emit import stage_family as sf
from coordinator_core.ops.dispatch_emit import write_baseline


def _put(tmp_path, run, row, stage, rec):
    p = fir.record_path(tmp_path, run, row, stage)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def test_contract_names():
    assert fir.INVOKE_MODULE == "coordinator_core.ops.dispatch_emit.family_invoke"
    assert sf.FAMILY_AUTHORED_KEY == "family_authored"
    assert sf.VENDOR_MODEL_KEY in fir.RECORD_KEYS
    assert fir.fingerprint is write_baseline.fingerprint


def test_paths_live_under_family_scratch_dir(tmp_path):
    d = tmp_path / sf.family_scratch_dir("C1", "r1")
    assert fir.record_path(tmp_path, "r1", "C1", "write") == d / "write.invocation.json"
    assert fir.raw_stdout_path(tmp_path, "r1", "C1", "write") == d / "write.raw.stdout"
    assert fir.raw_stderr_path(tmp_path, "r1", "C1", "write") == d / "write.raw.stderr"


def test_read_returns_every_stage_in_ended_at_order(tmp_path):
    _put(tmp_path, "r", "C1", "write", {"stage": "write", "ended_at": "2026-01-02T00:00:00Z"})
    _put(tmp_path, "r", "C1", "review", {"stage": "review", "ended_at": "2026-01-01T00:00:00Z"})
    (fir.record_path(tmp_path, "r", "C1", "junk")).write_text("{not json", encoding="utf-8")
    got = fir.read(tmp_path, "r", "C1")
    assert [r["stage"] for r in got] == ["review", "write"]


def test_read_absent_dir_is_empty(tmp_path):
    assert fir.read(tmp_path, "r", "nope") == []


def test_authored_paths_only_changed_on_exit_zero():
    rec = {"exit_code": 0, "pre": {"a": "1", "b": "2", "c": None}, "post": {"a": "1", "b": "3", "c": "9"}}
    assert fir.authored_paths(rec) == frozenset({"b", "c"})


def test_authored_paths_refuses_nonzero_and_malformed():
    base = {"pre": {"a": "1"}, "post": {"a": "2"}}
    assert fir.authored_paths({**base, "exit_code": 1}) == frozenset()
    assert fir.authored_paths({**base}) == frozenset()
    assert fir.authored_paths({"exit_code": 0, "pre": None, "post": {"a": "2"}}) == frozenset()
    assert fir.authored_paths(None) == frozenset()
