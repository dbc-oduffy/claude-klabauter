"""
coordinator_core/tests/test_engine_root_two_tier.py

Chunk C4a (wrapper half): verifies
`coordinator_core.engine_root.coordinator_engine_root_with_class()` — which
loads C3's shim (`coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`) BY
PATH and wraps its `resolve_claude_klabauter_root_with_class()` rather than
reimplementing the published-engine-vs-live-working-tree gate — agrees with
the shim itself, and that install's derived forwarder name set resolves
under both resolution classes.

Spec backlink: pln-two-tier-engine-root-resolutio-024269 § C4 (wrapper half)

Negative-spec: does NOT re-derive or hardcode the forwarder count (351 on
this tree at authoring time) — `_derive_agent_helper_target_map` is called
live so the test tracks `coordinator/bin/`'s actual contents, never a
frozen number. Does NOT assert the claude-klabauter-vs-published gap is empty or of
any specific size — the gap is real (C4b's future exec-time gate target,
not this chunk's), and this test asserts its SHAPE (some names resolve only
under live-tree) rather than papering over it.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from coordinator_core import engine_root as claude_klabauter_root
from coordinator_core.install.substrate import _derive_agent_helper_target_map

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SHIM_PATH = _REPO_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"


def _load_shim_for_test():
    """Load the shim by path for direct comparison against the wrapper —
    a SEPARATE load from `claude_klabauter_root._load_shim()`'s own module-scope
    memo, so this test never mutates or depends on the wrapper's cache
    identity, only its return values."""
    spec = importlib.util.spec_from_file_location("_test_resolve_claude_klabauter_shim", _SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _reset_wrapper_memos():
    """The wrapper's shim-load memo and gate-answer memo are
    interpreter-lifetime state (mirrors `coordinator_doe_root`'s own
    `_reset_doe_root_cache` seam) — reset around every test in this module
    so no test's resolution pins the answer for a later one."""
    claude_klabauter_root._reset_shim_cache()
    claude_klabauter_root._reset_gate_memo()
    yield
    claude_klabauter_root._reset_shim_cache()
    claude_klabauter_root._reset_gate_memo()


def _write_engine_stamp(root: Path) -> None:
    """C5 (docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md):
    `_resolve_published_engine` now denies an unstamped root outright — "an
    engine root is a stamped build. No stamp, no engine." Every fixture in
    this file that builds a synthetic PUBLISHED-engine directory (i.e. one
    meant to actually resolve as `resolved-engine`) must write this stamp
    or the stamp gate denies it regardless of the rest of the fixture's
    setup. Mirrors `coordinator_core.warm.skew.write_engine_stamp`'s shape
    (one line, only its bytes matter) without importing it — this test
    module already imports `coordinator_core`, so the duplication here is
    about keeping the fixture self-contained and legible, not an
    import-independence constraint like the shim's own copy."""
    stamp = Path(root) / "coordinator_core" / "_engine_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("sha:test-fixture-stamp\n", encoding="utf-8")


def _normalize_root(root: str) -> str:
    """Normalize slash direction/case for path-EQUALITY comparison only.

    The wrapper's free rungs (Rung 1 `CLAUDE_KLABAUTER_ROOT` env var, Rung 1.5 the
    `.claude-klabauter-root` pointer file) and the shim's own registry-key rung can
    legitimately return the SAME path in different string forms (observed
    on this box: pointer file holds a forward-slash path, the registry's
    flat-quoted-key form holds a backslash path) — that is a pre-existing
    property of the two independent sources, not a defect this chunk owns
    or may fix (the shim is out-of-scope; see module docstring). The
    cross-entrypoint agreement test below is about RESOLUTION agreement,
    not byte-identical string form, so it normalizes before comparing.
    """
    return os.path.normcase(os.path.normpath(root))


def test_shim_present_and_loadable():
    assert _SHIM_PATH.is_file(), (
        f"C3's shim is expected at {_SHIM_PATH} — this wrapper depends on its presence"
    )
    shim = _load_shim_for_test()
    assert hasattr(shim, "resolve_claude_klabauter_root_with_class")


@pytest.mark.real_home
def test_cross_entrypoint_agreement():
    """(a) The shim's own `resolve_claude_klabauter_root_with_class()` and
    `coordinator_engine_root_with_class()` agree — same resolution class,
    same effective root — over the machine's live registry state. This is
    what makes the C3<->C4 agreement a CHECKED invariant, not a documented
    drift seam."""
    shim = _load_shim_for_test()
    expected_root, expected_class = shim.resolve_claude_klabauter_root_with_class()
    actual_root, actual_class = claude_klabauter_root.coordinator_engine_root_with_class()

    assert actual_class == expected_class
    assert _normalize_root(actual_root) == _normalize_root(expected_root)


