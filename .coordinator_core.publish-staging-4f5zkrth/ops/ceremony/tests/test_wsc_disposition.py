"""
coordinator_core.ops.ceremony.tests.test_wsc_disposition

Tests for wsc_disposition -- the canonical WSC consumed-handoff disposition
constants module and its read-side legacy-spelling alias.

Coverage:
  (a) canonicalize_accepts_both_spellings -- both "chain-terminal" (legacy) and
                                      "predecessor-consumed" (canonical) map to
                                      PREDECESSOR_CONSUMED
  (b) canonicalize_single_session_unchanged -- SINGLE_SESSION round-trips as-is
  (c) canonicalize_rejects_unknown_value -- an unrecognised token raises ValueError
  (d) valid_is_closed -- VALID contains exactly the four recognised spellings,
                          nothing else
  (e) write_token_bound_to_canonical -- WRITE_TOKEN is bound to the canonical
                                      spelling following C6's flip
  (f) persisted_receipts_resolve_through_canonicalize -- AC10: every
                                      ``state/ceremony/wsc/*.json`` receipt on
                                      disk (pre-flip, carrying the legacy
                                      spelling) still resolves through
                                      canonicalize() -- skips loudly (does not
                                      vacuously pass) when the corpus is
                                      empty, per state/lessons/2026-07-22-
                                      frozen-golden-over-an-empty-corpus-is-
                                      worse-than-a-skip.yaml
  (g) canonicalize_memo_predecessor_round_trips -- MEMO_PREDECESSOR ("memo-
                                      predecessor") canonicalizes to itself
                                      unchanged and has no legacy alias

Spec backlink: chunk C1, chunk C6 (test f), pln-wsc-completeness-gate-pickup-s-9793ca.
MEMO_PREDECESSOR coverage (test g) added by chunk C1,
docs/plans/2026-08-05-memo-predecessor-representable-outcome.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import wsc_disposition as wd

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WSC_CEREMONY_DIR = _REPO_ROOT / "state" / "ceremony" / "wsc"


def test_canonicalize_accepts_both_spellings():
    assert wd.canonicalize(wd.LEGACY_PREDECESSOR_CONSUMED) == wd.PREDECESSOR_CONSUMED
    assert wd.canonicalize(wd.PREDECESSOR_CONSUMED) == wd.PREDECESSOR_CONSUMED
    assert wd.canonicalize("chain-terminal") == "predecessor-consumed"
    assert wd.canonicalize("predecessor-consumed") == "predecessor-consumed"


def test_canonicalize_single_session_unchanged():
    assert wd.canonicalize(wd.SINGLE_SESSION) == wd.SINGLE_SESSION
    assert wd.canonicalize("single-session") == "single-session"


def test_canonicalize_rejects_unknown_value():
    with pytest.raises(ValueError):
        wd.canonicalize("not-a-real-disposition")
    with pytest.raises(ValueError):
        wd.canonicalize("")


def test_valid_is_closed():
    assert wd.VALID == frozenset(
        {"single-session", "predecessor-consumed", "chain-terminal", "memo-predecessor"}
    )


def test_write_token_bound_to_canonical():
    assert wd.WRITE_TOKEN == wd.PREDECESSOR_CONSUMED == "predecessor-consumed"


def test_canonicalize_memo_predecessor_round_trips():
    assert wd.MEMO_PREDECESSOR == "memo-predecessor"
    assert wd.MEMO_PREDECESSOR in wd.VALID
    assert wd.canonicalize(wd.MEMO_PREDECESSOR) == wd.MEMO_PREDECESSOR
    assert wd.canonicalize("memo-predecessor") == "memo-predecessor"


def test_canonicalize_existing_members_unaffected_by_memo_predecessor():
    assert wd.canonicalize(wd.LEGACY_PREDECESSOR_CONSUMED) == wd.PREDECESSOR_CONSUMED
    assert wd.canonicalize(wd.PREDECESSOR_CONSUMED) == wd.PREDECESSOR_CONSUMED
    assert wd.canonicalize(wd.SINGLE_SESSION) == wd.SINGLE_SESSION


def _collect_disposition_values(node) -> list[str]:
    """Recursively collect every string value keyed ``"disposition"`` inside
    a decoded ceremony-receipt JSON structure (nodes[].evidence.disposition,
    and any other depth the receipt shape may carry it at)."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "disposition" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_collect_disposition_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_disposition_values(item))
    return found


