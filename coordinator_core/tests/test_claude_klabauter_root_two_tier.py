"""
coordinator_core/tests/test_claude_klabauter_root_two_tier.py

Chunk C4a (wrapper half): verifies
`coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root_with_class()` — which
loads C3's shim (`coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`) BY
PATH and wraps its `resolve_claude_klabauter_root_with_class()` rather than
reimplementing the published-engine-vs-live-working-tree gate — agrees with
the shim itself, and that install's derived forwarder name set resolves
under both resolution classes.

Spec backlink: docs/plans/2026-08-07-two-tier-engine-root-adopt-dr132.md § C4 (wrapper half)

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
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from coordinator_core import claude_klabauter_root
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
    `coordinator_claude_klabauter_root_with_class()` agree — same resolution class,
    same effective root — over the machine's live registry state. This is
    what makes the C3<->C4 agreement a CHECKED invariant, not a documented
    drift seam."""
    shim = _load_shim_for_test()
    expected_root, expected_class = shim.resolve_claude_klabauter_root_with_class()
    actual_root, actual_class = claude_klabauter_root.coordinator_claude_klabauter_root_with_class()

    assert actual_class == expected_class
    assert _normalize_root(actual_root) == _normalize_root(expected_root)


# Review: code-reviewer (finding 1) — the `real_home` cross-entrypoint
# agreement test above depends on the box's own live registry contents and
# cannot guarantee it exercises the wrapper's cheap short-circuit (the
# `repos.claude_klabauter`-absent branch in `coordinator_claude_klabauter_root_with_class`
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
    cheap short-circuit (step 2, see `coordinator_claude_klabauter_root_with_class`'s
    own docstring) fires, calling `shim._ml_dir()`/`shim._resolve_claude_klabauter_root()`
    directly instead of `shim.resolve_claude_klabauter_root_with_class()`. Both must
    still agree with the shim's own full-ladder answer — this is the
    mechanical backstop the module docstrings (both files) point to."""
    shim = _load_shim_for_test()
    expected_root, expected_class = shim.resolve_claude_klabauter_root_with_class()
    actual_root, actual_class = claude_klabauter_root.coordinator_claude_klabauter_root_with_class()

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
# Spec backlink: docs/plans/2026-08-07-two-tier-engine-root-adopt-dr132.md § C5
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

    root, cls = claude_klabauter_root.coordinator_claude_klabauter_root_with_class()

    assert cls == "resolved-engine"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_wrapper_skew_advisory_fires_on_dual_registration(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)

    root, cls = claude_klabauter_root.coordinator_claude_klabauter_root_with_class()

    assert cls == "resolved-engine"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "note:" in captured.err


def test_wrapper_skew_advisory_silent_single_tier(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=False)

    root, cls = claude_klabauter_root.coordinator_claude_klabauter_root_with_class()

    assert cls == "resolved-engine"
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_wrapper_skew_advisory_stderr_only(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)

    claude_klabauter_root.coordinator_claude_klabauter_root_with_class()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""


def test_wrapper_skew_advisory_once_per_process(_skew_fixture, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)

    claude_klabauter_root.coordinator_claude_klabauter_root_with_class()
    claude_klabauter_root.coordinator_claude_klabauter_root_with_class()
    claude_klabauter_root._reset_gate_memo()
    claude_klabauter_root.coordinator_claude_klabauter_root_with_class()

    captured = capsys.readouterr()
    assert captured.err.count("note:") == 1


def test_wrapper_skew_advisory_kill_switch(_skew_fixture, monkeypatch, capsys):
    _skew_fixture.write_registry(claude_klabauter=True)
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT_SKEW_QUIET", "1")

    root, cls = claude_klabauter_root.coordinator_claude_klabauter_root_with_class()

    assert cls == "resolved-engine"
    captured = capsys.readouterr()
    assert captured.err == ""


# --- C4b: per-target existence gate on the published-engine rung -----------
#
# Spec backlink: docs/plans/2026-08-07-two-tier-engine-root-adopt-dr132.md
# (chunk C4, the `exec_cli` half) + tasks/two-tier-engine-root-exec/briefs/C4b.md
#
# `exec_cli` only ever exercises Windows' `_run_target_in_process` leg in
# this suite (`os.name == "nt"` on this box) — its `sys.exit(code)` is
# caught here via `pytest.raises(SystemExit)` rather than actually
# replacing the interpreter, which is what the POSIX `os.execv` leg would
# do if exercised directly (out of scope: that leg is untouched by this
# chunk, see the shim's own negative-spec docstring).


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


def test_exec_cli_falls_back_to_live_tree_for_published_only_gap(_exec_fallback_fixture, capsys):
    fx = _exec_fallback_fixture
    (fx.live_bin / "live-only-cli").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    if os.name == "nt":
        (fx.live_bin / "live-only-cli.exe").write_bytes(b"")

    shim = _load_shim_for_test()
    with pytest.raises(SystemExit) as excinfo:
        shim.exec_cli("live-only-cli", [])

    assert excinfo.value.code == 0


def test_exec_cli_target_absent_from_both_roots_exits_127_naming_both(_exec_fallback_fixture, capsys):
    fx = _exec_fallback_fixture
    shim = _load_shim_for_test()

    with pytest.raises(SystemExit) as excinfo:
        shim.exec_cli("nowhere-cli", [])

    assert excinfo.value.code == 127
    captured = capsys.readouterr()
    assert str(fx.published_bin) in captured.err or fx.published_bin.as_posix() in captured.err
    assert str(fx.live_bin) in captured.err or fx.live_bin.as_posix() in captured.err


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
