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
