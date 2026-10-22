"""Parity: the control-socket protocol wiki page names every protocol.py constant."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1]
_PROTOCOL = _BIN / "lib" / "claude_author_control" / "protocol.py"
_DOC = _BIN.parent / "docs" / "wiki" / "claude-author-control-socket-protocol.md"


def _load():
    spec = importlib.util.spec_from_file_location("_cac_protocol_doc_parity", _PROTOCOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


if not _DOC.is_file():
    pytest.skip("protocol wiki page is not shipped in this tree", allow_module_level=True)

proto = _load()
DOC = _DOC.read_text(encoding="utf-8")


def test_every_op_documented():
    for op in proto.OPS:
        assert f"`{op}`" in DOC, op


def test_every_error_code_documented():
    for code in proto.ERROR_CODES:
        assert f"`{code}`" in DOC, code


def test_every_state_documented():
    for state in proto.STATES:
        assert f"`{state}`" in DOC, state


def test_deferred_idle_marked_reserved():
    assert "`deferred_idle` is reserved" in DOC
    assert proto.RESERVED_STATES == ("deferred_idle",)


def test_status_and_discovery_fields_documented():
    for field in proto.STATUS_FIELDS + proto.DISCOVERY_FIELDS:
        assert field in DOC, field


def test_env_knobs_and_defaults_documented():
    for name in (proto.ENV_IDLE_MS, proto.ENV_FORCE_SETTLE_MS):
        assert name in DOC
    assert f"default {proto.DEFAULT_IDLE_MS})" in DOC
    assert f"default {proto.DEFAULT_FORCE_SETTLE_MS})" in DOC


def test_marker_and_limits_documented():
    assert proto.RELAY_MARKER_BASE in DOC
    assert proto.RELAY_MARKER_PM.strip() in DOC
    assert str(proto.MAX_SOCK_PATH_BYTES) in DOC
    assert "1 MiB" in DOC
    assert proto.PIPE_PREFIX in DOC
