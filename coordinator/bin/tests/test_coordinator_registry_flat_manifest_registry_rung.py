"""Falsifying fixture for the canonical registry rung's manifest lookup in
``coordinator_registry.py`` — the ``repos.doe_claude`` arm of the ladder.

Sibling of ``test_coordinator_registry_flat_payload_pointer.py``, same class of
defect one rung higher: the private tree keeps the registry manifest under
``coordinator/schemas/`` while the published mirror ships it flat at
``schemas/``. ``_mp_candidate_manifest_path()`` has always probed BOTH layouts,
but the registry rung hardcoded the ``coordinator/`` arm, so a
``repos.doe_claude`` naming a flat mirror — the shape a cloud container
registers — missed the manifest that was sitting right there. The ladder then
fell through to rungs a not-yet-written ``.doe-root`` had already starved, and
the module raised ``FileNotFoundError`` at import, which took out
``gen-claude-doe-launcher`` and ``gen-claude-doe-shim`` and left "coordinator
will NOT load in any interactive session" in the install log.

Run against a git stash of the pre-fix module to confirm it fails there.

The import is driven in a SUBPROCESS with a scrubbed environment, not in-process:
the resolution runs at module import time, so an in-process arm would either
observe this box's real install or an already-imported module's cached verdict.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
_REPO_ROOT = os.path.dirname(os.path.dirname(_BIN_DIR))

_MANIFEST_RELNAME = "coordinator-registry.manifest.json"

#: Read from the tree under test, never synthesized: the module validates the
#: manifest's own shape after loading it, so a stub `{}` would fail this test for
#: a reason that has nothing to do with the rung being exercised.
_REAL_MANIFEST = os.path.join(_BIN_DIR, "..", "schemas", _MANIFEST_RELNAME)


class TestFlatManifestViaRegistryRung(unittest.TestCase):
    """Flat-mirror-shaped tree named by `repos.doe_claude`, no `coordinator/`."""

    def setUp(self) -> None:
        if not os.path.isfile(_REAL_MANIFEST):
            self.skipTest(f"no registry manifest in this tree at {_REAL_MANIFEST}")
        self._tmp = tempfile.mkdtemp(prefix="flat-manifest-registry-rung-")
        self.flat = os.path.join(self._tmp, "coordinator-claude")
        os.makedirs(os.path.join(self.flat, ".claude-plugin"))
        os.makedirs(os.path.join(self.flat, "schemas"))
        with open(os.path.join(self.flat, ".claude-plugin", "plugin.json"), "w") as fh:
            fh.write("{}")
        with open(_REAL_MANIFEST, "rb") as src:
            payload = src.read()
        with open(os.path.join(self.flat, "schemas", _MANIFEST_RELNAME), "wb") as dst:
            dst.write(payload)

        self.settings_home = os.path.join(self._tmp, "settings-home")
        os.makedirs(os.path.join(self.settings_home, "machine-local"))
        # TOML literal string (single-quoted): a Windows path's backslashes are
        # escape sequences in a basic string and would fail to parse.
        with open(
            os.path.join(self.settings_home, "machine-local", "registry.local.toml"), "w"
        ) as fh:
            fh.write(f"schema = 1\n\"repos.doe_claude\" = '{self.flat}'\n")

        self.home = os.path.join(self._tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_registry_rung_finds_a_flat_mirrors_manifest(self) -> None:
        probe = (
            "import json, sys\n"
            f"sys.path.insert(0, {_LIB_DIR!r})\n"
            "import coordinator_registry as reg\n"
            "print(json.dumps({'manifest': reg._MANIFEST_PATH}))\n"
        )
        # Scrubbed env: DOE_ROOT / REPO_DOE_CLAUDE / CLAUDE_PLUGIN_ROOT absent, so
        # the registry rung is the ONLY thing that can resolve the manifest. On
        # the pre-fix module this subprocess dies with FileNotFoundError.
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
            f"import failed — the registry rung did not resolve the flat manifest:\n"
            f"{result.stderr}",
        )
        resolved = json.loads(result.stdout.strip().splitlines()[-1])["manifest"]
        self.assertEqual(
            os.path.realpath(resolved),
            os.path.realpath(os.path.join(self.flat, "schemas", _MANIFEST_RELNAME)),
        )


if __name__ == "__main__":
    unittest.main()