# Review: code-reviewer (finding 1) — the `real_home` cross-entrypoint
# agreement test above depends on the box's own live registry contents and
# cannot guarantee it exercises the wrapper's cheap short-circuit (the
# `repos.claude_klabauter`-absent branch in `coordinator_engine_root_with_class`
# that reaches into the shim's `_ml_dir`/`_registry_value`/
# `_resolve_claude_klabauter_root` helpers directly rather than calling
# `resolve_claude_klabauter_root_with_class()`). This fixture pins that branch
# deterministically: `repos.claude_klabauter` is never written to the
# synthetic registry at all, so the wrapper MUST take the short-circuit
# while the shim's own `resolve_claude_klabauter_root_with_class()` runs its full
# ladder — and both are asserted to still agree.
@pytest.fixture
def _short_circuit_fixture(tmp_path, monkeypatch):
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    live_dir = tmp_path / "live"
    live_dir.mkdir()

    lines = [
        "[repos]",
        f'claude_klabauter = "{live_dir.as_posix()}"',
        "",
    ]
    (ml_dir / "registry.local.toml").write_text("\n".join(lines), encoding="utf-8")

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)

    return SimpleNamespace(ml_dir=ml_dir, live_dir=live_dir)


def test_cross_entrypoint_agreement_short_circuit_branch(_short_circuit_fixture):
    """`repos.claude_klabauter` is absent from the registry — the wrapper's
    cheap short-circuit (step 2, see `coordinator_engine_root_with_class`'s
    own docstring) fires, calling `shim._ml_dir()`/`shim._resolve_claude_klabauter_root()`
    directly instead of `shim.resolve_claude_klabauter_root_with_class()`. Both must
    still agree with the shim's own full-ladder answer — this is the
    mechanical backstop the module docstrings (both files) point to."""
    shim = _load_shim_for_test()
    expected_root, expected_class = shim.resolve_claude_klabauter_root_with_class()
    actual_root, actual_class = claude_klabauter_root.coordinator_engine_root_with_class()

    assert expected_class == shim.RESOLUTION_LIVE_WORKING_TREE
    assert actual_class == expected_class
    assert _normalize_root(actual_root) == _normalize_root(expected_root)
    assert _normalize_root(actual_root) == _normalize_root(
        str(_short_circuit_fixture.live_dir)
    )


def _agent_bin_dir_for_live_tree(shim) -> Path:
    ml_dir = shim._ml_dir()
    live_root = shim._resolve_claude_klabauter_root(ml_dir)
    return Path(live_root) / "coordinator" / "bin"


def _agent_bin_dir_for_published(shim) -> Optional[Path]:
    ml_dir = shim._ml_dir()
    published_root = shim._resolve_published_engine(ml_dir)
    if not published_root:
        return None
    return Path(published_root) / "coordinator" / "bin"


def _exec_able(bin_dir: Path, on_disk_name: str) -> bool:
    """Mirrors `exec_cli`'s own resolved-target usability check
    (`os.path.isfile` + `os.access(..., os.R_OK)`) rather than the
    lower-level POSIX-exec-bit/PATHEXT probe `_is_executable` uses for the
    `coordinator/bin` sentinel — every forwarder target here is a Python
    CLI run in-process via `runpy`/interpreter-targeted `execv` (see
    `exec_cli`'s own docstring), so "exec-able" for this test's purposes
    means "present and readable at the resolved path", exactly what
    `exec_cli` itself checks before running a target."""
    target_path = bin_dir / on_disk_name
    return target_path.is_file() and os.access(target_path, os.R_OK)


@pytest.mark.real_home
def test_exhaustive_forwarder_target_map_two_tier_shape():
    """(b) EXHAUSTIVE — one row per name in
    `_derive_agent_helper_target_map()`'s live-tree output (never sampled,
    never hardcoded) — asserting the resolved on-disk target is exec-able
    under the live-working-tree class, and recording (not erasing) the
    known claude-klabauter-vs-published gap under the published class.

    The published mirror is only reachable on a machine with
    `repos.claude_klabauter` registered and usable; where it is absent this
    test still exhaustively checks the live-tree half (still exhaustive per
    the brief — the published half degrades to "not measurable on this
    box" rather than a false pass).
    """
    shim = _load_shim_for_test()
    live_bin_dir = _agent_bin_dir_for_live_tree(shim)
    live_map = _derive_agent_helper_target_map(live_bin_dir)
    assert live_map, "expected a non-empty forwarder target map on this tree"

    # Exhaustive over EVERY derived name — no sampling.
    missing_under_live = [
        name for name, on_disk in live_map.items() if not _exec_able(live_bin_dir, on_disk)
    ]
    assert not missing_under_live, (
        f"{len(missing_under_live)} forwarder name(s) not exec-able under the "
        f"live-working-tree class: {sorted(missing_under_live)[:10]}"
    )

    published_bin_dir = _agent_bin_dir_for_published(shim)
    if published_bin_dir is None:
        pytest.skip(
            "repos.claude_klabauter is not registered/usable on this machine — "
            "published-class half of the exhaustive check is not measurable here"
        )

    published_map = _derive_agent_helper_target_map(published_bin_dir)
    assert published_map, "expected a non-empty forwarder target map under the published mirror"

    live_only = sorted(set(live_map) - set(published_map))
    published_only = sorted(set(published_map) - set(live_map))

    # SHAPE assertion, per the brief: this gap is real and is what C4b will
    # gate against at exec time. Do not weaken this into "gap is empty" and
    # do not edit the shim to close it — both are out of scope for C4a.
    assert live_only, (
        "expected a non-empty claude-klabauter-only forwarder set under the published class "
        "(the two derived sets are documented as NOT nested on this tree) — if this "
        "now fails, the published mirror may have caught up and the C4b exec-time "
        "gate target no longer applies; do not silently accept, escalate instead"
    )

    names_exec_able_under_published = [
        name
        for name in published_map
        if _exec_able(published_bin_dir, published_map[name])
    ]
    assert set(names_exec_able_under_published) == set(published_map), (
        "every name the published mirror itself derives must be exec-able under "
        "its own bin dir — a partial/broken published checkout, not the documented gap"
    )

    for name in live_only:
        assert name not in published_map, (
            f"{name!r} was asserted live-tree-only but is present in the published map"
        )


