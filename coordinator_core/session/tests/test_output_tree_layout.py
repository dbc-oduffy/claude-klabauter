"""Tests for session/output_tree_layout: shard rule, locate, write_rel, iter_entries, DoE parity."""

from __future__ import annotations

import importlib.util
import os

import pytest

from coordinator_core.session import output_tree_layout as otl

NAMES = {
    "docs/plans": ["2026-11-03-foo.md", "2026-01-31-x.md", "2026-11-031-bad.md", "undated.md", "README.md"],
    "state/sizings": ["2026-10-20-a.yaml", "notes.yaml"],
    "state/handoffs": ["2026-09-01-h.md"],
    "docs/delivery": ["ask-20261103T072346-abc.md", "warp-2026-11-03-d1d77f-x.md", "2026-11-03-x.md"],
}


def test_shard_path_rules():
    assert otl.shard_path("docs/plans", "2026-11-03-foo.md") == "docs/plans/2026-11/2026-11-03-foo.md"
    assert otl.shard_path("docs/delivery", "ask-20261103T072346-abc.md") == "docs/delivery/2026-11/ask-20261103T072346-abc.md"
    assert otl.shard_path("docs/delivery", "warp-2026-11-03-d1-x.md") == "docs/delivery/2026-11/warp-2026-11-03-d1-x.md"
    assert otl.shard_path("docs/plans\\", "a/b\\2026-02-01-q.md") == "docs/plans/2026-02/2026-02-01-q.md"
    with pytest.raises(ValueError):
        otl.shard_path("docs/plans", "2026-11-031-bad.md")
    with pytest.raises(ValueError):
        otl.shard_path("docs/delivery", "2026-11-03-x.md")
    with pytest.raises(KeyError):
        otl.shard_path("state/other", "2026-11-03-x.md")


def test_write_rel_dated_shards_undated_stays_flat():
    assert otl.write_rel("state/sizings", "2026-10-20-a.yaml") == "state/sizings/2026-10/2026-10-20-a.yaml"
    assert otl.write_rel("state/sizings", "notes.yaml") == "state/sizings/notes.yaml"
    assert otl.write_rel("docs/delivery", "2026-11-03-x.md") == "docs/delivery/2026-11-03-x.md"


def _touch(root, rel, text="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_locate_flat_wins_when_it_exists(tmp_path):
    _touch(tmp_path, "docs/plans/2026-11-03-foo.md")
    assert otl.locate(str(tmp_path), "docs/plans/2026-11-03-foo.md") == "docs/plans/2026-11-03-foo.md"
    assert otl.locate(str(tmp_path), "docs\\plans\\2026-11-03-foo.md") == "docs/plans/2026-11-03-foo.md"


def test_locate_missing_flat_goes_to_shard(tmp_path):
    assert otl.locate(str(tmp_path), "docs/plans/2026-11-03-foo.md") == "docs/plans/2026-11/2026-11-03-foo.md"
    assert otl.locate(str(tmp_path), "docs/plans/undated.md") == "docs/plans/undated.md"


def test_locate_redirects_row_beats_shard_rule(tmp_path):
    _touch(tmp_path, "docs/plans/REDIRECTS.md",
           "| old | new |\n|---|---|\n| `docs/plans/2026-11-03-foo.md` | `docs/plans/archive/foo.md` |\n")
    assert otl.locate(str(tmp_path), "docs/plans/2026-11-03-foo.md") == "docs/plans/archive/foo.md"
    assert otl.locate(str(tmp_path), "docs/plans/2026-11-04-bar.md") == "docs/plans/2026-11/2026-11-04-bar.md"


def test_locate_foreign_and_nested_paths_pass_through(tmp_path):
    assert otl.locate(str(tmp_path), "src/a.py") == "src/a.py"
    assert otl.locate(str(tmp_path), "docs/plans/2026-11/2026-11-03-foo.md") == "docs/plans/2026-11/2026-11-03-foo.md"


def test_locate_is_cwd_independent(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    root = tmp_path / "repo"
    _touch(root, "state/handoffs/2026-09-01-h.md")
    monkeypatch.chdir(other)
    assert otl.locate(str(root), "state/handoffs/2026-09-01-h.md") == "state/handoffs/2026-09-01-h.md"


def test_iter_entries_flat_and_sharded(tmp_path):
    _touch(tmp_path, "docs/plans/2026-10-01-a.md")
    _touch(tmp_path, "docs/plans/README.md")
    _touch(tmp_path, "docs/plans/REDIRECTS.md")
    _touch(tmp_path, "docs/plans/2026-11/2026-11-03-b.md")
    _touch(tmp_path, "docs/plans/2026-11/c.txt")
    _touch(tmp_path, "docs/plans/archive/2026-11-03-z.md")
    got = sorted(otl.iter_entries(str(tmp_path), "docs/plans", ".md"))
    assert got == ["docs/plans/2026-10-01-a.md", "docs/plans/2026-11/2026-11-03-b.md"]
    assert list(otl.iter_entries(str(tmp_path), "state/sizings", ".yaml")) == []


def _load_doe():
    root = os.environ.get("CONTENT_ROOT_ROOT")
    if not root:
        return None
    path = os.path.join(root, "coordinator", "lib", "output_tree_layout.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("doe_output_tree_layout", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shard_path_parity_with_doe():
    doe = _load_doe()
    if doe is None:
        pytest.skip("CONTENT_ROOT_ROOT output_tree_layout.py not present")
    for tree, names in NAMES.items():
        for name in names:
            try:
                want = doe.shard_path(tree, name)
            except ValueError:
                want = ValueError
            try:
                got = otl.shard_path(tree, name)
            except ValueError:
                got = ValueError
            assert got == want, (tree, name)
    assert otl.trees() == doe.trees()
