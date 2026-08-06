"""Tests for coordinator_core.install.ensure_venv -- Port B native venv-ensure.

Covers AC B1-B9: health-check parity (both-imports), cross-platform VENV_PY,
ready/rebuilt/would-rebuild/would-write status parity, build-lock contention
contract (POSIX flock AND Windows msvcrt branches -- coverage M1), pin
idempotence + doubled-pin self-heal, CLAUDE_HOME /.claude-suffix fail-loud,
pip network-vs-generic failure classification, WHOAMI_PKG seam, and the
trusted-root-guard fail-loud precondition.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.install import ensure_venv as ev
from coordinator_core.install.write_surface import ShapedClause, StaticClause, validate
from coordinator_core.trusted_root_guard import UntrustedRootError


def _make_exe(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _trusted_plugin_root(tmp_path: Path, monkeypatch) -> Path:
    """A plugin_root under $CLAUDE_HOME/.claude/... passes the trust guard."""
    claude_home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    plugin_root = claude_home / ".claude" / "plugin"
    plugin_root.mkdir(parents=True)
    return plugin_root


# ---------------------------------------------------------------------------
# venv_python_path -- cross-platform VENV_PY (AC B2)
# ---------------------------------------------------------------------------


def test_venv_python_path_posix(monkeypatch):
    monkeypatch.delenv("OSTYPE", raising=False)
    monkeypatch.delenv("OS", raising=False)
    venv_dir = Path("/tmp/x/.coordinator-venv")
    assert ev.venv_python_path(venv_dir) == venv_dir / "bin" / "python"


def test_venv_python_path_windows_via_ostype(monkeypatch):
    monkeypatch.setenv("OSTYPE", "msys")
    venv_dir = Path("/tmp/x/.coordinator-venv")
    assert ev.venv_python_path(venv_dir) == venv_dir / "Scripts" / "python.exe"


def test_venv_python_path_windows_via_os(monkeypatch):
    monkeypatch.delenv("OSTYPE", raising=False)
    monkeypatch.setenv("OS", "Windows_NT")
    venv_dir = Path("/tmp/x/.coordinator-venv")
    assert ev.venv_python_path(venv_dir) == venv_dir / "Scripts" / "python.exe"


# ---------------------------------------------------------------------------
# _venv_healthy -- both-imports parity (AC B2)
# ---------------------------------------------------------------------------


def test_venv_healthy_requires_executable(tmp_path):
    venv_py = tmp_path / "bin" / "python"
    assert ev._venv_healthy(venv_py) is False


def test_venv_healthy_true_when_both_imports_succeed(tmp_path, monkeypatch):
    venv_py = tmp_path / "bin" / "python"
    _make_exe(venv_py)

    def fake_run(argv, **kwargs):
        assert argv[0] == str(venv_py)
        assert "import coordinator_whoami; import pydantic" in argv[2]
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(ev.subprocess, "run", fake_run)
    assert ev._venv_healthy(venv_py) is True


def test_venv_healthy_false_when_import_fails(tmp_path, monkeypatch):
    venv_py = tmp_path / "bin" / "python"
    _make_exe(venv_py)
    monkeypatch.setattr(
        ev.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1)
    )
    assert ev._venv_healthy(venv_py) is False


def test_venv_healthy_false_on_timeout(tmp_path, monkeypatch):
    venv_py = tmp_path / "bin" / "python"
    _make_exe(venv_py)

    def raise_timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

    monkeypatch.setattr(ev.subprocess, "run", raise_timeout)
    assert ev._venv_healthy(venv_py) is False


def test_venv_healthy_probes_psutil_alongside_whoami_and_pydantic(tmp_path, monkeypatch):
    """AC B2 widened probe: the health-check import list must include psutil,
    not just coordinator_whoami/pydantic -- regression guard for the oracle/
    dep-set lockstep (_VENV_IMPORT_PROBES/_VENV_PIP_DEPS)."""
    venv_py = tmp_path / "bin" / "python"
    _make_exe(venv_py)

    def fake_run(argv, **kwargs):
        assert "import psutil" in argv[2]
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(ev.subprocess, "run", fake_run)
    assert ev._venv_healthy(venv_py) is True


def test_venv_healthy_false_when_whoami_and_pydantic_ok_but_psutil_missing(tmp_path, monkeypatch):
    """The critical regression guard: a venv with coordinator_whoami and
    pydantic importable but NOT psutil must be judged UNHEALTHY -- otherwise
    every pre-existing psutil-less venv passes the fast path forever and the
    added pip dep (change 1) is a no-op."""
    venv_py = tmp_path / "bin" / "python"
    _make_exe(venv_py)

    def fake_run(argv, **kwargs):
        probe = argv[2]
        if "psutil" in probe:
            return subprocess.CompletedProcess(argv, 1, stderr="ModuleNotFoundError: psutil")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(ev.subprocess, "run", fake_run)
    assert ev._venv_healthy(venv_py) is False


# ---------------------------------------------------------------------------
# _resolve_ml_cli -- delegates to _shared.resolve_machine_local_cli
#
# Contract: returns an ARGV LIST, not a Path. The Path form handed subprocess an
# extension-less shebang script, which is unexecutable on Windows (WinError 193)
# and broke the documented install. Resolution order is now _shared's:
#   1. templates/bin/_machine_local.py via sys.executable  (no shebang exec)
#   2. bin/machine-local shim
#   3. machine-local on PATH
# ---------------------------------------------------------------------------


def test_resolve_ml_cli_prefers_python_impl(tmp_path, monkeypatch):
    """Convention B wins over the shim — this is the Windows-safe path."""
    plugin_root = tmp_path / "plugin"
    py_impl = plugin_root / "templates" / "bin" / "_machine_local.py"
    py_impl.parent.mkdir(parents=True)
    py_impl.write_text("# impl")
    _make_exe(plugin_root / "bin" / "machine-local")
    monkeypatch.setattr(ev.shutil, "which", lambda name: "/should/not/be/used")
    assert ev._resolve_ml_cli(plugin_root) == [sys.executable, str(py_impl)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX: the bare shim is directly executable")
def test_resolve_ml_cli_bin_relative_first(tmp_path, monkeypatch):
    plugin_root = tmp_path / "plugin"
    ml = plugin_root / "bin" / "machine-local"
    _make_exe(ml)
    monkeypatch.setattr(ev.shutil, "which", lambda name: "/should/not/be/used")
    assert ev._resolve_ml_cli(plugin_root) == [str(ml)]


@pytest.mark.skipif(os.name != "nt", reason="Windows shim-rung shape")
def test_resolve_ml_cli_windows_prefers_cmd_never_bare_shim(tmp_path, monkeypatch):
    """On Windows the bare extension-less shim must never be returned.

    CreateProcess cannot exec it (WinError 193) — the same defect the py_impl
    rung avoids, which used to survive on this fallback path.
    """
    plugin_root = tmp_path / "plugin"
    ml = plugin_root / "bin" / "machine-local"
    _make_exe(ml)
    monkeypatch.setattr(ev.shutil, "which", lambda name: None)

    # Bare shim only -> must NOT be selected; falls through (here: to None).
    assert ev._resolve_ml_cli(plugin_root) != [str(ml)]

    # .cmd sibling present -> selected.
    ml_cmd = ml.with_suffix(".cmd")
    ml_cmd.write_text("@echo off\n")
    assert ev._resolve_ml_cli(plugin_root) == [str(ml_cmd)]


def test_resolve_ml_cli_falls_back_to_path(tmp_path, monkeypatch):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    monkeypatch.setattr(ev.shutil, "which", lambda name: "/usr/local/bin/machine-local")
    assert ev._resolve_ml_cli(plugin_root) == ["/usr/local/bin/machine-local"]


def test_resolve_ml_cli_none_when_absent(tmp_path, monkeypatch):
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    monkeypatch.setattr(ev.shutil, "which", lambda name: None)
    assert ev._resolve_ml_cli(plugin_root) is None


# ---------------------------------------------------------------------------
# _resolve_whoami_pkg -- WHOAMI_PKG seam (AC B8)
# ---------------------------------------------------------------------------


def test_whoami_pkg_falls_back_to_plugin_root_when_no_ml_cli(tmp_path):
    plugin_root = tmp_path / "plugin"
    assert ev._resolve_whoami_pkg(plugin_root, None) == plugin_root / "whoami"


def test_whoami_pkg_uses_registry_seam_when_valid_dir(tmp_path, monkeypatch):
    plugin_root = tmp_path / "plugin"
    seam_dir = tmp_path / "seam-whoami"
    seam_dir.mkdir()
    ml_cli = tmp_path / "machine-local"

    monkeypatch.setattr(ev, "_ml_get", lambda cli, key: str(seam_dir))
    assert ev._resolve_whoami_pkg(plugin_root, ml_cli) == seam_dir


def test_whoami_pkg_warns_and_falls_back_on_stale_seam(tmp_path, monkeypatch, capsys):
    plugin_root = tmp_path / "plugin"
    ml_cli = tmp_path / "machine-local"
    monkeypatch.setattr(ev, "_ml_get", lambda cli, key: str(tmp_path / "does-not-exist"))

    result = ev._resolve_whoami_pkg(plugin_root, ml_cli)
    assert result == plugin_root / "whoami"
    assert "is not a directory" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _set_pin -- idempotence + doubled-pin self-heal (AC B5)
# ---------------------------------------------------------------------------


def test_set_pin_warns_when_ml_cli_absent(capsys, tmp_path):
    ev._set_pin(None, tmp_path / "venv" / "bin" / "python")
    err = capsys.readouterr().err
    assert "machine-local CLI not found" in err


def test_set_pin_skips_write_when_already_correct(tmp_path, monkeypatch):
    ml_cli = tmp_path / "machine-local"
    venv_py = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(ev, "_ml_get", lambda cli, key: str(venv_py))
    calls = []
    monkeypatch.setattr(ev, "_ml_set", lambda cli, key, value: calls.append(value))
    ev._set_pin(ml_cli, venv_py)
    assert calls == []


def test_set_pin_writes_when_stale(tmp_path, monkeypatch):
    ml_cli = tmp_path / "machine-local"
    venv_py = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(ev, "_ml_get", lambda cli, key: "/old/path")
    calls = []
    monkeypatch.setattr(ev, "_ml_set", lambda cli, key, value: calls.append((key, value)))
    ev._set_pin(ml_cli, venv_py)
    assert calls == [("coordinator.python", str(venv_py))]


def test_set_pin_self_heals_doubled_claude_pin(tmp_path, monkeypatch, capsys):
    ml_cli = tmp_path / "machine-local"
    venv_py = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(
        ev, "_ml_get", lambda cli, key: "/x/.claude/.claude/.coordinator-venv/bin/python"
    )
    monkeypatch.setattr(ev, "_ml_set", lambda cli, key, value: None)
    ev._set_pin(ml_cli, venv_py)
    assert "self-healing doubled venv pin" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _clear_dangling_pin -- non-dangling-pin guarantee
# ---------------------------------------------------------------------------


def test_clear_dangling_pin_noop_when_ml_cli_absent(tmp_path):
    ev._clear_dangling_pin(None, tmp_path / "venv" / "bin" / "python")  # must not raise


def test_clear_dangling_pin_clears_when_pin_matches(tmp_path, monkeypatch, capsys):
    ml_cli = tmp_path / "machine-local"
    venv_py = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(ev, "_ml_get", lambda cli, key: str(venv_py))
    calls = []
    monkeypatch.setattr(ev, "_ml_set", lambda cli, key, value: calls.append((key, value)))
    ev._clear_dangling_pin(ml_cli, venv_py)
    assert calls == [("coordinator.python", "")]
    assert "clearing dangling coordinator.python pin" in capsys.readouterr().err


def test_clear_dangling_pin_leaves_unrelated_pin_alone(tmp_path, monkeypatch):
    ml_cli = tmp_path / "machine-local"
    venv_py = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(ev, "_ml_get", lambda cli, key: "/unrelated/python")
    calls = []
    monkeypatch.setattr(ev, "_ml_set", lambda cli, key, value: calls.append((key, value)))
    ev._clear_dangling_pin(ml_cli, venv_py)
    assert calls == []


# ---------------------------------------------------------------------------
# _install_deps -- network-vs-generic classification (AC B6)
# ---------------------------------------------------------------------------


def test_install_deps_success_no_raise(tmp_path, monkeypatch):
    venv_py = tmp_path / "venv" / "bin" / "python"
    whoami_pkg = tmp_path / "whoami"

    def fake_run(argv, **kwargs):
        assert argv[:4] == [str(venv_py), "-m", "pip", "install"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(ev.subprocess, "run", fake_run)
    ev._install_deps(venv_py, whoami_pkg)  # must not raise


def test_install_deps_pip_argv_includes_psutil(tmp_path, monkeypatch):
    venv_py = tmp_path / "venv" / "bin" / "python"
    whoami_pkg = tmp_path / "whoami"
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(ev.subprocess, "run", fake_run)
    ev._install_deps(venv_py, whoami_pkg)
    assert "psutil>=5.9" in captured[0]


def test_install_deps_network_failure_classified(tmp_path, monkeypatch, capsys):
    venv_py = tmp_path / "venv" / "bin" / "python"
    whoami_pkg = tmp_path / "whoami"
    monkeypatch.setattr(
        ev.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Temporary failure in name resolution"
        ),
    )
    with pytest.raises(ev.EnsureVenvError):
        ev._install_deps(venv_py, whoami_pkg)
    assert "could not reach PyPI" in capsys.readouterr().err


def test_install_deps_generic_failure_classified(tmp_path, monkeypatch, capsys):
    venv_py = tmp_path / "venv" / "bin" / "python"
    whoami_pkg = tmp_path / "whoami"
    monkeypatch.setattr(
        ev.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="ERROR: no matching distribution"
        ),
    )
    with pytest.raises(ev.EnsureVenvError):
        ev._install_deps(venv_py, whoami_pkg)
    assert "pip install failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# ensure_coordinator_venv -- orchestration + status parity (AC B3)
# ---------------------------------------------------------------------------


def test_trusted_root_guard_fail_loud(tmp_path, monkeypatch):
    """AC B9: an untrusted plugin_root aborts before any mutation."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", raising=False)
    untrusted_root = tmp_path / "somewhere" / "else"
    untrusted_root.mkdir(parents=True)
    settings_home_path = tmp_path / "settings-home"

    with pytest.raises(UntrustedRootError):
        ev.ensure_coordinator_venv(untrusted_root, settings_home_path)
    assert not settings_home_path.exists()


