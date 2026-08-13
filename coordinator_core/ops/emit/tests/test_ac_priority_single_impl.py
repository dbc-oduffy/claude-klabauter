"""Acceptance battery, part 4 — dangling targets + ledger-guard (AC2, AC9, AC16).

C6 (2026-07-30): this module used to also cross-check the emission path
(``ops/emit/sections/handoffs.py``) against a SECOND caller of
``priority_resolve.resolve_priority`` on the orientation side
(``orientation.regenerate_cache._emit_priorities``) — the
SINGLE-IMPLEMENTATION-BY-IMPORT guarantee (priority_resolve.py module
docstring) that a future second walk forked into only one of the two paths
would break. The orientation-side caller is retired: the writer's
``## Priorities`` section (handoff/spinoff-derived, answer-shaped) is gone
in favour of computed ROUTING pointers — a skill already recomputes priority
at the moment it matters (plan/baton collision at planning), so boot no
longer carries it at all. ``resolve_priority`` now has exactly one caller
(the emission path below), which makes the cross-check moot rather than
failing — nothing left to fork a second walk into.

Spec backlink: coordinator-claude docs/plans/2026-07-26-priority-ledger.md § Acceptance Criteria
  (AC2, AC9, AC16).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import handoffs as handoffs_section
from coordinator_core.write_guards import block_priority_ledger_edit as ledger_guard

# Review: coordinator:code-reviewer — Finding 1: _write_node extracted to conftest.py
# (shared across the five priority-ledger test modules that used a byte-for-byte copy).
from coordinator_core.ops.emit.tests.conftest import _write_node  # noqa: F401


def _base_handoff_fm(**overrides) -> dict:
    fm = {
        "title": "Test Handoff",
        "created": "2026-07-27",
        "status": "open",
        "deployment_state": "shipped",
        "predecessor": "none",
    }
    fm.update(overrides)
    return fm


def _make_ctx(repo_root: Path, repo_name: str = "test-org/test-repo") -> EmitContext:
    central = repo_root / "state"
    central.mkdir(parents=True, exist_ok=True)
    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=central,
        git_branch="test-branch",
        git_sha="0" * 40,
        git_sha_short="00000000",
        observed_at="2026-07-27T00:00:00Z",
        hostname="test-host",
        repo_name=repo_name,
    )


def _emission_records(tmp_path: Path, records: list[dict], ledger_entries: dict, repo_name: str):
    """Run the EMISSION path (``handoffs_section.collect``) against mocked
    ``_query_records``/``load_priority_ledger``, returning ``(records, malformed)``.
    """
    ctx = _make_ctx(tmp_path, repo_name=repo_name)

    def query_records(ctx_arg, record_type):
        if record_type == "handoff":
            return records
        return []

    with patch.object(handoffs_section, "_query_records", side_effect=query_records), \
            patch.object(handoffs_section, "load_priority_ledger", return_value=ledger_entries):
        return handoffs_section.collect(ctx)


# ---------------------------------------------------------------------------
# AC16 — dangling target: a ledger entry whose target_id resolves to no
# emitted handoff is REPORTED in the malformed bucket, never carried as a
# record. Inverse: a non-"handoff" target_kind entry is NOT flagged by the
# handoffs section (the ledger holds assignments for targets defined
# elsewhere; a plan-targeted entry is not a handoffs-section defect).
# ---------------------------------------------------------------------------


def test_dangling_handoff_target_reported_not_carried_as_record(tmp_path: Path):
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    a_id = "hnd-node-a-aaaaaa"
    _write_node(handoff_dir, "a.md", handoff_id=a_id, predecessor=None)

    records = [
        {"path": "state/handoffs/a.md", "frontmatter": _base_handoff_fm(handoff_id=a_id)},
    ]
    ledger_entries = {
        a_id: {"priority": "high", "target_kind": "handoff"},
        "hnd-ghost-999999": {"priority": "urgent", "target_kind": "handoff"},
    }

    emitted, malformed = _emission_records(tmp_path, records, ledger_entries, "test-org/repo")

    assert len(emitted) == 1
    emitted_ids = {r["handoff_id"] for r in emitted}
    assert "hnd-ghost-999999" not in emitted_ids  # never becomes a record

    dangling = [m for m in malformed if "hnd-ghost-999999" in m.get("reason", "")]
    assert len(dangling) == 1
    assert "dangling" in dangling[0]["reason"]


def test_dangling_check_ignores_non_handoff_target_kind(tmp_path: Path):
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    a_id = "hnd-node-a-aaaaaa"
    _write_node(handoff_dir, "a.md", handoff_id=a_id, predecessor=None)

    records = [
        {"path": "state/handoffs/a.md", "frontmatter": _base_handoff_fm(handoff_id=a_id)},
    ]
    ledger_entries = {
        a_id: {"priority": "high", "target_kind": "handoff"},
        "pln-some-plan-000000": {"priority": "urgent", "target_kind": "plan"},
    }

    emitted, malformed = _emission_records(tmp_path, records, ledger_entries, "test-org/repo")

    assert len(emitted) == 1
    assert malformed == []


# ---------------------------------------------------------------------------
# AC9 — handoff_id is populated on EVERY emitted record: authored (matching
# the minted shape) where present, derived otherwise, with
# handoff_id_derivation discriminating the two. No emitted record ever has a
# null handoff_id.
# ---------------------------------------------------------------------------


def test_handoff_id_authored_and_derived_both_present_never_null(tmp_path: Path):
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)

    authored_id = "hnd-authored-node-abc123"
    _write_node(handoff_dir, "authored.md", handoff_id=authored_id, predecessor=None)
    _write_node(handoff_dir, "no-id.md", predecessor=None)

    records = [
        {
            "path": "state/handoffs/authored.md",
            "frontmatter": _base_handoff_fm(handoff_id=authored_id),
        },
        {
            "path": "state/handoffs/no-id.md",
            "frontmatter": _base_handoff_fm(),  # no handoff_id key at all
        },
    ]

    emitted, malformed = _emission_records(tmp_path, records, {}, "test-org/repo")

    assert malformed == []
    assert len(emitted) == 2
    for r in emitted:
        assert r["handoff_id"] is not None
        assert r["handoff_id_derivation"] in ("authored", "derived")

    by_path = {r["provenance"]["path"]: r for r in emitted}
    authored_record = by_path["state/handoffs/authored.md"]
    derived_record = by_path["state/handoffs/no-id.md"]

    assert authored_record["handoff_id"] == authored_id
    assert authored_record["handoff_id_derivation"] == "authored"

    assert derived_record["handoff_id"] != authored_id
    assert derived_record["handoff_id_derivation"] == "derived"


# ---------------------------------------------------------------------------
# AC2 — a hand-edit of the ledger directory is intercepted by a path-matched
# guard that NAMES the op (design-as-offers: an offer, not a bare refusal).
# ---------------------------------------------------------------------------


def test_ledger_hand_edit_is_redirected_and_names_priority_set():
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "state/priority-ledger/hnd-some-target-abc123.yaml",
            "content": "priority: high\n",
        },
    }

    result = ledger_guard.check(payload)

    assert result is not None
    # a69586381 flipped this guard from a hard deny to an ADVISORY redirect (guard-class
    # census, DR-27): it now emits `additionalContext` and no `permissionDecision` at all.
    # That is the doctrine's ergonomics-over-enforcement default — the acceptance criterion
    # here was never "deny", it was "the redirect names the op", which the advisory shape
    # carries verbatim. Pin the message, not the enforcement class.
    hook_output = result["hookSpecificOutput"]
    assert "permissionDecision" not in hook_output
    reason = hook_output["additionalContext"]

    # The guard's denial MESSAGE names the alternative op — not a bare
    # refusal. This is the acceptance-level pin: the guard offers the
    # `priority.set` op (via its `priority-set` CLI trampoline name) as the
    # right way to accomplish what the blocked hand-edit was trying to do.
    assert "priority-set" in reason
    assert "priority.set" in reason
