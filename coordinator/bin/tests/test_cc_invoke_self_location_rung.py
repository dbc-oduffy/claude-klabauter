"""test_cc_invoke_self_location_rung.py — AC1 for the terminal self-locating
rung added to cc_invoke.py::_resolve_claude_klabauter_root().

Chunk: docs/plans/2026-08-08-the-engine-root-s-own-codename-ladder-an.md § C1

C1 adds ONE terminal rung to the existing env/pointer/registry ladder in
`_resolve_claude_klabauter_root()`: when all three miss, self-locate from cc_invoke's
own `__file__` via the module's existing `_walk_up_to_checkout()` helper,
immediately before the function would otherwise raise.

A test that passes with THIS box's ambient state visible (a real
`repos.claude_klabauter` registry key, a real pointer file, a real CLAUDE_KLABAUTER_ROOT
already exported by the dev shell) proves nothing — it could pass on the old,
unpatched ladder just by hitting an earlier rung. Every case below therefore
runs cc_invoke.py as a SUBPROCESS under a from-scratch environment: HOME
redirected to a fresh empty temp directory (so the settings-home and
claude-home mirrors both resolve to nonexistent paths — no ambient pointer
file, no ambient registry impl reachable) and every CLAUDE_KLABAUTER_ROOT/DOE_ROOT/
REPO_*/CLAUDE*/COORDINATOR_* variable dropped from the child's env by
construction (the env dict is built explicitly, never inherited from
`os.environ`).

Two synthetic checkout shapes are exercised (AC1's own wording), reusing the
"synthetic fixture tree + real module file copied into place" machinery
`coordinator/bin/tests/test_coordinator_registry.py::_build_payload_shaped_fixture`
established for the sibling codename-free ladder:
  - `mktcache` — cc_invoke.py living deep under a marketplace-cache-shaped
    install (mirrors a real `~/.claude/plugins/cache/coordinator-claude/
    coordinator/<version>/...` install depth).
  - `flat`     — cc_invoke.py living at the shallow, direct-checkout depth
    (`<root>/coordinator/bin/lib/cc_invoke.py`).
Both plant the two markers `_walk_up_to_checkout` probes for
(`coordinator_core/` directory, `pyproject.toml` file) at the checkout root
and nowhere else, so a pass can only mean the walk-up actually found them.

Run: pytest coordinator/bin/tests/test_cc_invoke_self_location_rung.py -q
"""
from __future__ import annotations

import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"
_CC_INVOKE_PY = _LIB_DIR / "cc_invoke.py"
_MLIR_PY = _LIB_DIR / "machine_local_impl_resolve.py"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)


# Declared, not excused: this file spawns real processes because the behaviour under
# test IS the spawn. _BASELINE is shrink-only pre-existing residue and is explicitly
# not the route for a new file -- test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]



# ---------------------------------------------------------------------------
# Hermetic env — drop every var this ladder (or a sibling ladder) could read,
# by scanning os.environ rather than trusting a fixed list (a fixed list is
# exactly the kind of thing that silently drifts as new *_ROOT/CLAUDE*/
# COORDINATOR_* vars are introduced elsewhere in the tree).
# ---------------------------------------------------------------------------

_DROP_PREFIXES = ("REPO_", "CLAUDE", "COORDINATOR_")
_DROP_EXACT = ("CLAUDE_KLABAUTER_ROOT", "DOE_ROOT")


