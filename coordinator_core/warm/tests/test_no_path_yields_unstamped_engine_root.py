"""
coordinator_core.warm.tests.test_no_path_yields_unstamped_engine_root

Spec backlink: docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C8, AC3

THE REGRESSION GUARD: "an engine root is a stamped build. No stamp, no
engine." (C2) is enforced piecemeal across several chunks — C4 (skew's
fallback), C5 (the published-engine gate + the live-tree/published split),
C6 (cc_invoke's two ambient rungs), C13 (exec_cli's per-target fallback).
Each of those chunks pinned its OWN change with its own test. This file is
the cross-cutting guard: it enumerates every rung on the DISPATCH axis
("which engine executes?") — env var, pointer file, registry key, cwd,
`__file__` — and asserts each one, individually, cannot hand back an
unstamped tree as THE ENGINE. Without this the change reverts the first
time someone adds a convenience default, which is precisely how the
current state arose, twice (see this plan's own § Problem statement).

THE AMBIENT/DELIBERATE BOUNDARY, asserted in BOTH directions (per this
chunk's own body): an ambient path must not reach an unstamped tree, AND a
deliberate invocation must still work (Hard constraint 2 — "a script run by
name must still find its own tree"). A guard that only tests the first half
would pass on a build that broke Hard constraint 2.

DISPATCH vs LOCATOR, stated once so every test below does not re-litigate
it: self-location via `Path(__file__)` is BANNED on the DISPATCH axis
(`resolve_claude_klabauter_root_with_class`, cc_invoke's `_resolve_claude_klabauter_root`) and
DELIBERATELY PRESERVED on the LOCATOR axis (`engine_root.current_engine_clone`,
`resolve_engine_root`, `resolve_colocated_claude_klabauter_root` — C6's own body names
this distinction explicitly). This guard therefore asserts against the
DISPATCH *symbol set* — the specific functions that answer "which engine
executes" — never against the bare string `__file__` itself; a blanket AST
scan for `Path(__file__)` would red the entire self-location bucket C7
deliberately keeps on the locator axis.

Negative-spec: this file does not re-derive C5's stamp-gate fixtures
(`coordinator/lib/resolve-claude-klabauter/tests/test_dispatch_prefers_stamped_engine.py`)
or C6's delegation fixtures (`coordinator/bin/tests/test_cc_invoke_no_ambient_live_tree.py`)
in full — it reuses their loading patterns to pin the union across all five
rungs in one place, the shape those per-chunk tests were never asked to
cover together.
"""
from __future__ import annotations

import importlib.util
import inspect
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RESOLVE_CLAUDE_KLABAUTER_PY = _REPO_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"
_CC_INVOKE_PY = _REPO_ROOT / "coordinator" / "bin" / "lib" / "cc_invoke.py"


def _load_by_path(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"could not load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def shim():
    return _load_by_path("_c8_guard_resolve_claude_klabauter_shim", _RESOLVE_CLAUDE_KLABAUTER_PY)


@pytest.fixture
def cc_invoke_mod():
    return _load_by_path("_c8_guard_cc_invoke", _CC_INVOKE_PY)


def _write_stamp(root: Path, body: str = "sha:c8-guard-stamp\n") -> None:
    stamp = root / "coordinator_core" / "_engine_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(body, encoding="utf-8")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    return SimpleNamespace(ml_dir=ml_dir, settings_home=settings_home)


def _register_published(ml_dir: Path, published_root: Path) -> None:
    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root.as_posix()}\'\n',
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Rung: registry key (`repos.claude_klabauter`) — the published-engine rung.
# ---------------------------------------------------------------------------


def test_registry_key_rung_denies_an_unstamped_published_root(tmp_path, registry, shim):
    """An ambient registry entry pointing at an unstamped directory must
    never resolve as the engine — `_resolve_published_engine` (C5) is the
    ONE place this rung's answer comes from."""
    published_root = tmp_path / "published"
    (published_root / "coordinator_core").mkdir(parents=True)  # no stamp

    _register_published(registry.ml_dir, published_root)

    assert shim._resolve_published_engine(registry.ml_dir) is None


def test_registry_key_rung_allows_a_stamped_published_root(tmp_path, registry, shim):
    """The other half of the boundary: a DELIBERATELY stamped, registered
    build still resolves — the rung is not broken outright, only the
    unstamped case is denied."""
    published_root = tmp_path / "published"
    (published_root / "coordinator_core").mkdir(parents=True)
    _write_stamp(published_root)

    _register_published(registry.ml_dir, published_root)

    assert shim._resolve_published_engine(registry.ml_dir) == published_root.as_posix()


