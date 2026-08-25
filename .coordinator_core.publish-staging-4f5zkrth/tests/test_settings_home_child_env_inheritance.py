"""
Tests for coordinator_core._settings_home.settings_home_child_env — AC11's
POSITIVE claim: "settings-home is resolved once per process tree and passed
to children via child env." C6 (a sibling chunk) guards only the negative
(nothing persists durably); this module is the only place in the spine that
proves children actually observe rung-0 without a CLI round-trip.

Four assertions, per pln-the-machine-local-registry-rea-50be37 § C5:
  (a) a real child process observes rung-0 (the env var) without invoking
      the `coordinator-settings-home` CLI.
  (b) no second resolver subprocess is spawned for a child whose parent
      already resolved once.
  (c) an explicitly-set child env value is never overwritten.
  (d) the long-lived-parent / re-derive-per-dispatch bound holds — no
      per-process cache staleness.

Spec backlink: pln-the-machine-local-registry-rea-50be37 § C5, AC11
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core._settings_home import settings_home_child_env

# `test_a_real_child_process_observes_rung0_without_invoking_cli` spawns a
# real `sys.executable` child -- the property under test (a real process
# environment, not an in-process dict) cannot be observed any other way.
# SPAWN-RATCHET Rule 2/4 (coordinator_core/tests/test_no_new_spawning_tests.py)
# require both markers on any file with a function-level spawn site.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_precedence_fills_gap_when_child_env_lacks_the_var(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "resolved-home"))
    base_env = {"PATH": os.environ.get("PATH", "")}

    result = settings_home_child_env(base_env)

    assert result["COORDINATOR_SETTINGS_HOME"] == str(tmp_path / "resolved-home")
    # base_env is not mutated in place — callers must not have their own dict
    # silently rewritten out from under them.
    assert "COORDINATOR_SETTINGS_HOME" not in base_env


def test_c_explicit_child_value_is_never_overwritten(monkeypatch, tmp_path):
    """(c) An explicitly-set child env value survives untouched, even though
    the parent would resolve to a different root."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "parent-resolved"))
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "COORDINATOR_SETTINGS_HOME": str(tmp_path / "operator-scoped-home"),
    }

    result = settings_home_child_env(base_env)

    assert result["COORDINATOR_SETTINGS_HOME"] == str(tmp_path / "operator-scoped-home")


def test_b_no_subprocess_is_spawned_to_resolve(monkeypatch, tmp_path):
    """(b) Resolving and propagating settings-home for a child never itself
    spawns a resolver subprocess — settings_home() is a pure env/home read."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "resolved-home"))

    def _forbidden(*args, **kwargs):
        raise AssertionError("settings_home_child_env must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    result = settings_home_child_env({"PATH": os.environ.get("PATH", "")})

    assert result["COORDINATOR_SETTINGS_HOME"] == str(tmp_path / "resolved-home")


def test_d_re_derive_per_dispatch_holds_no_stale_per_process_cache(monkeypatch, tmp_path):
    """(d) Long-lived-parent bound: two "dispatches" in the same long-lived
    process, with the resolved root changing in between (e.g. a differently-
    rooted tenant, or an operator export mid-session), must each observe the
    CURRENT root — not a value cached from an earlier dispatch hours ago."""
    first_root = tmp_path / "root-at-dispatch-one"
    second_root = tmp_path / "root-at-dispatch-two"

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(first_root))
    first = settings_home_child_env({"PATH": os.environ.get("PATH", "")})
    assert first["COORDINATOR_SETTINGS_HOME"] == str(first_root)

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(second_root))
    second = settings_home_child_env({"PATH": os.environ.get("PATH", "")})
    assert second["COORDINATOR_SETTINGS_HOME"] == str(second_root)


_CHILD_OBSERVES_RUNG0_SCRIPT = """
import os
import sys

sentinel = sys.argv[1]
val = os.environ.get("COORDINATOR_SETTINGS_HOME", "")
with open(sentinel, "w", encoding="utf-8") as fh:
    fh.write(val)
"""


def test_a_real_child_process_observes_rung0_without_invoking_cli(monkeypatch, tmp_path):
    """(a) Spawn a REAL child process with the parent's resolved
    settings-home propagated via settings_home_child_env, and a fake
    `coordinator-settings-home` on PATH that records an invocation if
    called. The child must observe the value directly from its env (rung 0)
    and the fake CLI must never fire.

    `base_env` passed into `settings_home_child_env` deliberately LACKS
    COORDINATOR_SETTINGS_HOME — only the parent process's own os.environ
    (via monkeypatch.setenv, which `settings_home()` reads) carries the
    value. This proves the (a)-resolve-and-propagate path, not just the
    (c)-never-overwrite precedence: pre-seeding base_env with the value
    under test would pass identically even against a no-op
    `settings_home_child_env` that returned `dict(base_env)` unchanged."""
    resolved_home = tmp_path / "resolved-settings-home"
    resolved_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(resolved_home))

    fake_bin_dir = tmp_path / "fake-bin"
    fake_bin_dir.mkdir()
    cli_invocation_marker = tmp_path / "cli-was-invoked"
    fake_cli_name = "coordinator-settings-home.cmd" if os.name == "nt" else "coordinator-settings-home"
    fake_cli = fake_bin_dir / fake_cli_name
    if os.name == "nt":
        fake_cli.write_text(  # abs-path-ok: synthetic literal the fake CLI would print, not a real machine path
            f'@echo off\r\necho invoked > "{cli_invocation_marker}"\r\necho C:\\should-not-be-used\r\n',
            encoding="utf-8",
        )
    else:
        fake_cli.write_text(
            f'#!/bin/sh\necho invoked > "{cli_invocation_marker}"\necho /should-not-be-used\n',
            encoding="utf-8",
        )
        fake_cli.chmod(0o755)

    base_env = {
        "PATH": f"{fake_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    child_env = settings_home_child_env(base_env)

    script_path = tmp_path / "child_observe.py"
    script_path.write_text(_CHILD_OBSERVES_RUNG0_SCRIPT, encoding="utf-8")
    sentinel = tmp_path / "observed-value.txt"

    completed = subprocess.run(
        [sys.executable, str(script_path), str(sentinel)],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert completed.returncode == 0, completed.stderr
    assert sentinel.read_text(encoding="utf-8") == str(resolved_home)
    assert not cli_invocation_marker.exists(), (
        "the fake coordinator-settings-home CLI fired — the child re-resolved "
        "instead of observing the propagated rung-0 env var"
    )