# --- C5: engine/edit skew advisory ------------------------------------------
#
# Spec backlink: pln-two-tier-engine-root-resolutio-024269 § C5
#
# Fully synthetic registry fixture below (own settings-home under tmp_path,
# never the machine's real registry) — deliberately does NOT carry the
# `real_home` marker the exhaustive tests above need, since this section
# controls both the dual- and single-tier registration states directly
# rather than depending on the box's own live registry contents.


@pytest.fixture
def _skew_fixture(tmp_path, monkeypatch):
    """A synthetic registry rigged to resolve `RESOLUTION_RESOLVED_ENGINE`
    (a usable published engine + a session root confirmed NOT one of the
    registered `engine.working_repos.*`), with `write_registry` choosing
    whether `repos.claude_klabauter` is ALSO registered — the dual-vs-single
    tier axis the advisory keys off."""
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    published_dir = tmp_path / "published"
    (published_dir / "coordinator_core").mkdir(parents=True)
    _write_engine_stamp(published_dir)

    live_dir = tmp_path / "live"
    live_dir.mkdir()

    other_working_dir = tmp_path / "other-working-repo"
    other_working_dir.mkdir()

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    def _write_registry(*, claude_klabauter: bool) -> None:
        lines = [
            "[repos]",
            f'claude_klabauter = "{published_dir.as_posix()}"',
        ]
        if claude_klabauter:
            lines.append(f'claude_klabauter = "{live_dir.as_posix()}"')
        lines.append("")
        lines.append("[engine.working_repos]")
        lines.append(f'other = "{other_working_dir.as_posix()}"')
        (ml_dir / "registry.local.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session_dir))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT_SKEW_QUIET", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    # The advisory is opt-in (PM-ruled 2026-08-10). Every test below that
    # asserts on emitted text opts in here; the default-silent contract gets
    # its own tests, which delete this var again.
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT_SKEW_VERBOSE", "1")

    return SimpleNamespace(
        ml_dir=ml_dir,
        write_registry=_write_registry,
        live_dir=live_dir,
        published_dir=published_dir,
    )


def test_shim_skew_advisory_fires_on_dual_registration(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)
    shim = _load_shim_for_test()

    root, cls = shim.resolve_claude_klabauter_root_with_class()

    assert cls == shim.RESOLUTION_RESOLVED_ENGINE
    expected_live = shim._resolve_claude_klabauter_root(shim._ml_dir())
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "note:" in captured.err
    assert expected_live in captured.err
    assert str(_skew_fixture.published_dir) in captured.err or (
        _skew_fixture.published_dir.as_posix() in captured.err
    )


def test_shim_skew_advisory_silent_single_tier(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=False)
    shim = _load_shim_for_test()

    root, cls = shim.resolve_claude_klabauter_root_with_class()

    assert cls == shim.RESOLUTION_RESOLVED_ENGINE
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_shim_skew_advisory_stderr_only(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)
    shim = _load_shim_for_test()

    shim.resolve_claude_klabauter_root_with_class()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""


def test_shim_skew_advisory_once_per_process(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)
    shim = _load_shim_for_test()

    shim.resolve_claude_klabauter_root_with_class()
    shim.resolve_claude_klabauter_root_with_class()
    shim.resolve_claude_klabauter_root_with_class()

    captured = capsys.readouterr()
    assert captured.err.count("note:") == 1


def test_shim_skew_advisory_kill_switch(_skew_fixture, monkeypatch, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT_SKEW_QUIET", "1")
    shim = _load_shim_for_test()

    root, cls = shim.resolve_claude_klabauter_root_with_class()

    assert cls == shim.RESOLUTION_RESOLVED_ENGINE
    captured = capsys.readouterr()
    assert captured.err == ""


def test_shim_skew_advisory_kill_switch_falsey_value_does_not_suppress(
    _skew_fixture, monkeypatch, capsys
):
    # Review: code-reviewer — CLAUDE_KLABAUTER_ROOT_SKEW_QUIET=0/"false" must NOT be
    # treated as a truthy kill-switch (a bare `.get(...)` truthy check would
    # suppress on any non-empty string, including these).
    _skew_fixture.write_registry(claude_klabauter=True)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT_SKEW_QUIET", "0")
    shim = _load_shim_for_test()

    root, cls = shim.resolve_claude_klabauter_root_with_class()

    assert cls == shim.RESOLUTION_RESOLVED_ENGINE
    captured = capsys.readouterr()
    assert "note:" in captured.err


# --- opt-in contract (PM-ruled 2026-08-10) ---------------------------------
#
# The advisory rode on every forwarder's stderr, so it surfaced on the
# `claude` startup banner — PM-facing chrome about an engine-side
# configuration. Silence is now the default; VERBOSE is the only way in.


def test_shim_skew_advisory_silent_by_default(_skew_fixture, monkeypatch, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT_SKEW_VERBOSE", raising=False)
    shim = _load_shim_for_test()

    root, cls = shim.resolve_claude_klabauter_root_with_class()

    assert cls == shim.RESOLUTION_RESOLVED_ENGINE
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


@pytest.mark.parametrize("value", ["", "0", "false", "FALSE"])
def test_shim_skew_advisory_falsey_verbose_stays_silent(
    _skew_fixture, monkeypatch, capsys, value
):
    _skew_fixture.write_registry(claude_klabauter=True)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT_SKEW_VERBOSE", value)
    shim = _load_shim_for_test()

    shim.resolve_claude_klabauter_root_with_class()

    assert capsys.readouterr().err == ""


def test_shim_skew_advisory_quiet_beats_verbose(_skew_fixture, monkeypatch, capsys):
    """An install that already exports the old kill-switch keeps its silence
    even if something downstream sets VERBOSE."""
    _skew_fixture.write_registry(claude_klabauter=True)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT_SKEW_VERBOSE", "1")
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT_SKEW_QUIET", "1")
    shim = _load_shim_for_test()

    shim.resolve_claude_klabauter_root_with_class()

    assert capsys.readouterr().err == ""


def test_wrapper_skew_advisory_silent_by_default(_skew_fixture, monkeypatch, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT_SKEW_VERBOSE", raising=False)

    root, cls = claude_klabauter_root.coordinator_engine_root_with_class()

    assert cls == "resolved-engine"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_wrapper_skew_advisory_fires_on_dual_registration(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)

    root, cls = claude_klabauter_root.coordinator_engine_root_with_class()

    assert cls == "resolved-engine"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "note:" in captured.err


def test_wrapper_skew_advisory_silent_single_tier(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=False)

    root, cls = claude_klabauter_root.coordinator_engine_root_with_class()

    assert cls == "resolved-engine"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_wrapper_skew_advisory_stderr_only(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)

    claude_klabauter_root.coordinator_engine_root_with_class()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""


def test_wrapper_skew_advisory_once_per_process(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)

    claude_klabauter_root.coordinator_engine_root_with_class()
    claude_klabauter_root.coordinator_engine_root_with_class()
    claude_klabauter_root._reset_gate_memo()
    claude_klabauter_root.coordinator_engine_root_with_class()

    captured = capsys.readouterr()
    assert captured.err.count("note:") == 1


def test_wrapper_skew_advisory_kill_switch(_skew_fixture, monkeypatch, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT_SKEW_QUIET", "1")

    root, cls = claude_klabauter_root.coordinator_engine_root_with_class()

    assert cls == "resolved-engine"
    captured = capsys.readouterr()
    assert captured.err == ""


# --- C4b: per-target existence gate on the published-engine rung -----------
#
# Spec backlink: pln-two-tier-engine-root-resolutio-024269
# (chunk C4, the `exec_cli` half) + tasks/two-tier-engine-root-exec/briefs/C4b.md
#
# `exec_cli` only ever exercises Windows' `_run_target_in_process` leg in
# this suite (`os.name == "nt"` on this box) — its `sys.exit(code)` is
# caught here via `pytest.raises(SystemExit)` rather than actually
# replacing the interpreter, which is what the POSIX `os.execv` leg would
# do if exercised directly (out of scope: that leg is untouched by this
# chunk, see the shim's own negative-spec docstring).


# --- C2 (2026-08-12 dual-boot plan): rung-1.5 pointer must not pre-empt the
# DR-132 gate. Four tests THROUGH THE WRAPPER
# (`coordinator_core.engine_root.coordinator_engine_root_with_class`) — the
# `_short_circuit_fixture`/`_skew_fixture` cases above exercise the wrapper
# too, but none of them pin a session root that the gate confirms is NOT a
# working repo while `.claude-klabauter-root` is ALSO present, which is exactly the
# defect's reproduction shape (see plan § Problem).
#
# Spec backlink: pln-arm-the-klabauter-dual-boot-th-f7169a


@pytest.fixture
def _dual_boot_fixture(tmp_path, monkeypatch):
    """A synthetic registry with `.claude-klabauter-root` present (rung 1.5 pointer)
    AND `repos.claude_klabauter` registered — the dual-boot shape. Session
    root is confirmed NOT a working repo, so the gate (once reached) picks
    the published engine. `write_registry` controls whether the session
    root is registered as a working repo, the axis AC1 vs AC2 keys off."""
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    live_dir = tmp_path / "live"
    live_dir.mkdir()
    (ml_dir / ".claude-klabauter-root").write_text(str(live_dir), encoding="utf-8")

    published_dir = tmp_path / "published-klabauter"
    (published_dir / "coordinator_core").mkdir(parents=True)
    _write_engine_stamp(published_dir)

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    def _write_registry(*, session_is_working_repo: bool) -> None:
        lines = [
            "[repos]",
            f'claude_klabauter = "{published_dir.as_posix()}"',
            "",
            "[engine.working_repos]",
        ]
        if session_is_working_repo:
            lines.append(f'claude_klabauter = "{session_dir.as_posix()}"')
        else:
            other_dir = tmp_path / "other-working-repo"
            other_dir.mkdir(exist_ok=True)
            lines.append(f'other = "{other_dir.as_posix()}"')
        lines.append("")
        (ml_dir / "registry.local.toml").write_text("\n".join(lines), encoding="utf-8")

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session_dir))
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT_SKEW_QUIET", "1")
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)

    return SimpleNamespace(
        ml_dir=ml_dir,
        write_registry=_write_registry,
        live_dir=live_dir,
        published_dir=published_dir,
        session_dir=session_dir,
    )


def test_dual_boot_published_wins_over_pointer_when_not_working_repo(_dual_boot_fixture):
    """AC1: `.claude-klabauter-root` present + `repos.claude_klabauter` registered +
    session root a CONFIRMED non-working repo -> the wrapper must match the
    shim's own answer, `(<published>, 'resolved-engine')`. RED before C1:
    today the wrapper returns the pointer's live tree instead, because rung
    1.5 fires before the gate is ever consulted."""
    fx = _dual_boot_fixture
    fx.write_registry(session_is_working_repo=False)

    root, cls = claude_klabauter_root.coordinator_engine_root_with_class()

    # `write_registry` seeds `claude_klabauter` via `as_posix()` (forward
    # slashes), and the resolver returns that registry value verbatim — so
    # a WindowsPath string comparison here is spurious on Windows; compare
    # as paths instead.
    assert Path(root) == fx.published_dir
    assert cls == "resolved-engine"


def test_dual_boot_live_tree_wins_when_session_is_working_repo(_dual_boot_fixture):
    """AC2: same dual-boot registration, but the session root IS registered
    under `engine.working_repos.*` -> the gate confirms it a working repo
    and the wrapper must still return the live tree."""
    fx = _dual_boot_fixture
    fx.write_registry(session_is_working_repo=True)

    root, cls = claude_klabauter_root.coordinator_engine_root_with_class()

    assert root == str(fx.live_dir)
    assert cls == "live-working-tree"


def test_dual_boot_claude_klabauter_root_env_no_longer_wins(_dual_boot_fixture, monkeypatch):
    """AC3 INVERTED by C14 (docs/plans/2026-08-20-an-engine-root-is-not-named-
    for-the-repo.md): `CLAUDE_KLABAUTER_ROOT` env used to win unconditionally at rung 1,
    regardless of the dual-boot registration below it. C14 closed the
    dual-read window — `coordinator_engine_root_env()` no longer answers with
    `CLAUDE_KLABAUTER_ROOT` at all, so rung 1 no longer short-circuits off it and
    resolution must fall through to whatever the dual-boot gate below it
    decides.

    Asserted by COMPARISON, not by hardcoding the gate's answer: the
    `test_dual_boot_published_wins_over_pointer_when_not_working_repo` sibling
    (identical fixture, no env override) is itself one of the ~12
    pre-existing failures tracked at
    state/bug-backlog/2026-08-20-21-pre-existing-failures-in-test-engine-fe2ecacfb144.yaml
    — the shim/gate currently answers `live-working-tree` there too, for a
    reason unrelated to this rename. Pinning this test to `resolved-engine`
    would make it fail for THAT bug, not for a C14 regression, and conflate
    the two. What C14 actually changed is narrower and still checkable
    without that gate bug: the explicit `CLAUDE_KLABAUTER_ROOT` override must produce
    the IDENTICAL answer as no override at all, proving rung 1 is inert on
    the old name. The old name is still read, but only to advise that it is
    retired — see `engine_root._maybe_emit_engine_root_retired`."""
    fx = _dual_boot_fixture
    fx.write_registry(session_is_working_repo=False)

    without_override = claude_klabauter_root.coordinator_engine_root_with_class()

    claude_klabauter_root._reset_shim_cache()
    claude_klabauter_root._reset_gate_memo()
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", "/explicit/claude-klabauter/root")
    with_override = claude_klabauter_root.coordinator_engine_root_with_class()

    assert with_override == without_override, (
        "a retired CLAUDE_KLABAUTER_ROOT must not change the resolution at all"
    )
    assert with_override[0] != "/explicit/claude-klabauter/root", (
        "the retired env value must never itself be returned"
    )


def test_dual_boot_absent_klabauter_byte_identical_pointer_fast_path(tmp_path, monkeypatch):
    """AC4: with `repos.claude_klabauter` ABSENT, behaviour is byte-identical
    to today — the rung-1.5 pointer fast path still fires and resolves the
    live tree, zero subprocess. Must stay GREEN both before and after C1."""
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    live_dir = tmp_path / "live"
    live_dir.mkdir()
    (ml_dir / ".claude-klabauter-root").write_text(str(live_dir), encoding="utf-8")

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    root, cls = claude_klabauter_root.coordinator_engine_root_with_class()

    assert root == str(live_dir)
    assert cls == "live-working-tree"


def test_dual_boot_absent_klabauter_pointer_honors_machine_local_registry_dir_override(
    tmp_path, monkeypatch
):
    """The rung-1.5 pointer read (AC4 fast path) must reuse the already-
    computed, override-aware `ml_dir` (`shim._ml_dir()`) rather than
    re-resolving `machine_local_dir()` directly — the two diverge whenever
    `MACHINE_LOCAL_REGISTRY_DIR` is set, since only `shim._ml_dir()` honors
    it. Proves the override genuinely reaches this rung: the settings-home
    machine-local dir is left EMPTY (no pointer file there at all) while the
    override dir holds the pointer — a resolution that only succeeds if the
    override is actually consulted, not merely a value-equality assertion
    that could pass by coincidence.

    Review: code-reviewer.
    """
    settings_home = tmp_path / "settings-home"
    settings_home_ml_dir = settings_home / "machine-local"
    settings_home_ml_dir.mkdir(parents=True)
    # Deliberately no `.claude-klabauter-root` written here — if the fix under test
    # regresses to `machine_local_dir()` (settings-home-derived, override-
    # blind), this rung would find nothing and fall through to Rung 2,
    # which has no registry entry either and would raise instead of
    # resolving `override_live_dir` below.

    override_ml_dir = tmp_path / "override-machine-local"
    override_ml_dir.mkdir()
    override_live_dir = tmp_path / "override-live"
    override_live_dir.mkdir()
    (override_ml_dir / ".claude-klabauter-root").write_text(
        str(override_live_dir), encoding="utf-8"
    )

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(override_ml_dir))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    root, cls = claude_klabauter_root.coordinator_engine_root_with_class()

    assert root == str(override_live_dir)
    assert cls == "live-working-tree"


def _make_bin_dir_with_sentinel(root, extra_targets=()):
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    sentinel = bin_dir / "archive-stamp-cli"
    sentinel.write_text("#!/bin/sh\necho SENTINEL\n", encoding="utf-8")
    sentinel.chmod(0o755)
    if os.name == "nt":
        sentinel.with_name(sentinel.name + ".exe").write_bytes(b"")
    for name, code in extra_targets:
        target = bin_dir / name
        target.write_text(code, encoding="utf-8")
        if os.name == "nt":
            target.with_name(target.name + ".exe").write_bytes(b"")
    return bin_dir


@pytest.fixture
def _exec_fallback_fixture(tmp_path, monkeypatch):
    """A synthetic two-tier registry resolving `RESOLUTION_RESOLVED_ENGINE`
    (published mirror usable, session confirmed NOT a working repo), with
    independently-controllable published/live `coordinator/bin/` contents —
    the axis C4b's per-target fallback gate keys off."""
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    published_root = tmp_path / "published"
    (published_root / "coordinator_core").mkdir(parents=True)
    _write_engine_stamp(published_root)
    published_bin = _make_bin_dir_with_sentinel(published_root)

    live_root = tmp_path / "live"
    live_bin = _make_bin_dir_with_sentinel(live_root)

    other_working_dir = tmp_path / "other-working-repo"
    other_working_dir.mkdir()
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    lines = [
        "[repos]",
        f'claude_klabauter = "{published_root.as_posix()}"',
        f'claude_klabauter = "{live_root.as_posix()}"',
        "",
        "[engine.working_repos]",
        f'other = "{other_working_dir.as_posix()}"',
        "",
    ]
    (ml_dir / "registry.local.toml").write_text("\n".join(lines), encoding="utf-8")

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session_dir))
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT_SKEW_QUIET", "1")
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)

    return SimpleNamespace(
        published_root=published_root,
        published_bin=published_bin,
        live_root=live_root,
        live_bin=live_bin,
    )


