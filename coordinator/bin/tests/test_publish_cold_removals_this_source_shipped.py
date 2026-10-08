"""A cold round removes only destination paths this repo once shipped and since deleted.

A klabauter dir a row mirrors can hold another publisher's files (b0d4a5e07 landed ~45 DoE
files in coordinator/{lib,bin,tests}); "in the destination, not in this row's payload" is not
proof they are ours to delete.

Run: python -m pytest coordinator/bin/tests/test_publish_cold_removals_this_source_shipped.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_cold_removals_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _row(source_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(name="row", source_dir=source_dir)


def test_keeps_only_paths_this_repo_deleted(capsys):
    target = _row(publish._REPO_ROOT / "coordinator" / "lib")
    shipped = frozenset({"coordinator/lib/throwaway_tree.py"})

    kept = publish._removals_this_source_shipped(target, {"throwaway_tree.py", "collab_detect.py"}, shipped)

    assert kept == {"throwaway_tree.py"}
    assert "keeping 1 destination path(s) this repo never shipped" in capsys.readouterr().out


def test_a_source_outside_the_repo_removes_nothing(tmp_path):
    kept = publish._removals_this_source_shipped(_row(tmp_path), {"a.py"}, frozenset({"a.py"}))

    assert kept == set()


def test_deleted_paths_query_is_one_spawn_over_the_row_dirs(monkeypatch):
    calls = []

    def _fake_capture(path, *args):
        calls.append(args)
        return "coordinator/lib/gone.py\n\ncoordinator/bin/gone-too.py"

    monkeypatch.setattr(publish, "_git_capture", _fake_capture)
    targets = [_row(publish._REPO_ROOT / "coordinator" / "lib"), _row(publish._REPO_ROOT / "coordinator" / "bin")]

    deleted = publish._source_deleted_paths(targets)

    assert deleted == frozenset({"coordinator/lib/gone.py", "coordinator/bin/gone-too.py"})
    assert len(calls) == 1
    assert "--no-renames" in calls[0] and "--diff-filter=D" in calls[0]
    assert calls[0][-2:] == ("coordinator/bin", "coordinator/lib")


def test_a_failed_query_removes_nothing(monkeypatch):
    monkeypatch.setattr(publish, "_git_capture", lambda path, *args: None)

    assert publish._source_deleted_paths([_row(publish._REPO_ROOT / "coordinator" / "lib")]) == frozenset()


def test_the_union_filters_cold_removals_before_the_live_source_refusal():
    tree = ast.parse((_BIN_DIR / "publish.py").read_text(encoding="utf-8"))
    union = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_assemble_root_union")
    source = ast.unparse(union)

    assert source.index("_removals_this_source_shipped(") < source.index("refuse_removals_with_live_source(target.source_dir, removals")