# ---------------------------------------------------------------------------
# Rung: env var (`CLAUDE_KLABAUTER_ROOT`) — cc_invoke Rung 1 and the shared gate's own
# Rung 1. Ambient by construction: every fired session inherits its parent's
# environment. C6 closed this by no longer letting cc_invoke answer the
# candidate verbatim — every candidate is delegated through the single gate.
# ---------------------------------------------------------------------------


def test_env_var_rung_no_longer_returns_its_candidate_verbatim(cc_invoke_mod):
    """Static guard mirroring C6's own pin: `_resolve_claude_klabauter_root()`'s Rung 1
    must not fast-path `return existing` — it must delegate through
    `_delegate_to_gate()`, the single place the DISPATCH answer comes from."""
    source = inspect.getsource(cc_invoke_mod._resolve_claude_klabauter_root)
    assert "return existing\n" not in source, (
        "Rung 1 (CLAUDE_KLABAUTER_ROOT env) regressed to an ambient verbatim return — "
        "it must delegate through _delegate_to_gate()."
    )
    assert "_delegate_to_gate(existing" in source


# ---------------------------------------------------------------------------
# Rung: self-location (`__file__`) — cc_invoke's terminal Rung 3. Preserved
# on the LOCATOR axis (Hard constraint 2: a script run by name must still
# find its own tree) but banned from answering the DISPATCH question
# directly — its candidate must also be delegated through the gate.
# ---------------------------------------------------------------------------


def test_self_location_rung_no_longer_returns_its_candidate_verbatim(cc_invoke_mod):
    """Mirrors C6's own pin for Rung 3: self-location still supplies a
    CANDIDATE (Hard constraint 2 is preserved) but must not itself decide
    the final answer — that is `_delegate_to_gate()`'s job."""
    source = inspect.getsource(cc_invoke_mod._resolve_claude_klabauter_root)
    assert "return _self_located\n" not in source, (
        "Rung 3 (self-location via __file__) regressed to answering the "
        "DISPATCH question directly — it must delegate through "
        "_delegate_to_gate()."
    )
    assert "_delegate_to_gate(_self_located" in source


def test_dispatch_symbol_set_never_touches_file_directly(cc_invoke_mod, shim):
    """THE DISPATCH SYMBOL SET, precisely: `_resolve_claude_klabauter_root` (cc_invoke)
    and `resolve_claude_klabauter_root_with_class` (the shared shim) are the two
    functions that answer "which engine executes". Neither reads `__file__`
    directly to PRODUCE its answer — `_resolve_claude_klabauter_root` only reaches
    `__file__` to seed `_walk_up_to_checkout`, whose result is then
    delegated (asserted above), and `resolve_claude_klabauter_root_with_class` does
    not reference `__file__` at all. This is a targeted assertion against
    the DISPATCH symbol set, deliberately NOT a blanket "no Path(__file__)
    anywhere in this resolution surface" scan — that scan would also flag
    `engine_root.current_engine_clone` / `resolve_engine_root` /
    `resolve_colocated_claude_klabauter_root`, which keep self-location on the
    LOCATOR axis on purpose (see this module's own docstring)."""
    dispatch_source = inspect.getsource(shim.resolve_claude_klabauter_root_with_class)
    assert "__file__" not in dispatch_source

    cc_invoke_source = inspect.getsource(cc_invoke_mod._resolve_claude_klabauter_root)
    assert "__file__" in cc_invoke_source, (
        "sanity: this function DOES consult __file__ (Rung 3) — the "
        "point is that it delegates, not that it never touches it"
    )


def test_locator_axis_keeps_self_location_deliberately():
    """The other half of the DISPATCH/LOCATOR distinction: the LOCATOR
    functions this plan explicitly preserves must still use `Path(__file__)`
    — a regression here would mean someone "fixed" the locator axis too,
    breaking Hard constraint 2 for every co-located script."""
    from coordinator_core.warm import engine_root

    source = inspect.getsource(engine_root.current_engine_clone)
    assert "__file__" in source


# ---------------------------------------------------------------------------
# Rung: pointer file (`.claude-klabauter-root` / `.claude-klabauter-root`) — cc_invoke's
# Rung 1.5, the one documented direct-return exception. Safe only because the
# published pointer is written by the same install pass that registers a
# STAMPED mirror (C5's `_resolve_published_engine` is what makes that
# registration mean anything) — this test pins that the exception still
# exists in exactly this shape, so a future edit does not silently widen it.
# ---------------------------------------------------------------------------


def test_pointer_file_rung_is_the_one_documented_direct_return(cc_invoke_mod):
    source = inspect.getsource(cc_invoke_mod._resolve_claude_klabauter_root)
    assert "return _published_pointer_val" in source
    assert "return _pointer_val" in source
    # Neither pointer rung is delegated -- that is the documented exception,
    # not a bug; C6's own test (test_cc_invoke_no_ambient_live_tree.py) pins
    # the same fact. Cross-checked here so this guard's enumeration is
    # complete without re-deriving that test's fixtures.
    assert "_delegate_to_gate(_published_pointer_val" not in source
    assert "_delegate_to_gate(_pointer_val" not in source


