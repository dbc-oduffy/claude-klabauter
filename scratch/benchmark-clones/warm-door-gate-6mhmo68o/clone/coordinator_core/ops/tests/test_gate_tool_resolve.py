"""
coordinator_core.ops.tests.test_gate_tool_resolve

Unit tests for the shared DoD tool-resolution seam (C1b,
docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C1b) — each of the
five tools resolving and not resolving, and the `UNAVAILABLE`-shaped reason
string being caller-legible (names the tool and how to get it).

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C1b
"""

from __future__ import annotations

import pytest

from coordinator_core.ops import gate_tool_resolve
from coordinator_core.ops.gate_tool_resolve import KNOWN_TOOLS, resolve_tool


def test_unknown_tool_raises() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        resolve_tool("not-a-real-tool")


@pytest.mark.parametrize("tool", ["mypy", "ruff", "interrogate", "diff-cover"])
def test_which_backed_tool_resolves_when_present(monkeypatch, tool: str) -> None:
    monkeypatch.setattr(
        gate_tool_resolve.shutil, "which", lambda name: f"/fake/bin/{name}"
    )
    result = resolve_tool(tool)
    assert result.available is True
    assert result.path == f"/fake/bin/{tool}"
    assert result.reason == "resolved"


@pytest.mark.parametrize("tool", ["mypy", "ruff", "interrogate", "diff-cover"])
def test_which_backed_tool_unavailable_when_absent(monkeypatch, tool: str) -> None:
    monkeypatch.setattr(gate_tool_resolve.shutil, "which", lambda name: None)
    result = resolve_tool(tool)
    assert result.available is False
    assert result.path is None
    assert tool in result.reason
    assert "install" in result.reason.lower()


def test_pytest_cov_resolves_when_module_importable(monkeypatch) -> None:
    import importlib.util

    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: object() if name == "pytest_cov" else None
    )
    result = resolve_tool("pytest-cov")
    assert result.available is True
    assert result.path is None
    assert result.reason == "resolved"


def test_pytest_cov_unavailable_when_module_missing(monkeypatch) -> None:
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    result = resolve_tool("pytest-cov")
    assert result.available is False
    assert result.path is None
    assert "pytest-cov" in result.reason
    assert "install" in result.reason.lower()


def test_all_known_tools_have_install_hints() -> None:
    for tool, hint in KNOWN_TOOLS.items():
        assert hint, f"{tool} has an empty install hint"


def test_live_resolution_does_not_raise_for_every_known_tool() -> None:
    """Smoke test against the real machine (no monkeypatching) -- resolution
    must never raise regardless of what's actually installed here."""
    for tool in KNOWN_TOOLS:
        result = resolve_tool(tool)
        assert result.tool == tool
        assert isinstance(result.available, bool)
