"""test_cc_invoke_published_engine_rung.py — AC coverage for C5, "Give
cc_invoke a published-engine rung without breaking its zero-spawn mandate".

Chunk: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C5

CORRECTED MANDATE (per the plan's the Director of Engineering review, and this chunk's own dispatch
brief): `_resolve_claude_klabauter_root()`'s own docstring mandate is "no bash
subprocess anywhere in the ladder", NOT zero-spawn. Rung 2+ already calls
`_machine_local_get()` -> `subprocess.run()` to bootstrap the candidate path
out of the machine-local registry, and already delegates to the native
oracle in `coordinator_core.claude_klabauter_root` once the candidate is importable —
both are UNCHANGED by this chunk. The one thing that changed: Rung 2+'s
delegation target switched from the classless `coordinator_claude_klabauter_root()` to
`coordinator_claude_klabauter_root_with_class()`, so cc_invoke gets the DR-132
published-engine-vs-live-working-tree gate for free, with zero new
resolution logic.

Rungs 1 (CLAUDE_KLABAUTER_ROOT env), 1.5 (`.claude-klabauter-root` pointer file), and 3
(self-location from `__file__`) remain gate-BLIND by design — they return
before the oracle is ever reached, and none of them may gain a NEW
subprocess as part of this chunk. Rung 1.5 is the exact defect commit
0fdfb61d6 fixed *inside* `coordinator_claude_klabauter_root_with_class()` itself (the
pointer pre-empting the DR-132 gate on every installed machine) — that fix
lives in the two-tier wrapper Rung 2+ now calls into, and does not
retroactively gate Rungs 1/1.5/3 here.

Tests:
  AC-call-site   Rung 2+ imports and calls `coordinator_claude_klabauter_root_with_class`
                 (not the classless `coordinator_claude_klabauter_root`), and returns
                 only the resolved root — no branching on the resolution
                 class (that belongs to a future C8 consumer, not this rung).
  AC-no-bash     No literal "bash" executable is ever passed to
                 `subprocess.run`/`Popen` anywhere in `_resolve_claude_klabauter_root`'s
                 reachable call graph (a fabricated child that *would* fail
                 loud if invoked is planted and never spawned).
  AC-no-new-spawn-early-rungs
                 Rungs 1, 1.5, and 3 resolve with `subprocess.run` never
                 called at all.
  AC-rung2-one-spawn
                 Rung 2+ still performs exactly the one pre-existing
                 `_machine_local_get` bootstrap spawn — no second subprocess
                 was added alongside the `_with_class` switch.

Run: pytest coordinator/bin/tests/test_cc_invoke_published_engine_rung.py -q
"""
from __future__ import annotations

import contextlib
import inspect
import os
import sys
import types
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)

pytestmark = [pytest.mark.cadence]


class TestCallSiteUsesWithClassVariant(unittest.TestCase):
    """AC-call-site: source-inspection guard that Rung 2+ delegates to the
    classed sibling, not the classless one this chunk retires the call to."""

    def test_resolve_claude_klabauter_root_imports_with_class_variant(self) -> None:
        source = inspect.getsource(_mod._resolve_claude_klabauter_root)
        self.assertIn(
            "coordinator_claude_klabauter_root_with_class",
            source,
            "Rung 2+ must delegate to coordinator_claude_klabauter_root_with_class() "
            "(the DR-132 two-tier published-engine gate), per C5.",
        )
        self.assertNotIn(
            "import coordinator_claude_klabauter_root\n",
            source,
            "the classless coordinator_claude_klabauter_root() import must be gone — "
            "C5 switches the sole call site to the _with_class sibling.",
        )
        self.assertEqual(
            source.count("from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root_with_class"),
            1,
            "expected exactly one import of coordinator_claude_klabauter_root_with_class "
            "in _resolve_claude_klabauter_root — found a different count, check for a "
            "duplicated call site.",
        )
        self.assertEqual(
            source.count("= coordinator_claude_klabauter_root_with_class()"),
            1,
            "expected exactly one call of coordinator_claude_klabauter_root_with_class() "
            "in _resolve_claude_klabauter_root.",
        )

    def test_rung2_returns_root_and_does_not_branch_on_class(self) -> None:
        """Rung 2+ unpacks (root, resolution_class) and returns only root —
        no branching on the class value (C8's job, not this rung's)."""
        fake_pkg_root = _make_fake_coordinator_core_with_class(
            root="/fake/published/root", resolution_class="resolved-engine"
        )
        with (
            unittest.mock.patch.dict(os.environ, {}, clear=False),
            unittest.mock.patch.object(_mod, "_machine_local_get", return_value=fake_pkg_root),
            _no_env_claude_klabauter_root(),
            _no_pointer_file(),
        ):
            resolved = _mod._resolve_claude_klabauter_root()
        self.assertEqual(resolved, "/fake/published/root")


