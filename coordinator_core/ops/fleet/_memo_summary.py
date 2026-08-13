"""
coordinator_core.ops.fleet._memo_summary — shared prose-first summary derivation.

Purpose: Single source of truth for deriving a memo's `summary:` frontmatter
field from a body when the caller does not supply one explicitly. Factored
out of memo_compose.py so memo_send.py can share the SAME prose-first rule
(footgun #4) instead of re-implementing the older, naive "first non-empty
line" derivation that emits a literal `# Heading` (including the `#`) as the
summary when a body opens with a Markdown ATX heading.

Both memo_compose.py and memo_send.py import `derive_prose_summary` and
`_SUMMARY_MAX_CHARS` from here, keeping the compose and send paths
consistent (the plan's own shared-helper pattern — mirrors `_memo_resolver.py`,
factored out of memo_send.py for the same reason).

Spec backlink:
    docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C5 (AC5)
    Footgun #4 source: cross-repo/inbox/2026-07-17-example-retrieval-repo-em-cross-repo-
        memo-cli-footguns.md

Negative-spec:
  - Does NOT know about frontmatter composition, YAML quoting, or the
    9-canonical-field emission order — those stay in memo_compose.py /
    memo_send.py respectively. This module is summary-derivation ONLY.
  - Does NOT know about draft-vs-delivery refusal POLICY (warn-and-write at
    memo.draft, hard-refuse at memo.compose/memo.send) — `validate_explicit_
    summary` below only performs the length comparison and formats the
    existing message text for the caller's mode; deciding whether an
    over-cap result blocks the call or merely advises stays with each
    caller (docs/plans/2026-08-07-memo-summary-cap-warn-at-draft.md § C2/C3).

Spec backlink (SUMMARY_PLACEHOLDER / is_placeholder_summary /
validate_explicit_summary):
    docs/plans/2026-08-07-memo-summary-cap-warn-at-draft.md § C1
"""

from __future__ import annotations

import re

# YAML summary max length — shared cap for both memo.compose and memo.send.
_SUMMARY_MAX_CHARS = 120

# Self-measuring ruler written into a fresh memo.draft's `summary:` field when
# no usable summary resolves (docs/plans/2026-08-07-memo-summary-cap-warn-at-
# draft.md § C1/AC3). A PM-authored placeholder VALUE, not a comment — the
# trailing-YAML-comment route for surfacing the cap on the `summary:` line
# was tried and rejected (both `read_fm_field` and coordinator-claude's `_parse_outbox_file`
# are line-oriented, comment-unaware parsers; see memo_draft._BODY_PLACEHOLDER's
# comment block for the verified failure). The double space after the first
# sentence is deliberate — it is what makes the stated count come out — so it
# must NOT be normalized. Its length is asserted in
# tests/test_memo_summary_placeholder.py rather than trusted from the prose.
SUMMARY_PLACEHOLDER = (
    "[Replace me as a summary, no more than 100 characters.  this is 99 "
    "characters, it just so happens!]"
)

# validate_explicit_summary's per-caller message shape. Each entry is
# (op_prefix, suffix) reproducing an EXISTING caller's wording verbatim —
# these strings predate this module (memo_draft._validate_draft_params,
# memo_compose._validate_compose_params, memo_send._validate_send_params,
# and memo_send._compose_memo's ValueError backstop) and must not drift, so
# no caller's error text changes shape by moving the check here (AC8).
# "send" is _validate_send_params' message; "send_backstop" is
# _compose_memo's defense-in-depth raise — same cap, same op prefix, but it
# reuses "compose"'s longer suffix rather than "send"'s terse one, an
# existing discrepancy this module preserves rather than resolves.
_VALIDATION_MESSAGES = {
    "draft": (
        "memo.draft",
        "shorten it or omit summary and let memo.compose derive one from "
        "the body instead",
    ),
    "compose": (
        "memo.compose",
        "shorten it or omit summary to derive one from the body instead",
    ),
    "send": ("memo.send", "shorten it"),
    "send_backstop": (
        "memo.send",
        "shorten it or omit summary to derive one from the body instead",
    ),
}

# Heading lines (ATX-style Markdown, 1-6 '#'s) are structural, not prose — skip them
# when deriving a summary (footgun #4: the prior rule took the first non-empty line
# unconditionally, so a body opening with a heading emitted the heading as the summary).
_HEADING_RE = re.compile(r"^#{1,6}(\s|$)")

# HTML-comment-only lines (memo.draft's own placeholder body opens with these) are
# also not prose — skip them for the same reason as headings.
_HTML_COMMENT_ONLY_RE = re.compile(r"^<!--.*-->$")

# First-sentence boundary: '.', '!', or '?' followed by whitespace or end-of-string.
_SENTENCE_END_RE = re.compile(r"^(.*?[.!?])(\s|$)")


def derive_prose_summary(body: str) -> str:
    """Derive a one-line summary from the first PROSE sentence of body.

    Skips blank lines, ATX heading lines (``#`` .. ``######``), and
    HTML-comment-only lines — none of those are prose. Within the first
    surviving line, returns up to the first sentence-ending punctuation
    (``.``/``!``/``?``); falls back to the whole (stripped) line when no
    sentence-ending punctuation is found. Truncates to _SUMMARY_MAX_CHARS
    with a trailing '…' when the result is still too long.

    Returns "" when body has no surviving prose line (mirrors the prior
    all-blank-body fallback in memo_send._compose_memo / coordinator-claude's _derive_summary).
    """
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _HEADING_RE.match(stripped):
            continue
        if _HTML_COMMENT_ONLY_RE.match(stripped):
            continue

        m = _SENTENCE_END_RE.match(stripped)
        candidate = m.group(1) if m else stripped
        if len(candidate) <= _SUMMARY_MAX_CHARS:
            return candidate
        return candidate[: _SUMMARY_MAX_CHARS - 1] + "…"
    return ""


def is_placeholder_summary(value: str | None) -> bool:
    """True iff `value` is the SUMMARY_PLACEHOLDER ruler (or absent).

    Exact match against SUMMARY_PLACEHOLDER after `.strip()` — NOT a
    substring or prefix test (docs/plans/2026-08-07-memo-summary-cap-warn-
    at-draft.md Anti-scope: a real summary that happens to quote the
    ruler's words must not be swallowed). None and "" both count as
    placeholder/absent, matching every existing "no usable summary" check
    this predicate is meant to replace.
    """
    if not value:
        return True
    return value.strip() == SUMMARY_PLACEHOLDER


def validate_explicit_summary(mode: str, summary: str | None) -> str | None:
    """The one over-cap length check, shared by every explicit-summary caller.

    Returns an error message when `summary` is present and exceeds
    `_SUMMARY_MAX_CHARS`, else None. `summary is None` always validates
    (an omitted summary is not this check's concern — see each caller's own
    presence/required-ness rules, e.g. memo.send's DEC-1 gate).

    `mode` selects which EXISTING caller's message wording to reproduce —
    one of "draft", "compose", "send", "send_backstop" (see
    _VALIDATION_MESSAGES above). Callers decide what to DO with a non-None
    result (hard-refuse at compose/send, advise-and-write at draft) — this
    function only measures and formats.
    """
    if summary is None or len(summary) <= _SUMMARY_MAX_CHARS:
        return None
    op_prefix, suffix = _VALIDATION_MESSAGES[mode]
    return (
        f"{op_prefix}: summary is {len(summary)} chars, cap is "
        f"{_SUMMARY_MAX_CHARS} — {suffix}"
    )
