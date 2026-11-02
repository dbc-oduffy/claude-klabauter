"""Tests for codex_runner.resolve on the posix and Windows fnm layouts (tmp_path fixtures)."""
import os
from pathlib import Path

import pytest

from coordinator_core.codex_runner import resolve


def _install(root: Path, version: str, windows: bool) -> Path:
    inst = root / "node-versions" / version / "installation"
    if windows:
        node = inst / "node.exe"
        js = inst / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    else:
        node = inst / "bin" / "node"
        js = inst / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    for p in (node, js):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    return inst


def _alias(root: Path, inst: Path) -> None:
    (root / "aliases").mkdir(parents=True, exist_ok=True)
    link = root / "aliases" / "default"
    try:
        os.symlink(inst, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")


def test_posix_layout_via_fnm_dir(tmp_path):
    inst = _install(tmp_path, "v24.1.0", windows=False)
    _alias(tmp_path, inst)
    argv = resolve.resolve_codex("linux", {"FNM_DIR": str(tmp_path)})
    assert argv == [
        str(Path(os.path.realpath(inst)) / "bin" / "node"),
        str(Path(os.path.realpath(inst)) / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"),
    ]


def test_windows_layout_has_no_shim_in_chain(tmp_path):
    inst = _install(tmp_path, "v24.1.0", windows=True)
    _alias(tmp_path, inst)
    argv = resolve.resolve_codex("win32", {"FNM_DIR": str(tmp_path)})
    assert Path(argv[0]).name == "node.exe"
    assert Path(argv[1]).name == "codex.js"
    assert not any(a.lower().endswith((".cmd", ".bat", "cmd.exe")) for a in argv)


def test_default_alias_picks_its_version_not_another(tmp_path):
    old = _install(tmp_path, "v22.0.0", windows=False)
    new = _install(tmp_path, "v26.0.0", windows=False)
    _alias(tmp_path, new)
    argv = resolve.resolve_codex("darwin", {"FNM_DIR": str(tmp_path)})
    assert "v26.0.0" in argv[0] and str(old) not in argv[0]


def test_root_defaults_per_platform(tmp_path):
    assert resolve.fnm_root("win32", {"APPDATA": str(tmp_path)}) == tmp_path / "fnm"
    assert resolve.fnm_root("linux", {"XDG_DATA_HOME": str(tmp_path)}) == tmp_path / "fnm"
    assert resolve.fnm_root("darwin", {}) == Path.home() / ".local" / "share" / "fnm"
    assert resolve.fnm_root("linux", {"FNM_DIR": "/x/fnm", "XDG_DATA_HOME": "/y"}) == Path("/x/fnm")
    with pytest.raises(resolve.CodexUnavailable):
        resolve.fnm_root("win32", {})


def test_absent_alias_raises(tmp_path):
    with pytest.raises(resolve.CodexUnavailable, match="alias"):
        resolve.resolve_codex("linux", {"FNM_DIR": str(tmp_path)})


def test_missing_codex_package_raises(tmp_path):
    inst = _install(tmp_path, "v1.0.0", windows=False)
    (inst / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js").unlink()
    _alias(tmp_path, inst)
    with pytest.raises(resolve.CodexUnavailable, match="codex"):
        resolve.resolve_codex("linux", {"FNM_DIR": str(tmp_path)})


def test_missing_node_raises(tmp_path):
    inst = _install(tmp_path, "v1.0.0", windows=False)
    (inst / "bin" / "node").unlink()
    _alias(tmp_path, inst)
    with pytest.raises(resolve.CodexUnavailable, match="node"):
        resolve.resolve_codex("linux", {"FNM_DIR": str(tmp_path)})


class _Proc:
    def __init__(self, out="", err=""):
        self.stdout, self.stderr = out, err


def _fake(out="", err="", calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append((cmd, kw))
        return _Proc(out, err)
    return run


def test_chatgpt_signin_passes_and_runs_status_once():
    calls = []
    resolve.require_subscription(["node", "codex.js"], {}, _fake("Logged in using ChatGPT\n", calls=calls))
    assert len(calls) == 1 and calls[0][0] == ["node", "codex.js", "login", "status"]


def test_api_key_login_raises_naming_subscription():
    with pytest.raises(resolve.CodexUnavailable, match="subscription"):
        resolve.require_subscription(["n", "c"], {}, _fake("Logged in using an API key - sk-***"))


def test_api_key_login_passes_only_under_flag():
    run = _fake("Logged in using an API key")
    resolve.require_subscription(["n", "c"], {resolve.ALLOW_API_KEY_FLAG: "1"}, run)
    with pytest.raises(resolve.CodexUnavailable):
        resolve.require_subscription(["n", "c"], {}, run)


def test_allow_api_key_default_off_env_and_request():
    assert resolve.allow_api_key({}) is False
    assert resolve.allow_api_key({resolve.ALLOW_API_KEY_FLAG: "0"}) is False
    assert resolve.allow_api_key({resolve.ALLOW_API_KEY_FLAG: "1"}) is True
    assert resolve.allow_api_key({}, requested=True) is True


def test_request_flag_relaxes_subscription_preflight():
    run = _fake("Logged in using an API key")
    resolve.require_subscription(["n", "c"], {}, run, requested=True)
    with pytest.raises(resolve.CodexUnavailable):
        resolve.require_subscription(["n", "c"], {}, run)


def test_preflight_auth_refuses_apikey_by_default_and_accepts_with_flag(tmp_path):
    (tmp_path / "auth.json").write_text('{"auth_mode": "apikey"}', encoding="utf-8")
    env = {"CODEX_HOME": str(tmp_path)}
    with pytest.raises(resolve.ApiKeyRefused):
        resolve.preflight_auth(env)
    resolve.preflight_auth(env, requested=True)
    resolve.preflight_auth({**env, resolve.ALLOW_API_KEY_FLAG: "1"})
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt"}', encoding="utf-8")
    resolve.preflight_auth(env)


def test_logged_out_and_unknown_raise():
    assert resolve.login_status_mode(["n"], {}, _fake("", "Not logged in")) == "logged_out"
    for out in ("Not logged in", "gibberish"):
        with pytest.raises(resolve.CodexUnavailable):
            resolve.require_subscription(["n"], {}, _fake(out))


def test_spawn_failure_is_codex_unavailable():
    def boom(cmd, **kw):
        raise FileNotFoundError("node")
    with pytest.raises(resolve.CodexUnavailable, match="could not run"):
        resolve.require_subscription(["n"], {}, boom)