class _ExecSentinel(Exception):
    """Raised by the monkeypatched exec primitives below instead of letting
    `exec_cli` actually replace the pytest process — see
    `test_exec_cli_falls_back_to_live_tree_for_published_only_gap`'s
    docstring-equivalent comment for why (chosen approach 3 of the dispatch
    brief's preference order: options 1/2 were not available — C4b's
    per-target fallback decision is not separable from the exec call inside
    `exec_cli` itself, and a subprocess boundary would need a real script
    entrypoint this module doesn't have)."""


def test_exec_cli_falls_back_to_live_tree_for_published_only_gap(
    _exec_fallback_fixture, capsys, monkeypatch
):
    """Proves the C4b fallback DECISION (resolved-engine + published-only
    gap -> live tree) without ever letting `exec_cli` reach a real exec.

    Bug: this test used to call `shim.exec_cli(...)` bare and assert
    `SystemExit(0)`. Under the `resolved-engine` class, `exec_cli` takes the
    published->live fallback, finds `live-only-cli` in the live tree, and
    ACTUALLY EXECS it (`os.execv` on POSIX, `_run_target_in_process` on
    Windows) — replacing the pytest process. Under `-n` that crashes the
    xdist worker; standalone it silently truncates the run with no summary
    line and a shell-visible EXIT=0, a false green.

    Fix: monkeypatch both exec primitives to record their target argument
    and raise `_ExecSentinel` instead of exec'ing — `exec_cli` never
    completes a real process replacement, but the recorded target proves
    which root it resolved to.
    """
    fx = _exec_fallback_fixture
    (fx.live_bin / "live-only-cli").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    if os.name == "nt":
        (fx.live_bin / "live-only-cli.exe").write_bytes(b"")

    shim = _load_shim_for_test()
    recorded: dict = {}

    def _fake_execv(executable, argv):
        recorded["executable"] = executable
        recorded["target_path"] = argv[1]
        raise _ExecSentinel()

    def _fake_run_target_in_process(target_path, argv, claude_klabauter_root):
        recorded["target_path"] = target_path
        recorded["claude_klabauter_root"] = claude_klabauter_root
        raise _ExecSentinel()

    monkeypatch.setattr(shim.os, "execv", _fake_execv)
    monkeypatch.setattr(shim, "_run_target_in_process", _fake_run_target_in_process)

    with pytest.raises(_ExecSentinel):
        shim.exec_cli("live-only-cli", [])

    expected_target = str(fx.live_bin / "live-only-cli")
    assert _normalize_root(recorded["target_path"]) == _normalize_root(expected_target)


