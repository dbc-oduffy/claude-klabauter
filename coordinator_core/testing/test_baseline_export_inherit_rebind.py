"""The baseline-export trampoline passes pid-bound handoffs one hop, re-stamped to itself."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coordinator_core.testing import baseline_export

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "coordinator" / "lib"))
from percolate.wire_contract import INHERITED_LOCK_ROOTS_ENV  # noqa: E402

# Trap: a static import of coordinator_core.percolate breaks the restricted klabauter publish
# tree's import closure (it does not ship percolate), so the contract module is looked up here.
_slot = pytest.importorskip("coordinator_core.percolate.publish_slot")
PUBLISH_SLOT_INHERITED_ENV = _slot.PUBLISH_SLOT_INHERITED_ENV
decode_inherited = _slot.decode_inherited


def test_rebound_env_names_are_the_live_contract_names():
    assert set(baseline_export._PID_BOUND_INHERIT_ENVS) == {
        PUBLISH_SLOT_INHERITED_ENV,
        INHERITED_LOCK_ROOTS_ENV,
    }


def test_parent_stamped_slot_token_reaches_the_runner(tmp_path):
    parent, me = os.getppid(), os.getpid()
    base = {
        PUBLISH_SLOT_INHERITED_ENV: f"{parent}=claude-klabauter=host/1/s/n",
        INHERITED_LOCK_ROOTS_ENV: f"{parent}=/dest",
    }
    env = baseline_export.export_env(tmp_path / "x", base)
    assert decode_inherited(env[PUBLISH_SLOT_INHERITED_ENV], me) == {
        "claude-klabauter": "host/1/s/n"
    }
    assert env[INHERITED_LOCK_ROOTS_ENV] == f"{me}=/dest"


def test_entries_stamped_by_anyone_else_are_dropped(tmp_path):
    base = {PUBLISH_SLOT_INHERITED_ENV: "1=claude-klabauter=host/1/s/n", INHERITED_LOCK_ROOTS_ENV: "junk"}
    env = baseline_export.export_env(tmp_path / "x", base)
    assert PUBLISH_SLOT_INHERITED_ENV not in env
    assert INHERITED_LOCK_ROOTS_ENV not in env
