"""
coordinator_core.install.tests.test_fleet_env_health_stamp — P175-C3: the
health-stamp fast path skips `_fleet_env_healthy`'s child import probe (a
`subprocess.run` spawning the environment's own interpreter) when nothing
the probe depends on has changed since the last real pass.

Purpose: proves the defect the bug row named
(`state/bug-backlog/2026-09-23-fleet-env-health-probe-imports-torch-on-every-install.yaml`)
is actually fixed — a key-match run makes NO `subprocess.run` call at all —
without weakening the probe itself: a miss on any single key input still
runs the FULL, unmodified probe.

Spec backlink: docs/plans/2026-09-23-klabauter-installer-performance.md § C3

Negative-spec:
    - Does NOT touch a real fleet environment or spawn `uv` — every case
      builds a synthetic generation directory under `tmp_path` with
      `LOCK_PYTHON_MINOR`/`_FLEET_ENV_IMPORT_PROBES` monkeypatched to make
      `sys.executable` read as healthy (same fixture shape as
      `test_fleet_env_healthy_minor_check.py`).
    - Does NOT narrow `_FLEET_ENV_IMPORT_PROBES` or swap any import for
      `find_spec` — the stamp only skips running the probe; when it misses,
      the real probe (still asserting every configured import) runs
      unchanged.
    - Does NOT re-test `_fleet_env_healthy`'s own minor/import contract —
      that is `test_fleet_env_healthy_minor_check.py`'s surface.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coordinator_core.install import fleet_env


def _current_minor_string() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _stub_probe_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _current_minor_string())
    monkeypatch.setattr(fleet_env, "_FLEET_ENV_IMPORT_PROBES", ())
    monkeypatch.setattr(fleet_env, "_is_windows_shell", lambda: False)


def _make_env_root(tmp_path: Path) -> Path:
    """A plain (non-junction) generation directory with a real interpreter
    symlink at the conventional `bin/python` path — `junction.junction_target`
    on a plain directory returns `None`, which is a legal, consistent
    `generation` key value throughout this file (never a junction swap
    case; that is the module's own junction-layer's surface, not this
    chunk's)."""
    env_root = tmp_path / "fleet-env"
    (env_root / "bin").mkdir(parents=True)
    (env_root / "bin" / "python").symlink_to(Path(sys.executable))
    return env_root


def _raising_subprocess_run(*args, **kwargs):
    raise AssertionError(
        "subprocess.run must not be called on a health-stamp key match"
    )


def test_key_match_skips_the_child_probe_entirely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point of the stamp: once a stamp is written after a real
    pass, a second call with nothing changed makes zero `subprocess.run`
    calls — proven by monkeypatching `subprocess.run` to raise."""
    _stub_probe_inputs(monkeypatch)
    env_root = _make_env_root(tmp_path)
    python_bin = fleet_env._env_python_path(env_root)

    # First call: real probe runs and passes, stamp gets written.
    assert fleet_env._fleet_env_healthy_stamped(python_bin, env_root) is True
    assert fleet_env._health_stamp_path(env_root).is_file()

    # Second call: any subprocess spawn is a hard failure.
    monkeypatch.setattr(fleet_env.subprocess, "run", _raising_subprocess_run)
    assert fleet_env._fleet_env_healthy_stamped(python_bin, env_root) is True


@pytest.mark.parametrize(
    "mutate",
    [
        "lock",
        "interpreter_mtime",
        "site_packages_mtime",
        "probe_tuple",
        "minor",
        "generation",
    ],
)
def test_each_key_input_change_forces_a_re_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutate: str
) -> None:
    """A changed lock hash, interpreter mtime, site-packages mtime, probe
    tuple, minor, or generation name each reads as a stamp miss and re-runs
    the real probe (never silently trusts a stale stamp)."""
    _stub_probe_inputs(monkeypatch)
    env_root = _make_env_root(tmp_path)
    python_bin = fleet_env._env_python_path(env_root)
    (env_root / "lib").mkdir()  # ensure a site-packages dir exists for the mtime key
    monkeypatch.setattr(
        fleet_env, "_site_packages_dir", lambda root: env_root / "lib"
    )

    assert fleet_env._fleet_env_healthy_stamped(python_bin, env_root) is True
    stamp_path = fleet_env._health_stamp_path(env_root)
    assert stamp_path.is_file()

    probe_calls = {"count": 0}
    real_run = fleet_env.subprocess.run

    def _counting_run(*args, **kwargs):
        probe_calls["count"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(fleet_env.subprocess, "run", _counting_run)

    if mutate == "lock":
        monkeypatch.setattr(fleet_env, "_LOCK_PATH", tmp_path / "different-lock.lock")
        (tmp_path / "different-lock.lock").write_text("changed\n", encoding="utf-8")
    elif mutate == "interpreter_mtime":
        import os
        import shutil

        # Never mutate the real, shared `sys.executable` on disk (this box
        # is a 50-70-session fleet machine) — copy it into tmp_path first,
        # point `python_bin` at the independent copy, then bump the copy's
        # own mtime.
        python_bin.unlink()
        copy_path = tmp_path / "python-copy"
        shutil.copy2(sys.executable, copy_path)
        copy_path.chmod(0o755)
        python_bin.symlink_to(copy_path)
        st = copy_path.stat()
        os.utime(copy_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    elif mutate == "site_packages_mtime":
        import os

        site_dir = env_root / "lib"
        st = site_dir.stat()
        os.utime(site_dir, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    elif mutate == "probe_tuple":
        monkeypatch.setattr(fleet_env, "_FLEET_ENV_IMPORT_PROBES", ("os",))
    elif mutate == "minor":
        monkeypatch.setattr(
            fleet_env, "LOCK_PYTHON_MINOR", f"{sys.version_info.major}.{sys.version_info.minor + 1}"
        )
    elif mutate == "generation":
        monkeypatch.setattr(
            fleet_env.junction, "junction_target", lambda p: tmp_path / "some-other-generation"
        )

    if mutate == "minor":
        # A mismatched minor makes the re-probe itself fail (unhealthy),
        # not merely re-run — assert the miss was detected and no stamp
        # survives the failed re-probe.
        assert fleet_env._fleet_env_healthy_stamped(python_bin, env_root) is False
    else:
        assert fleet_env._fleet_env_healthy_stamped(python_bin, env_root) is True
    assert probe_calls["count"] >= 1


def test_corrupt_stamp_forces_re_probe_without_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A present-but-unparseable stamp reads as a miss, not an error."""
    _stub_probe_inputs(monkeypatch)
    env_root = _make_env_root(tmp_path)
    python_bin = fleet_env._env_python_path(env_root)
    fleet_env._health_stamp_path(env_root).write_text("not json{{{", encoding="utf-8")

    assert fleet_env._fleet_env_healthy_stamped(python_bin, env_root) is True


def test_failing_probe_never_writes_a_stamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real probe that fails must never leave a stamp behind that a later
    call could wrongly trust."""
    monkeypatch.setattr(fleet_env, "LOCK_PYTHON_MINOR", _current_minor_string())
    monkeypatch.setattr(fleet_env, "_FLEET_ENV_IMPORT_PROBES", ("definitely_not_a_real_module_xyz",))
    monkeypatch.setattr(fleet_env, "_is_windows_shell", lambda: False)
    env_root = _make_env_root(tmp_path)
    python_bin = fleet_env._env_python_path(env_root)

    assert fleet_env._fleet_env_healthy_stamped(python_bin, env_root) is False
    assert not fleet_env._health_stamp_path(env_root).is_file()


def test_post_build_call_ignores_a_present_matching_stamp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The post-build health check (`_fleet_env_healthy` called directly,
    never `_fleet_env_healthy_stamped`) must always run the real probe even
    when a matching stamp is present — proven by making the probe itself
    observably run via a counting `subprocess.run` wrapper."""
    _stub_probe_inputs(monkeypatch)
    env_root = _make_env_root(tmp_path)
    python_bin = fleet_env._env_python_path(env_root)

    # Prime a matching stamp via the stamped path.
    assert fleet_env._fleet_env_healthy_stamped(python_bin, env_root) is True
    assert fleet_env._health_stamp_path(env_root).is_file()

    calls = {"count": 0}
    real_run = fleet_env.subprocess.run

    def _counting_run(*args, **kwargs):
        calls["count"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(fleet_env.subprocess, "run", _counting_run)

    assert fleet_env._fleet_env_healthy(python_bin) is True
    assert calls["count"] == 1


def test_junction_retarget_mid_probe_skips_the_stamp_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If `env_root`'s junction target changes between the key being
    computed and the probe finishing (a concurrent rebuild's publish), the
    stamp write must be skipped — never land a stamp computed for a
    generation that is about to be reclaimed, and never write into the new
    one either."""
    _stub_probe_inputs(monkeypatch)
    env_root = _make_env_root(tmp_path)
    python_bin = fleet_env._env_python_path(env_root)

    key = fleet_env._health_stamp_key(python_bin, env_root)
    assert key is not None
    # Simulate a retarget: the key names one generation, but by the time we
    # write, the junction (per this mock) points elsewhere.
    monkeypatch.setattr(
        fleet_env.junction, "junction_target", lambda p: tmp_path / "a-different-generation"
    )
    fleet_env._write_health_stamp_atomic(env_root, key)

    assert not fleet_env._health_stamp_path(env_root).is_file()


def test_check_only_uses_the_stamped_path_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ensure_fleet_env(check_only=True)` reports "ready" from the stamp
    with no probe spawn, on a second call."""
    _stub_probe_inputs(monkeypatch)
    env_root = _make_env_root(tmp_path)
    monkeypatch.setattr(fleet_env, "resolve_environment_root", lambda **_: env_root)

    status = fleet_env.ensure_fleet_env(
        check_only=True, settings_home_factory=lambda: tmp_path / "settings-home"
    )
    assert status == "ready"

    monkeypatch.setattr(fleet_env.subprocess, "run", _raising_subprocess_run)
    status = fleet_env.ensure_fleet_env(
        check_only=True, settings_home_factory=lambda: tmp_path / "settings-home"
    )
    assert status == "ready"