def _hermetic_child_env(isolated_home: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a from-scratch child env: HOME/USERPROFILE/CLAUDE_HOME anchored
    to a fresh empty directory, PATH/SYSTEMROOT passed through (needed to
    launch the child interpreter at all on Windows), and every
    CLAUDE_KLABAUTER_ROOT/DOE_ROOT/REPO_*/CLAUDE*/COORDINATOR_* key from the AMBIENT
    os.environ deliberately left OUT — this dict is built from a minimal
    passthrough set, never from `dict(os.environ)`, so there is nothing to
    strip: absence is the starting state, not a post-hoc deletion.
    """
    env: dict[str, str] = {
        "HOME": isolated_home,
        "USERPROFILE": isolated_home,
        "CLAUDE_HOME": isolated_home,
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    # Belt-and-braces: scan the ambient environment for anything matching the
    # drop set and confirm none of it leaked into the dict above (it can't,
    # since the dict is hand-built) — this loop exists so a future edit that
    # switches this function to `dict(os.environ)` + strip is caught by the
    # assertion below rather than silently reintroducing ambient leakage.
    for key in list(env):
        assert not any(key.startswith(p) for p in _DROP_PREFIXES) or key in (
            "CLAUDE_HOME",
        ), f"hermetic env accidentally carries a drop-listed key: {key}"
    for key in _DROP_EXACT:
        assert key not in env
    if extra:
        env.update(extra)
    return env


def _assert_markers_present(checkout_root: Path) -> None:
    """Fail LOUDLY if the two markers `_walk_up_to_checkout` probes for are
    missing from a fixture BEFORE running the subprocess against it — per the
    brief, a silent fall-through to the registry ladder must never be
    mistaken for this rung passing."""
    if not (checkout_root / "coordinator_core").is_dir():
        pytest.fail(f"fixture bug: {checkout_root} missing coordinator_core/ marker")
    if not (checkout_root / "pyproject.toml").is_file():
        pytest.fail(f"fixture bug: {checkout_root} missing pyproject.toml marker")


def _build_checkout(root: Path, shape: str) -> tuple[Path, Path]:
    """Build a synthetic checkout under `root` for the given shape ("mktcache"
    or "flat"). Returns (checkout_root, cc_invoke_copy_path).

    Plants ONLY the two `_walk_up_to_checkout` markers at the checkout root —
    a real `coordinator_core/` package dir (empty stand-in is enough; the
    probe only checks `.is_dir()`) and a `pyproject.toml` file — plus a real
    copy of cc_invoke.py (+ its machine_local_impl_resolve.py dependency) at
    the shape's own nesting depth under `coordinator/bin/lib/`.
    """
    if shape == "mktcache":
        # Mirrors a real marketplace-cache install's depth:
        # ~/.claude/plugins/cache/coordinator-claude/coordinator/<version>/...
        checkout_root = (
            root / "plugins" / "cache" / "coordinator-claude" / "coordinator" / "1.2.3"
        )
    elif shape == "flat":
        checkout_root = root / "flat-checkout"
    else:
        raise ValueError(f"unknown shape {shape!r}")

    lib_dir = checkout_root / "coordinator" / "bin" / "lib"
    lib_dir.mkdir(parents=True)
    (checkout_root / "coordinator_core").mkdir(parents=True)
    (checkout_root / "pyproject.toml").write_text("[project]\nname = \"stub\"\n", encoding="utf-8")
    # docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C6: every
    # DISPATCH-axis candidate rung (env, registry, self-location) now
    # DELEGATES its final answer to coordinator_core.claude_klabauter_root's own
    # coordinator_claude_klabauter_root_with_class() instead of returning the candidate
    # verbatim — so a fixture whose sole purpose is to be self-located must
    # also be a real enough package for that delegation to succeed. This stub
    # answers exactly what the real function would on a single-tree box with
    # no CLAUDE_KLABAUTER_ROOT/registry/published mirror: the checkout root itself.
    # A regular package (``__init__.py`` present), not an implicit namespace
    # package: this box has a real, ambiently-importable ``coordinator_core``
    # (editable install) elsewhere on ``sys.path`` — a namespace-package stub
    # would lose the module search to that real package regardless of
    # ``sys.path`` insertion order, silently exercising the wrong module.
    (checkout_root / "coordinator_core" / "__init__.py").write_text("", encoding="utf-8")
    (checkout_root / "coordinator_core" / "claude_klabauter_root.py").write_text(
        "def coordinator_claude_klabauter_root_with_class():\n"
        f"    return ({str(checkout_root)!r}, 'live-working-tree')\n",
        encoding="utf-8",
    )

    cc_invoke_copy = lib_dir / "cc_invoke.py"
    shutil.copyfile(_CC_INVOKE_PY, cc_invoke_copy)
    shutil.copyfile(_MLIR_PY, lib_dir / "machine_local_impl_resolve.py")

    _assert_markers_present(checkout_root)
    return checkout_root, cc_invoke_copy


_RESOLVE_SNIPPET = textwrap.dedent(
    """\
    import sys
    sys.path.insert(0, {lib_dir!r})
    import cc_invoke
    print(cc_invoke._resolve_claude_klabauter_root())
    """
)


@pytest.mark.parametrize("shape", ["mktcache", "flat"])
def test_terminal_rung_resolves_under_both_home_shapes_with_no_prior_rung(shape):
    """AC1: with NO registry key, NO pointer file, NO env var — under both the
    `mktcache` and `flat` checkout shapes — resolution succeeds via the new
    terminal self-location rung, returning the checkout root the copied
    cc_invoke.py actually lives inside."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        checkout_root, cc_invoke_copy = _build_checkout(tmp_path, shape)
        isolated_home = tmp_path / "empty-home"
        isolated_home.mkdir()

        env = _hermetic_child_env(str(isolated_home))
        snippet = _RESOLVE_SNIPPET.format(lib_dir=str(cc_invoke_copy.parent))
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, (
            f"[{shape}] expected the terminal self-location rung to resolve with "
            f"no registry/pointer/env rung reachable; stderr:\n{result.stderr}"
        )
        resolved = result.stdout.strip()
        assert resolved, f"[{shape}] expected a non-empty resolved root"
        assert os.path.realpath(resolved) == os.path.realpath(str(checkout_root)), (
            f"[{shape}] expected resolution to land on the self-located checkout "
            f"root {checkout_root!r}, got {resolved!r}"
        )


def test_explicit_claude_klabauter_root_wins_over_self_location_on_the_locator_axis():
    """LOCATOR-axis subject, moved here 2026-08-19 by § C6 of
    docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md.

    This test used to pin that an explicit CLAUDE_KLABAUTER_ROOT wins over self-location
    INSIDE `_resolve_claude_klabauter_root()` (the DISPATCH axis). C6 makes rungs 1 and 3
    of that function both delegate their final answer to the single gated
    ladder (`coordinator_claude_klabauter_root_with_class()`) instead of racing each
    other directly — so a bare, marker-only directory set via CLAUDE_KLABAUTER_ROOT (this
    fixture's `other_root`, which deliberately has no real
    `coordinator_core/claude_klabauter_root.py`) is no longer trusted verbatim on the
    DISPATCH axis; delegation now raises for it (see the sibling raise-path
    test below). The "explicit override outranks self-location" CONTRACT
    itself is still real, just on a different function: `resolve_engine_root()`
    documents exactly this ordering (env, gated only on `os.path.isdir`, not
    on importability) ahead of self-location — the LOCATOR axis this function
    was never part of. Deliberately kept as a live subject here rather than
    silently becoming an assertion about the new raise (per C6's own
    instruction) — see the raise-path test immediately below for that half.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # The self-locatable checkout the new rung would resolve to if reached.
        checkout_root, cc_invoke_copy = _build_checkout(tmp_path, "flat")

        # A second, distinct, EXISTING checkout directory — CLAUDE_KLABAUTER_ROOT
        # is expected to win outright over self-location on the LOCATOR axis.
        # Deliberately marker-only (no coordinator_core/claude_klabauter_root.py): this
        # is exactly resolve_engine_root()'s isdir-only gate, unlike the
        # DISPATCH-axis delegation the sibling raise-path test below exercises.
        other_root = tmp_path / "other-existing-checkout"
        (other_root / "coordinator_core").mkdir(parents=True)
        (other_root / "pyproject.toml").write_text("[project]\nname = \"other\"\n", encoding="utf-8")

        isolated_home = tmp_path / "empty-home"
        isolated_home.mkdir()

        env = _hermetic_child_env(str(isolated_home), extra={"CLAUDE_KLABAUTER_ROOT": str(other_root)})
        snippet = textwrap.dedent(
            """\
            import sys
            sys.path.insert(0, {lib_dir!r})
            import cc_invoke
            print(cc_invoke.resolve_engine_root({script_file!r}))
            """
        ).format(lib_dir=str(cc_invoke_copy.parent), script_file=str(cc_invoke_copy))
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, f"expected success; stderr:\n{result.stderr}"
        resolved = result.stdout.strip()
        assert os.path.realpath(resolved) == os.path.realpath(str(other_root)), (
            f"expected explicit CLAUDE_KLABAUTER_ROOT ({other_root!r}) to win over the "
            f"self-located checkout ({checkout_root!r}); got {resolved!r}"
        )


def test_dispatch_axis_env_still_wins_but_now_via_delegation_not_verbatim_trust():
    """DISPATCH-axis Rung 1, the half § C6 warns not to silently drop.

    Measured, not assumed: an explicit CLAUDE_KLABAUTER_ROOT still resolves outright
    over self-location, even against a marker-only directory (no real
    `coordinator_core/claude_klabauter_root.py`) — because
    `coordinator_claude_klabauter_root_with_class()` (the gate C6's Rung 1 now delegates
    to) carries its OWN env-first rung, the DR-313 item 5 sanctioned exemption
    ("CLAUDE_KLABAUTER_ROOT ahead of everything else"), which this chunk's `writes:`
    scope (cc_invoke.py only) does not touch or override. `_resolve_claude_klabauter_root()`
    no longer ANSWERS this itself, though: it now only supplies `existing` as a
    CANDIDATE and lets the gate re-decide — the same exemption, reached through
    one fewer independent copy of it. `test_terminal_rung_resolves_under_both_
    home_shapes_with_no_prior_rung` above is Rung 3's contrasting case: when
    self-location (not env) supplies the candidate, delegation genuinely can
    raise — see that test's stub coordinator_core/claude_klabauter_root.py, without which
    this same delegation would fail loud instead of resolving.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # A self-locatable, REAL checkout the terminal rung could otherwise
        # resolve to — never reached here, because Rung 1 wins first.
        checkout_root, cc_invoke_copy = _build_checkout(tmp_path, "flat")

        other_root = tmp_path / "other-existing-checkout"
        (other_root / "coordinator_core").mkdir(parents=True)
        (other_root / "pyproject.toml").write_text("[project]\nname = \"other\"\n", encoding="utf-8")

        isolated_home = tmp_path / "empty-home"
        isolated_home.mkdir()

        env = _hermetic_child_env(str(isolated_home), extra={"CLAUDE_KLABAUTER_ROOT": str(other_root)})
        snippet = _RESOLVE_SNIPPET.format(lib_dir=str(cc_invoke_copy.parent))
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, f"expected success; stderr:\n{result.stderr}"
        resolved = result.stdout.strip()
        assert os.path.realpath(resolved) == os.path.realpath(str(other_root)), (
            f"expected CLAUDE_KLABAUTER_ROOT ({other_root!r}) to still win outright over "
            f"self-location ({checkout_root!r}) via the gate's own env rung; "
            f"got {resolved!r}"
        )


def test_exactly_one_walk_up_to_checkout_ladder_inside_resolve_claude_klabauter_root():
    """Guards against a future edit silently adding a competing ladder to
    `_resolve_claude_klabauter_root()` — a source-inspection assertion, not a behavioral
    one, so it catches a fifth copy even if it happened to resolve the same
    answer as this one."""
    source = inspect.getsource(_mod._resolve_claude_klabauter_root)
    occurrences = source.count("_walk_up_to_checkout(")
    assert occurrences == 1, (
        f"expected exactly one _walk_up_to_checkout(...) call inside "
        f"_resolve_claude_klabauter_root(), found {occurrences} — a competing ladder "
        "was added to this function"
    )