def test_exec_cli_target_absent_from_resolved_root_exits_127_naming_only_that_root(
    _exec_fallback_fixture, capsys
):
    """C13 retired exec_cli's C4b live-tree fallback: a missing target now
    fails loud on the SINGLE root `resolve_claude_klabauter_root_with_class()`
    actually resolved — there is no second root to silently reach into, and
    none to name in the error either. Renamed and re-scoped 2026-08-19 (this
    plan's own C5/C13 fallout) from `..._exits_127_naming_both`, which
    encoded the pre-C13 two-root world C13 deliberately retired; see
    `_resolve_claude_klabauter.py::exec_cli`'s docstring, "C4b (RETIRED by C13)"."""
    fx = _exec_fallback_fixture
    shim = _load_shim_for_test()

    root, cls = shim.resolve_claude_klabauter_root_with_class()
    resolved_root, other_root = (
        (fx.published_root, fx.live_root)
        if cls == shim.RESOLUTION_RESOLVED_ENGINE
        else (fx.live_root, fx.published_root)
    )

    with pytest.raises(SystemExit) as excinfo:
        shim.exec_cli("nowhere-cli", [])

    assert excinfo.value.code == 127
    captured = capsys.readouterr()
    assert str(resolved_root) in captured.err or resolved_root.as_posix() in captured.err
    assert str(other_root) not in captured.err
    assert other_root.as_posix() not in captured.err