def test_claude_home_doubled_suffix_fails_loud(tmp_path, monkeypatch):
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"
    with pytest.raises(ev.EnsureVenvError, match="ends in '/.claude'"):
        ev.ensure_coordinator_venv(
            plugin_root,
            settings_home_path,
            claude_home=str(tmp_path / "foo" / ".claude"),
        )


def test_check_mode_ready_when_healthy_and_pinned(tmp_path, monkeypatch):
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"
    venv_dir = settings_home_path / ".coordinator-venv"
    venv_py = ev.venv_python_path(venv_dir)

    monkeypatch.setattr(ev, "_venv_healthy", lambda py: True)
    monkeypatch.setattr(ev, "_resolve_ml_cli", lambda root: None)

    status = ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=True)
    assert status == "ready"
    assert not settings_home_path.exists()  # check mode mutates nothing


def test_check_mode_would_write_when_healthy_but_unpinned(tmp_path, monkeypatch):
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"
    ml_cli = tmp_path / "machine-local"

    monkeypatch.setattr(ev, "_venv_healthy", lambda py: True)
    monkeypatch.setattr(ev, "_resolve_ml_cli", lambda root: ml_cli)
    monkeypatch.setattr(ev, "_ml_get", lambda cli, key: "/stale/path")

    status = ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=True)
    assert status == "would-write"


