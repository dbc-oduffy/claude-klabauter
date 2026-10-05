"""Standalone-install regression: the OSS-published klabauter mirror carries
no coordinator-content-repo checkout at all (PM ruling — klabauter must run standalone), so
every live resolution rung in ``coordinator_registry.py`` (registry key,
Content-root pointer, marketplace cache, flat layout, ``CLAUDE_PLUGIN_ROOT``,
``plugin.mirrors...live_path``) is structurally dead there. Prior to the
vendored fallback rung this raised ``FileNotFoundError`` at import time,
which took out every caller of ``coordinator-doc-new`` (all *_assemble
scaffold-directive tests) on a genuine standalone install.

Run against a git stash of the pre-fix module to confirm it fails there.

The import is driven in a SUBPROCESS with a scrubbed environment, not
in-process, matching the sibling fixtures in this directory — resolution
runs at module import time, so an in-process arm would observe this box's
real install or an already-imported module's cached verdict instead of the
standalone-install scenario under test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
_REPO_ROOT = os.path.dirname(os.path.dirname(_BIN_DIR))

_VENDORED_MANIFEST = os.path.join(_LIB_DIR, "_vendor", "coordinator-registry.manifest.json")


class TestVendoredFallbackRung(unittest.TestCase):

    def setUp(self) -> None:
        if not os.path.isfile(_VENDORED_MANIFEST):
            self.fail(f"vendored manifest missing at {_VENDORED_MANIFEST}")
        self._tmp = tempfile.mkdtemp(prefix="vendored-manifest-fallback-")
        # No flat/coordinator layout anywhere under home, no registry key set —
        # every live rung above the vendored fallback must miss.
        self.settings_home = os.path.join(self._tmp, "settings-home")
        os.makedirs(os.path.join(self.settings_home, "machine-local"))
        with open(
            os.path.join(self.settings_home, "machine-local", "registry.local.toml"), "w", encoding="utf-8"
        ) as fh:
            fh.write("schema = 1\n")
        self.home = os.path.join(self._tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_vendored_manifest_is_used_when_every_live_rung_misses(self) -> None:
        probe = (
            "import json, sys\n"
            f"sys.path.insert(0, {_LIB_DIR!r})\n"
            "import coordinator_registry as reg\n"
            "print(json.dumps({'manifest': reg._MANIFEST_PATH, "
            "'known_types_nonempty': bool(reg.KNOWN_TYPES)}))\n"
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": self.home,
            "CLAUDE_HOME": self.home,
            "COORDINATOR_SETTINGS_HOME": self.settings_home,
        }
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=_REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"import failed — the vendored fallback rung did not fire:\n{result.stderr}",
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(
            os.path.realpath(payload["manifest"]),
            os.path.realpath(_VENDORED_MANIFEST),
        )
        self.assertTrue(payload["known_types_nonempty"])


if __name__ == "__main__":
    unittest.main()
