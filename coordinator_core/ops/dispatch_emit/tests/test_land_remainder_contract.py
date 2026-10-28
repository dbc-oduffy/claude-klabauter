"""Pins the land-verified-remainder contract and its registration anchors."""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ops.dispatch_emit import land_remainder_contract as c

_ROOT = Path(__file__).resolve().parents[4]


def test_op_name_and_module_resolve_in_registry_map():
    assert c.OP_NAME == "dispatch.land_verified_remainder"
    assert OP_MODULE_MAP[c.OP_NAME] == c.OP_MODULE


def test_op_scope_and_ops_init_register_the_name():
    scopes = (_ROOT / "coordinator_core/op_scopes.py").read_text(encoding="utf-8")
    assert re.search(rf'"{re.escape(c.OP_NAME)}":\s+"common_dir"', scopes)
    init = (_ROOT / "coordinator_core/ops/__init__.py").read_text(encoding="utf-8")
    assert f'("{c.OP_MODULE}", \'registers "{c.OP_NAME}"\')' in init


def test_keys_and_refusal_codes_pinned():
    assert c.REQUEST_KEYS == {"run_id", "worktree_root"}
    assert c.REPLY_KEYS == {"landed", "sha", "refused", "omitted", "closure_open", "retest"}
    assert {
        "out-of-write-set", "retest-failed", "import-closure-open", "not-halted", "no-base-export",
        "retest-pending",
    } == c.REFUSAL_CODES
