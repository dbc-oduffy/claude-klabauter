"""
coordinator_core.ops.tests.test_handoff_discharge_criteria

Regression coverage for chain-review Slice B findings against
`coordinator_core.ops.handoff_discharge_criteria` (F1, F4, F5, F6). Reuses
`test_handoff_correct_body.py`'s helpers by import rather than duplicating
them — that file already carries the bulk of this op's happy-path/
ownership-gate coverage (added under C7).

Coverage:
  (F1a) a checkbox item resolves its criterion_id from its OWN text first,
        not the previous item's wrapped continuation lines immediately
        above it (the wrap-across-lines mis-binding the reviewer measured
        against all 43 live state/handoffs/*.md files).
  (F1b) ambiguous identity resolution (two checkboxes both carrying the
        same AC token in their own text) refuses rather than guessing.
  (F4)  a `split` whose `unmet_text` omits the criterion's own identity
        token is refused (the still-unmet line must remain addressable by
        criterion_id); a `split` that includes it stays addressable by
        criterion_id afterward.
  (F5)  a context expansion whose smallest disambiguating window still
        exceeds handoff_correct_body's _MAX_OLD_STRING_LEN cap is reported
        in this module's own terms, not as an opaque correct_body refusal
        naming a param the caller never supplied.
  (F6)  a non-UTF-8 handoff body returns the {exit_code: 1} envelope
        instead of letting UnicodeDecodeError escape uncaught.

Spec backlink: coordinator_core/ops/handoff_discharge_criteria.py
               state/subagent-share/444e8728-700a-402a-ae89-e5754cdeef7c/
               chain-review-B-newop-registration.md
"""

from __future__ import annotations

import asyncio
from pathlib import Path

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_correct_body  # noqa: F401 — fires @register_op
import coordinator_core.ops.handoff_discharge_criteria  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_discharge_criteria import (
    _handler as _discharge_handler,
)
from coordinator_core.ops.handoff_discharge_criteria import _parse_checkboxes

# Reuse the C7 helpers rather than duplicating repo/handoff scaffolding.
from coordinator_core.ops.tests.test_handoff_correct_body import (
    _make_git_repo,
    _seed_claimed_handoff,
    _set_calling_session,
)

_DISCHARGE_OP_NAME = "handoff.discharge_criteria"
assert _DISCHARGE_OP_NAME in _REGISTRY, (
    f"import guard failed: {_DISCHARGE_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_discharge_criteria @register_op did not fire"
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# F1a — own-text-first resolution across a wrapped multi-line criterion
# ---------------------------------------------------------------------------

_WRAPPED_ACC_BODY = (
    "\n## Acceptance criteria\n\n"
    "- [ ] Klabauter rows are re-published, and claude-klabauter's own destination-native\n"
    "      check (AC4 / C14 shape) returns zero residue.\n"
    "- [ ] The DOE-PORT adjacent class is explicitly ruled in or out of the\n"
    "      checker's scope.\n"
)


def test_own_text_first_resolves_wrapped_criterion_to_its_own_checkbox():
    """Ground-truth shape from the reviewer's F1 measurement
    (state/handoffs/2026-08-03-percolate-publish-round.md): the AC4 token
    lives in the FIRST checkbox's own (wrapped) text, not in any line above
    it. The old backward-only scan mis-bound it to the SECOND checkbox;
    own-text-first must resolve it to the first."""
    items = _parse_checkboxes(_WRAPPED_ACC_BODY)
    assert len(items) == 2
    assert items[0]["criterion_id"] == "AC4"
    assert items[1]["criterion_id"] is None


def test_discharge_tick_by_criterion_id_targets_own_text_owner_not_next_item(
    tmp_path, monkeypatch
):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-wrap.md", body=_WRAPPED_ACC_BODY
    )
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC4"},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 0, result
    assert result["resolved_position"] == 1

    text = hpath.read_text(encoding="utf-8")
    assert "- [x] Klabauter rows are re-published" in text
    assert "- [ ] The DOE-PORT adjacent class" in text


# ---------------------------------------------------------------------------
# F1b — ambiguity refuses rather than guessing
# ---------------------------------------------------------------------------

_AMBIGUOUS_ACC_BODY = (
    "\n## Acceptance criteria\n\n"
    "- [ ] First half of AC3 (AC3 shape one).\n"
    "- [ ] Second half of AC3 (AC3 shape two).\n"
)


def test_ambiguous_criterion_id_refuses_rather_than_guessing(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-ambiguous.md", body=_AMBIGUOUS_ACC_BODY
    )
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC3"},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "ambiguous identity resolution" in result["error"]
    assert not hpath.read_text(encoding="utf-8").count("[x]")


# ---------------------------------------------------------------------------
# F5 — bounded context expansion reports in this module's own terms
# ---------------------------------------------------------------------------


