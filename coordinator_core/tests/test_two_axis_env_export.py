"""C18: dispatch and locator are two variables, and the split is ADDITIVE.

Chunk: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md § C18
Axis definition: docs/decisions/DR-326.

WHAT THESE PIN, and why the additive half is the load-bearing half. C10's rename
window is safe because both names carry the SAME value, so a fallback read is
always right. C18 is a SEMANTIC split: afterwards there are two facts and a
fallback is right for only one. The invariant that makes it landable across four
version-skewed parties -- live tree, published mirror, deployed settings home,
sibling repos -- is that the EXISTING variable never changes meaning and the
locator export is purely additive. A consumer that ignores the new key must
behave exactly as it did before.

So the most important assertions here are the NEGATIVE ones: that the dispatch
variable is untouched. A future edit that makes the split "cleaner" by
repointing the shared variable would pass every positive test in this file and
break the mirror, the settings home, and DoE-claude silently.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import coordinator_core.engine_root as engine_root

_DISPATCH = "COORDINATOR_ENGINE_ROOT"
_DISPATCH_OLD = "CLAUDE_KLABAUTER_ROOT"
_LOCATOR = "COORDINATOR_ENGINE_SOURCE_ROOT"

_CC_INVOKE_LIB = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "lib"


@pytest.fixture(autouse=True)
def _clean_axis_env(monkeypatch):
    for var in (_DISPATCH, _DISPATCH_OLD, _LOCATOR):
        monkeypatch.delenv(var, raising=False)
    engine_root._reset_locator_axis_advisories()
    engine_root._reset_engine_root_env_advisories()
    yield
    engine_root._reset_locator_axis_advisories()
    engine_root._reset_engine_root_env_advisories()


@pytest.fixture
def cc_invoke():
    """Import cc_invoke the way its CLIs do -- by putting its lib dir on sys.path."""
    if str(_CC_INVOKE_LIB) not in sys.path:
        sys.path.insert(0, str(_CC_INVOKE_LIB))
    import cc_invoke as mod

    return mod


@pytest.fixture(autouse=True)
def _force_the_registry_read_through_the_stubbed_path(monkeypatch):
    """Neutralise `_locator_env_export`'s IN-PROCESS registry read.

    Every locator test below stubs `cc_invoke._machine_local_get`, but that is
    the FALLBACK. `_locator_env_export` calls `_machine_local_get_in_process`
    first and only falls through on its miss -- and that reader resolves the
    REAL machine-local registry, memoized for the interpreter's life in
    `cc_invoke._IN_PROCESS_REGISTRY_MEMO`.

    So the stub was reached only while the primary happened to miss. Alone,
    under the quarantined HOME, it does; in the tier, once any earlier test in
    the worker warms that memo with this box's real `repos.claude_klabauter`, the
    primary answers `X:/claude-klabauter` and the stub is never called at all --
    which is exactly how these four failed: a `KeyError: 'key'` from a spy that
    never ran, a locator reading the real repo instead of `/src/checkout`, and
    the locator present in an env the test had arranged to be unresolvable.
    Order-dependent, and nothing to do with the engine-root memos.

    Forcing the primary to a miss makes every test here exercise the fallback it
    actually patches, deterministically and regardless of what ran first. The
    in-process reader's own behaviour is not this module's subject -- it is the
    spawn-avoidance optimisation, covered where it is introduced.
    """
    if str(_CC_INVOKE_LIB) not in sys.path:
        sys.path.insert(0, str(_CC_INVOKE_LIB))
    import cc_invoke as mod

    mod._IN_PROCESS_REGISTRY_MEMO.clear()
    monkeypatch.setattr(mod, "_machine_local_get_in_process", lambda key: None)
    yield
    mod._IN_PROCESS_REGISTRY_MEMO.clear()


# --- the two axes are two variables ----------------------------------------

def test_the_two_axes_use_distinct_variable_names():
    """If these ever collapse to one name the whole chunk is undone."""
    assert engine_root._ENGINE_SOURCE_ROOT_VAR != engine_root._ENGINE_ROOT_NEW_VAR
    assert engine_root._ENGINE_SOURCE_ROOT_VAR == _LOCATOR


def test_locator_name_carries_no_repo_token():
    """Same property that made the module and dispatch-variable renames correct:
    with no repo token the publish transform is a no-op and both trees ship one
    spelling."""
    assert "claude-klabauter" not in engine_root._ENGINE_SOURCE_ROOT_VAR.lower()
    assert "klabauter" not in engine_root._ENGINE_SOURCE_ROOT_VAR.lower()


# --- locator read accessor --------------------------------------------------

def test_locator_prefers_its_own_variable(monkeypatch, capsys):
    monkeypatch.setenv(_LOCATOR, "/src/checkout")
    monkeypatch.setenv(_DISPATCH, "/engines/published")
    assert engine_root.coordinator_engine_source_root_env("t") == "/src/checkout"
    assert capsys.readouterr().err == "", "no advisory when the locator name answered"


def test_locator_falls_back_to_the_shared_variable_and_says_so(monkeypatch, capsys):
    """The fallback exists so an unrouted caller keeps working -- and emits,
    because C18's exit condition is evidence that the misread stopped, not an
    assertion that it did."""
    monkeypatch.setenv(_DISPATCH, "/engines/published")
    assert engine_root.coordinator_engine_source_root_env("site-a") == "/engines/published"
    err = capsys.readouterr().err
    assert "site-a" in err and _LOCATOR in err


def test_locator_misread_advisory_is_once_per_site(monkeypatch, capsys):
    monkeypatch.setenv(_DISPATCH, "/engines/published")
    engine_root.coordinator_engine_source_root_env("site-a")
    capsys.readouterr()
    engine_root.coordinator_engine_source_root_env("site-a")
    assert capsys.readouterr().err == ""
    engine_root.coordinator_engine_source_root_env("site-b")
    assert "site-b" in capsys.readouterr().err


def test_locator_returns_none_when_nothing_is_set():
    assert engine_root.coordinator_engine_source_root_env("t") is None


# --- the additive invariant -------------------------------------------------

def test_locator_exports_only_its_own_key():
    """THE NEGATIVE SPEC. The write helper must never emit a dispatch key."""
    exports = engine_root.coordinator_engine_source_root_exports("/src/checkout")
    assert exports == {_LOCATOR: "/src/checkout"}
    assert _DISPATCH not in exports and _DISPATCH_OLD not in exports


def test_locator_export_is_empty_when_unresolvable():
    """A box with no registered checkout keeps spawning children exactly as it
    does today, rather than exporting an empty or invented value."""
    assert engine_root.coordinator_engine_source_root_exports(None) == {}
    assert engine_root.coordinator_engine_source_root_exports("") == {}


def test_subprocess_env_adds_the_locator_without_touching_dispatch(cc_invoke, monkeypatch):
    """The whole chunk in one assertion: the child gains a locator variable and
    the dispatch variable is byte-identical to what it was before.

    C14 CHANGED WHICH KEY CARRIES THE DISPATCH VALUE, and the change is
    orthogonal to C18's split this file otherwise pins. Before C14
    `_build_subprocess_env` exported the dispatch value under BOTH
    `COORDINATOR_ENGINE_ROOT` and `CLAUDE_KLABAUTER_ROOT`; C14 closed that dual-write, so
    the child now carries the value under the new name only (see
    `cc_invoke._build_subprocess_env`'s own C14 docstring paragraph). This test
    asserted the old key because it predates C14 — the invariant it exists to
    prove ("the dispatch value itself doesn't change" untouched by the
    locator's addition) still holds, just checked against the surviving key."""
    monkeypatch.setattr(cc_invoke, "_machine_local_get", lambda key: "/src/checkout")
    env = cc_invoke._build_subprocess_env("/engines/published")
    assert env[_DISPATCH] == "/engines/published", "dispatch value must not change"
    assert _DISPATCH_OLD not in env, "C14 closed the dual-write; the old name is no longer exported"
    assert env[_LOCATOR] == "/src/checkout"
    assert env["PYTHONPATH"].startswith("/engines/published"), "PYTHONPATH still the engine"


def test_subprocess_env_unchanged_when_the_locator_is_unresolvable(cc_invoke, monkeypatch):
    """See test_subprocess_env_adds_the_locator_without_touching_dispatch's C14
    note: the dispatch value now lives under the new name only."""
    monkeypatch.setattr(cc_invoke, "_machine_local_get", lambda key: None)
    env = cc_invoke._build_subprocess_env("/engines/published")
    assert _LOCATOR not in env
    assert env[_DISPATCH] == "/engines/published"
    assert _DISPATCH_OLD not in env


def test_locator_lookup_failure_never_breaks_the_spawn(cc_invoke, monkeypatch):
    """Best-effort by design: this runs on the commit hot path for every session
    on the box, so a registry read failure must degrade to "no locator export",
    never to a raise.

    See test_subprocess_env_adds_the_locator_without_touching_dispatch's C14
    note: the dispatch value now lives under the new name only."""
    def _boom(key):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(cc_invoke, "_machine_local_get", _boom)
    env = cc_invoke._build_subprocess_env("/engines/published")
    assert _LOCATOR not in env
    assert env[_DISPATCH] == "/engines/published"
    assert _DISPATCH_OLD not in env


def test_the_locator_resolves_off_the_registry_not_the_dispatch_ladder(cc_invoke, monkeypatch):
    """The axes must not be wired to the same source. `_resolve_claude_klabauter_root`
    deliberately prefers the published engine; the registry key IS the locator
    answer. If a future edit repoints this at the ladder the two axes silently
    reconverge and the split is undone while every other test still passes."""
    seen = {}

    def _spy(key):
        seen["key"] = key
        return "/src/checkout"

    monkeypatch.setattr(cc_invoke, "_machine_local_get", _spy)
    monkeypatch.setattr(
        cc_invoke, "_resolve_claude_klabauter_root", lambda: pytest.fail("locator must not call the dispatch ladder")
    )
    cc_invoke._build_subprocess_env("/engines/published")
    assert seen["key"] == "repos.claude_klabauter"
