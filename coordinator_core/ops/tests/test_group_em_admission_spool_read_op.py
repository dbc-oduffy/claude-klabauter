"""Tests for "groupem.admission_spool_read"."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.group_em import admission_spool
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_admission_spool_read  # noqa: F401
from coordinator_core.remote_engine import policy

OP = "groupem.admission_spool_read"


def test_registration_set() -> None:
    assert ipc._REGISTRY.get(OP) is not None
    assert _registry_map.OP_MODULE_MAP[OP] == "coordinator_core.ops.group_em_admission_spool_read"
    assert OP_KEY_SCOPE[OP] == "none"
    assert OP_CLASSIFICATION[OP] is OpClass.COMPUTE_ONLY


def test_remote_safe() -> None:
    assert policy.remote_safe(OP)


def test_reads_from_offset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert admission_spool.spool_line({"phase": "START", "session_id": "a"}) is None
    assert admission_spool.spool_line({"phase": "END", "session_id": "a"}) is None
    first = ipc._REGISTRY[OP]({"offset": 0})
    assert first["ok"] is True and len(first["lines"]) == 2
    assert first["lines"] == admission_spool.read_since(0)["lines"]
    rest = ipc._REGISTRY[OP]({"offset": len(first["lines"][0].encode()) + 1})
    assert rest["lines"] == first["lines"][1:]
    assert rest["next_offset"] == first["next_offset"]


def test_missing_spool_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    out = ipc._REGISTRY[OP]({"offset": 5})
    assert out == {"ok": True, "lines": [], "next_offset": 0}


@pytest.mark.parametrize("params", [{}, {"offset": -1}, {"offset": "0"}, {"offset": True}, {"offset": 1.5}, None])
def test_invalid_offset_refused(params) -> None:
    assert "error" in ipc._REGISTRY[OP](params)
