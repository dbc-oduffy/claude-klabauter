"""
coordinator_core.frontmatter.consumed_marker

Shared constants and regex for consumed-marker handling.

Spec backlink: DoE-claude `coordinator/bin/lib/consumed-marker.js` (27 LoC),
itself citing docs/plans/2026-05-14-normalize-consumed-frontmatter.md.

TERMINAL_STATUS and TERMINAL_DEPLOYMENT's single source of truth moved to
coordinator_core.lifecycle_constants (DR-084 C3); this module re-exports
them under their original names for backward compatibility. This module
remains the home of CONSUMED_MARKER_RE, so the write-time path (the claude-klabauter
port of normalize-consumed-frontmatter.js) and the read-time path (the
Claude-klabauter port of query-records.js) are greppably aligned, mirroring the JS
original's own stated purpose.

Negative-spec: this module does no I/O and holds no state -- pure constants
and a compiled regex, 1:1 with the JS original. It is a plain module, not a
registered op (no `@register_op` decorator, no registry-map entry) -- callers
import the names directly, exactly as the JS original is `require()`d
directly rather than invoked as a CLI.
"""
from __future__ import annotations

import re

from coordinator_core.lifecycle_constants import (
    HANDOFF_TERMINAL_DEPLOYMENT as TERMINAL_DEPLOYMENT,
    HANDOFF_TERMINAL_STATUS as TERMINAL_STATUS,
)

__all__ = ["TERMINAL_STATUS", "TERMINAL_DEPLOYMENT", "CONSUMED_MARKER_RE"]

# Matches `<!-- consumed: YYYY-MM-DD [optional notes] -->` in a document body.
# Capture group 2 is lazy, terminator-anchored (`(.*?)\s*-->`) so `>`
# characters in notes (e.g. "shipped via PR > main") are captured correctly
# rather than being swallowed by a greedy `[^>]*` stop -- ported verbatim
# from the JS original's own the Staff Engineer F4 fix.
#
# Engine-level divergence (benign): when the optional non-capturing notes
# group doesn't participate at all (no notes present), Python's `re`
# returns `''` for group(2) where JS's regex engine returns `undefined`.
# Both are falsy; callers gate on truthiness, not identity, so this does
# not change behavior.
CONSUMED_MARKER_RE = re.compile(
    r"<!--\s*consumed:\s*(\d{4}-\d{2}-\d{2})(?:\s+(.*?))?\s*-->", re.IGNORECASE
)
