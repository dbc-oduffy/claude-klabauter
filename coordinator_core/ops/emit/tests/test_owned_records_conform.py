"""Oracle: every claude-klabauter-owned handoff, plan and cross-repo memo passes the emit filter and the pin.

Reads the live repo tree on purpose, as a regression guard: a hand-written record that lacks a
Cockpit-required field turns this red and the failure message is the worklist. Read-only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.emit import validate
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import cross_repo_memos, handoffs, plans

_REPO_ROOT = Path(__file__).resolve().parents[4]

_SECTIONS = (
    ("handoffs", handoffs, "handoff-summary"),
    ("plans", plans, "plan-summary"),
    ("cross_repo_memos", cross_repo_memos, "cross-repo-memo-summary"),
)


def _live_ctx() -> EmitContext:
    return EmitContext(
        repo_root=_REPO_ROOT,
        coordinator_root=_REPO_ROOT,
        central_state_root=_REPO_ROOT / "state",
        git_branch="oracle",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-01-01T00:00:00Z",
        hostname="oracle-host",
        repo_name="dbc-oduffy/claude-klabauter",
    )


@pytest.fixture(scope="module")
def collected() -> dict[str, tuple[list[dict], list[dict]]]:
    ctx = _live_ctx()
    return {name: module.collect(ctx) for name, module, _ in _SECTIONS}


@pytest.mark.parametrize("name", [s[0] for s in _SECTIONS])
def test_no_malformed_records(collected, name):
    _, malformed = collected[name]
    worklist = "\n".join(f"  {m.get('path')}: {m.get('reason')}" for m in malformed)
    assert not malformed, f"{name}: {len(malformed)} malformed record(s)\n{worklist}"


@pytest.mark.parametrize("name,entity", [(s[0], s[2]) for s in _SECTIONS])
def test_emitted_rows_pass_the_pin(collected, name, entity):
    records, _ = collected[name]
    assert records, f"{name}: no rows emitted; the oracle would be vacuous"
    validator = _compiled(entity)
    failures = []
    for row in records:
        errors = sorted(validator.iter_errors(row), key=lambda e: list(e.path))
        if errors:
            path = (row.get("provenance") or {}).get("path")
            failures.append(f"  {path}: {errors[0].message}")
    assert not failures, f"{name}: {len(failures)} row(s) fail {entity}\n" + "\n".join(failures[:20])


def _compiled(entity: str):
    return validate._compiled_entity_validator(entity)
