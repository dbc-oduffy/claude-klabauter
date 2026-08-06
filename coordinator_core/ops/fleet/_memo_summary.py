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
"""

from __future__ import annotations

import re

# YAML summary max length — shared cap for both memo.compose and memo.send.
_SUMMARY_MAX_CHARS = 120

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
    all-blank-body fallback in memo_send._compose_memo / example-doctrine-repo's _derive_summary).
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
