"""review_edits.partition / live_sibling_declared / review_writes delegate."""

import json
import sys
import types

from coordinator_core.ops.dispatch_emit import review_edits as re_


def _p(writes, **kw):
    args = dict(changed=set(), ignored=[], committed=set(), sibling_declared={}, contested=set(),
                present=lambda p: True)
    args.update(kw)
    return re_.partition(writes, **args)


def test_land_when_changed_present_uncontested():
    v = _p({"b/x.py": "a1", "a/y.py": "a1"}, changed={"a/y.py", "b/x.py"})
    assert v.land == ("a/y.py", "b/x.py") and v.halt == {} and v.unanswerable is None


def test_halt_tokens():
    v = _p(
        {"c.py": "a", "d.py": "a", "../sib/e.py": "a", "/abs/f.py": "a"},
        changed={"c.py", "d.py"},
        contested={"c.py"},
        present=lambda p: p != "d.py",
    )
    assert v.land == ()
    assert v.halt == {
        "../sib/e.py": "out-of-worktree",
        "/abs/f.py": "out-of-worktree",
        "c.py": "peer-contested",
        "d.py": "deleted",
    }


def test_committed_clean_ignored_sibling_are_neither():
    v = _p(
        {"a": "x", "b": "x", "c": "x", "d": "x"},
        changed={"a", "c", "d"},
        committed={"a"},
        ignored=["c"],
        sibling_declared={"d": "run-2"},
    )
    assert v.land == () and v.halt == {}


def test_none_writes_is_unanswerable():
    v = _p(None)
    assert v.unanswerable and v.land == () and v.halt == {}


def test_backslash_key_normalises():
    v = _p({"a\\b\\c.py": "x"}, changed={"a/b/c.py"})
    assert v.land == ("a/b/c.py",)


def test_live_sibling_declared(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    monkeypatch.setattr(
        abandoned_runs, "_token_records",
        lambda root: [{"run_id": "own"}, {"run_id": "sib"}, {"run_id": "bad"}, {"run_id": "gone"}],
    )
    for run, body in (("own", {"review_declared_paths": ["own.py"]}),
                      ("sib", {"review_declared_paths": ["s\\one.py", "two.py"]})):
        d = tmp_path / "state" / "warp" / run
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
    bad = tmp_path / "state" / "warp" / "bad"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("{not json", encoding="utf-8")
    assert re_.live_sibling_declared(tmp_path, "own") == {"s/one.py": "sib", "two.py": "sib"}


def test_review_writes_delegates_lazily(tmp_path, monkeypatch):
    mod = types.ModuleType("coordinator_core.ops.dispatch_emit.review_edit_source")
    mod.session_review_writes = lambda root, sid, since: {"f.py": f"{sid}:{since}"}
    monkeypatch.setitem(sys.modules, "coordinator_core.ops.dispatch_emit.review_edit_source", mod)
    import coordinator_core.ops.dispatch_emit as pkg
    monkeypatch.setattr(pkg, "review_edit_source", mod, raising=False)
    assert re_.review_writes(tmp_path, "s1", 5.0) == {"f.py": "s1:5.0"}
