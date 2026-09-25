"""test_coordinator_doc_new_decision_id_mint.py -- regression coverage for the
`--type decision` id-allocation path (issue #85).

Bug: `coordinator-doc-new --type decision` stamped `DR-2050`, already claimed
by `DR-2026-09-10-live-tree-currency-disposition.md` -- a date-named record
whose real id lives only in its `id:` frontmatter, not its filename. The
filename's leading date digits (`2026`) satisfied the allocator's DR-number
regex, so the scan never read that file's frontmatter and never saw the real,
already-claimed `2050`. The same repro also involved a stale reservation
sitting one past the collided number.

This suite pins the fix at the CLI call site: the unprefixed `--type decision`
path must route through `coordinator_core.ops.decision_record_mint.
mint_next_dr_id` -- the SAME reservation-backed allocator the
`decision_record.mint_id` op exposes -- rather than a second, independent
scan that can (and did) disagree with it.

Calls `main(argv)` directly (no subprocess) -- same in-process convention as
`test_coordinator_doc_new_placeholder_id_mint.py`; `_current_repo_root` is
monkeypatched rather than `os.chdir`-ing the whole test process, since that
would racily perturb the cwd `coordinator-doc-new`'s own bootstrap uses.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_decision_id_mint.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_decision_id_mint_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_decision_id_mint_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_MOD = _load_cli_module()

_ID_RE = re.compile(r"^id:\s*(DR-\d+)\s*$", re.MULTILINE)


class TestDecisionIdMintNotShadowedByDateNamedFile(unittest.TestCase):
    def _run_decision_scaffold(self, tmp_path: Path, out_path: Path) -> str:
        """Invokes `main()` for `--type decision` with `_current_repo_root`
        pinned at `tmp_path`, returns the written file's `id:` frontmatter value.
        """
        orig = _MOD._current_repo_root
        _MOD._current_repo_root = lambda: str(tmp_path)
        try:
            rc = _MOD.main(
                [
                    "--type", "decision",
                    "--title", "A test decision",
                    "--out", str(out_path),
                ]
            )
        finally:
            _MOD._current_repo_root = orig
        self.assertEqual(rc, 0, "coordinator-doc-new --type decision exited non-zero")
        text = out_path.read_text(encoding="utf-8")
        m = _ID_RE.search(text)
        self.assertIsNotNone(m, f"no id: frontmatter found in scaffolded file:\n{text}")
        return m.group(1)

    def test_date_named_frontmatter_claimed_id_is_not_reissued(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            decisions = tmp_path / "docs" / "decisions"
            decisions.mkdir(parents=True)
            (decisions / "DR-2026-09-10-live-tree-currency-disposition.md").write_bytes(
                b"---\nid: DR-2050\n---\n"
            )

            minted = self._run_decision_scaffold(tmp_path, decisions / "out-1.md")
            self.assertNotEqual(minted, "DR-2050")
            self.assertNotEqual(minted, "DR-2026")
            self.assertEqual(minted, "DR-2051")

    def test_stale_reservation_plus_date_named_file_both_avoided(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            decisions = tmp_path / "docs" / "decisions"
            decisions.mkdir(parents=True)
            (decisions / "DR-2026-09-10-live-tree-currency-disposition.md").write_bytes(
                b"---\nid: DR-2050\n---\n"
            )
            reservations = tmp_path / "state" / "decision-record-reservations"
            reservations.mkdir(parents=True)
            (reservations / "DR-2051.reserved").write_bytes(b'{"reserved_at": "now"}\n')

            minted = self._run_decision_scaffold(tmp_path, decisions / "out-2.md")
            self.assertNotIn(minted, ("DR-2050", "DR-2051"))
            self.assertEqual(minted, "DR-2052")


if __name__ == "__main__":
    unittest.main()
