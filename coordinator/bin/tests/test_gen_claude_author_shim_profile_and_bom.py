"""Pins the engine op's PowerShell leg: profile via MyDocuments, utf-8-sig output."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coordinator_core.ops import gen_claude_author_shim as op

_BOM = b"\xef\xbb\xbf"


class TestProfilePath(unittest.TestCase):
    def test_profile_derives_from_mydocuments_not_home(self):
        docs = os.path.join("redirected", "OneDrive", "Documents")
        with mock.patch.object(op, "_my_documents_dir", return_value=docs):
            got = op._powershell_profile_path()
        self.assertEqual(got, os.path.join(docs, "PowerShell", "Microsoft.PowerShell_profile.ps1"))

    def test_unresolvable_mydocuments_is_a_typed_refusal_naming_rc_not_a_home_guess(self):
        with mock.patch.object(op, "_my_documents_dir", return_value=None), mock.patch.object(
            op, "_resolve_home", return_value="H"
        ):
            with self.assertRaises(op._ProfileUnresolved) as ctx:
                op._powershell_profile_path()
        self.assertIn("--rc", str(ctx.exception))

    def test_explicit_home_redirect_is_not_overridden_by_mydocuments(self):
        with mock.patch.object(op.os, "name", "nt"), mock.patch.dict(
            os.environ, {"HOME": "sandbox-home", "USERPROFILE": "real-profile"}
        ):
            self.assertIsNone(op._my_documents_dir())


class TestBomHelpers(unittest.TestCase):
    def test_shim_bytes_adds_bom_for_powershell_only_and_once(self):
        self.assertEqual(op._shim_bytes(b"x", "powershell"), _BOM + b"x")
        self.assertEqual(op._shim_bytes(_BOM + b"x", "powershell"), _BOM + b"x")
        self.assertEqual(op._shim_bytes(b"x", "bash"), b"x")

    def test_ensure_utf8_sig_adds_bom_once_and_ignores_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "profile.ps1"
            p.write_bytes("# —\n".encode("utf-8"))
            with mock.patch.object(op, "declare_write"):
                op._ensure_utf8_sig(str(p))
                op._ensure_utf8_sig(str(p))
                op._ensure_utf8_sig(os.path.join(d, "absent.ps1"))
            data = p.read_bytes()
        self.assertTrue(data.startswith(_BOM))
        self.assertFalse(data.startswith(_BOM + _BOM))
        self.assertEqual(data[3:].decode("utf-8"), "# —\n")


class TestMainPowershell(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.tmpl = root / "claude-author-shim.ps1.tmpl"
        self.tmpl.write_bytes("# em dash — here\n".encode("utf-8"))
        self.rc = root / "profile.ps1"
        env = {"HOME": str(self.home), "CLAUDE_HOME": str(self.home), "COORDINATOR_SHIM_RC": ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for target in ("declare_write",):
            p = mock.patch.object(op, target)
            p.start()
            self.addCleanup(p.stop)
        self.shim = self.home / ".claude" / "shell" / "claude-author-shim.ps1"

    def _run(self, *extra):
        return op.main(["--shell", "powershell", "--template", str(self.tmpl), "--rc", str(self.rc), *extra])

    def test_shim_and_new_rc_are_utf8_sig(self):
        self.assertEqual(self._run(), 0)
        self.assertTrue(self.shim.read_bytes().startswith(_BOM))
        self.assertTrue(self.rc.read_bytes().startswith(_BOM))
        self.assertIn("em dash —", self.shim.read_bytes()[3:].decode("utf-8"))

    def test_existing_bomless_rc_is_rewritten_with_bom_and_block_kept(self):
        self.rc.write_bytes("# mine —\n".encode("utf-8"))
        self.assertEqual(self._run(), 0)
        data = self.rc.read_bytes()
        self.assertTrue(data.startswith(_BOM))
        self.assertIn("# mine —", data[3:].decode("utf-8"))
        self.assertIn(op.SENTINEL_BEGIN, data.decode("utf-8-sig"))

    def test_noop_rerun_backfills_bom_on_a_legacy_bomless_rc(self):
        self.assertEqual(self._run(), 0)
        self.rc.write_bytes(self.rc.read_bytes()[3:])
        self.assertEqual(self._run(), 0)
        self.assertTrue(self.rc.read_bytes().startswith(_BOM))

    def test_check_only_reads_shim_as_fresh_and_does_not_touch_rc(self):
        self.assertEqual(self._run(), 0)
        self.rc.write_bytes(self.rc.read_bytes()[3:])
        self.assertEqual(self._run("--check-only"), 0)
        self.assertFalse(self.rc.read_bytes().startswith(_BOM))

    def test_bash_family_stays_bom_free(self):
        tmpl = self.tmpl.with_name("claude-author-shim.sh.tmpl")
        tmpl.write_bytes(b"echo\n")
        rc = self.tmpl.with_name("bashrc")
        self.assertEqual(op.main(["--shell", "bash", "--template", str(tmpl), "--rc", str(rc)]), 0)
        self.assertFalse(rc.read_bytes().startswith(_BOM))
        self.assertFalse((self.home / ".claude" / "shell" / "claude-author-shim.sh").read_bytes().startswith(_BOM))


if __name__ == "__main__":
    unittest.main()
