"""test_coordinator_registry_flat_payload_pointer.py — falsifying fixture for
the published-payload helper-dir lookup in
coordinator_registry.py::_mp_doe_root_pointer_rung().

Spec backlink: docs/plans/published-engine-resolves-without-a-codename.md
chunk C1F.

The private tree ships the shared `.doe-root` pointer helper at
`coordinator/lib/read_doe_root_pointer.py`; the published OSS mirror
flattens it to `lib/read_doe_root_pointer.py` at repo root, with NO
`coordinator/lib` directory at all. Pre-fix, `_mp_doe_root_pointer_rung()`
only probed the private-tree path, so on a payload-shaped install the
`sys.path.insert` pointed at a directory that does not exist, the
`from read_doe_root_pointer import ...` raised ModuleNotFoundError, the
broad `except Exception: return ""` swallowed it, and the pointer rung
silently yielded nothing.

This test builds a temp tree laid out exactly like the payload (flat
`lib/`, no `coordinator/lib`) and asserts the rung still resolves the
pointer file. Run standalone against a git stash of the pre-fix
coordinator_registry.py to confirm it fails there; passes post-fix.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import coordinator_registry as reg  # noqa: E402

_REAL_HELPER_SRC = os.path.join(
    os.path.dirname(_BIN_DIR), "lib", "read_doe_root_pointer.py"
)


class TestFlatPayloadPointerRung(unittest.TestCase):
    """Falsifying fixture: payload-shaped tree, no coordinator/lib."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="c1f-payload-fixture-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

        # Payload-shaped helper dir: <tmp>/lib/read_doe_root_pointer.py.
        # Deliberately NO <tmp>/coordinator/lib anywhere.
        flat_lib_dir = os.path.join(self._tmp, "lib")
        os.makedirs(flat_lib_dir)
        shutil.copyfile(
            _REAL_HELPER_SRC,
            os.path.join(flat_lib_dir, "read_doe_root_pointer.py"),
        )

        # Synthetic plugin root holding the published manifest layout, so a
        # full-ladder assertion has something concrete to resolve against.
        plugin_root = os.path.join(self._tmp, "plugin-root")
        os.makedirs(os.path.join(plugin_root, "schemas"))
        with open(
            os.path.join(plugin_root, "schemas", "coordinator-registry.manifest.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("{}")

        # Planted .doe-root pointer file (durable settings-home sentinel
        # shape) pointing at the synthetic plugin root.
        settings_home = os.path.join(self._tmp, "settings-home")
        os.makedirs(os.path.join(settings_home, "machine-local"))
        with open(
            os.path.join(settings_home, "machine-local", ".doe-root"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(plugin_root + "\n")

        self._plugin_root = plugin_root
        self._flat_lib_dir = flat_lib_dir
        self._orig_coordinator_lib_dir = reg._COORDINATOR_LIB_DIR
        self._orig_coordinator_lib_dir_flat = getattr(
            reg, "_COORDINATOR_LIB_DIR_FLAT", None
        )
        self._env_patch = {"COORDINATOR_SETTINGS_HOME": settings_home}
        self._orig_env = {
            k: os.environ.get(k) for k in self._env_patch
        }
        os.environ.update(self._env_patch)

    def tearDown(self) -> None:
        reg._COORDINATOR_LIB_DIR = self._orig_coordinator_lib_dir
        if self._orig_coordinator_lib_dir_flat is not None:
            reg._COORDINATOR_LIB_DIR_FLAT = self._orig_coordinator_lib_dir_flat
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_pointer_rung_resolves_via_flat_payload_helper_dir(self) -> None:
        """Payload-shaped tree: private-tree probe path does not exist;
        the fallback flat `<root>/lib` probe must be used instead."""
        nonexistent_private_dir = os.path.join(self._tmp, "coordinator", "lib")
        self.assertFalse(os.path.isdir(nonexistent_private_dir))

        reg._COORDINATOR_LIB_DIR = nonexistent_private_dir
        if not hasattr(reg, "_COORDINATOR_LIB_DIR_FLAT"):
            self.fail(
                "coordinator_registry.py has no _COORDINATOR_LIB_DIR_FLAT "
                "fallback — pre-fix code, or fix regressed."
            )
        reg._COORDINATOR_LIB_DIR_FLAT = self._flat_lib_dir

        resolved = reg._mp_doe_root_pointer_rung()
        self.assertEqual(resolved, self._plugin_root)


if __name__ == "__main__":
    unittest.main()
