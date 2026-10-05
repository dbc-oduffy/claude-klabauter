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
import machine_local_impl_resolve as mlir  # noqa: E402


class TestContentRootPointerRung(unittest.TestCase):
    """The pointer rung reads through `machine_local_impl_resolve`, a sibling
    that ships beside `coordinator_registry` in every layout (private tree and
    flat published payload), so no helper directory needs staging."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="c1f-payload-fixture-")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

        self._plugin_root = os.path.join(self._tmp, "plugin-root")
        os.makedirs(os.path.join(self._plugin_root, "schemas"))

        self._settings_home = os.path.join(self._tmp, "settings-home")
        os.makedirs(os.path.join(self._settings_home, "machine-local"))

        # CLAUDE_HOME is pinned into the fixture alongside settings-home so the
        # home-level pointer can only ever resolve inside this tmpdir. Without it
        # the assertions below read whatever the developer's own box has
        # configured, and pass or fail on machine state rather than on the code
        # they pin.
        claude_home = os.path.join(self._tmp, "claude-home")
        os.makedirs(os.path.join(claude_home, ".claude"))

        env_patch = {
            "COORDINATOR_SETTINGS_HOME": self._settings_home,
            "CLAUDE_HOME": claude_home,
        }
        orig_env = {k: os.environ.get(k) for k in env_patch}
        os.environ.update(env_patch)
        self.addCleanup(self._restore_env, orig_env)

    @staticmethod
    def _restore_env(orig_env: dict) -> None:
        for k, v in orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_pointer(self, name: str) -> None:
        with open(
            os.path.join(self._settings_home, "machine-local", name), "w", encoding="utf-8"
        ) as fh:
            fh.write(self._plugin_root + "\n")

    def test_pointer_rung_resolves_the_content_root_pointer(self) -> None:
        self._write_pointer(mlir._CONTENT_ROOT_POINTER)
        self.assertEqual(reg._mp_content_root_pointer_rung(), self._plugin_root)

    def test_pointer_rung_resolves_a_box_carrying_only_the_pre_rename_pointer(self) -> None:
        self._write_pointer(mlir._LEGACY_ROOT_POINTER)
        self.assertEqual(reg._mp_content_root_pointer_rung(), self._plugin_root)

    def test_pointer_rung_is_empty_when_no_pointer_exists(self) -> None:
        self.assertEqual(reg._mp_content_root_pointer_rung(), "")


if __name__ == "__main__":
    unittest.main()
