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
        "coordinator_doc_new_slug_boundary_test", str(_BIN_DIR / "coordinator-doc-new.py")
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


class TestSlugFromTitleWordBoundary(unittest.TestCase):
    """F8 (klabauter#71): a >40-char title must not be cut mid-word."""

    def test_does_not_truncate_mid_word(self):
        title = "Onboarding docs cover every subsystem end to end thoroughly"
        slug = _MOD._slug_from_title(title)
        self.assertLessEqual(len(slug), 40)
        # A mid-word cut would produce a fragment like "thorough" from
        # "thoroughly" (or similar): the slug must end on a word that is a
        # WHOLE token from the title, not a prefix of a longer one.
        words = title.lower().split()
        last_word = slug.rsplit("-", 1)[-1]
        self.assertIn(
            last_word, words,
            f"_slug_from_title truncated mid-word: {slug!r} (last token {last_word!r} "
            f"is not a whole word from the title)",
        )

    def test_single_overlong_word_still_hard_cuts(self):
        title = "a" * 60
        slug = _MOD._slug_from_title(title)
        self.assertEqual(len(slug), 40)


class TestMintArtifactIdBoundary(unittest.TestCase):
    def test_30_char_cut_on_separator_does_not_double_dash(self):
        slug = _MOD._slug_from_title(_BOUNDARY_TITLE)
        artifact_id = _MOD._mint_artifact_id("hnd", slug)
        self.assertNotIn(
            "--", artifact_id,
            f"_mint_artifact_id produced a double-dash id at the 30-char boundary: {artifact_id!r}",
        )
        self.assertTrue(artifact_id.startswith("hnd-execute-the-tier-f-grant-gate-"))


if __name__ == "__main__":
    unittest.main()