def test_check_mode_would_rebuild_when_unhealthy(tmp_path, monkeypatch):
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"

    monkeypatch.setattr(ev, "_venv_healthy", lambda py: False)

    status = ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=True)
    assert status == "would-rebuild"
    assert not settings_home_path.exists()


def test_mutate_mode_fast_path_ready(tmp_path, monkeypatch):
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"

    monkeypatch.setattr(ev, "_venv_healthy", lambda py: True)
    pins = []
    monkeypatch.setattr(ev, "_set_pin", lambda cli, py: pins.append(py))

    status = ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=False)
    assert status == "ready"
    assert pins  # pin attempted on fast path too
    assert not settings_home_path.exists()  # nothing built


def test_mutate_mode_rebuilds_when_unhealthy(tmp_path, monkeypatch):
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"
    venv_dir = settings_home_path / ".coordinator-venv"

    healthy_calls = {"n": 0}

    def fake_healthy(py):
        healthy_calls["n"] += 1
        # First two calls (fast-path check, post-lock re-check) unhealthy;
        # the build-then-check path never re-probes health after install
        # (mirrors bash — pip success is trusted), so both calls return False.
        return False

    monkeypatch.setattr(ev, "_venv_healthy", fake_healthy)
    monkeypatch.setattr(ev, "_resolve_base_python", lambda: "/usr/bin/python3")

    created = []
    monkeypatch.setattr(ev, "_create_venv", lambda base_py, dst: created.append(dst) or dst.mkdir(parents=True))

    installed = []
    monkeypatch.setattr(ev, "_install_deps", lambda py, pkg: installed.append((py, pkg)))

    pins = []
    monkeypatch.setattr(ev, "_set_pin", lambda cli, py: pins.append(py))

    status = ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=False)
    assert status == "rebuilt"
    assert created == [venv_dir]
    assert installed
    assert pins


