"""shape_codex_child: PATH placement, writable TEMP/TMP, trailing preflight."""

from __future__ import annotations

import os

import pytest

from coordinator_core.codex_runner import sandbox_preflight
from coordinator_core.codex_runner.codex_spawn_env import place_installed_pwsh, shape_codex_child
from coordinator_core.codex_runner.sandbox_preflight import SandboxSetupRefused


def _dir_with_pwsh(root, *parts):
    d = root.joinpath(*parts)
    d.mkdir(parents=True)
    (d / "pwsh.exe").write_text("")
    return str(d)


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(sandbox_preflight, "_is_windows", lambda: True)


def _layout(tmp_path, *, ps7=True):
    pf = tmp_path / "ProgramFiles"
    ps7_dir = _dir_with_pwsh(pf, "PowerShell", "7") if ps7 else str(pf / "PowerShell" / "7")
    apps = _dir_with_pwsh(tmp_path, "WindowsApps")
    private = tmp_path / "private-bin"
    private.mkdir()
    return {"ProgramFiles": str(pf)}, ps7_dir, apps, str(private)


def _path(env):
    return env["PATH"].split(os.pathsep)


def test_windowsapps_first_gets_ps7_first_and_alias_dropped(tmp_path, win):
    environ, ps7, apps, _ = _layout(tmp_path)
    env = {"PATH": os.pathsep.join([apps, "other"])}
    out = shape_codex_child(env, writable_root=None, environ=environ, windows=True)
    assert out is env
    assert _path(env) == [ps7, "other"]


def test_leading_private_bin_stays_first(tmp_path, win):
    environ, ps7, apps, private = _layout(tmp_path)
    env = {"PATH": os.pathsep.join([private, apps, "other"])}
    shape_codex_child(env, writable_root=None, environ=environ, windows=True)
    assert _path(env) == [private, ps7, "other"]


def test_later_ps7_duplicate_dropped(tmp_path, win):
    environ, ps7, apps, private = _layout(tmp_path)
    env = {"PATH": os.pathsep.join([private, apps, ps7])}
    shape_codex_child(env, writable_root=None, environ=environ, windows=True)
    assert _path(env) == [private, ps7]


def test_ps7_already_first_holder_alias_still_dropped(tmp_path, win):
    environ, ps7, apps, private = _layout(tmp_path)
    env = {"PATH": os.pathsep.join([private, ps7, apps])}
    shape_codex_child(env, writable_root=None, environ=environ, windows=True)
    assert _path(env) == [private, ps7]


def test_windowsapps_dir_without_pwsh_kept(tmp_path, win):
    environ, ps7, _, _ = _layout(tmp_path)
    bare = tmp_path / "WindowsApps2" / "WindowsApps"
    bare.mkdir(parents=True)
    env = {"PATH": os.pathsep.join([str(bare), ps7])}
    shape_codex_child(env, writable_root=None, environ=environ, windows=True)
    assert _path(env) == [str(bare), ps7]


def test_place_installed_pwsh_no_real_pwsh_leaves_env(tmp_path):
    environ, _, apps, _ = _layout(tmp_path, ps7=False)
    env = {"PATH": apps}
    assert place_installed_pwsh(env, environ, windows=True) is env
    assert env == {"PATH": apps}


def test_place_installed_pwsh_non_windows_leaves_env(tmp_path):
    environ, _, apps, _ = _layout(tmp_path)
    env = {"PATH": apps}
    assert place_installed_pwsh(env, environ, windows=False) is env
    assert env == {"PATH": apps}


def test_ps7_appended_when_no_entry_holds_pwsh(tmp_path, win):
    environ, ps7, _, private = _layout(tmp_path)
    env = {"PATH": private}
    shape_codex_child(env, writable_root=None, environ=environ, windows=True)
    assert _path(env) == [private, ps7]


def test_path_key_case_preserved(tmp_path, win):
    environ, ps7, apps, _ = _layout(tmp_path)
    env = {"Path": apps}
    shape_codex_child(env, writable_root=None, environ=environ, windows=True)
    assert env["Path"].split(os.pathsep) == [ps7] and "PATH" not in env


def test_no_ps7_with_windowsapps_only_refuses(tmp_path, win):
    environ, _, apps, _ = _layout(tmp_path, ps7=False)
    env = {"PATH": apps}
    with pytest.raises(SandboxSetupRefused):
        shape_codex_child(env, writable_root=None, environ=environ, windows=True)


def test_writable_root_sets_existing_temp_tmp(tmp_path, win):
    environ, _, apps, _ = _layout(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    env = {"PATH": apps, "TEMP": "x", "TMP": "y"}
    shape_codex_child(env, writable_root=root, environ=environ, windows=True)
    want = root / "scratch" / "codex-tmp"
    assert want.is_dir()
    assert env["TEMP"] == env["TMP"] == str(want)


def test_writable_root_none_leaves_temp_tmp(tmp_path, win):
    environ, _, apps, _ = _layout(tmp_path)
    env = {"PATH": apps, "TEMP": "x", "TMP": "y"}
    shape_codex_child(env, writable_root=None, environ=environ, windows=True)
    assert (env["TEMP"], env["TMP"]) == ("x", "y")
    assert not (tmp_path / "scratch").exists()


def test_non_windows_leaves_path_untouched_but_sets_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_preflight, "_is_windows", lambda: False)
    environ, _, apps, _ = _layout(tmp_path)
    env = {"PATH": apps}
    shape_codex_child(env, writable_root=tmp_path, environ=environ, windows=False)
    assert env["PATH"] == apps
    assert env["TEMP"] == str(tmp_path / "scratch" / "codex-tmp")


def test_preflight_runs_last_on_shaped_env(tmp_path, monkeypatch):
    environ, ps7, apps, _ = _layout(tmp_path)
    seen = []
    monkeypatch.setattr(
        "coordinator_core.codex_runner.codex_spawn_env.check_pwsh_launchable",
        lambda e: seen.append((_path(e)[0], e.get("TEMP"))),
    )
    env = {"PATH": apps}
    shape_codex_child(env, writable_root=tmp_path, environ=environ, windows=True)
    assert seen == [(ps7, str(tmp_path / "scratch" / "codex-tmp"))]
