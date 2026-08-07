# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""bin/test_sync_cockpit_contract.py — Tests for sync-cockpit-contract.py

Purpose: verifies the vendor-sync staleness check exits 0 on matching versions,
exits non-zero with a DRIFT message on mismatched versions, and handles error
conditions (missing vendored file, missing canonical) loudly.

Converted from a dashed pytest-uncollectable filename (sync-cockpit-contract.test.py)
to a pytest-collectable test_* module; test bodies unchanged (already unittest.TestCase).

Spec backlink: state/handoffs/2026-06-22_230001_roadmap-cockpit-contract-ext-2026-06-22-tc-1.md
§ AC — vendor-sync staleness-check script authored, tested, fails loud on a planted stale-stamp fixture.
Port backlink: docs/plans/2026-07-19-debash-coordinator-windows.md § E3-b

Retiring-ruling backlink: 2026-08-02 fast-tier stale-test triage
(tasks/mise-verify/triage-C-cockpit.md § test_sync_cockpit_contract.py). This
module's own `_CANONICAL` was a `__file__`-relative walk to
`coordinator/../cockpit-contract/schema/...` that the 2026-07-22
executable-surface migration (DR-047) left behind when it moved this test
into this repo while `cockpit-contract/` (contract data) stayed in
Example-doctrine-repo. `sync-cockpit-contract.py` itself was already fixed at migration
time via `_default_canonical()` -> `coordinator_data_root.data_root()`; only
this test's copy of the path was never migrated. Fixed by resolving the same
way the production script does, instead of re-deriving the path here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_SCRIPT_DIR, "sync-cockpit-contract.py")

_LIB_DIR = os.path.join(_SCRIPT_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from coordinator_data_root import data_root  # noqa: E402
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402


def _resolve_canonical() -> str | None:
    """Resolve the canonical cockpit-contract schema the same way the script
    under test does, or None when this machine has no example-doctrine-repo clone.

    `data_root()` RAISES RuntimeError when neither rung resolves, and this
    resolution happens at module scope — so an unguarded call turns a
    clone-less machine (fresh clone, CI runner, a Windows box with no example-doctrine-repo
    checkout) into 7 collection ERRORS instead of 7 skips. Swallowing only
    RuntimeError keeps that one documented failure mode graceful without
    masking anything else.
    """
    try:
        root = data_root("cockpit-contract")
    except RuntimeError:
        return None
    return os.path.join(str(root), "schema", "cockpit-contract.schema.json")


_CANONICAL = _resolve_canonical()

# Presence of the SPECIFIC required artifact, not merely of the clone root —
# the same convention coordinator_core/contract/cockpit_schema/tests/conftest.py
# uses (`SCHEMA_AVAILABLE`/`skip_no_schema`), and for the same reason: the example-doctrine-repo
# clone resolving says nothing about whether `cockpit-contract/schema/` still
# exists at that HEAD.
_CANONICAL_AVAILABLE = _CANONICAL is not None and os.path.isfile(_CANONICAL)

# Negative-spec: this guard is NOT how the 7 failures were resolved — the
# resolver swap above is, and it makes every one of them pass on any machine
# with a example-doctrine-repo clone (i.e. every machine that can exercise the script under test
# at all). This only covers the clone-less machine, where the production script
# itself resolves no canonical and exits 2 by design; there is nothing left to
# assert about vendor-sync drift there.
requires_canonical = unittest.skipUnless(
    _CANONICAL_AVAILABLE,
    "example-doctrine-repo cockpit-contract schema/ not available on this machine "
    "(example-doctrine-repo clone absent, or schema/ missing at its HEAD)",
)


def _run(args, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [sys.executable, _SCRIPT, *args],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
        **no_console_creationflags(),
    )
    return proc.returncode, proc.stdout + proc.stderr


class SyncCockpitContractTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    @property
    def canonical_version(self) -> str:
        """The canonical schema's version stamp.

        Read on demand rather than in setUp: test_e (no --vendored) and test_f
        (explicit --canonical override) never consult the repo canonical, so an
        unconditional setUp read made them collateral casualties of a canonical
        that would not resolve.
        """
        with open(_CANONICAL, "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("version", ""))

    def tearDown(self):
        self.tmpdir.cleanup()

    def _fixture(self, version: str) -> str:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in version)
        path = os.path.join(self.tmpdir.name, "fixture-%s.json" % safe)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "version": version,
                    "title": "fixture",
                },
                fh,
            )
        return path

    @requires_canonical
    def test_a_matching_version_exits_0(self):
        match_fixture = self._fixture(self.canonical_version)
        rc, out = _run(["--vendored", match_fixture])
        self.assertEqual(rc, 0, out)
        self.assertIn("in sync", out)
        self.assertIn("v%s" % self.canonical_version, out)

    @requires_canonical
    def test_b_stale_vendored_stamp_exits_1(self):
        stale_fixture = self._fixture("0.0.1")
        rc, out = _run(["--vendored", stale_fixture])
        self.assertEqual(rc, 1, out)
        self.assertIn("DRIFT", out)
        self.assertIn("canonical v%s" % self.canonical_version, out)
        self.assertIn("vendored v0.0.1", out)
        self.assertIn("re-vendor", out)

    @requires_canonical
    def test_c_future_vendored_stamp_exits_1(self):
        future_fixture = self._fixture("99.99.99")
        rc, out = _run(["--vendored", future_fixture])
        self.assertEqual(rc, 1, out)
        self.assertIn("DRIFT", out)

    @requires_canonical
    def test_d_missing_vendored_file_exits_1(self):
        missing_path = os.path.join(self.tmpdir.name, "does-not-exist.json")
        rc, out = _run(["--vendored", missing_path])
        self.assertEqual(rc, 1, out)
        self.assertIn("DRIFT", out)
        self.assertIn(missing_path, out)

    def test_e_vendored_omitted_exits_2(self):
        rc, out = _run([], env={"COCKPIT_VENDORED": ""})
        self.assertEqual(rc, 2, out)
        self.assertIn("--vendored", out)

    def test_f_canonical_flag_override(self):
        custom_canonical = self._fixture("2.0.0")
        match_to_custom = self._fixture("2.0.0")
        rc, out = _run(
            ["--canonical", custom_canonical, "--vendored", match_to_custom]
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("in sync (v2.0.0)", out)

    @requires_canonical
    def test_g_cockpit_vendored_env_var(self):
        stale_env_fixture = self._fixture("0.0.2")
        rc, out = _run([], env={"COCKPIT_VENDORED": stale_env_fixture})
        self.assertEqual(rc, 1, out)
        self.assertIn("DRIFT", out)