def test_mutate_mode_removes_partial_venv_on_install_failure(tmp_path, monkeypatch):
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"
    venv_dir = settings_home_path / ".coordinator-venv"

    monkeypatch.setattr(ev, "_venv_healthy", lambda py: False)
    monkeypatch.setattr(ev, "_resolve_base_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(ev, "_create_venv", lambda base_py, dst: dst.mkdir(parents=True))

    def fail_install(py, pkg):
        raise ev.EnsureVenvError("boom")

    monkeypatch.setattr(ev, "_install_deps", fail_install)

    with pytest.raises(ev.EnsureVenvError):
        ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=False)
    assert not venv_dir.exists()


def test_mutate_mode_failed_install_clears_dangling_pin_when_opted_in(tmp_path, monkeypatch):
    """A pre-existing coordinator.python pin pointing at the (now-destroyed)
    venv is cleared, not left dangling, when the caller opts in via
    clear_pin_on_failure=True -- resolve_python_bin treats a found-but-broken
    pin as a hard failure and never falls through to OS-detect, so a stale
    pin here would turn one failed rebuild into every subsequent coordinator
    invocation hard-failing. substrate.py's C10a-3 call site opts into this
    (fatal-with-fallback disposition); see
    test_mutate_mode_failed_install_leaves_pin_untouched_by_default for the
    (now-default) opposite case an advisory caller gets without opting in."""
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"
    venv_dir = settings_home_path / ".coordinator-venv"
    venv_py = ev.venv_python_path(venv_dir)

    ml_cli = tmp_path / "machine-local"
    monkeypatch.setattr(ev, "_resolve_ml_cli", lambda root: ml_cli)
    monkeypatch.setattr(ev, "_venv_healthy", lambda py: False)
    monkeypatch.setattr(ev, "_resolve_base_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(ev, "_create_venv", lambda base_py, dst: dst.mkdir(parents=True))

    def fail_install(py, pkg):
        raise ev.EnsureVenvError("boom")

    monkeypatch.setattr(ev, "_install_deps", fail_install)

    # Registry currently pins the venv we're about to destroy.
    registry = {"coordinator.python": str(venv_py)}
    monkeypatch.setattr(ev, "_ml_get", lambda cli, key: registry.get(key, ""))

    def fake_set(cli, key, value):
        registry[key] = value

    monkeypatch.setattr(ev, "_ml_set", fake_set)

    with pytest.raises(ev.EnsureVenvError):
        ev.ensure_coordinator_venv(
            plugin_root, settings_home_path, check_only=False, clear_pin_on_failure=True,
        )

    assert not venv_dir.exists()
    assert registry["coordinator.python"] == ""


def test_mutate_mode_failed_install_leaves_pin_untouched_by_default(tmp_path, monkeypatch):
    """DEFAULT behavior (no clear_pin_on_failure passed): a failed rebuild
    must NOT blank an existing coordinator.python pin, even one that names
    the just-destroyed venv. Regression guard for the 2026-07-28
    install-dogfood friction log's F7 "second-order damage" -- an ADVISORY
    phase (maximalist.py Step 6) degrading persisted registry state on
    failure. Disposition-laden mutation belongs to an opted-in caller (see
    the clear_pin_on_failure=True sibling test), never the shared mechanic's
    default."""
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"
    venv_dir = settings_home_path / ".coordinator-venv"
    venv_py = ev.venv_python_path(venv_dir)

    ml_cli = tmp_path / "machine-local"
    monkeypatch.setattr(ev, "_resolve_ml_cli", lambda root: ml_cli)
    monkeypatch.setattr(ev, "_venv_healthy", lambda py: False)
    monkeypatch.setattr(ev, "_resolve_base_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(ev, "_create_venv", lambda base_py, dst: dst.mkdir(parents=True))

    def fail_install(py, pkg):
        raise ev.EnsureVenvError("boom")

    monkeypatch.setattr(ev, "_install_deps", fail_install)

    # Registry currently pins the venv we're about to destroy -- a "good"
    # pin from the caller's point of view up until this failure.
    registry = {"coordinator.python": str(venv_py)}
    monkeypatch.setattr(ev, "_ml_get", lambda cli, key: registry.get(key, ""))

    def fake_set(cli, key, value):
        registry[key] = value

    monkeypatch.setattr(ev, "_ml_set", fake_set)

    with pytest.raises(ev.EnsureVenvError):
        ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=False)

    assert not venv_dir.exists()  # the venv itself is still cleaned up
    assert registry["coordinator.python"] == str(venv_py)  # but the pin survives


def test_mutate_mode_failed_install_leaves_unrelated_pin_untouched(tmp_path, monkeypatch):
    """A pin pointing somewhere OTHER than the venv we just tried (and failed)
    to build is left alone -- clearing is scoped to the dangling case."""
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"

    ml_cli = tmp_path / "machine-local"
    monkeypatch.setattr(ev, "_resolve_ml_cli", lambda root: ml_cli)
    monkeypatch.setattr(ev, "_venv_healthy", lambda py: False)
    monkeypatch.setattr(ev, "_resolve_base_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(ev, "_create_venv", lambda base_py, dst: dst.mkdir(parents=True))

    def fail_install(py, pkg):
        raise ev.EnsureVenvError("boom")

    monkeypatch.setattr(ev, "_install_deps", fail_install)

    registry = {"coordinator.python": "/some/other/interpreter"}
    monkeypatch.setattr(ev, "_ml_get", lambda cli, key: registry.get(key, ""))
    calls = []
    monkeypatch.setattr(ev, "_ml_set", lambda cli, key, value: calls.append(value))

    with pytest.raises(ev.EnsureVenvError):
        ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=False)

    assert calls == []
    assert registry["coordinator.python"] == "/some/other/interpreter"


def test_mutate_mode_fails_loud_when_no_base_python(tmp_path, monkeypatch):
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"

    monkeypatch.setattr(ev, "_venv_healthy", lambda py: False)
    monkeypatch.setattr(ev, "_resolve_base_python", lambda: None)

    with pytest.raises(ev.EnsureVenvError, match="no python3 or python found"):
        ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=False)


