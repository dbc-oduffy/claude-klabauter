"""Tests for IBMFR-R12: --central resolution scope, and plain-YAML attach.

(a) coordinator-initiative's "central" resolution must resolve from the
invoking repo's own root (coordinator_state_root(central=False), Rule 5),
never unconditionally from claude-klabauter's own engine install location
(coordinator_state_root(central=True), Rule 4's hardcoded backward-compat
default).

(b) `attach` must accept a plain-YAML record -- a whole file that IS a single
YAML mapping document, with no `---` frontmatter fence -- and inject the
`initiative:` FK by rewriting/appending a top-level `initiative:` line rather
than refusing for lack of a fence.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
import unittest.mock
import yaml
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_initiative_central_attach_test", str(_BIN_DIR / "coordinator-initiative.py")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_initiative_central_attach_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _FakeStateRootError(Exception):
    pass


class _FakeCrossCuttingStateRoot(Exception):
    pass


@contextlib.contextmanager
def _fake_state_root_module(resolver):
    """Install a fake coordinator_core.state_root so _resolve_initiatives_dir's
    lazy `from coordinator_core.state_root import ...` binds to `resolver`
    instead of the real engine, and records the `central=` kwarg it was
    called with.
    """
    fake_state_root_mod = types.ModuleType("coordinator_core.state_root")
    fake_state_root_mod.CrossCuttingStateRoot = _FakeCrossCuttingStateRoot
    fake_state_root_mod.StateRootError = _FakeStateRootError
    fake_state_root_mod.coordinator_state_root = resolver

    fake_pkg = types.ModuleType("coordinator_core")
    fake_pkg.state_root = fake_state_root_mod

    saved = {
        "coordinator_core": sys.modules.get("coordinator_core"),
        "coordinator_core.state_root": sys.modules.get("coordinator_core.state_root"),
    }
    sys.modules["coordinator_core"] = fake_pkg
    sys.modules["coordinator_core.state_root"] = fake_state_root_mod
    try:
        yield
    finally:
        for key, val in saved.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val


class TestCentralResolvesFromInvokingRepo(unittest.TestCase):
    def test_resolve_initiatives_dir_calls_central_false_not_true(self):
        calls: list[dict] = []

        def resolver(**kwargs):
            calls.append(kwargs)
            return "/invoking-repo/state"

        env = dict(os.environ)
        env.pop("COORDINATOR_INITIATIVE_ROOT", None)
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with _fake_state_root_module(resolver):
                result = _cli._resolve_initiatives_dir()

        self.assertEqual(result, "/invoking-repo/state/initiatives")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].get("central"),
            False,
            "must resolve via central=False (Rule 5, invoking repo's own git root), "
            "never central=True (Rule 4, hardcoded to claude-klabauter's engine install location)",
        )

    def test_env_override_still_bypasses_state_root_entirely(self):
        def resolver(**kwargs):
            raise AssertionError("coordinator_state_root must not be called when the env override is set")

        env = dict(os.environ)
        env["COORDINATOR_INITIATIVE_ROOT"] = "/explicit/override"
        with unittest.mock.patch.dict(os.environ, env):
            with _fake_state_root_module(resolver):
                result = _cli._resolve_initiatives_dir()

        self.assertEqual(result, "/explicit/override")


class TestAttachAcceptsPlainYaml(unittest.TestCase):
    def _run_attach(self, artifact_path: str, initiative_id: str, initiatives_root: str) -> int:
        env = dict(os.environ)
        env["COORDINATOR_INITIATIVE_ROOT"] = initiatives_root
        with unittest.mock.patch.dict(os.environ, env):
            with unittest.mock.patch("sys.stdout", io.StringIO()):
                with unittest.mock.patch("sys.stderr", io.StringIO()) as err:
                    rc = _cli.main(["attach", artifact_path, initiative_id])
                    self._last_stderr = err.getvalue()
        return rc

    def test_plain_yaml_record_no_fence_gets_initiative_fk_injected(self):
        with tempfile.TemporaryDirectory() as tmp:
            initiatives_dir = os.path.join(tmp, "initiatives")
            os.makedirs(initiatives_dir)
            with open(os.path.join(initiatives_dir, "my-init.yaml"), "w") as f:
                f.write('id: "my-init"\nlabel: "My Initiative"\nstatus: active\n')

            record_path = os.path.join(tmp, "silent-failure.yaml")
            with open(record_path, "w") as f:
                f.write(
                    'id: "silent-failure"\n'
                    'label: "Silent Failure"\n'
                    "status: active\n"
                    'owner: "example-market-data-repo-em"\n'
                    "target_date: null\n"
                )

            rc = self._run_attach(record_path, "my-init", initiatives_dir)
            self.assertEqual(rc, 0, self._last_stderr)

            with open(record_path) as f:
                new_content = f.read()

            parsed = yaml.safe_load(new_content)
            self.assertEqual(parsed["initiative"], "my-init")
            # Original fields survive untouched.
            self.assertEqual(parsed["id"], "silent-failure")
            self.assertEqual(parsed["owner"], "example-market-data-repo-em")

    def test_plain_yaml_record_with_existing_initiative_line_is_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            initiatives_dir = os.path.join(tmp, "initiatives")
            os.makedirs(initiatives_dir)
            with open(os.path.join(initiatives_dir, "new-init.yaml"), "w") as f:
                f.write('id: "new-init"\nlabel: "New"\nstatus: active\n')

            record_path = os.path.join(tmp, "record.yaml")
            with open(record_path, "w") as f:
                f.write("id: rec-1\ninitiative: old-init\nstatus: active\n")

            rc = self._run_attach(record_path, "new-init", initiatives_dir)
            self.assertEqual(rc, 0, self._last_stderr)

            with open(record_path) as f:
                parsed = yaml.safe_load(f.read())
            self.assertEqual(parsed["initiative"], "new-init")
            self.assertEqual(parsed["id"], "rec-1")

    def test_non_mapping_plain_yaml_document_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            initiatives_dir = os.path.join(tmp, "initiatives")
            os.makedirs(initiatives_dir)
            with open(os.path.join(initiatives_dir, "an-init.yaml"), "w") as f:
                f.write('id: "an-init"\nlabel: "A"\nstatus: active\n')

            record_path = os.path.join(tmp, "list.yaml")
            with open(record_path, "w") as f:
                f.write("- one\n- two\n")

            rc = self._run_attach(record_path, "an-init", initiatives_dir)
            self.assertEqual(rc, 1)
            self.assertIn("not a plain-YAML mapping document", self._last_stderr)

    def test_fenced_frontmatter_artifact_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            initiatives_dir = os.path.join(tmp, "initiatives")
            os.makedirs(initiatives_dir)
            with open(os.path.join(initiatives_dir, "fenced-init.yaml"), "w") as f:
                f.write('id: "fenced-init"\nlabel: "Fenced"\nstatus: active\n')

            artifact_path = os.path.join(tmp, "handoff.md")
            with open(artifact_path, "w") as f:
                f.write("---\nkind: handoff\n---\nbody text\n")

            rc = self._run_attach(artifact_path, "fenced-init", initiatives_dir)
            self.assertEqual(rc, 0, self._last_stderr)

            with open(artifact_path) as f:
                content = f.read()
            self.assertIn("initiative: fenced-init", content)
            self.assertIn("body text", content)


if __name__ == "__main__":
    unittest.main()
