"""Tests for coordinator_core.ops.detect_plugin_layout (op detect.plugin_layout).

Covers the flat/nested classification, the register_op handler
contract (plain sync handler), and the CC-4/AC7 double-invocation idempotency
proof (pure read-only existence check — a second call with identical inputs
is a no-op that yields byte-identical output).
"""
from __future__ import annotations

from coordinator_core.ops.detect_plugin_layout import _handler, classify_plugin_layout


def test_flat_layout_when_agent_md_present(tmp_path):
    marker = tmp_path / "docs" / "install" / "AGENT.md"
    marker.parent.mkdir(parents=True)
    marker.write_text("agent\n", encoding="utf-8")

    assert classify_plugin_layout(tmp_path) == "flat"


def test_nested_layout_when_agent_md_absent(tmp_path):
    assert classify_plugin_layout(tmp_path) == "nested"


def test_nested_layout_when_docs_install_dir_entirely_absent(tmp_path):
    plugin_root = tmp_path / "plugins" / "coordinator-claude" / "coordinator"
    plugin_root.mkdir(parents=True)

    assert classify_plugin_layout(plugin_root) == "nested"


def test_handler_returns_flat_layout(tmp_path):
    marker = tmp_path / "docs" / "install" / "AGENT.md"
    marker.parent.mkdir(parents=True)
    marker.write_text("agent\n", encoding="utf-8")

    result = _handler({"plugin_root": str(tmp_path)})

    assert result == {"layout": "flat"}


def test_handler_returns_nested_layout(tmp_path):
    result = _handler({"plugin_root": str(tmp_path)})

    assert result == {"layout": "nested"}


def test_double_invocation_is_idempotent_no_op(tmp_path):
    marker = tmp_path / "docs" / "install" / "AGENT.md"
    marker.parent.mkdir(parents=True)
    marker.write_text("agent\n", encoding="utf-8")

    params = {"plugin_root": str(tmp_path)}
    first = _handler(params)
    second = _handler(params)

    assert first == second == {"layout": "flat"}