# ---------------------------------------------------------------------------
# Build-lock contention contract (AC B4, coverage M1) -- POSIX flock branch
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fcntl.flock branch")
def test_lock_contention_fails_loud_posix(tmp_path, monkeypatch):
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"
    settings_home_path.mkdir(parents=True)

    monkeypatch.setattr(ev, "_venv_healthy", lambda py: False)

    lock_path = settings_home_path / ".coordinator-venv.lock"
    import fcntl

    holder_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ev.EnsureVenvContention):
            ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=False)
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_lock_contention_is_immediate_no_polling(tmp_path, monkeypatch):
    """NB-immediate-fail: contention raises on the FIRST try, no backoff loop."""
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"

    monkeypatch.setattr(ev, "_venv_healthy", lambda py: False)
    monkeypatch.setattr(ev, "_plat_try_lock", lambda fd: False)

    import time

    start = time.monotonic()
    with pytest.raises(ev.EnsureVenvContention):
        ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=False)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0  # no poll-with-backoff


def test_lock_released_only_when_acquired_by_this_call(tmp_path, monkeypatch):
    """When contention is hit, this call never releases a lock it doesn't hold."""
    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"

    monkeypatch.setattr(ev, "_venv_healthy", lambda py: False)
    monkeypatch.setattr(ev, "_plat_try_lock", lambda fd: False)
    unlock_calls = []
    monkeypatch.setattr(ev, "_plat_unlock", lambda fd: unlock_calls.append(fd))

    with pytest.raises(ev.EnsureVenvContention):
        ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=False)
    assert unlock_calls == []


