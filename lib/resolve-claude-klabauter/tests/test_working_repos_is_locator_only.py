"""
coordinator/lib/resolve-claude-klabauter/tests/test_working_repos_is_locator_only.py

Chunk C4 (docs/plans/2026-08-16-one-engine-for-the-whole-box.md): retires the
per-repo exemption duty from ``engine.working_repos`` — ``_is_engine_working_repo``
and ``_engine_working_repo_roots`` are REMOVED from ``_resolve_claude_klabauter.py``, and
``resolve_claude_klabauter_root_with_class()``'s live-tree-vs-published discriminant is
now structural (``_is_claude_klabauter_source_tree``: is the session's own root the ONE
path ``_resolve_claude_klabauter_root`` resolves?), never per-repo registry-key identity.
``engine.working_repos`` itself is UNCHANGED — it survives as a pure LOCATOR
for other callers (``setup_chain_walker.py``'s ``engine.working_repos.doe_claude``
rung; DoE-claude's own resolver, untouched by this chunk).

PM ruling implemented here: a per-repo exemption family cannot express a
box-wide choice; the structural check is not a family (nothing to enumerate,
nothing to maintain per repo) and does not reintroduce working-set membership
derived from ``repos.*`` — see the tripwire this must not re-derive,
``coordinator-tripwires/repos-star-is-not-engine-working-set.md``
(``REPOS-STAR-IS-NOT-ENGINE-WORKING-SET``, DoE-claude).

Divert requires BOTH the structural gate (confirmed ``False``) AND
``engine.target`` being readable (AC20, ruling correction 2026-08-18) —
presence/readability only, its value never inspected. A box with
``repos.claude_klabauter`` registered but no ``engine.target`` written
(every machine installed before C8) must never divert, even when the
structural gate independently confirms the session is not the live tree.

Covers:
  - the two retired functions are gone from the module (pins the retirement
    itself, not just a behavior change around it);
  - no resolution path branches on repo identity — a session registered
    under ``engine.working_repos.*`` (by name, as a "working repo") but
    whose OWN root differs from the resolved live tree still diverts to the
    published engine; membership alone no longer exempts it;
  - the flip side: a session whose OWN root literally IS the resolved live
    tree resolves live-working-tree, with no ``engine.working_repos.*``
    entry registered at all;
  - AC20: absent ``engine.target`` never diverts, even when the structural
    gate alone would;
  - each locator caller enumerated for this chunk still finds its root:
    ``setup_chain_walker.py``'s ``engine.working_repos.doe_claude`` rung
    (a thin re-exercise; the exhaustive case matrix lives in that module's
    own test file and is unaffected by this chunk) and
    ``percolate-liveops-preflight.py``'s AFFECTED/UNAFFECTED classification,
    now routed through ``_resolve_claude_klabauter_root`` directly rather than a
    second, independently-coded ``engine.working_repos.*`` scan.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C4
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_SHIM_PATH = _REPO_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"
_SETUP_CHAIN_WALKER_PATH = _REPO_ROOT / "coordinator_core" / "ops" / "setup_chain_walker.py"
_PREFLIGHT_PATH = _REPO_ROOT / "coordinator" / "bin" / "percolate-liveops-preflight.py"


def _load_shim():
    spec = importlib.util.spec_from_file_location("_c4_resolve_claude_klabauter_under_test", _SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


resolve_claude_klabauter = _load_shim()


def _make_claude_klabauter_fixture(root: Path) -> None:
    """Minimal on-disk shape ``_validate_bin_dir`` (not exercised directly by
    these tests, but ``resolve_claude_klabauter_root_with_class``'s callers expect a
    real-looking root) and ``os.path.isdir`` checks accept."""
    (root / "coordinator_core").mkdir(parents=True)


def _make_published_engine_fixture(root: Path) -> None:
    (root / "coordinator_core").mkdir(parents=True)


# ---------------------------------------------------------------------------
# The retirement itself
# ---------------------------------------------------------------------------


def test_is_engine_working_repo_is_removed():
    assert not hasattr(resolve_claude_klabauter, "_is_engine_working_repo")


def test_engine_working_repo_roots_is_removed():
    assert not hasattr(resolve_claude_klabauter, "_engine_working_repo_roots")


def test_structural_gate_is_present():
    assert hasattr(resolve_claude_klabauter, "_is_claude_klabauter_source_tree")


# ---------------------------------------------------------------------------
# No resolution path branches on repo identity
# ---------------------------------------------------------------------------


def test_working_repos_membership_alone_does_not_exempt_a_different_session(
    tmp_path: Path, monkeypatch
):
    """A session registered BY NAME under ``engine.working_repos.*`` — the
    retired discriminant's own positive case — still diverts to the
    published engine when its own root is not the resolved live tree.
    Membership in the locator namespace is not consulted by the gate at
    all."""
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    live_root = tmp_path / "live-claude-klabauter"
    _make_claude_klabauter_fixture(live_root)
    (ml_dir / ".claude-klabauter-root").write_text(str(live_root), encoding="utf-8")

    published_root = tmp_path / "published-klabauter"
    _make_published_engine_fixture(published_root)

    session_root = tmp_path / "session-repo"
    session_root.mkdir()

    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root}\'\n'
        # Registered as a "working repo" by name -- the exact fact that used
        # to exempt this session from diverting. It no longer does.
        f'"engine.working_repos.claude_klabauter" = \'{session_root}\'\n'
        # engine.target must be readable for the divert to fire at all
        # (AC20) -- this fixture isolates the identity discriminant, not
        # the target-presence one (that has its own dedicated test).
        '"engine.target" = \'candidate\'\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session_root))

    root, cls = resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()
    assert root == str(published_root)
    assert cls == resolve_claude_klabauter.RESOLUTION_RESOLVED_ENGINE


def test_no_working_repos_entry_needed_when_session_is_the_live_root(
    tmp_path: Path, monkeypatch
):
    """The flip side: a session whose own root IS the resolved live tree
    resolves live-working-tree with NO ``engine.working_repos.*`` entry
    registered anywhere — the structural check needs nothing enumerated."""
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    live_root = tmp_path / "live-claude-klabauter"
    _make_claude_klabauter_fixture(live_root)
    (ml_dir / ".claude-klabauter-root").write_text(str(live_root), encoding="utf-8")

    published_root = tmp_path / "published-klabauter"
    _make_published_engine_fixture(published_root)

    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root}\'\n', encoding="utf-8"
    )

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(live_root))

    root, cls = resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()
    assert root == str(live_root)
    assert cls == resolve_claude_klabauter.RESOLUTION_LIVE_WORKING_TREE


def test_absent_engine_target_does_not_divert_regardless_of_identity(tmp_path: Path, monkeypatch):
    """AC20: `engine.target` unreadable (never written -- every machine
    installed before C8) MUST NOT divert even when the structural gate
    independently confirms the session is NOT the live tree. Divert
    requires BOTH conjuncts; identity alone (confirmed False) is not
    sufficient, matching the "not yet rolled out, never a silent opt-in"
    rule this repo's C3/C8 chunks established."""
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    live_root = tmp_path / "live-claude-klabauter"
    _make_claude_klabauter_fixture(live_root)
    (ml_dir / ".claude-klabauter-root").write_text(str(live_root), encoding="utf-8")

    published_root = tmp_path / "published-klabauter"
    _make_published_engine_fixture(published_root)

    session_root = tmp_path / "session-repo"
    session_root.mkdir()

    # repos.claude_klabauter registered, session confirmed NOT the live
    # tree, but no "engine.target" key anywhere.
    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root}\'\n', encoding="utf-8"
    )

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session_root))

    assert resolve_claude_klabauter.resolve_engine_target(ml_dir) is None
    assert resolve_claude_klabauter._is_claude_klabauter_source_tree(ml_dir) is False

    root, cls = resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()
    assert root == str(live_root)
    assert cls == resolve_claude_klabauter.RESOLUTION_LIVE_WORKING_TREE


