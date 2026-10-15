"""Tests for the grind terminal-commit contract."""

from __future__ import annotations

import copy
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import grind_commit_contract as gcc


def _good() -> dict:
    return {
        "profile": "bug",
        "rows": [
            {"row_id": "a", "outcome": "committed", "paths": ["src/a.py"], "deleted_paths": []},
            {"row_id": "b", "outcome": "committed", "paths": ["src/b.py"], "deleted_paths": ["state/backlog/b.md"]},
        ],
        "halted": None,
    }


def test_accepts_well_formed_params():
    assert gcc.validate_grind_params(_good()) is None


def test_halted_str_and_absent_accepted():
    g = _good()
    g["halted"] = "budget-exhausted"
    assert gcc.validate_grind_params(g) is None
    del g["halted"]
    assert gcc.validate_grind_params(g) is None


def test_trailer_collapses_whitespace():
    assert gcc.render_row_trailer("a  b\n", " committed\t") == "Grind-Row: a b committed"


def test_is_grind_call():
    assert gcc.is_grind_call({gcc.GRIND_COMMIT_PARAM: {"rows": []}})
    assert not gcc.is_grind_call({gcc.GRIND_COMMIT_PARAM: {"rows": "x"}})
    assert not gcc.is_grind_call({gcc.GRIND_COMMIT_PARAM: []})
    assert not gcc.is_grind_call({})
    assert not gcc.is_grind_call(None)


def _mutated(fn) -> dict:
    g = copy.deepcopy(_good())
    fn(g)
    return g


@pytest.mark.parametrize(
    "mutate",
    [
        lambda g: g.update(profile=""),
        lambda g: g.update(profile=3),
        lambda g: g.update(rows="x"),
        lambda g: g["rows"].append("junk"),
        lambda g: g["rows"][0].update(row_id=""),
        lambda g: g["rows"][0].update(outcome=None),
        lambda g: g["rows"][1].update(row_id="a"),
        lambda g: g["rows"][0].update(paths="src/a.py"),
        lambda g: g["rows"][0].update(deleted_paths=None),
        lambda g: g["rows"][0].update(paths=[3]),
        lambda g: g["rows"][0].update(paths=["/abs/x.py"]),
        lambda g: g["rows"][0].update(paths=["C:/x.py"]),
        lambda g: g["rows"][0].update(paths=["a\\b.py"]),
        lambda g: g["rows"][0].update(paths=["a/../b.py"]),
        lambda g: g["rows"][0].update(deleted_paths=["../b.py"]),
        lambda g: g.update(halted=""),
        lambda g: g.update(halted=7),
    ],
)
def test_refuses_each_rule(mutate):
    assert isinstance(gcc.validate_grind_params(_mutated(mutate)), str)


_EVIDENCE = "state/queue-grind/bug/evidence/run1/a.done.md"


def test_validate_accepts_ledger_root_path():
    g = _mutated(lambda g: g["rows"][0].update(paths=["src/a.py", _EVIDENCE]))
    assert gcc.validate_grind_params(g) is None


def test_partition_splits_ledger_root_and_keeps_order():
    kept, dropped = gcc.partition_ledger_paths(["src/z.py", _EVIDENCE, "src/a.py"])
    assert kept == ["src/z.py", "src/a.py"]
    assert dropped == [_EVIDENCE]


def test_refuses_non_dict():
    assert isinstance(gcc.validate_grind_params([]), str)


def test_import_adds_no_non_stdlib_module():
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import coordinator_core.ops.dispatch_emit.grind_commit_contract\n"
        "new = set(sys.modules) - before\n"
        "print('\\n'.join(sorted(m for m in new if m.split('.')[0] == 'coordinator_core')))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    allowed = {
        "coordinator_core.ops.dispatch_emit.grind_commit_contract",
        "coordinator_core._engine_landing",
        "coordinator_core.op_scopes",
    }
    extra = [m for m in out if m not in allowed and not any(allowed_pkg == m for allowed_pkg in (
        "coordinator_core", "coordinator_core.ops", "coordinator_core.ops.dispatch_emit"))]
    assert extra == []