# ---------------------------------------------------------------------------
# Build-lock contention contract -- Windows msvcrt branch (the Staff Engineer F0, M1)
# ---------------------------------------------------------------------------


def test_lock_contention_msvcrt_branch(tmp_path, monkeypatch):
    """Exercises locked_write._plat_try_lock's Windows msvcrt backend even
    on a POSIX test host, by forcing _FCNTL_AVAILABLE False and stubbing
    msvcrt (which is unimportable on non-Windows) into sys.modules.

    Also stubs a Windows-shaped ``errno`` module (MSVCRT's errno.h defines
    EDEADLOCK=36 as an EDEADLK alias; macOS/BSD's errno module does not
    carry that attribute at all -- using the real host errno module here
    would AttributeError on the tuple literal in `_plat_try_lock`'s except
    clause for a reason that has nothing to do with the Windows behavior
    under test).
    """
    import types

    from coordinator_core import locked_write as lw

    monkeypatch.setattr(lw, "_FCNTL_AVAILABLE", False)

    calls = {"locking": 0}

    class _FakeMsvcrt(types.ModuleType):
        LK_NBLCK = 1
        LK_UNLCK = 2

        def locking(self, fd, mode, nbytes):
            calls["locking"] += 1
            if mode == self.LK_NBLCK:
                raise OSError(13, "Permission denied")  # EACCES -- contention
            return None

    fake_msvcrt = _FakeMsvcrt("msvcrt")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    fake_errno = types.ModuleType("errno")
    fake_errno.EACCES = 13
    fake_errno.EDEADLOCK = 36
    monkeypatch.setitem(sys.modules, "errno", fake_errno)

    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"
    monkeypatch.setattr(ev, "_venv_healthy", lambda py: False)

    with pytest.raises(ev.EnsureVenvContention):
        ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=False)
    assert calls["locking"] >= 1


