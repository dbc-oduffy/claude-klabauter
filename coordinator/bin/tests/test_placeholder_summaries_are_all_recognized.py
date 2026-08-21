"""test_placeholder_summaries_are_all_recognized.py — standing gate: every
placeholder `summary:` a scaffolder EMITS is a placeholder the normalizer
RECOGNIZES.

Purpose: a placeholder summary is present and under the 140-char cap, so no
validator objects to it. The only thing that stops it being committed to
`state/` as a record's permanent summary is
`handoff_normalize._PLACEHOLDER_SUMMARIES` treating the literal as absent and
routing it through the H1-derivation backfill. That works exactly as far as the
set is complete.

On 2026-08-21 it was not: `coordinator-doc-new.py` emits SIX distinct
placeholder-summary literals and the set carried one. The other five —
session-handoff, recovery, roadmap stub, vision-slice, capability-arc — passed
straight through. Found while classifying the authoring surface for
`state/handoffs/2026-08-21-handoffs-and-spinoffs-minimal-for-hand-rolling.md`.

Two hand-maintained lists in two repositories' worth of separation cannot be
held together by anyone remembering. This test is the artifact that holds them:
it reads the emitting literals out of the scaffolder's own source and asserts
the normalizer knows every one. A seventh scaffolder that adds a placeholder
without registering it fails here rather than three months later in a report
nobody reads.

FAST TIER ONLY: source text in, set membership out. No spawn, no engine socket.

Run:
    python3 -m pytest coordinator/bin/tests/test_placeholder_summaries_are_all_recognized.py -v
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from coordinator_core.ops.handoff_normalize import _PLACEHOLDER_SUMMARIES

_DOC_NEW = Path(__file__).resolve().parent.parent / "coordinator-doc-new.py"

#: Matches the scaffolders' own assignment shape:
#:     placeholder_summary = "PLACEHOLDER — ..."
#:     placeholder_summary = f"PLACEHOLDER — ..."
#: Anchored on the variable name rather than on the word PLACEHOLDER so the
#: many `title = "PLACEHOLDER — ..."` assignments (a different field, with a
#: different normalization story) are not swept in.
_EMITTED_RE = re.compile(
    r'^\s*placeholder_summary\s*=\s*f?"([^"]+)"', re.MULTILINE
)


def _emitted_literals() -> set[str]:
    return set(_EMITTED_RE.findall(_DOC_NEW.read_text(encoding="utf-8")))


class PlaceholderSummaryParityTest(unittest.TestCase):
    def test_the_scaffolder_emits_at_least_the_six_known_literals(self):
        """Guards the guard: if the assignment shape ever changes and this
        regex silently matches nothing, the parity assertion below would pass
        vacuously."""
        self.assertGreaterEqual(len(_emitted_literals()), 6)

    def test_every_emitted_placeholder_is_recognized_by_the_normalizer(self):
        unrecognized = sorted(_emitted_literals() - set(_PLACEHOLDER_SUMMARIES))
        self.assertEqual(
            unrecognized,
            [],
            "coordinator-doc-new.py emits placeholder summaries that "
            "handoff_normalize._PLACEHOLDER_SUMMARIES does not recognize; they "
            "will be committed to state/ verbatim. Add each literal to that "
            "frozenset — do not generalize it to a startswith() heuristic.",
        )

    def test_the_normalizer_carries_no_literal_nothing_emits(self):
        """The reverse direction. A stale entry is harmless at runtime but
        means the set has drifted from the scaffolder it mirrors, which is
        exactly how the forward direction rots too."""
        stale = sorted(set(_PLACEHOLDER_SUMMARIES) - _emitted_literals())
        self.assertEqual(stale, [], f"unemitted placeholder literals: {stale}")


if __name__ == "__main__":
    unittest.main()