class TestNoBashSubprocessAnywhere(unittest.TestCase):
    """AC-no-bash: the hard constraint — no bash subprocess anywhere on the
    ladder. Fails if a future edit introduces one."""

    def test_no_bash_literal_in_ladder_source(self) -> None:
        """Static guard: no subprocess-spawn call in _resolve_claude_klabauter_root's own
        body names 'bash' as an executable. (Prose mentions of "bash" in
        comments/docstrings are expected and are not what this asserts.)"""
        source = inspect.getsource(_mod._resolve_claude_klabauter_root)
        for bad in ('"bash"', "'bash'", "[\"bash\"", "['bash'"):
            self.assertNotIn(
                bad,
                source,
                f"found a literal bash-executable token {bad!r} in "
                "_resolve_claude_klabauter_root — the ladder must never spawn bash.",
            )

    def test_rung2_bootstrap_spawn_never_names_bash(self) -> None:
        """Behavioral guard: drive Rung 2+ for real (mocking only the OS-level
        subprocess.run), and assert no captured invocation's argv[0] is/contains
        'bash'."""
        captured_argvs: list[list[str]] = []
        real_run = __import__("subprocess").run

        def _spy_run(argv, *args, **kwargs):
            captured_argvs.append(list(argv) if isinstance(argv, (list, tuple)) else [str(argv)])
            raise FileNotFoundError("no real child spawned by this test")

        fake_pkg_root = _make_fake_coordinator_core_with_class(
            root="/fake/published/root2", resolution_class="live-working-tree"
        )
        with (
            unittest.mock.patch.object(_mod, "_machine_local_get", return_value=fake_pkg_root),
            _no_env_claude_klabauter_root(),
            _no_pointer_file(),
        ):
            resolved = _mod._resolve_claude_klabauter_root()
        self.assertEqual(resolved, "/fake/published/root2")
        for argv in captured_argvs:
            self.assertTrue(
                argv and "bash" not in os.path.basename(str(argv[0])).lower(),
                f"a bash-named executable was spawned: {argv!r}",
            )


