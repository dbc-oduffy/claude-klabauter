"""Shared pytest configuration for the coordinator_core.ops.emit test package.

Skip guards for tests that depend on the vendored cockpit-contract JSON Schema pin being
installed. On a fresh clone without running ``bin/claude-klabauter-revendor-cockpit-contract.py`` the
pin is absent and every test that calls
``emit()`` (which triggers ``validate.assert_pin_integrity()``) will fail with a
``ContractPinError`` before the test logic is even reached — giving a confusing error
on CI runners and fresh developer machines.

The ``requires_vendor_pin`` fixture (session-scoped) checks for the pin once and either
passes silently or skips the enclosing test with a clear remediation message.  Attach it
to any test that calls ``emit()`` directly: mark the test class/function with
``@pytest.mark.usefixtures("requires_vendor_pin")`` or request it as a fixture parameter.

Default-path/graceful-absent tests (test_emit_default_path.py, test_emit_graceful_absent.py)
use this fixture to avoid confusing ``ContractPinError`` failures on pin-absent machines
(F6, tc-3 code review).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Review: coordinator:code-reviewer — Finding 1 (S5 priority-ledger battery): _write_node/_ledger
# were copy-pasted byte-for-byte across five test modules; extracted here so drift can't happen.


def _write_node(dir_path: Path, name: str, **frontmatter) -> Path:
    """Write a minimal handoff-shaped .md fixture with the given frontmatter
    fields. ``None`` values are rendered as the literal ``none`` sentinel
    (mirrors real ``predecessor: none`` authoring); list values render as an
    inline YAML list. Omitting a key entirely means "no entry at all" for
    that field.
    """
    lines = ["---"]
    for key, value in frontmatter.items():
        if value is None:
            lines.append(f"{key}: none")
        elif isinstance(value, list):
            rendered = ", ".join(str(v) for v in value)
            lines.append(f"{key}: [{rendered}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {name}")
    path = dir_path / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _ledger(**entries) -> dict:
    """Build a ledger_entries dict: target_id -> {"priority": ..., "target_kind": "handoff"}."""
    return {tid: {"priority": prio, "target_kind": "handoff"} for tid, prio in entries.items()}

# Resolve pin paths relative to the validate module so this conftest never drifts
# from the actual integrity check in validate.assert_pin_integrity().
#
# 2026-07-21: the pin-integrity target moved from the (now-retired) built Zod artifacts
# (dist/index.js + the node validator .mjs — see validate.py module docstring's negative-spec)
# to the vendored JSON Schema bundle + per-entity schema directory, matching
# validate.assert_pin_integrity()'s own checks.
_EMIT_DIR = Path(__file__).resolve().parent.parent
_VENDOR_ROOT = _EMIT_DIR / "_vendor"
_VENDOR_SCHEMA_DIR = _VENDOR_ROOT / "cockpit-contract" / "schema"
_VENDOR_SCHEMA_BUNDLE = _VENDOR_SCHEMA_DIR / "cockpit-contract.schema.json"


def _vendor_pin_present() -> bool:
    return _VENDOR_SCHEMA_BUNDLE.exists() and any(_VENDOR_SCHEMA_DIR.glob("*.schema.json"))


@pytest.fixture(scope="session")
def requires_vendor_pin() -> None:
    """Skip the test when the vendored cockpit-contract pin is not installed.

    Run ``python bin/claude-klabauter-revendor-cockpit-contract.py`` to populate
    ``coordinator_core/ops/emit/_vendor/`` and unblock these tests.
    """
    if not _vendor_pin_present():
        pytest.skip(
            "Vendored cockpit-contract pin not installed — "
            "run python bin/claude-klabauter-revendor-cockpit-contract.py to populate "
            "coordinator_core/ops/emit/_vendor/ and unblock emit-dependent tests."
        )
