"""A `hooks.*` op is eager through `coordinator_core.hooks._EAGER_HOOK_MODULES`, the list the lazy
hooks package's `_eager_import_all()` iterates; `_EAGER_OP_MODULES` names only the package.

Before: every commit touching a hook module was refused, "hooks.<op>: _EAGER_OP_MODULES in
coordinator_core/ops/__init__.py", though the op registers eagerly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.authz.registration_quad import check_registration_quad
from coordinator_core.authz.registration_quad_static import registration_violations

pytestmark = pytest.mark.complete

_ROOT = Path(__file__).resolve().parents[3]
_HOOK = "coordinator_core/hooks/postuse_advisory_dispatch.py"
_HOOKS_INIT = "coordinator_core/hooks/__init__.py"


def _tree(overrides: Optional[dict] = None):
    def read(rel: str) -> Optional[bytes]:
        if overrides and rel in overrides:
            return overrides[rel]
        try:
            return (_ROOT / rel).read_bytes()
        except OSError:
            return None
    return read


def test_a_listed_hook_module_passes_the_static_gate() -> None:
    verdict = registration_violations(_tree(), [_HOOK])
    assert verdict.outcome != "refuse", verdict.reason


def test_a_hook_module_dropped_from_the_hook_list_still_refuses() -> None:
    init = (_ROOT / _HOOKS_INIT).read_bytes().replace(
        b'    "coordinator_core.hooks.postuse_advisory_dispatch",\n', b""
    )
    verdict = registration_violations(_tree({_HOOKS_INIT: init}), [_HOOK])
    assert verdict.outcome == "refuse"
    assert "hooks.postuse_advisory_dispatch" in verdict.reason


def test_the_live_check_counts_a_listed_hook_module_as_eager() -> None:
    v = check_registration_quad(
        registry={"hooks.postuse_advisory_dispatch": None},
        classification={"hooks.postuse_advisory_dispatch": None},
        scope={"hooks.postuse_advisory_dispatch": None},
        module_map={"hooks.postuse_advisory_dispatch": "coordinator_core.hooks.postuse_advisory_dispatch"},
        eager_modules=frozenset({"coordinator_core.hooks"}),
    )
    assert v == []
