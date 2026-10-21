"""Pins landing_remedy: runnable invoke lines, revise-resolves remedy, param withhold, review-only anchor."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import landing_remedy as lr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

GENERIC = "Re-review the current bytes and pass their blob, or leave the path out."


def _json_of(line: str) -> dict:
    return json.loads(line.split(" '", 2)[2].rstrip("'"))


def test_invoke_call_emits_json_and_quotes_path():
    line = lr.invoke_call("dispatch.land_run", "tasks/a b.output", {"k": ["x"]})
    assert line.startswith("coordinator-invoke dispatch.land_run --from-task-output 'tasks/a b.output' '")
    assert json.loads(line.split("' '", 1)[1].rstrip("'")) == {"k": ["x"]}


@pytest.mark.parametrize("path", [None, "", "a'b", "a<b", "a&b", "a\nb"])
def test_invoke_call_unquotable_path_prints_placeholder(path):
    assert "--from-task-output TASK_OUTPUT_PATH '" in lr.invoke_call("op", path, {})


def test_release_held_call_sorts_ids():
    line = lr.release_held_call("dispatch.terminal_commit", "t.output", ["C2", "C0", "C1"])
    assert _json_of(line) == {"release_held": ["C0", "C1", "C2"]}


def test_revise_resolves_follows_run_json(tmp_path):
    d = tmp_path / RECORD_DIR_ROOT / "run-a"
    d.mkdir(parents=True)
    assert lr.revise_resolves(tmp_path, "run-a") is False
    (d / "run.json").write_text("{}", encoding="utf-8")
    assert lr.revise_resolves(tmp_path, "run-a") is True
    assert lr.revise_resolves(tmp_path, "../escape") is False


def test_blob_drift_remedy_keeps_revise_when_resolvable(tmp_path):
    d = tmp_path / RECORD_DIR_ROOT / "run-a"
    d.mkdir(parents=True)
    (d / "run.json").write_text("{}", encoding="utf-8")
    out = lr.blob_drift_remedy(f"blob-drift. {GENERIC}", tmp_path, "run-a", "dispatch.land_run", None, ["a.py"])
    assert "workflow.revise run_id=run-a" in out
    assert GENERIC not in out


def test_blob_drift_remedy_ledgerless_names_reemit_and_runnable_withhold(tmp_path):
    out = lr.blob_drift_remedy(f"blob-drift. {GENERIC}", tmp_path, "inv-t1-p1-land.workflow", "dispatch.land_run", "t.output", ["a.py"])
    assert "workflow.revise" not in out
    assert "--review-only" in out
    assert GENERIC not in out
    params = _json_of(out[out.index("coordinator-invoke"):])
    assert lr.param_withholds(params) == {"a.py": lr._DRIFT_REASON}


def test_param_withholds_round_trips_and_rejects_malformed():
    assert lr.param_withholds({}) == {}
    assert lr.param_withholds({"withhold_paths": [{"path": "a/b.py", "reason": " why "}]}) == {"a/b.py": "why"}
    for bad in ("x", [1], [{"path": "../x", "reason": "r"}], [{"path": "a.py", "reason": ""}]):
        with pytest.raises(ValueError):
            lr.param_withholds({"withhold_paths": bad})


def _request(review_only: bool):
    return SimpleNamespace(review_only=review_only, chunks=[SimpleNamespace(paths=("a.py",)), SimpleNamespace(paths=("b.py", "a.py"))])


def test_review_only_anchor_none_for_other_requests(tmp_path):
    side = tmp_path / "prep.md"
    side.write_text("x", encoding="utf-8")
    assert lr.review_only_anchor(tmp_path, _request(False), {"prep_sidecar": "prep.md"}) == (None, frozenset())
    assert lr.review_only_anchor(tmp_path, _request(True), {}) == (None, frozenset())
    assert lr.review_only_anchor(tmp_path, _request(True), {"prep_sidecar": "missing.md"}) == (None, frozenset())
    assert lr.review_only_anchor(tmp_path, _request(True), {"prep_sidecar": "../outside.md"}) == (None, frozenset())


def test_review_only_anchor_uses_sidecar_mtime(tmp_path):
    side = tmp_path / "prep.md"
    side.write_text("---\nagent_type: engine:prep-freeze\n---\n", encoding="utf-8")
    os.utime(side, (1_700_000_000, 1_700_000_000))
    at, paths = lr.review_only_anchor(tmp_path, _request(True), {"prep_sidecar": "prep.md"})
    assert at == 1_700_000_000
    assert paths == frozenset({"a.py", "b.py"})


def test_review_only_anchor_prefers_recorded_at(tmp_path):
    side = tmp_path / "prep.md"
    side.write_text("---\nrecorded_at: '2026-10-21T00:00:00Z'\n---\n", encoding="utf-8")
    at, _ = lr.review_only_anchor(tmp_path, _request(True), {"prep_sidecar": "prep.md"})
    assert at == 1_792_540_800.0