def test_lock_msvcrt_branch_acquires_and_releases_on_success(tmp_path, monkeypatch):
    """Windows msvcrt backend: successful acquire + release path (fast-path
    ready after re-check under lock -- exercises _plat_unlock's msvcrt arm)."""
    import types

    from coordinator_core import locked_write as lw

    monkeypatch.setattr(lw, "_FCNTL_AVAILABLE", False)

    calls = {"locking": 0, "unlocking": 0}

    class _FakeMsvcrt(types.ModuleType):
        LK_NBLCK = 1
        LK_UNLCK = 2

        def locking(self, fd, mode, nbytes):
            if mode == self.LK_NBLCK:
                calls["locking"] += 1
            elif mode == self.LK_UNLCK:
                calls["unlocking"] += 1
            return None

    fake_msvcrt = _FakeMsvcrt("msvcrt")
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    plugin_root = _trusted_plugin_root(tmp_path, monkeypatch)
    settings_home_path = tmp_path / "settings-home"

    # Unhealthy pre-lock, healthy once "under lock" (another session finished
    # building while we waited) -- exercises the re-check-after-acquire path.
    healthy_sequence = iter([False, True])
    monkeypatch.setattr(ev, "_venv_healthy", lambda py: next(healthy_sequence))
    monkeypatch.setattr(ev, "_set_pin", lambda cli, py: None)

    status = ev.ensure_coordinator_venv(plugin_root, settings_home_path, check_only=False)
    assert status == "ready"
    assert calls["locking"] == 1
    assert calls["unlocking"] == 1


