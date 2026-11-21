"""external_workspace.rehydrate: clones only the named key into its registered path, resets a
worktree-less .git, no-ops a healthy one, and reports (never clones) under all_missing."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops import external_workspace_rehydrate as op
from coordinator_core.ops._registry_map import OP_MODULE_MAP

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def test_op_is_registered_in_the_lazy_map_and_eager_list():
    from coordinator_core.ops import _EAGER_OP_MODULES

    assert OP_MODULE_MAP[op.OP_KEY] == "coordinator_core.ops.external_workspace_rehydrate"
    assert any(m == "coordinator_core.ops.external_workspace_rehydrate" for m, _ in _EAGER_OP_MODULES)
    assert op._SCHEMA["additionalProperties"] is False
