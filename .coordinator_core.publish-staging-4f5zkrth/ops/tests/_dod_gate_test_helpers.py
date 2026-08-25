"""
coordinator_core.ops.tests._dod_gate_test_helpers

Shared hermeticity helpers for the DoD gate dimension test suite (C1b/C2/C3,
docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md), imported by test
files in both `coordinator_core/ops/tests/` and `coordinator/tests/` (a plain
package import, not a conftest fixture -- conftest fixtures don't cross those
two suite roots, and the repo-root `conftest.py`'s own negative spec forbids
adding fixtures there).

WHY THIS EXISTS -- TWO DISTINCT HERMETICITY GAPS
    1. `resolve_tool()` stubbing. Every dimension test that asserts a
       tool-present or tool-absent verdict must stub `resolve_tool` rather
       than let it hit the real machine -- otherwise the suite's own verdict
       depends on whether mypy/ruff/interrogate/diff-cover/pytest-cov happen
       to be installed on the machine running it. `available()`/
       `unavailable()`/`resolve_tool_stub()` below replace five near-duplicate
       hand-rolled monkeypatch lambdas with one shared shape.

    2. Dimension-registry stub-shape pollution. `test_gate_dimension_types.py`
       and `test_gate_dimension_docstrings.py` import their own dimension
       modules, and each import's `register_dimension()` side effect
       permanently replaces that slot in the *module-level*
       `_DIMENSION_REGISTRY` for the rest of the pytest session -- regardless
       of test order, a whole-session collection that includes those two
       files means `test_gate_validate_invocable.py` and
       `test_gate_validate_invocable_cli.py` no longer see the "types"/
       "docstrings" stub they assert against; they see the REAL check. With
       mypy/ruff/interrogate absent on PATH, the real check still degrades to
       UNAVAILABLE, so this reads as passing by accident -- the same
       tool-absence-shaped illusion `resolve_tool_stub()` exists to close.
       Install any one of those tools (or, as proven in this fix's own
       validation, simulate presence via a PATH/sys.path shim with no
       install) and the real check starts returning PASS, and five tests in
       those two files that assert "the untouched slot is still stub
       UNAVAILABLE" break for a reason that has nothing to do with the code
       under test. `canonical_stub_dimension_registry()` closes this by
       building the registry's default stub shape fresh from
       `gate_validate_invocable`'s own stub factories, rather than trusting
       whatever `_DIMENSION_REGISTRY` happens to hold at test start.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C1b
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from coordinator_core.ops.gate_tool_resolve import ToolResolution


def available(tool: str, path: Optional[str] = None) -> ToolResolution:
    """A `ToolResolution` simulating `tool` present on PATH (or importable,
    for pytest-cov) -- no real install, no real `shutil.which`/`find_spec`
    call."""
    return ToolResolution(
        tool=tool, available=True, path=path or f"/fake/bin/{tool}", reason="resolved"
    )


def unavailable(tool: str, reason: Optional[str] = None) -> ToolResolution:
    """A `ToolResolution` simulating `tool` absent -- the shape every
    UNAVAILABLE-degrade test asserts against."""
    return ToolResolution(
        tool=tool,
        available=False,
        path=None,
        reason=reason or f"{tool} not found; install with: pip install {tool}",
    )


def resolve_tool_stub(
    overrides: Dict[str, ToolResolution],
) -> Callable[[str], ToolResolution]:
    """Build a `resolve_tool`-shaped replacement: a tool named in `overrides`
    returns that literal `ToolResolution`; every other tool defaults to
    `available()`. Covers the common "one tool absent, the rest present"
    shape in one call instead of a per-test lambda with an inline
    if/else branch."""

    def _resolve(tool: str) -> ToolResolution:
        if tool in overrides:
            return overrides[tool]
        return available(tool)

    return _resolve


def canonical_stub_dimension_registry() -> dict:
    """The five-dimension `_DIMENSION_REGISTRY` shape `gate_validate_invocable`
    ships with before any `register_dimension()` call has fired this session.

    Built fresh from that module's own stub factories (`_stub_unavailable`,
    `_tests_stub_skipped_gated`) rather than a snapshot of the live registry,
    so a test asserting "this slot is still stub" is deterministic regardless
    of whether some OTHER test file in the same pytest session already
    imported a real dimension module (see this module's docstring, gap 2).
    """
    from coordinator_core.ops.gate_validate_invocable import (
        DIMENSION_NAMES,
        _stub_unavailable,
        _tests_stub_skipped_gated,
    )

    registry = {
        name: _stub_unavailable(name, f"{name} not wired (stub)")
        for name in DIMENSION_NAMES
        if name != "tests"
    }
    if "tests" in DIMENSION_NAMES:
        registry["tests"] = _tests_stub_skipped_gated
    return registry