# ---------------------------------------------------------------------------
# Rung: cwd — `_session_repo_root()`'s `Path.cwd()` walk. Never itself a
# candidate for "which engine executes" -- it only feeds the STRUCTURAL
# discriminant (`_is_claude_klabauter_source_tree`) that decides whether to prefer the
# published engine, never returned as a resolved root in its own right.
# ---------------------------------------------------------------------------


def test_cwd_rung_never_feeds_the_dispatch_answer_directly(shim):
    """`resolve_claude_klabauter_root_with_class()` (the DISPATCH ladder) must not call
    `_session_repo_root()` itself -- cwd only reaches the ladder indirectly,
    through `_is_claude_klabauter_source_tree`'s boolean gate, never as a path
    returned to a caller."""
    dispatch_source = inspect.getsource(shim.resolve_claude_klabauter_root_with_class)
    assert "_session_repo_root(" not in dispatch_source

    gate_source = inspect.getsource(shim._is_claude_klabauter_source_tree)
    assert "_session_repo_root(" in gate_source, (
        "sanity: _session_repo_root is still consulted somewhere -- just "
        "not as a direct dispatch answer"
    )


def test_cwd_rung_cannot_resolve_an_unstamped_tree_via_the_structural_gate(
    tmp_path, registry, shim, monkeypatch
):
    """Behavioral half: even when the CURRENT working tree (cwd) differs
    from the live-tree ladder's own answer -- the case `_is_claude_klabauter_source_tree`
    exists to detect -- an unstamped published engine is still refused, so
    cwd cannot be used to smuggle an unstamped tree in as "the engine" by
    manipulating which tree the session appears to be inside."""
    published_root = tmp_path / "published"
    (published_root / "coordinator_core").mkdir(parents=True)  # unstamped

    live_root = tmp_path / "live"
    live_root.mkdir()

    session_dir = tmp_path / "session-elsewhere"
    session_dir.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session_dir))

    _register_published(registry.ml_dir, published_root)
    (registry.ml_dir / ".claude-klabauter-root").write_text(str(live_root), encoding="utf-8")

    root, resolution_class = shim.resolve_claude_klabauter_root_with_class()
    assert root == str(live_root)
    assert resolution_class == shim.RESOLUTION_LIVE_WORKING_TREE
    assert root != published_root.as_posix()


# ---------------------------------------------------------------------------
# exec_cli's C4b fallback (C13) -- the missing-target branch must not reach
# for a second, live-tree-only root once the resolved root (whichever class
# answered) misses a target on disk.
# ---------------------------------------------------------------------------


def test_exec_cli_no_longer_falls_back_to_resolve_claude_klabauter_bin_dir(shim):
    """C13: a missing target under the resolved root fails loud (127) naming
    the ONE root tried -- it must not call `resolve_claude_klabauter_bin_dir()` (the
    single-tier, live-tree-only locator) as a second-root fallback."""
    source = inspect.getsource(shim.exec_cli)
    assert "= resolve_claude_klabauter_bin_dir(" not in source, (
        "exec_cli's C4b live-tree fallback has regressed -- a missing "
        "target must fail loud against the one resolved root, not silently "
        "reach into a second (unstamped-capable) tree."
    )
    assert "resolve_claude_klabauter_bin_dir()" not in source.split('"""', 2)[-1], (
        "exec_cli's own body (past its docstring) must not call "
        "resolve_claude_klabauter_bin_dir() -- that would resurrect the C4b fallback."
    )
    assert "C13" in source


# ---------------------------------------------------------------------------
# The ambient/deliberate boundary, both directions, on the one rung with a
# clean in-process fixture for it (registry key / published engine): an
# ambient (unstamped) registration cannot resolve, and a deliberate
# (stamped) one still does -- see the two registry-key tests above, which
# together ARE this boundary. This test asserts the pairing explicitly so a
# future edit that breaks only one half is caught by name.
# ---------------------------------------------------------------------------


def test_ambient_and_deliberate_registry_answers_diverge_only_on_the_stamp(
    tmp_path, registry, shim
):
    unstamped_root = tmp_path / "unstamped"
    (unstamped_root / "coordinator_core").mkdir(parents=True)

    stamped_root = tmp_path / "stamped"
    (stamped_root / "coordinator_core").mkdir(parents=True)
    _write_stamp(stamped_root)

    _register_published(registry.ml_dir, unstamped_root)
    assert shim._resolve_published_engine(registry.ml_dir) is None

    _register_published(registry.ml_dir, stamped_root)
    assert shim._resolve_published_engine(registry.ml_dir) == stamped_root.as_posix()
