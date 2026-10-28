"""`land_remainder_writeset`: out-of-write-set refusal and declared dirty paths split."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import land_remainder_writeset as tool
from coordinator_core.ops.dispatch_emit.dirty_write_set import DirtyWriteSetError

RUN = "warp-test-run"


def _row(rid, writes, under=()):
    return SimpleNamespace(id=rid, writes=list(writes), surface="", writes_under=list(under))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    run_dir = tmp_path / "state" / "warp" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "sizings" / "s.yaml").write_text("plan: docs/plans/p.md\n", encoding="utf-8")
    rows = [_row("C1", ["a/one.py", "a/two.py"]), _row("C2", ["b/three.py"], under=["gen/dir"])]
    monkeypatch.setattr(tool, "read_spine", lambda plan: rows)

    def fake_dirty(specs, root, **_kw):
        found = set()
        for spec in specs:
            p = root / spec
            if p.is_file():
                found.add(spec)
            elif p.is_dir():
                found.update(f.relative_to(root).as_posix() for f in p.rglob("*") if f.is_file())
        return sorted(found)

    monkeypatch.setattr(tool, "_dirty_in_write_set", fake_dirty)
    return tmp_path


def _write(repo, *rels):
    for rel in rels:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")


def test_out_of_write_set_path_is_refused_naming_it(repo) -> None:
    got = tool.refuse_out_of_write_set(repo, RUN, ["a/one.py", "z/stray.py", "gen/dir/x.txt"])
    assert got == ["z/stray.py"]


def test_in_set_paths_pass_and_prefix_sibling_is_refused(repo) -> None:
    assert tool.refuse_out_of_write_set(repo, RUN, ["b/three.py", "gen/dir/deep/y"]) == []
    assert tool.refuse_out_of_write_set(repo, RUN, ["gen/dirty.txt"]) == ["gen/dirty.txt"]


def test_run_without_plan_raises(tmp_path) -> None:
    (tmp_path / "state" / "warp" / RUN).mkdir(parents=True)
    with pytest.raises(ValueError):
        tool.refuse_out_of_write_set(tmp_path, RUN, ["x"])


def test_dirty_declared_returns_declared_dirt_and_not_foreign(repo) -> None:
    _write(repo, "a/one.py", "gen/dir/n.txt", "foreign.py")
    to_stage, omitted = tool.dirty_declared(repo, RUN)
    assert to_stage == ["a/one.py", "gen/dir/n.txt"]
    assert omitted == []


def test_generated_output_is_named_not_staged(repo) -> None:
    (repo / "coordinator.local.md").write_text("---\ngenerated_outputs: gen/dir/**\n---\n", encoding="utf-8")
    _write(repo, "a/two.py", "gen/dir/n.txt")
    to_stage, omitted = tool.dirty_declared(repo, RUN)
    assert to_stage == ["a/two.py"]
    assert [p for p, _ in omitted] == ["gen/dir/n.txt"]
    assert omitted[0][1]


def test_git_failure_raises(repo, monkeypatch) -> None:
    monkeypatch.setattr(tool, "_dirty_in_write_set", lambda *a, **k: None)
    with pytest.raises(DirtyWriteSetError):
        tool.dirty_declared(repo, RUN)
