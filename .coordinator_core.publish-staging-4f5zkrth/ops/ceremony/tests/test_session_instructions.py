"""
coordinator_core.ops.ceremony.tests.test_session_instructions

Tests for the ceremony.session_instructions render op.

Coverage:
  (a) missing_sid          — exit_code=1 when 'sid' param absent
  (b) missing_repo_root    — exit_code=1 when repo_root is None
  (c) invocable_via_registry — get_op_handler("ceremony.session_instructions") resolves
  (d) invocable_via_handler_registry_call — the registered handler callable, invoked
                                             through get_op_handler (the seam
                                             coordinator_core.invoke itself uses),
                                             returns the same shape as a direct call
  (T2) genericity          — rendered output carries no wsc step-ID or phase-label literal
  (prune) membership       — single-session output omits STEP_2_7/2_75/2_9C/2_96;
                              chain-terminal includes them
  (DR-502) dropdown        — a J-node's dropdown carries harvested_default without
                              pre-selecting; the EM-pick (answer) is still required/empty

Spec backlink:
  coordinator_core/ops/ceremony/session_instructions.py
  docs/plans/2026-07-08-session-specific-instruction-set-emitter.op-spec.md §§1,4,5,6,8
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Optional

import pytest

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops.ceremony.session_instructions import _handler
from coordinator_core.ops.ceremony.node_handlers import (
    STEP_2_7,
    STEP_2_75,
    STEP_2_96,
    STEP_2_9C,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run(result) -> Any:
    """Pass a plain (already computed) result through unchanged
    (2026-08-07: _handler is now plain `def`)."""
    return result


class SessionInstructionsRepo:
    """Lightweight git repo fixture mirroring WscResolveRepo (test_wsc_resolve.py)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / ".git" / "coordinator-sessions").mkdir(parents=True, exist_ok=True)
        (root / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
        (root / "cross-repo" / "inbox").mkdir(parents=True, exist_ok=True)
        (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
        (root / "archive" / "completed").mkdir(parents=True, exist_ok=True)

    @property
    def common_dir(self) -> Path:
        return self.root / ".git"

    def seed_session_shape(
        self,
        sid: str,
        *,
        pickup_happened: bool = False,
        handoff_path: str = "",
        scope_mode: str = "architecture",
    ) -> Path:
        sid_dir = self.common_dir / "coordinator-sessions" / sid
        sid_dir.mkdir(parents=True, exist_ok=True)
        shape: dict[str, Any] = {
            "schema_version": 1,
            "pickup": {"happened": pickup_happened, "handoff_path": handoff_path},
            "actioned_memos": [],
            "plan": {"scope_mode": scope_mode},
            "magnitude": "",
        }
        path = sid_dir / "session-shape.json"
        path.write_text(json.dumps(shape, indent=2), encoding="utf-8")
        return path

    def seed_handoff(self, name: str, *, claimed_by: Optional[str] = None) -> Path:
        path = self.root / "state" / "handoffs" / name
        lines = [
            'title: "Test Handoff"',
            "created: 2026-01-01",
            "branch: work/test/2026-01-01",
            "status: claimed" if claimed_by else "status: open",
        ]
        if claimed_by:
            lines.append(f"claimed_by: {claimed_by}")
            lines.append("claimed_at: 2026-01-02")
        fm = "\n".join(lines)
        path.write_text(f"---\n{fm}\n---\n\n# Handoff Body\n", encoding="utf-8")
        return path

    def invoke(self, sid: str, extra_params: Optional[dict] = None) -> dict:
        params: dict[str, Any] = {"sid": sid}
        if extra_params:
            params.update(extra_params)
        return _run(_handler(params, repo_root=self.common_dir))


@pytest.fixture
def repo(tmp_path) -> SessionInstructionsRepo:
    return SessionInstructionsRepo(tmp_path / "repo")


# ---------------------------------------------------------------------------
# (a) missing_sid
# ---------------------------------------------------------------------------


def test_missing_sid(repo):
    result = _run(_handler({}, repo_root=repo.common_dir))
    assert result["exit_code"] == 1
    assert "sid" in result["error"]


# ---------------------------------------------------------------------------
# (b) missing_repo_root
# ---------------------------------------------------------------------------


def test_missing_repo_root():
    result = _run(_handler({"sid": "abc123"}, repo_root=None))
    assert result["exit_code"] == 1
    assert "repo_root" in result["error"]


# ---------------------------------------------------------------------------
# (c) invocable_via_registry
# ---------------------------------------------------------------------------


def test_invocable_via_registry():
    handler = get_op_handler("ceremony.session_instructions")
    assert handler is not None, (
        "ceremony.session_instructions must self-register via @register_op "
        "when coordinator_core.ops is imported"
    )


# ---------------------------------------------------------------------------
# (d) invocable_via_handler_registry_call — exercises the SAME callable
# coordinator_core.invoke's dispatch_message resolves via get_op_handler(method),
# confirming the registry entry is the live handler (not a stale/duplicate object).
# ---------------------------------------------------------------------------


def test_invocable_via_handler_registry_call(repo):
    sid = "sess-registry-call-001"
    repo.seed_session_shape(sid, pickup_happened=False)

    handler = get_op_handler("ceremony.session_instructions")
    assert handler is not None
    result = _run(handler({"sid": sid}, repo_root=repo.common_dir))
    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"
    assert "instructions" in result


# ---------------------------------------------------------------------------
# Membership prune (op-spec §8.1)
# ---------------------------------------------------------------------------


def test_single_session_omits_chain_terminal_steps(repo):
    sid = "sess-instr-single-001"
    repo.seed_session_shape(sid, pickup_happened=False)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"

    rendered_ids = {entry["id"] for entry in result["instructions"]}
    assert STEP_2_7 not in rendered_ids
    assert STEP_2_75 not in rendered_ids
    assert STEP_2_9C not in rendered_ids
    assert STEP_2_96 not in rendered_ids

    assert rendered_ids == set(result["applicable_node_ids"]), (
        "rendered instruction ids must match applicable_node_ids exactly (prune-by-membership)"
    )


def test_chain_terminal_includes_chain_terminal_steps(repo):
    sid = "sess-instr-chain-001"
    repo.seed_handoff("consumed-instr.md", claimed_by=sid)
    repo.seed_session_shape(
        sid, pickup_happened=True, handoff_path="state/handoffs/consumed-instr.md"
    )
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"

    rendered_ids = {entry["id"] for entry in result["instructions"]}
    assert STEP_2_7 in rendered_ids
    assert STEP_2_9C in rendered_ids
    # Negative-spec: STEP_2_75 is deliberately NOT here. Reviewer finding 690dd6f9
    # dropped it from `branch_resolution._CHAIN_TERMINAL_ONLY_STEPS` because the
    # handoff-tracker retirement removed its only `ctx.add_node` call site, so the
    # id can never reach the ledger. Re-asserting it pins a step that cannot render.
    assert STEP_2_75 not in rendered_ids


# ---------------------------------------------------------------------------
# T2 — genericity (op-spec §8.2): no wsc step-ID or phase-label literal in the
# rendered output. Asserted by construction — the render function keys ONLY on
# node["type"] (see session_instructions._render_node), so no entry can carry a
# step-ID-shaped or wsc-phase-label-shaped VALUE either.  Grep-style check below
# is defense-in-depth on top of the by-construction guarantee.
# ---------------------------------------------------------------------------

_WSC_STEP_ID_LITERALS = (
    "step_2.7", "step_2.75", "step_2.9c", "step_2.96",
    "branch 12", "branch_12",
)


def test_genericity_no_wsc_literals_in_rendered_values(repo):
    sid = "sess-instr-generic-001"
    repo.seed_session_shape(sid, pickup_happened=False)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    for entry in result["instructions"]:
        assert set(entry.keys()) <= {
            "id", "type", "instruction", "resolving_op",
            "question", "dropdown", "prose_slot", "review_bracket",
        }, f"rendered entry introduced a non-generic key: {entry.keys()!r}"
        # The entry's `id` is expected to be the step id itself (from applicable_node_ids) —
        # excluded from the literal-scan; every OTHER rendered value must be free of
        # wsc step-ID / phase-label literals.
        for key, value in entry.items():
            if key == "id":
                continue
            serialized = json.dumps(value).lower()
            for literal in _WSC_STEP_ID_LITERALS:
                assert literal not in serialized, (
                    f"rendered field {key!r} leaked wsc-specific literal {literal!r}: {value!r}"
                )


def test_genericity_render_keys_only_on_type():
    """By-construction check: _render_node's branches are dispatched on node['type']
    alone — no step-id conditional exists in the render function.
    """
    from coordinator_core.ops.ceremony import session_instructions as mod

    source = inspect.getsource(mod._render_node)
    for literal in ("STEP_2_7", "STEP_2_75", "STEP_2_9C", "Branch 12", "chain_terminal"):
        assert literal not in source, (
            f"_render_node must not contain the ceremony-specific literal {literal!r}"
        )


# ---------------------------------------------------------------------------
# DR-502 — dropdown pre-fills, never pre-selects (op-spec §6)
# ---------------------------------------------------------------------------


def test_j_node_dropdown_harvested_default_does_not_preselect(repo):
    sid = "sess-instr-dr502-001"
    repo.seed_session_shape(sid, pickup_happened=False)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    j_entries = [e for e in result["instructions"] if e["type"] == "J"]
    assert j_entries, "expected at least one applicable J-node in single-session disposition"

    for entry in j_entries:
        assert "dropdown" in entry
        dropdown = entry["dropdown"]
        assert set(dropdown.keys()) == {"question", "options", "harvested_default"}
        # DR-502: harvested_default may be pre-filled but never pre-selected. In this
        # fixture no commits exist yet, so Tier-2 harvest is silent — harvested_default
        # must be None (never a guessed value), and the render carries no EM-authored
        # answer field on the entry at all (the underlying node's answer stays "" in ctx —
        # the render op never promotes a default into an affirmative pick).
        assert dropdown["harvested_default"] is None, (
            "no commits were made in this fixture — harvested_default must be None "
            "(Tier-2 harvest fallback), never a guessed value"
        )
        assert "answer" not in entry, (
            "the render op must never surface a pre-selected 'answer' on a J-node entry — "
            "that would collapse the dropdown into de-facto auto-resolve (DR-502)"
        )