def test_exec_cli_live_working_tree_class_unchanged_no_fallback(tmp_path, monkeypatch, capsys):
    """A `live-working-tree` resolution takes no fallback path: a target
    absent from the (sole) resolved root exits 127 exactly as before this
    chunk, naming only that one root — byte-identical behaviour."""
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    live_root = tmp_path / "only-live"
    _make_bin_dir_with_sentinel(live_root)

    (ml_dir / ".claude-klabauter-root").write_text(str(live_root), encoding="utf-8")

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    shim = _load_shim_for_test()
    root, cls = shim.resolve_claude_klabauter_root_with_class()
    assert cls == shim.RESOLUTION_LIVE_WORKING_TREE

    with pytest.raises(SystemExit) as excinfo:
        shim.exec_cli("nowhere-cli", [])

    assert excinfo.value.code == 127
    captured = capsys.readouterr()
    assert "under both" not in captured.err
    assert "coordinator helper" in captured.err


# --- C2 (this plan, pln-the-ceremony-tail-stops-lying-b58fb3): Rung 2's
# `TimeoutExpired` arm stops reporting the same disposition as an exec
# failure (AC4) and stops advising `machine-local set` for a read that
# never got far enough to see the key (AC1's claude_klabauter_root half).


@pytest.fixture
def _rung2_fixture(tmp_path, monkeypatch):
    """No env override, no `.claude-klabauter-root` pointer, `machine-local` present
    on PATH — forces resolution all the way to Rung 2's subprocess call."""
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.setattr(claude_klabauter_root.shutil, "which", lambda name: "machine-local")

    return SimpleNamespace(settings_home=settings_home)


