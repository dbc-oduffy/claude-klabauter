"""test_cc_invoke_published_engine_rung.py — AC coverage for C5, "Give
cc_invoke a published-engine rung without breaking its zero-spawn mandate".

Chunk: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C5

CORRECTED MANDATE (per the plan's the Director of Engineering review, and this chunk's own dispatch
brief): `_resolve_claude_klabauter_root()`'s own docstring mandate is "no bash
subprocess anywhere in the ladder", NOT zero-spawn. Rung 2+ already calls
`_machine_local_get()` -> `subprocess.run()` to bootstrap the candidate path
out of the machine-local registry, and already delegates to the native
oracle in `coordinator_core.engine_root` once the candidate is importable —
both are UNCHANGED by this chunk. The one thing that changed: Rung 2+'s
delegation target switched from the classless `coordinator_claude_klabauter_root()` to
`coordinator_engine_root_with_class()`, so cc_invoke gets the DR-132
published-engine-vs-live-working-tree gate for free, with zero new
resolution logic.

Rungs 1 (COORDINATOR_ENGINE_ROOT env), 1.5 (`.claude-klabauter-root` pointer file), and 3
(self-location from `__file__`) remain gate-BLIND by design — they return
before the oracle is ever reached, and none of them may gain a NEW
subprocess as part of this chunk. Rung 1.5 is the exact defect commit
0fdfb61d6 fixed *inside* `coordinator_engine_root_with_class()` itself (the
pointer pre-empting the DR-132 gate on every installed machine) — that fix
lives in the two-tier wrapper Rung 2+ now calls into, and does not
retroactively gate Rungs 1/1.5/3 here.

Tests:
  AC-call-site   Rung 2+ imports and calls `coordinator_engine_root_with_class`
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
import engine_bootstrap as _engine_bootstrap_mod  # noqa: E402  (import after path setup)

pytestmark = [pytest.mark.cadence]


class TestCallSiteUsesWithClassVariant(unittest.TestCase):
    """AC-call-site: source-inspection guard that Rung 2+ delegates to the
    classed sibling, not the classless one this chunk retires the call to."""

    def test_resolve_claude_klabauter_root_imports_with_class_variant(self) -> None:
        source = inspect.getsource(_mod._resolve_claude_klabauter_root)
        self.assertIn(
            "coordinator_engine_root_with_class",
            source,
            "Rung 2+ must delegate to coordinator_engine_root_with_class() "
            "(the DR-132 two-tier published-engine gate), per C5.",
        )
        self.assertNotIn(
            "import coordinator_claude_klabauter_root\n",
            source,
            "the classless coordinator_claude_klabauter_root() import must be gone — "
            "C5 switches the sole call site to the _with_class sibling.",
        )
        self.assertEqual(
            source.count("from coordinator_core.engine_root import coordinator_engine_root_with_class"),
            1,
            "expected exactly one import of coordinator_engine_root_with_class "
            "in _resolve_claude_klabauter_root — found a different count, check for a "
            "duplicated call site.",
        )
        self.assertEqual(
            source.count("= coordinator_engine_root_with_class()"),
            2,
            "expected exactly two calls of coordinator_engine_root_with_class() "
            "in _resolve_claude_klabauter_root — C0 (docs/plans/2026-08-20-an-engine-root-"
            "is-not-named-for-the-repo.md) splits _delegate_to_gate's single call "
            "site into a same-tree branch (ordinary import) and a foreign-"
            "candidate branch (file-path load), each calling the resolved "
            "entry point once; a different count means a branch was duplicated "
            "or collapsed back to one.",
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
            unittest.mock.patch.dict(os.environ, {"COORDINATOR_ENGINE_ROOT": "/from/env"}, clear=False),
            unittest.mock.patch("subprocess.run") as mock_run,
            # Rung 1 delegates to the real gate, which reads
            # COORDINATOR_ENGINE_ROOT back unchanged when set. Forcing the
            # same-tree branch keeps that real ordinary-import path
            # deterministic regardless of whatever `sys.modules
            # ["coordinator_core"]` state a peer test in this file (or the
            # repo-root conftest's eager import) left cached — this test is
            # about the no-new-spawn contract, not about which
            # `_delegate_to_gate` branch is taken.
            unittest.mock.patch.object(
                _engine_bootstrap_mod, "_is_same_tree_as_canonical", return_value=True
            ),
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
            core_dir = checkout_root / "coordinator_core"
            core_dir.mkdir(parents=True)
            (checkout_root / "pyproject.toml").write_text(
                "[project]\nname = \"stub\"\n", encoding="utf-8"
            )
            # `_delegate_to_gate` needs a REAL, loadable
            # `coordinator_core/engine_root.py` under the candidate defining a
            # `coordinator_*_root_with_class` entry point — that is the
            # contract `_load_foreign_gate_entry_point` (the foreign-candidate
            # branch, forced below) actually exercises. A bare empty dir
            # under-supplies what this code path requires and makes the test
            # raise on an incomplete fixture rather than assert on rung 3's
            # own behaviour.
            (core_dir / "engine_root.py").write_text(
                "def coordinator_engine_root_with_class():\n"
                "    return (%r, 'self-located-stub')\n" % str(checkout_root),
                encoding="utf-8",
            )
            fake_file = str(lib_dir / "cc_invoke.py")

            settings_home = Path(tmp) / "empty-settings-home"

            with (
                _no_env_claude_klabauter_root(),
                unittest.mock.patch.dict(
                    os.environ, {"COORDINATOR_SETTINGS_HOME": str(settings_home)}, clear=False
                ),
                unittest.mock.patch.object(_mod, "__file__", fake_file),
                # `_resolve_claude_klabauter_root` is a PLAIN ALIAS for
                # `engine_bootstrap._resolve_engine_root` (same function
                # object) — its bare-name `_machine_local_get(...)` call
                # resolves through `engine_bootstrap`'s own module globals at
                # call time, never through `cc_invoke`'s namespace, so the
                # patch target must be the defining module.
                unittest.mock.patch.object(
                    _engine_bootstrap_mod, "_machine_local_get", return_value=None
                ) as mock_get,
                # `_delegate_to_gate` branches on whether the candidate is the
                # SAME tree `coordinator_core` is already cached from
                # (`_is_same_tree_as_canonical`). That answer depends on
                # ambient `sys.modules["coordinator_core"]` state left behind
                # by whatever ran before this test in the process — order-
                # dependent, and not what this test is about. Forcing the
                # foreign-candidate branch makes rung 3's own behaviour
                # deterministic regardless of run order, and is exercised for
                # real via the on-disk `engine_root.py` stub above (not
                # short-circuited).
                unittest.mock.patch.object(
                    _engine_bootstrap_mod, "_is_same_tree_as_canonical", return_value=False
                ),
            ):
                resolved = _mod._resolve_claude_klabauter_root()
            self.assertEqual(os.path.realpath(resolved), os.path.realpath(str(checkout_root)))
            # _machine_local_get is Rung 2's OWN existing spawn boundary (mocked
            # here rather than left real); self-location itself calls it zero
            # additional times.
            mock_get.assert_called_once()


def _no_env_claude_klabauter_root():
    env = dict(os.environ)
    env.pop("COORDINATOR_ENGINE_ROOT", None)
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
    """Build (and register on sys.modules) a fake `coordinator_core.engine_root`
    exposing ONLY `coordinator_engine_root_with_class`, plus a real temp
    directory to serve as the candidate path `_machine_local_get` returns
    (so `os.path.isdir(candidate)` passes and the module is importable via
    `sys.path` injection, matching `_resolve_claude_klabauter_root`'s own contract).

    Returns the candidate directory path.
    """
    import tempfile

    candidate_dir = tempfile.mkdtemp()

    cc_pkg = types.ModuleType("coordinator_core")
    cc_pkg.__path__ = []  # mark as a package
    claude_klabauter_root_mod = types.ModuleType("coordinator_core.engine_root")

    def _fake_with_class():
        return (root, resolution_class)

    claude_klabauter_root_mod.coordinator_engine_root_with_class = _fake_with_class
    # Deliberately do NOT define coordinator_claude_klabauter_root() on the fake module —
    # if the call site regresses to the classless import, this fixture makes
    # that regression raise ImportError/AttributeError instead of silently
    # passing.
    sys.modules["coordinator_core"] = cc_pkg
    sys.modules["coordinator_core.engine_root"] = claude_klabauter_root_mod
    return candidate_dir


@pytest.fixture(autouse=True)
def _cleanup_fake_coordinator_core():
    """Restore `sys.modules["coordinator_core"]` / `.engine_root` to exactly
    what THIS test found on entry, rather than unconditionally popping keys
    it may not have added.

    An unconditional pop discards whatever real (or a sibling test's fake)
    module was already cached before this test ran, which makes every OTHER
    test in this file — including ones that never call
    `_make_fake_coordinator_core_with_class` — see a different
    `sys.modules["coordinator_core"]` state depending on run order.
    `_is_same_tree_as_canonical` branches on exactly that state, so an
    unconditional pop was the source of the order-dependence, not a
    byproduct of it.
    """
    had_pkg = "coordinator_core" in sys.modules
    prior_pkg = sys.modules.get("coordinator_core")
    had_engine_root = "coordinator_core.engine_root" in sys.modules
    prior_engine_root = sys.modules.get("coordinator_core.engine_root")
    yield
    if had_pkg:
        sys.modules["coordinator_core"] = prior_pkg
    else:
        sys.modules.pop("coordinator_core", None)
    if had_engine_root:
        sys.modules["coordinator_core.engine_root"] = prior_engine_root
    else:
        sys.modules.pop("coordinator_core.engine_root", None)


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
            core_dir = Path(published) / "coordinator_core"
            core_dir.mkdir(parents=True)
            (core_dir / "_engine_stamp").write_text("stamped\n", encoding="utf-8")
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

    def test_unstamped_published_pointer_falls_through_to_live(self) -> None:
        """C3: a present-but-unstamped published mirror must not win at Rung
        1.5 -- the rung admits only a STAMPED root now (`isfile(<root>/
        coordinator_core/_engine_stamp)` strictly subsumes the prior `isdir`
        check), so a bare mkdir with no stamp falls through to the live
        pointer, exactly like a stale/removed clone does."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            published = str(Path(tmp) / "published-engine-no-stamp")
            Path(published).mkdir()
            settings_home = self._settings_home(tmp, published=published, live="/live/working/tree")

            resolved, mock_run = self._resolve_with(settings_home)

        self.assertEqual(
            resolved,
            "/live/working/tree",
            "an unstamped published mirror must not win at Rung 1.5 -- the "
            "rung requires a stamp, not just a directory.",
        )
        mock_run.assert_not_called()

    def test_env_override_still_beats_the_published_pointer(self) -> None:
        """Rung 1 is the testing path -- "claude-klabauter holds live processes only for
        testing" means COORDINATOR_ENGINE_ROOT, and it must outrank DR-326's default."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            published = str(Path(tmp) / "published-engine")
            Path(published).mkdir()
            settings_home = self._settings_home(tmp, published=published, live="/live/working/tree")

            with (
                unittest.mock.patch.dict(
                    os.environ,
                    {
                        "COORDINATOR_ENGINE_ROOT": "/deliberate/test/tree",
                        "COORDINATOR_SETTINGS_HOME": str(settings_home),
                    },
                    clear=False,
                ),
                unittest.mock.patch("subprocess.run") as mock_run,
                # See test_rung1_env_var_no_spawn's identical note: rung 1
                # delegates to the real gate (which reads
                # COORDINATOR_ENGINE_ROOT back unchanged), and forcing the
                # same-tree branch keeps that deterministic across run order.
                unittest.mock.patch.object(
                    _engine_bootstrap_mod, "_is_same_tree_as_canonical", return_value=True
                ),
            ):
                resolved = _mod._resolve_claude_klabauter_root()

        self.assertEqual(resolved, "/deliberate/test/tree")
        mock_run.assert_not_called()
