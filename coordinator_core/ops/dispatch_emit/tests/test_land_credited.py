"""``dispatch.land_credited``: commits exactly the credited paths differing from HEAD, names holds, refuses a lost CAS."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_credited as lane
from coordinator_core.ops.dispatch_emit import land_credited_contract as contract

pytestmark = [pytest.mark.complete]

RUN = "warp-test-run"
SCRIPT = "scratch/run.js"


def test_handler_refuses_missing_params_and_is_registered():
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_MODULE_MAP[contract.OP_NAME] == "coordinator_core.ops.dispatch_emit.land_credited"
    assert lane._handler({"run_id": RUN})["refused"] == contract.REFUSED_BAD_REQUEST


def test_op_modules_do_not_import_land_run():
    import ast

    for module in (lane, __import__("coordinator_core.ops.dispatch_emit.land_credited_holds", fromlist=["x"])):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = {
            n.module if isinstance(n, ast.ImportFrom) else a.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
            for a in (n.names if isinstance(n, ast.Import) else [None])
        }
        assert not any(m and m.endswith("land_run") for m in imported), module.__name__
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "land_run" not in names, module.__name__