# ---------------------------------------------------------------------------
# Locator callers still find their root
# ---------------------------------------------------------------------------


def test_setup_chain_walker_locator_rung_still_resolves(tmp_path: Path, monkeypatch):
    """``setup_chain_walker.py``'s ``engine.working_repos.doe_claude`` rung
    is untouched by this chunk (locator-use, never exemption-use) — thin
    smoke check that it still resolves a candidate off that registry key.
    The exhaustive case matrix (publish-mirror rejection, missing positive
    evidence, flag/env precedence, ...) lives in
    ``coordinator_core/ops/test_setup_chain_walker.py`` and is unaffected."""
    loader = importlib.machinery.SourceFileLoader(
        "_c4_setup_chain_walker_under_test", str(_SETUP_CHAIN_WALKER_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    scw = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(scw)

    doe_root = tmp_path / "doe-claude-plugin-source"
    (doe_root / ".claude-plugin").mkdir(parents=True)
    (doe_root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (doe_root / "commands").mkdir()

    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(
        scw,
        "registry_get",
        lambda key: str(doe_root.parent) if key == "engine.working_repos.doe_claude" else None,
    )
    monkeypatch.setattr(scw, "_is_publish_mirror", lambda path: False)
    monkeypatch.setattr(
        scw,
        "_resolve_plugin_root_for_machine_local",
        lambda raw: doe_root if raw == doe_root.parent else None,
    )

    candidate = scw._resolve_coordinator_root_ladder(argv=[])
    assert candidate is not None
    resolved_path, rung = candidate
    assert resolved_path == doe_root
    assert "engine.working_repos.doe_claude" in rung


def test_percolate_liveops_preflight_locator_still_resolves_source_tree(tmp_path: Path):
    """``percolate-liveops-preflight.py`` now imports
    ``_resolve_claude_klabauter.py::_resolve_claude_klabauter_root`` directly (C4: no longer a
    second, independently-coded scan over ``engine.working_repos.*``) —
    confirm the import wiring resolves and the function it pulled in is the
    SAME shim this file already loaded, not a stale/duplicate copy."""
    loader = importlib.machinery.SourceFileLoader(
        "_c4_percolate_preflight_under_test", str(_PREFLIGHT_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    preflight = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(preflight)

    # Each test module loads `_resolve_claude_klabauter.py` fresh by file path (no
    # shared `sys.modules` entry across the two loaders here), so the
    # imported names are not `is`-identical objects -- compare by source
    # file + qualname instead, which is what actually matters: the
    # preflight script pulled its resolver from THIS file, not a stale or
    # duplicated copy living elsewhere.
    actual = os.path.normpath(preflight._resolve_claude_klabauter_source_root.__code__.co_filename)
    assert actual == os.path.normpath(str(_SHIM_PATH))
    assert preflight._resolve_claude_klabauter_source_root.__qualname__ == "_resolve_claude_klabauter_root"
    assert preflight.ClaudeKlabauterResolutionError.__qualname__ == "ClaudeKlabauterResolutionError"
    assert issubclass(preflight.ClaudeKlabauterResolutionError, RuntimeError)
