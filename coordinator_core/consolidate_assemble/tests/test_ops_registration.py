"""`consolidate_assemble.brief` is a registered, COMPUTE_ONLY, show_top-scoped op."""
from __future__ import annotations

import asyncio
from pathlib import Path

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.ipc import get_op_handler
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map


def test_brief_resolves_via_get_op_handler() -> None:
    handler = get_op_handler("consolidate_assemble.brief")
    assert handler is not None
    assert handler.__name__ == "_consolidate_assemble_brief"


def test_brief_is_compute_only() -> None:
    assert classify("consolidate_assemble.brief") is OpClass.COMPUTE_ONLY


def test_brief_scopes_to_show_top() -> None:
    assert OP_KEY_SCOPE["consolidate_assemble.brief"] == "show_top"


def test_registry_map_names_the_ops_module() -> None:
    assert (
        _registry_map.OP_MODULE_MAP["consolidate_assemble.brief"]
        == "coordinator_core.consolidate_assemble.ops"
    )


def test_handler_passes_repo_root_and_superseded_through(monkeypatch, tmp_path: Path) -> None:
    from coordinator_core.consolidate_assemble import ops

    seen: dict = {}

    def fake_brief(**kwargs):
        seen.update(kwargs)
        return {"artifact": "x"}

    monkeypatch.setattr(ops, "_brief", fake_brief)
    handler = get_op_handler("consolidate_assemble.brief")

    assert asyncio.run(handler({"superseded": {"b": "abc"}}, tmp_path)) == {"artifact": "x"}
    assert seen == {"repo_root": tmp_path, "superseded": {"b": "abc"}}

    seen.clear()
    asyncio.run(handler({}, tmp_path))
    assert seen == {"repo_root": tmp_path}