class TestNoNewSpawnOnEarlyRungs(unittest.TestCase):
    """AC-no-new-spawn-early-rungs: rungs 1, 1.5, 3 must resolve with zero
    subprocess.run calls — no NEW spawn was added to any of them by this chunk."""

    def test_rung1_env_var_no_spawn(self) -> None:
        with (
            unittest.mock.patch.dict(os.environ, {"CLAUDE_KLABAUTER_ROOT": "/from/env"}, clear=False),
            unittest.mock.patch("subprocess.run") as mock_run,
        ):
            resolved = _mod._resolve_claude_klabauter_root()
        self.assertEqual(resolved, "/from/env")
        mock_run.assert_not_called()

    def test_rung1_5_pointer_file_no_spawn(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            settings_home = Path(tmp) / "settings-home"
            ml_dir = settings_home / "machine-local"
            ml_dir.mkdir(parents=True)
            (ml_dir / ".claude-klabauter-root").write_text("/from/pointer\n", encoding="utf-8")

            with (
                _no_env_claude_klabauter_root(),
                unittest.mock.patch.dict(
                    os.environ, {"COORDINATOR_SETTINGS_HOME": str(settings_home)}, clear=False
                ),
                unittest.mock.patch("subprocess.run") as mock_run,
            ):
                resolved = _mod._resolve_claude_klabauter_root()
            self.assertEqual(resolved, "/from/pointer")
            mock_run.assert_not_called()

    def test_rung3_self_location_no_new_spawn_beyond_the_registry_miss(self) -> None:
        """Rung 3 (self-location) fires only after Rung 2's registry read is
        attempted and misses; that ONE existing spawn is expected, but self-
        location itself (the _walk_up_to_checkout call) adds none."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            checkout_root = Path(tmp) / "self-located-checkout"
            lib_dir = checkout_root / "coordinator" / "bin" / "lib"
            lib_dir.mkdir(parents=True)
            (checkout_root / "coordinator_core").mkdir(parents=True)
            (checkout_root / "pyproject.toml").write_text(
                "[project]\nname = \"stub\"\n", encoding="utf-8"
            )
            fake_file = str(lib_dir / "cc_invoke.py")

            settings_home = Path(tmp) / "empty-settings-home"

            with (
                _no_env_claude_klabauter_root(),
                unittest.mock.patch.dict(
                    os.environ, {"COORDINATOR_SETTINGS_HOME": str(settings_home)}, clear=False
                ),
                unittest.mock.patch.object(_mod, "__file__", fake_file),
                unittest.mock.patch.object(
                    _mod, "_machine_local_get", return_value=None
                ) as mock_get,
            ):
                resolved = _mod._resolve_claude_klabauter_root()
            self.assertEqual(os.path.realpath(resolved), os.path.realpath(str(checkout_root)))
            # _machine_local_get is Rung 2's OWN existing spawn boundary (mocked
            # here rather than left real); self-location itself calls it zero
            # additional times.
            mock_get.assert_called_once()


def _no_env_claude_klabauter_root():
    env = dict(os.environ)
    env.pop("CLAUDE_KLABAUTER_ROOT", None)
    return unittest.mock.patch.dict(os.environ, env, clear=True)


def _no_pointer_file():
    """Point COORDINATOR_SETTINGS_HOME at a directory with no pointer file,
    so Rung 1.5 reliably misses without touching the real machine's state."""
    import tempfile

    tmp = tempfile.mkdtemp()
    env = dict(os.environ)
    env["COORDINATOR_SETTINGS_HOME"] = tmp
    return unittest.mock.patch.dict(os.environ, env, clear=False)


def _make_fake_coordinator_core_with_class(root: str, resolution_class: str) -> str:
    """Build (and register on sys.modules) a fake `coordinator_core.claude_klabauter_root`
    exposing ONLY `coordinator_claude_klabauter_root_with_class`, plus a real temp
    directory to serve as the candidate path `_machine_local_get` returns
    (so `os.path.isdir(candidate)` passes and the module is importable via
    `sys.path` injection, matching `_resolve_claude_klabauter_root`'s own contract).

    Returns the candidate directory path.
    """
    import tempfile

    candidate_dir = tempfile.mkdtemp()

    cc_pkg = types.ModuleType("coordinator_core")
    cc_pkg.__path__ = []  # mark as a package
    claude_klabauter_root_mod = types.ModuleType("coordinator_core.claude_klabauter_root")

    def _fake_with_class():
        return (root, resolution_class)

    claude_klabauter_root_mod.coordinator_claude_klabauter_root_with_class = _fake_with_class
    # Deliberately do NOT define coordinator_claude_klabauter_root() on the fake module —
    # if the call site regresses to the classless import, this fixture makes
    # that regression raise ImportError/AttributeError instead of silently
    # passing.
    sys.modules["coordinator_core"] = cc_pkg
    sys.modules["coordinator_core.claude_klabauter_root"] = claude_klabauter_root_mod
    return candidate_dir


@pytest.fixture(autouse=True)
def _cleanup_fake_coordinator_core():
    yield
    sys.modules.pop("coordinator_core.claude_klabauter_root", None)
    sys.modules.pop("coordinator_core", None)


if __name__ == "__main__":
    unittest.main()


class TestDR326PublishedPointerWinsAtRung1_5(unittest.TestCase):
    """DR-326: engine dispatch resolves to the PUBLISHED build, never to the
    live working tree.

    Regression guard for a measured defect, not a hypothetical. Before this,
    Rung 1.5 returned `.claude-klabauter-root` unconditionally, so on a dual-boot box
    `cc_invoke` answered the live working tree from EVERY caller location --
    including ones where the DR-132 gate itself would have said
    `claude-klabauter`. Every engine invocation therefore ran a tree whose warm
    generation token rotates on any commit by any of 50-70 concurrent sessions.

    These tests pin the rung ORDER. Restoring `.claude-klabauter-root` ahead of
    `.claude-klabauter-root` reintroduces the moving target.
    """

    @staticmethod
    def _settings_home(tmp: str, *, published: str | None, live: str | None) -> Path:
        settings_home = Path(tmp) / "settings-home"
        ml_dir = settings_home / "machine-local"
        ml_dir.mkdir(parents=True)
        if published is not None:
            (ml_dir / ".claude-klabauter-root").write_text(published + chr(10), encoding="utf-8")
        if live is not None:
            (ml_dir / ".claude-klabauter-root").write_text(live + chr(10), encoding="utf-8")
        return settings_home

    def _resolve_with(self, settings_home: Path) -> tuple[str, unittest.mock.MagicMock]:
        with (
            _no_env_claude_klabauter_root(),
            unittest.mock.patch.dict(
                os.environ, {"COORDINATOR_SETTINGS_HOME": str(settings_home)}, clear=False
            ),
            unittest.mock.patch("subprocess.run") as mock_run,
        ):
            return _mod._resolve_claude_klabauter_root(), mock_run

    def test_published_pointer_wins_when_both_present(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            published = str(Path(tmp) / "published-engine")
            Path(published).mkdir()
            settings_home = self._settings_home(tmp, published=published, live="/live/working/tree")

            resolved, mock_run = self._resolve_with(settings_home)

        self.assertEqual(
            resolved,
            published,
            "DR-326: with a published mirror installed, engine dispatch must "
            "resolve to it and never to the live working tree.",
        )
        mock_run.assert_not_called()

    def test_live_pointer_answers_only_without_a_published_mirror(self) -> None:
        """Single-tree box: the live tree is the only engine there is, and this
        rung keeps its pre-DR-326 behaviour."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            settings_home = self._settings_home(tmp, published=None, live="/live/working/tree")
            resolved, mock_run = self._resolve_with(settings_home)

        self.assertEqual(resolved, "/live/working/tree")
        mock_run.assert_not_called()

    def test_stale_published_pointer_falls_through_to_live(self) -> None:
        """A pointer naming a clone that no longer exists must not strand
        dispatch on a path with no engine in it."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            settings_home = self._settings_home(
                tmp,
                published=str(Path(tmp) / "removed-clone"),
                live="/live/working/tree",
            )
            resolved, mock_run = self._resolve_with(settings_home)

        self.assertEqual(resolved, "/live/working/tree")
        mock_run.assert_not_called()

    def test_env_override_still_beats_the_published_pointer(self) -> None:
        """Rung 1 is the testing path -- "claude-klabauter holds live processes only for
        testing" means CLAUDE_KLABAUTER_ROOT, and it must outrank DR-326's default."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            published = str(Path(tmp) / "published-engine")
            Path(published).mkdir()
            settings_home = self._settings_home(tmp, published=published, live="/live/working/tree")

            with (
                unittest.mock.patch.dict(
                    os.environ,
                    {
                        "CLAUDE_KLABAUTER_ROOT": "/deliberate/test/tree",
                        "COORDINATOR_SETTINGS_HOME": str(settings_home),
                    },
                    clear=False,
                ),
                unittest.mock.patch("subprocess.run") as mock_run,
            ):
                resolved = _mod._resolve_claude_klabauter_root()

        self.assertEqual(resolved, "/deliberate/test/tree")
        mock_run.assert_not_called()


class TestGateEntryPointByShape(unittest.TestCase):
    """`_gate_entry_point_by_shape` -- resolving a candidate whose engine-root
    module is spelled differently from this tree's.

    The publish transform renames the module and its entry point together
    (`claude_klabauter_root.py` -> `claude_klabauter_root.py`,
    `coordinator_claude_klabauter_root_with_class` ->
    `coordinator_claude_klabauter_root_with_class`), so `_delegate_to_gate`'s
    direct import of THIS tree's spelling cannot succeed against a candidate
    spelled the other way. Rung 1 hands it exactly such a candidate on a
    DR-326 box, where dispatch resolves to the published build by design.

    Backlink:
    state/bug-backlog/2026-08-19-cc-invoke-validates-a-candidate-root-by-a-c41f7a3e28b9.yaml
    """

    @staticmethod
    def _make_candidate(root, module_basename, token):
        pkg = root / "coordinator_core"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / (module_basename + ".py")).write_text(
            "def coordinator_" + token + "_root_with_class():\n"
            "    return ('resolved-from-" + token + "', 'resolved-engine')\n",
            encoding="utf-8",
        )
        return pkg

    @staticmethod
    @contextlib.contextmanager
    def _candidate_on_path(root):
        """Put `root` first on sys.path with NO ambient `coordinator_core` binding.

        `_gate_entry_point_by_shape` imports `coordinator_core.<module>`, which
        resolves through whatever `coordinator_core` is already in sys.modules.
        Under pytest that is the real repo package, whose `__path__` has no
        transformed twin -- so without this the test asserts ambient import
        state rather than the helper, and passes or fails by collection order.
        (Review: rev-D flagged exactly this sys.modules collision surface.)
        """
        saved = {k: v for k, v in sys.modules.items()
                 if k == "coordinator_core" or k.startswith("coordinator_core.")}
        for k in saved:
            del sys.modules[k]
        sys.path.insert(0, str(root))
        try:
            yield
        finally:
            try:
                sys.path.remove(str(root))
            except ValueError:
                pass
            for k in [k for k in sys.modules
                      if k == "coordinator_core" or k.startswith("coordinator_core.")]:
                del sys.modules[k]
            sys.modules.update(saved)

    def test_finds_entry_point_under_a_transformed_module_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_candidate(root, "claude_klabauter_root", "claude_klabauter")
            with self._candidate_on_path(root):
                found = _mod._gate_entry_point_by_shape(str(root))
            self.assertIsNotNone(found)
            self.assertEqual(
                found(), ("resolved-from-claude_klabauter", "resolved-engine")
            )

    def test_returns_none_when_candidate_has_no_engine_root_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "coordinator_core"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            # A decoy that MENTIONS the suffix without defining an entry point.
            # `coordinator_core/state_root.py` really does this in-tree, which
            # is why the scan matches a `def` line, not a bare substring.
            (pkg / "state_root.py").write_text(
                "# see coordinator_claude_klabauter_root_with_class for the gated ladder\n",
                encoding="utf-8",
            )
            self.assertIsNone(_mod._gate_entry_point_by_shape(str(root)))

    def test_returns_none_for_a_path_that_is_not_a_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_mod._gate_entry_point_by_shape(tmp))
        self.assertIsNone(_mod._gate_entry_point_by_shape("/no/such/path/at/all"))

    def test_matched_module_that_raises_on_import_returns_none(self):
        # A candidate matching by SHAPE but broken on import (syntax error
        # mid-publish, a failing module-level side effect, a partial checkout)
        # must fail CLEAN -- the caller then raises the one RuntimeError naming
        # the candidate. Letting an arbitrary exception escape would surface as
        # an unrelated traceback on the commit hot path. (Review: rev-D.)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "coordinator_core"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "claude_klabauter_root.py").write_text(
                "raise RuntimeError('module-level boom')\n"
                "def coordinator_claude_klabauter_root_with_class():\n"
                "    return ('x', 'y')\n",
                encoding="utf-8",
            )
            with self._candidate_on_path(root):
                self.assertIsNone(_mod._gate_entry_point_by_shape(str(root)))

    def test_scan_adds_no_subprocess(self):
        # The rungs-1/1.5/3 no-subprocess bound covers this fallback too:
        # directory listing, plain reads, one import.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_candidate(root, "claude_klabauter_root", "claude_klabauter")
            with self._candidate_on_path(root):
                with unittest.mock.patch("subprocess.run") as mock_run:
                    with unittest.mock.patch("subprocess.Popen") as mock_popen:
                        _mod._gate_entry_point_by_shape(str(root))
            mock_run.assert_not_called()
            mock_popen.assert_not_called()