def test_rung2_timeout_reports_distinguishably_from_exec_failure(_rung2_fixture, monkeypatch):
    """AC4: a `TimeoutExpired` arm must raise a DIFFERENT message than the
    `OSError` exec-failure arm's fallthrough-to-Rung-3 `_REMEDIATION`."""

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["machine-local"], timeout=claude_klabauter_root._RUNG2_TIMEOUT_SECS)

    monkeypatch.setattr(claude_klabauter_root.subprocess, "run", _raise_timeout)

    with pytest.raises(RuntimeError) as excinfo:
        claude_klabauter_root.coordinator_engine_root()

    assert str(excinfo.value) != claude_klabauter_root._REMEDIATION


def test_rung2_timeout_names_reader_timeout_not_machine_local_set(_rung2_fixture, monkeypatch):
    """AC1 (claude_klabauter_root half): the timeout arm's operator-facing text names
    a reader timeout and does not advise `machine-local set`."""

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["machine-local"], timeout=claude_klabauter_root._RUNG2_TIMEOUT_SECS)

    monkeypatch.setattr(claude_klabauter_root.subprocess, "run", _raise_timeout)

    with pytest.raises(RuntimeError) as excinfo:
        claude_klabauter_root.coordinator_engine_root()

    message = str(excinfo.value)
    assert "machine-local set" not in message