def test_persisted_wsc_receipts_resolve_through_canonicalize():
    """AC10: every state/ceremony/wsc/*.json receipt's embedded disposition
    value -- including one written before the WRITE_TOKEN flip and still
    carrying the legacy "chain-terminal" spelling -- resolves through
    canonicalize() without raising. Enumerates the directory itself
    (skip-if-empty, not a hard-coded count) -- a corpus that has gone empty
    (fresh clone, swept archive) must SKIP loudly, not vacuously pass, per
    state/lessons/2026-07-22-frozen-golden-over-an-empty-corpus-is-worse-
    than-a-skip.yaml.
    """
    if not _WSC_CEREMONY_DIR.is_dir():
        pytest.skip(f"no ceremony receipt corpus at {_WSC_CEREMONY_DIR}")

    receipts = sorted(_WSC_CEREMONY_DIR.glob("*.json"))
    if not receipts:
        pytest.skip(f"ceremony receipt corpus at {_WSC_CEREMONY_DIR} is empty")

    checked_any = False
    for receipt in receipts:
        data = json.loads(receipt.read_text(encoding="utf-8"))
        for value in _collect_disposition_values(data):
            checked_any = True
            assert wd.canonicalize(value) in (wd.SINGLE_SESSION, wd.PREDECESSOR_CONSUMED), (
                f"{receipt.name}: disposition value {value!r} did not canonicalize"
            )

    assert checked_any, (
        f"receipts present under {_WSC_CEREMONY_DIR} but none carried a "
        "'disposition' field anywhere -- test cannot have exercised canonicalize()"
    )


# ---------------------------------------------------------------------------
# resolve_env_override / normalize_override_handoff -- escalate-only WSC_
# DISPOSITION / WSC_CONSUMED_HANDOFF env override, shared by branch_resolution.py.
#
# Spec backlink: project-makima commit 1b07cded (coordinator/bin/
# wsc-session-disposition.py's resolve_disposition) established this contract;
# this module centralises it for every OTHER resolver that needs it.
# ---------------------------------------------------------------------------


def test_resolve_env_override_escalates_on_canonical():
    result = wd.resolve_env_override(env={"WSC_DISPOSITION": "predecessor-consumed"})
    assert result.escalate is True
    assert result.disposition == wd.WRITE_TOKEN
    assert any("override" in d for d in result.diagnostics)


def test_resolve_env_override_escalates_on_legacy_alias_case_insensitive():
    result = wd.resolve_env_override(env={"WSC_DISPOSITION": "  Chain-Terminal  "})
    assert result.escalate is True
    assert result.disposition == wd.PREDECESSOR_CONSUMED


def test_resolve_env_override_refuses_single_session_downgrade():
    result = wd.resolve_env_override(env={"WSC_DISPOSITION": "single-session"})
    assert result.escalate is False
    assert any("WARN" in d and "cannot downgrade" in d for d in result.diagnostics)


def test_resolve_env_override_ignores_unrecognised_value():
    result = wd.resolve_env_override(env={"WSC_DISPOSITION": "banana"})
    assert result.escalate is False
    assert any("WARN" in d and "unrecognised" in d for d in result.diagnostics)


def test_resolve_env_override_handoff_alone_does_not_escalate():
    result = wd.resolve_env_override(env={"WSC_CONSUMED_HANDOFF": "state/handoffs/x.md"})
    assert result.escalate is False
    assert any("NOTE" in d and "cannot flip disposition" in d for d in result.diagnostics)


def test_resolve_env_override_carries_consumed_handoff_raw_when_escalating():
    result = wd.resolve_env_override(env={
        "WSC_DISPOSITION": "predecessor-consumed",
        "WSC_CONSUMED_HANDOFF": "  state/handoffs/x.md  ",
    })
    assert result.escalate is True
    assert result.consumed_handoff_raw == "state/handoffs/x.md"


def test_resolve_env_override_empty_env_no_diagnostics():
    result = wd.resolve_env_override(env={})
    assert result.escalate is False
    assert result.diagnostics == ()


def test_normalize_override_handoff_empty_raw_is_noop():
    diagnostics: list[str] = []
    assert wd.normalize_override_handoff(Path("/repo"), "", diagnostics) == ""
    assert diagnostics == []


def test_normalize_override_handoff_relative_path_kept_as_is(tmp_path):
    diagnostics: list[str] = []
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "state" / "handoffs" / "x.md").write_text("x", encoding="utf-8")
    rel = wd.normalize_override_handoff(tmp_path, "state/handoffs/x.md", diagnostics)
    assert rel == "state/handoffs/x.md"
    assert diagnostics == []


def test_normalize_override_handoff_notes_nonexistent_path(tmp_path):
    diagnostics: list[str] = []
    rel = wd.normalize_override_handoff(tmp_path, "state/handoffs/gone.md", diagnostics)
    assert rel == "state/handoffs/gone.md"
    assert diagnostics and "does not exist on disk" in diagnostics[0]


def test_normalize_override_handoff_absolute_in_repo_relativized_posix(tmp_path):
    diagnostics: list[str] = []
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    target = tmp_path / "state" / "handoffs" / "x.md"
    target.write_text("x", encoding="utf-8")
    rel = wd.normalize_override_handoff(tmp_path, str(target), diagnostics)
    assert rel == "state/handoffs/x.md"
    assert "\\" not in rel