class TestWriteSurfaceDeclaration:
    """AC coverage for ensure_venv.WRITE_SURFACE (spec:
    docs/plans/2026-08-06-writer-declared-write-surface-manifest.md, chunk
    C3f). Two distinct kinds of machine touch: the venv TREE (SHAPED) and
    the interpreter PIN KEY (STATIC write + STATIC delete)."""

    def test_declaration_is_valid(self) -> None:
        assert validate(ev.WRITE_SURFACE) == ()

    def test_declaration_names_the_writer_and_module(self) -> None:
        assert ev.WRITE_SURFACE.writer_id == "ensure-venv"
        assert ev.WRITE_SURFACE.source_module == "coordinator_core.install.ensure_venv"

    def test_surface_is_four_clauses(self) -> None:
        assert len(ev.WRITE_SURFACE.clauses) == 4

    def test_build_lock_sidecar_is_declared(self) -> None:
        """The `.lock` file outlives the run — `O_CREAT` with no unlink — so it
        is a durable surface, not an in-run temp file out of the manifest's
        remit. Omitted on first authoring because the dispatch brief named only
        the tree and the pin key."""
        paths = [
            entry.path
            for clause in ev.WRITE_SURFACE.clauses
            if isinstance(clause, StaticClause)
            for entry in clause.entries
            if entry.kind == "file-path"
        ]
        assert any(p and p.endswith(".coordinator-venv.lock") for p in paths)

    def test_venv_tree_clause_is_shaped_not_enumerated(self) -> None:
        clause = ev.WRITE_SURFACE.clauses[0]
        assert isinstance(clause, ShapedClause)
        assert clause.entry_template.kind == "file-path"
        assert ".coordinator-venv" in clause.entry_template.path
        assert "<settings-home>" in clause.entry_template.path

    def test_pin_write_clause_uses_shared_pin_key_constant(self) -> None:
        clause = ev.WRITE_SURFACE.clauses[1]
        assert isinstance(clause, StaticClause)
        assert clause.effect == "write"
        assert len(clause.entries) == 1
        entry = clause.entries[0]
        assert entry.kind == "machine-local-key"
        assert entry.key == ev._PIN_KEY

    def test_pin_delete_clause_is_declared_as_delete_not_write(self) -> None:
        clause = ev.WRITE_SURFACE.clauses[2]
        assert isinstance(clause, StaticClause)
        assert clause.effect == "delete"
        assert len(clause.entries) == 1
        entry = clause.entries[0]
        assert entry.kind == "machine-local-key"
        assert entry.key == ev._PIN_KEY
        assert entry.effect == "delete"

    def test_write_and_delete_clauses_reference_the_same_key(self) -> None:
        write_key = ev.WRITE_SURFACE.clauses[1].entries[0].key
        delete_key = ev.WRITE_SURFACE.clauses[2].entries[0].key
        assert write_key == delete_key == "coordinator.python"

    def test_set_pin_and_clear_dangling_pin_use_the_same_constant(self) -> None:
        """`_set_pin`/`_clear_dangling_pin` and the declaration all read
        `_PIN_KEY` rather than restating the literal -- guards against the
        constant drifting from what the functions actually write."""
        assert ev._PIN_KEY == "coordinator.python"
