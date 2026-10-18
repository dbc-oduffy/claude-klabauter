"""terminal_commit releases the run's siblings when the product commit lands, ahead of the post-commit legs."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import terminal_commit as tc

SHA = "b" * 40
PARENT = "ask-parent-20261018T000000-abcdef"


def _run(rid, after=(), status=c.STATUS_READY):
    return c.SiblingRun(rid, rid, tuple(after), f"state/sizings/{rid}.yaml", status)


def _seed(root: Path):
    parent = c.ParentRecord(PARENT, (_run("A"), _run("B", ["A"], c.STATUS_AFTER)))
    rec = root / c.parent_record_rel(PARENT)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(parent.to_json()), encoding="utf-8")
    st = root / c.status_rel("A")
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps({"status": c.STATUS_RUNNING, "parent_id": PARENT}), encoding="utf-8")


def _status(root):
    return json.loads((root / c.status_rel("A")).read_text(encoding="utf-8"))


def _ctx(root, applies=True):
    return {"root": root, "run_id": "script-stem", "sibling_run_id": "A", "applies": applies}


def test_landing_marks_status_landed_with_the_commit_sha(tmp_path):
    _seed(tmp_path)
    tc._release_siblings_at_landing(_ctx(tmp_path), SHA)
    assert _status(tmp_path) == {"status": "landed", "sha": SHA, "parent_id": PARENT}


def test_close_block_rerelease_is_idempotent(tmp_path):
    _seed(tmp_path)
    tc._release_siblings_at_landing(_ctx(tmp_path), SHA)
    first = _status(tmp_path)
    tc._release_siblings_at_landing(_ctx(tmp_path), SHA)
    assert _status(tmp_path) == first


def test_not_applicable_close_writes_nothing(tmp_path):
    _seed(tmp_path)
    tc._release_siblings_at_landing(_ctx(tmp_path, applies=False), SHA)
    tc._release_siblings_at_landing(None, SHA)
    tc._release_siblings_at_landing({}, SHA)
    assert _status(tmp_path)["status"] == c.STATUS_RUNNING


def test_release_failure_never_fails_the_landed_commit(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_close

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(run_close, "sibling_release", boom)
    _seed(tmp_path)
    tc._release_siblings_at_landing(_ctx(tmp_path), SHA)
    assert _status(tmp_path)["status"] == c.STATUS_RUNNING


def test_release_is_wired_inside_the_landed_branch_before_post_commit_legs():
    src = inspect.getsource(tc._terminal_commit_inner)
    landed = src.index('token_state["landed"] = True')
    release = src.index("_release_siblings_at_landing(close_ctx")
    refusal_cleanup = src.index("if not reply.get(\"committed\"):", landed)
    pay = src.index("_pay_inline_legs(")
    assert landed < release < refusal_cleanup < pay