def test_rung2_timeout_message_carries_shared_token(_rung2_fixture, monkeypatch):
    """AC3b: the timeout text carries the literal shared discriminator
    token, and the constant's value itself is pinned so drift fails loudly."""
    assert claude_klabauter_root._REGISTRY_READ_TIMEOUT_TOKEN == "machine-local registry read timed out"

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["machine-local"], timeout=claude_klabauter_root._RUNG2_TIMEOUT_SECS)

    monkeypatch.setattr(claude_klabauter_root.subprocess, "run", _raise_timeout)

    with pytest.raises(RuntimeError) as excinfo:
        claude_klabauter_root.coordinator_engine_root()

    assert claude_klabauter_root._REGISTRY_READ_TIMEOUT_TOKEN in str(excinfo.value)


def test_rung2_exec_failure_still_falls_through_to_absent_key_remediation(
    _rung2_fixture, monkeypatch
):
    """The OSError exec-failure arm (machine-local vanished mid-race) keeps
    its existing disposition — falls through to Rung 3's `_REMEDIATION`,
    unlike the timeout arm above. Distinguishes the two failure modes."""

    def _raise_oserror(*args, **kwargs):
        raise OSError("exec failed")

    monkeypatch.setattr(claude_klabauter_root.subprocess, "run", _raise_oserror)

    with pytest.raises(RuntimeError) as excinfo:
        claude_klabauter_root.coordinator_engine_root()

    assert str(excinfo.value) == claude_klabauter_root._REMEDIATION


def test_rung2_absent_key_remediation_text_byte_identical(_rung2_fixture, monkeypatch):
    """AC2b: with the registry key genuinely absent (machine-local exits
    nonzero / empty stdout, not a timeout), `_REMEDIATION`'s existing
    `machine-local set repos.claude_klabauter` text is unchanged, byte for
    byte — this chunk edits what the TIMEOUT arm reports, not the
    already-shipped absent-key remediation."""
    assert claude_klabauter_root._REMEDIATION == (
        "coordinator_engine_root: cannot resolve CLAUDE_KLABAUTER_ROOT — repos.claude_klabauter is not set.\n"
        "  The machine-local registry has no 'repos.claude_klabauter' entry on this machine.\n"
        "  Remediate (choose one):\n"
        "    machine-local set repos.claude_klabauter /path/to/claude-klabauter\n"
        "    Re-run /coordinator:install to populate the repos.* registry entries.\n"
        "  Reference: plugins/coordinator-claude/coordinator/docs/wiki/machine-local-registry.md §4c"
    )

    def _fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(claude_klabauter_root.subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        claude_klabauter_root.coordinator_engine_root()

    assert str(excinfo.value) == claude_klabauter_root._REMEDIATION


def test_exec_cli_no_coordinator_core_import_introduced():
    """The shim's own module namespace, after loading and exercising
    `exec_cli`'s new resolution path, never gained a `coordinator_core`
    import — the shim stays standalone-importable (C4b hard constraint).
    The source-level guard (no `import coordinator_core` / `from
    coordinator_core` STATEMENT anywhere in the file, docstring prose
    referencing the module aside) is `test_standalone_shim_imports_no_coordinator_core`
    in `test_resolve_claude_klabauter.py` — not duplicated here."""
    shim = _load_shim_for_test()
    assert "coordinator_core" not in sys.modules or all(
        getattr(shim, name, None) is not sys.modules.get("coordinator_core")
        for name in dir(shim)
    )
