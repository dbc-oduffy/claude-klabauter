"""
coordinator_core.ops._fm_util — shared frontmatter scalar extraction primitive.

Purpose: Single implementation of the frontmatter scalar extractor, replacing the
duplicate definitions in ``handoff_author_fork._extract_frontmatter_scalar`` and
``review_trail_write._extract_frontmatter_field``.  Both callers import from here.

Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C3
Review: code-reviewer — promoted from duplicate to shared module (F3); YAML
        quote-stripping added to prevent silent mismatch on quoted scalar values (F4).

Negative-spec:
    - Does NOT parse full YAML — reads only the first frontmatter fence block.
    - Does NOT support block scalars (``|`` / ``>``); returns the first token only.
    - Does NOT write; read-only helper.
"""

from __future__ import annotations

from typing import Optional


def extract_frontmatter_scalar(text: str, field: str) -> Optional[str]:
    """Extract a scalar field value from YAML frontmatter (between first two --- fences).

    Equivalent to the oracle's awk pattern::

        awk '/^---/{f++} f==1 && /^{field}:/{print $2; exit}'

    Reads from the first ``---`` line through the second ``---`` line; returns the
    first whitespace-delimited token after ``{field}:`` on a matching line.
    Surrounding YAML quotes are stripped so that ``consumed_by: "sess-abc"`` returns
    ``"sess-abc"`` (without the quote chars), matching the unquoted resolved session id.
    Returns ``None`` when the field is absent.

    Args:
        text:  Full file text (frontmatter + body).
        field: Field name to look up (e.g. ``"consumed_by"``, ``"workstream"``).

    Returns:
        The stripped scalar value string, ``""`` when the key is present but has no
        value tokens, or ``None`` when the field does not appear in the frontmatter.

    Note — YAML-quote stripping (F4):
        Operators sometimes add YAML quoting (``consumed_by: "sess-abc"``); YAML tools
        may also emit quoted scalars for safety.  Without stripping, ``tokens[0]`` is
        ``'"sess-abc"'`` (with surrounding quotes), which silently fails a string
        equality check against the unquoted resolved session id.  ``strip("\"'")``
        handles both double- and single-quote wrapping.
    """
    prefix = f"{field}:"
    fence_count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---":
            fence_count += 1
            if fence_count >= 2:
                break
        # Review: code-reviewer — match line (unstripped) to anchor at column-0 only,
        #   consistent with oracle awk /^{field}:/ anchor.  ``stripped.startswith``
        #   would match indented sub-keys (false positive).
        elif fence_count == 1 and line.startswith(prefix):
            rest = line[len(prefix):].strip()
            tokens = rest.split()
            if not tokens:
                return ""
            # Review: code-reviewer (F4) — strip surrounding YAML quotes so that a
            #   manually edited or tool-emitted quoted value (e.g. consumed_by: "sess-abc")
            #   compares equal to the unquoted resolved session id.
            return tokens[0].strip("\"'")
    return None