def test_context_expansion_over_cap_reports_own_error_not_correct_body_param(
    tmp_path, monkeypatch
):
    repo = _make_git_repo(tmp_path)
    # A long, near-duplicate run of checkbox text forces the disambiguating
    # window past _MAX_OLD_STRING_LEN (1024 chars) before it can find a
    # unique window at all — construct one line long enough on its own.
    long_filler = "x" * 1100
    body = (
        "\n## Acceptance criteria\n\n"
        f"- [ ] {long_filler} target one\n"
        f"- [ ] {long_filler} target two\n"
    )
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-toolong.md", body=body
    )
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "position": 1},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "1024-character cap" in result["error"]
    assert "old_string" not in result["error"]
    assert not hpath.read_text(encoding="utf-8").count("[x]")


def test_split_context_expansion_over_cap_reports_own_error_not_correct_body_param(
    tmp_path, monkeypatch
):
    """(F5, split variant) the same 1024-character disambiguation-window cap
    fires distinctly for a `split` (met_text/unmet_text) discharge, not just
    a plain `tick` — proving the refusal is reachable through the SPLIT
    operation specifically, per AC19's claim, and that it reports in this
    module's own terms (not `handoff_correct_body`'s `old_string`-worded
    message) rather than the net-growth or body-ratio cap tripping first.
    """
    repo = _make_git_repo(tmp_path)
    long_filler = "x" * 1100
    body = (
        "\n## Acceptance criteria\n\n"
        f"- [ ] {long_filler} target one\n"
        f"- [ ] {long_filler} target two\n"
    )
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-split-toolong.md", body=body
    )
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {
            "handoff_path": str(hpath),
            "position": 1,
            "met_text": "target one done",
            "unmet_text": "target one still pending",
        },
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "1024-character cap" in result["error"]
    assert "old_string" not in result["error"]
    assert not hpath.read_text(encoding="utf-8").count("[x]")


# ---------------------------------------------------------------------------
# F6 — UnicodeDecodeError returns the envelope instead of escaping
# ---------------------------------------------------------------------------


def test_non_utf8_handoff_body_returns_envelope_not_uncaught_exception(
    tmp_path, monkeypatch
):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-badbytes.md",
        body="\n## Acceptance criteria\n\n- [ ] AC-1: fine\n",
    )
    # Corrupt the body with a non-UTF-8 byte after frontmatter, preserving
    # the frontmatter block so split_frontmatter still finds it — the crash
    # site under test is the initial read_bytes().decode("utf-8").
    raw = hpath.read_bytes()
    hpath.write_bytes(raw + b"\xff\xfe")
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {"handoff_path": str(hpath), "position": 1},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "cannot read handoff file" in result["error"]


# ---------------------------------------------------------------------------
# F4 — a split's unmet_text must carry the criterion's own identity token,
# so the still-unmet line remains addressable by criterion_id afterward.
# ---------------------------------------------------------------------------

# Own-text-carried id (F1's governing shape) — the ONLY layout that can stay
# unambiguously addressable by criterion_id after a one-to-two split: a
# separate-annotation-line id (e.g. "AC-9:\n- [ ] Done") would leak onto
# BOTH resulting lines (the met half falls back to that same preceding
# annotation when its own text carries no id), producing an ambiguous
# refusal on re-resolution rather than a clean single match.
_ID_ACC_BODY = (
    "\n## Context\n\n"
    + ("Padding filler text, kept well away from the checkbox context so the "
       "split's old_string never approaches the ratio-of-body cap. " * 6)
    + "\n\n## Acceptance criteria\n\n"
    "- [ ] AC-9: a criterion with an id\n"
    "- [ ] AC-10: an unrelated sibling criterion, kept untouched by the split\n"
)


def test_split_without_criterion_id_in_unmet_text_refuses(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-split-f4-refuse.md", body=_ID_ACC_BODY
    )
    _set_calling_session(monkeypatch)

    result = _run(_discharge_handler(
        {
            "handoff_path": str(hpath),
            "criterion_id": "AC-9",
            "met_text": "most of it",
            "unmet_text": "the remainder",
        },
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 1
    assert "AC-9" in result["error"]
    assert "unmet_text" in result["error"]
    assert not hpath.read_text(encoding="utf-8").count("[x]")


def test_split_with_criterion_id_in_unmet_text_stays_addressable_by_id(
    tmp_path, monkeypatch
):
    repo = _make_git_repo(tmp_path)
    hpath = _seed_claimed_handoff(
        repo, "2026-08-06-discharge-split-f4-ok.md", body=_ID_ACC_BODY
    )
    _set_calling_session(monkeypatch)

    split_result = _run(_discharge_handler(
        {
            "handoff_path": str(hpath),
            "criterion_id": "AC-9",
            "met_text": "most of it",
            "unmet_text": "the remainder (AC-9)",
        },
        repo_root=repo / ".git",
    ))
    assert split_result["exit_code"] == 0, split_result

    by_id = _run(_discharge_handler(
        {"handoff_path": str(hpath), "criterion_id": "AC-9"},
        repo_root=repo / ".git",
    ))
    assert by_id["exit_code"] == 0, by_id
    assert by_id["discharge_op"] == "tick"

    text = hpath.read_text(encoding="utf-8")
    assert "- [x] most of it" in text
    assert "- [x] the remainder (AC-9)" in text
