"""Every name `halted_run._index_call` is called with resolves, and a bad name raises."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, halted_withheld_index as idx


def _call_site_names() -> list:
    tree = ast.parse(Path(inspect.getsourcefile(halted_run)).read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_index_call":
            first = node.args[0]
            assert isinstance(first, ast.Constant) and isinstance(first.value, str), ast.dump(node)
            names.append(first.value)
    return names


def test_every_call_site_name_exists_on_the_index_module():
    names = _call_site_names()
    assert names
    for name in names:
        assert callable(getattr(idx, name, None)), name


def test_unknown_name_raises_instead_of_a_soft_miss(tmp_path):
    rd = tmp_path / halted_run.RECORD_DIR_ROOT / "run-1"
    with pytest.raises(KeyError):
        halted_run._index_call("record_claim_not_a_name", rd)


def test_index_write_failure_stays_best_effort(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(idx, "remove_run", boom)
    rd = tmp_path / halted_run.RECORD_DIR_ROOT / "run-1"
    halted_run._index_call("remove_run", rd)
