"""test_render_ceremony_receipt.py — Tier T tests for render-ceremony-receipt.py.

Fixtures receipt dicts and writes them to a temp directory; never reads live
state/ceremony/ artifacts. Loads the hyphenated CLI module by file path, the
same pattern test_check_doctrine_citations.py uses for its own sibling.

Negative-spec: does not invoke any subprocess and does not depend on
receipt_schema.py's factory helpers being importable (a fixture dict here is
allowed to omit `unknown` entirely, unlike make_empty_op_tail's always-present
posture, specifically to exercise the graceful-absent rendering path).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_PATH = os.path.join(_THIS_DIR, "render-ceremony-receipt.py")

_spec = importlib.util.spec_from_file_location("render_ceremony_receipt", _MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules["render_ceremony_receipt"] = _module
_spec.loader.exec_module(_module)


def _receipt(op_tail: dict) -> dict:
    return {
        "schema_version": 1,
        "ceremony": "wsc",
        "phase": "phase-2",
        "emitted_at": "2026-08-29T00:00:00Z",
        "scope_mode": "spec-dispatch",
        "nodes": [],
        "op_tail": op_tail,
    }


class RenderCeremonyReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def _write(self, name: str, payload) -> str:
        path = os.path.join(self._tmpdir.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(payload, str):
                fh.write(payload)
            else:
                json.dump(payload, fh)
        return path

    def _run(self, path: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = _module.main(["render-ceremony-receipt.py", path])
        return rc, out.getvalue(), err.getvalue()

    # --- AC 1: unknown renders, distinctly from every other partition ---

    def test_unknown_entries_render_distinctly(self) -> None:
        receipt = _receipt(
            {
                "phase": "archival",
                "acted": ["a1"],
                "skipped": ["s1"],
                "failed": [],
                "failed_critical": [],
                "unknown": ["u1", "u2"],
            }
        )
        path = self._write("r1.json", receipt)
        rc, out, err = self._run(path)
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("UNKNOWN", out)
        self.assertIn("u1", out)
        self.assertIn("u2", out)
        # Distinct section header from ACTED/SKIPPED, not merged into either.
        unknown_idx = out.index("UNKNOWN")
        acted_idx = out.index("ACTED")
        skipped_idx = out.index("SKIPPED")
        self.assertNotEqual(unknown_idx, acted_idx)
        self.assertNotEqual(unknown_idx, skipped_idx)
        # u1/u2 must not appear under the ACTED or SKIPPED sections.
        acted_block = out[acted_idx:skipped_idx]
        self.assertNotIn("u1", acted_block)

    def test_unknown_never_folds_into_other_partitions(self) -> None:
        receipt = _receipt(
            {
                "phase": "archival",
                "acted": [],
                "skipped": [],
                "failed": [],
                "failed_critical": [],
                "unknown": ["mystery"],
            }
        )
        path = self._write("r2.json", receipt)
        _, out, _ = self._run(path)
        acted_line = [ln for ln in out.splitlines() if "ACTED" in ln][0]
        skipped_line = [ln for ln in out.splitlines() if ln.strip().startswith("SKIPPED")][0]
        self.assertNotIn("mystery", acted_line)
        self.assertNotIn("mystery", skipped_line)

    # --- AC 2: graceful-absent — no unknown key vs. empty unknown[] ---

    def test_missing_unknown_key_renders_not_tracked(self) -> None:
        receipt = _receipt(
            {
                "phase": "archival",
                "acted": [],
                "skipped": [],
                "failed": [],
            }
        )
        path = self._write("r3.json", receipt)
        rc, out, err = self._run(path)
        self.assertEqual(rc, 0)
        self.assertIn("not tracked by this receipt", out)

    def test_empty_unknown_list_renders_none_not_absent(self) -> None:
        receipt = _receipt(
            {
                "phase": "archival",
                "acted": [],
                "skipped": [],
                "failed": [],
                "failed_critical": [],
                "unknown": [],
            }
        )
        path = self._write("r4.json", receipt)
        _, out, _ = self._run(path)
        unknown_line = [ln for ln in out.splitlines() if "UNKNOWN" in ln][0]
        rest = out[out.index(unknown_line) + len(unknown_line):]
        # "(none)" appears on the UNKNOWN line itself, not "not tracked".
        self.assertIn("(none)", unknown_line)
        self.assertNotIn("not tracked", unknown_line)

    def test_absent_vs_empty_render_differently(self) -> None:
        absent_receipt = _receipt(
            {"phase": "archival", "acted": [], "skipped": [], "failed": []}
        )
        empty_receipt = _receipt(
            {
                "phase": "archival",
                "acted": [],
                "skipped": [],
                "failed": [],
                "failed_critical": [],
                "unknown": [],
            }
        )
        _, out_absent, _ = self._run(self._write("r5.json", absent_receipt))
        _, out_empty, _ = self._run(self._write("r6.json", empty_receipt))
        self.assertNotEqual(out_absent, out_empty)

    # --- AC 3: missing / malformed receipt refuses loudly ---

    def test_missing_receipt_file_exits_nonzero_and_names_path(self) -> None:
        missing_path = os.path.join(self._tmpdir.name, "does-not-exist.json")
        rc, out, err = self._run(missing_path)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(missing_path, err)

    def test_malformed_json_exits_nonzero_and_names_path(self) -> None:
        path = self._write("bad.json", "{not valid json")
        rc, out, err = self._run(path)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(path, err)

    def test_directory_path_exits_nonzero(self) -> None:
        rc, out, err = self._run(self._tmpdir.name)
        self.assertEqual(rc, 1)
        self.assertIn(self._tmpdir.name, err)

    def test_non_object_json_exits_nonzero(self) -> None:
        path = self._write("list.json", [1, 2, 3])
        rc, out, err = self._run(path)
        self.assertEqual(rc, 1)
        self.assertIn(path, err)

    # --- AC 4: unknown does not influence exit code ---

    def test_unknown_present_and_nonempty_still_exits_zero(self) -> None:
        receipt = _receipt(
            {
                "phase": "archival",
                "acted": [],
                "skipped": [],
                "failed": [],
                "failed_critical": [],
                "unknown": ["one", "two", "three"],
            }
        )
        rc, _, err = self._run(self._write("r7.json", receipt))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_failed_critical_present_still_exits_zero_render_is_not_a_gate(self) -> None:
        # This CLI is a renderer, not the exit predicate — failed_critical is
        # the hard-exit-1 partition for the CEREMONY, not for this reader.
        receipt = _receipt(
            {
                "phase": "archival",
                "acted": [],
                "skipped": [],
                "failed": [],
                "failed_critical": ["boom"],
                "unknown": [],
            }
        )
        rc, out, _ = self._run(self._write("r8.json", receipt))
        self.assertEqual(rc, 0)
        self.assertIn("boom", out)


if __name__ == "__main__":
    unittest.main()
