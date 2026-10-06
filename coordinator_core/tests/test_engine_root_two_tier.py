from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core import engine_root as claude_klabauter_root

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
def _reset_wrapper_memos(monkeypatch):
    # A live-tree override in the invoking shell outranks every rung under test.
    monkeypatch.delenv("REPO_CLAUDE_KLABAUTER", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
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

    The wrapper's free Rung 1 (`CLAUDE_KLABAUTER_ROOT` env var) and the shim's own
    registry-key rung can legitimately return the SAME path in different
    string forms (observed on this box: one form uses forward slashes, the
    registry's flat-quoted-key form holds a backslash path) — that is a
    pre-existing property of the two independent sources, not a defect this
    chunk owns or may fix (the shim is out-of-scope; see module docstring). The
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
    shim = _load_shim_for_test()
    expected_root, expected_class = shim.resolve_claude_klabauter_root_with_class()
    actual_root, actual_class = claude_klabauter_root.coordinator_engine_root_with_class()

    assert expected_class == shim.RESOLUTION_LIVE_WORKING_TREE
    assert actual_class == expected_class
    assert _normalize_root(actual_root) == _normalize_root(expected_root)
    assert _normalize_root(actual_root) == _normalize_root(
        str(_short_circuit_fixture.live_dir)
    )


@pytest.fixture
def _dual_boot_fixture(tmp_path, monkeypatch):
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    live_dir = tmp_path / "live"
    live_dir.mkdir()
    (ml_dir / ".claude-klabauter-live-root").write_text(str(live_dir), encoding="utf-8")

    published_dir = tmp_path / "published-klabauter"
    (published_dir / "coordinator_core").mkdir(parents=True)
    _write_engine_stamp(published_dir)

    session_dir = tmp_path / "session"
    session_dir.mkdir()

    def _write_registry(*, session_is_engine_source_tree: bool) -> None:
        lines = [
            "[repos]",
            f'claude_klabauter = "{published_dir.as_posix()}"',
        ]
        if session_is_engine_source_tree:
            lines.append(f'claude_klabauter = "{session_dir.as_posix()}"')
        else:
            # A CONFIRMED not-the-source-tree session (literally False, not
            # the undeterminable None): the claude-klabauter root resolves to a real
            # tree that is NOT this session's.
            lines.append(f'claude_klabauter = "{live_dir.as_posix()}"')
        lines.append("")
        lines.append("[engine]")
        lines.append('target = "candidate"')
        lines.append("")
        lines.append("[engine.working_repos]")
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
    fx.write_registry(session_is_engine_source_tree=False)

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


def test_dual_boot_absent_klabauter_registry_resolved(tmp_path, monkeypatch):
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    live_dir = tmp_path / "live"
    live_dir.mkdir()
    (ml_dir / "registry.local.toml").write_text(
        f"\"repos.claude_klabauter\" = '{live_dir}'\n"
    )

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    root, cls = claude_klabauter_root.coordinator_engine_root_with_class()

    assert root == str(live_dir)
    assert cls == "live-working-tree"


def test_dual_boot_absent_klabauter_honors_machine_local_registry_dir_override(
    tmp_path, monkeypatch
):
    """The short-circuit branch must reuse the already-computed,
    override-aware `ml_dir` (`shim._ml_dir()`) rather than re-resolving
    `machine_local_dir()` directly — the two diverge whenever
    `MACHINE_LOCAL_REGISTRY_DIR` is set, since only `shim._ml_dir()` honors
    it. Proves the override genuinely reaches this branch: the settings-home
    machine-local dir is left EMPTY (no registry file there at all) while the
    override dir holds the registry entry — a resolution that only succeeds
    if the override is actually consulted, not merely a value-equality
    assertion that could pass by coincidence.

    """
    settings_home = tmp_path / "settings-home"
    settings_home_ml_dir = settings_home / "machine-local"
    settings_home_ml_dir.mkdir(parents=True)

    override_ml_dir = tmp_path / "override-machine-local"
    override_ml_dir.mkdir()
    override_live_dir = tmp_path / "override-live"
    override_live_dir.mkdir()
    (override_ml_dir / "registry.local.toml").write_text(
        f"\"repos.claude_klabauter\" = '{override_live_dir}'\n"
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


def test_exec_cli_live_working_tree_class_unchanged_no_fallback(tmp_path, monkeypatch, capsys):
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    live_root = tmp_path / "only-live"
    _make_bin_dir_with_sentinel(live_root)

    (ml_dir / "registry.local.toml").write_text(
        f"\"repos.claude_klabauter\" = '{live_root}'\n"
    )

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


@pytest.fixture
def _rung2_fixture(tmp_path, monkeypatch):
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.setattr(claude_klabauter_root.shutil, "which", lambda name: "machine-local")
    # This checkout is itself an engine root; the self-located last rung would
    # otherwise answer before the absent-key remediation these tests pin.
    monkeypatch.setattr(claude_klabauter_root, "_self_located_root", lambda: None)

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

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["machine-local"], timeout=claude_klabauter_root._RUNG2_TIMEOUT_SECS)

    monkeypatch.setattr(claude_klabauter_root.subprocess, "run", _raise_timeout)

    with pytest.raises(RuntimeError) as excinfo:
        claude_klabauter_root.coordinator_engine_root()

    message = str(excinfo.value)
    assert "machine-local set" not in message


def test_rung2_timeout_message_carries_shared_token(_rung2_fixture, monkeypatch):
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
        "  Reference: plugins/coordinator/docs/wiki/machine-local-registry.md §4c"
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
