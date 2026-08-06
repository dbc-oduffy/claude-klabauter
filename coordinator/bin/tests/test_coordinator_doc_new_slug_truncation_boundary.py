"""test_coordinator_doc_new_slug_truncation_boundary.py -- pins the
separator-at-truncation-boundary fix in `_slug_from_title`/`_mint_artifact_id`
(2026-08-05).

Purpose: review finding (coordinator:code-reviewer, corpus-sweep F1) found that
`_slug_from_title` stripped trailing separators BEFORE its 40-char clamp, and
`_mint_artifact_id` didn't re-strip after its own 30-char clamp at all -- so a
title whose sanitized slug happens to have a "-" exactly at the cut point
produced a trailing dash (`_slug_from_title`) or a double-dash id at the hex
boundary (`_mint_artifact_id`, e.g. `hnd-...-gate--49b7fa`). Both functions now
re-strip AFTER truncation.

Real boundary example (not synthetic): "Execute the Tier-F grant gate — a
sibling repo is blocked on chunk one" sanitizes to a slug whose char 30 lands
exactly on the dash between "gate" and "a", and whose char 40 lands exactly on
the dash between "sibling" and "repo" -- so this one title exercises both
functions' boundary in a single case.

Spec backlink: archive/handoffs/2026-08/2026-08-04-tier-f-is-grant-gated.md
(the archived record whose handoff_id, hand-swept before this fix, is only
correct because the sweep already applied the strip production now applies too)

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_predecessor.py.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_slug_truncation_boundary.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent

_BOUNDARY_TITLE = (
    "Execute the Tier-F grant gate — a sibling repo is blocked on chunk one"
)


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_slug_boundary_test", str(_BIN_DIR / "coordinator-doc-new")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_slug_boundary_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_MOD = _load_cli_module()


class TestSlugFromTitleBoundary(unittest.TestCase):
    def test_40_char_cut_on_separator_leaves_no_trailing_dash(self):
        slug = _MOD._slug_from_title(_BOUNDARY_TITLE)
        self.assertFalse(
            slug.endswith("-"),
            f"_slug_from_title left a trailing dash at the 40-char boundary: {slug!r}",
        )
        self.assertLessEqual(len(slug), 40)


class TestMintArtifactIdBoundary(unittest.TestCase):
    def test_30_char_cut_on_separator_does_not_double_dash(self):
        slug = _MOD._slug_from_title(_BOUNDARY_TITLE)
        artifact_id = _MOD._mint_artifact_id("hnd", slug)
        self.assertNotIn(
            "--", artifact_id,
            f"_mint_artifact_id produced a double-dash id at the 30-char boundary: {artifact_id!r}",
        )
        # Matches the already-hand-swept archived id's slug part exactly.
        self.assertTrue(artifact_id.startswith("hnd-execute-the-tier-f-grant-gate-"))


if __name__ == "__main__":
    unittest.main()
