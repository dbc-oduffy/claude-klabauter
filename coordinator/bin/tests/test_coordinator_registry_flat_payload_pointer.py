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

#: LEGACY `${CLAUDE_HOME:-$HOME}/.claude/.doe-root` rung — which on a configured
_REAL_LIB_DIR = os.path.join(os.path.dirname(_BIN_DIR), "lib")
_REAL_HELPER_SRCS = (
    os.path.join(_REAL_LIB_DIR, "read_doe_root_pointer.py"),
    os.path.join(_REAL_LIB_DIR, "settings_home.py"),
)


class TestFlatPayloadPointerRung(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="c1f-payload-fixture-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

        flat_lib_dir = os.path.join(self._tmp, "lib")
        os.makedirs(flat_lib_dir)
        for _helper in _REAL_HELPER_SRCS:
            shutil.copyfile(
                _helper, os.path.join(flat_lib_dir, os.path.basename(_helper))
            )

        plugin_root = os.path.join(self._tmp, "plugin-root")
        os.makedirs(os.path.join(plugin_root, "schemas"))
        with open(
            os.path.join(plugin_root, "schemas", "coordinator-registry.manifest.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("{}")

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
        # CLAUDE_HOME is pinned into the fixture alongside settings-home so the
        # helper's LEGACY rung (`${CLAUDE_HOME:-$HOME}/.claude/.doe-root`) can
        claude_home = os.path.join(self._tmp, "claude-home")
        os.makedirs(os.path.join(claude_home, ".claude"))

        self._env_patch = {
            "COORDINATOR_SETTINGS_HOME": settings_home,
            "CLAUDE_HOME": claude_home,
        }
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
