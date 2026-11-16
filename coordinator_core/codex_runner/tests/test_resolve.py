"""Tests for codex_runner.resolve on the posix and Windows fnm layouts (tmp_path fixtures)."""
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.codex_runner import resolve


@pytest.fixture(autouse=True)
def _hermetic_user_prefixes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    for key in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(fake_home))
    for key in ("npm_config_prefix", "NPM_CONFIG_PREFIX", "COORDINATOR_GROK_CLI", "COORDINATOR_AGY_CLI", "COORDINATOR_CODEX_CLI"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(resolve, "_BREW_BIN", str(tmp_path / "no-brew"))
    monkeypatch.setattr(resolve, "_warned_dirs", set())
    resolve._npm_global_bin.cache_clear()
    return fake_home


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


def test_posix_layout_via_fnm_dir(tmp_path: Path) -> None:
    inst = _install(tmp_path, "v24.1.0", windows=False)
    _alias(tmp_path, inst)
    argv = resolve.resolve_codex("linux", {"FNM_DIR": str(tmp_path)})
    assert argv == [
        str(Path(os.path.realpath(inst)) / "bin" / "node"),
        str(Path(os.path.realpath(inst)) / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"),
    ]


def test_windows_layout_has_no_shim_in_chain(tmp_path: Path) -> None:
    inst = _install(tmp_path, "v24.1.0", windows=True)
    _alias(tmp_path, inst)
    argv = resolve.resolve_codex("win32", {"FNM_DIR": str(tmp_path)})
    assert Path(argv[0]).name == "node.exe"
    assert Path(argv[1]).name == "codex.js"
    assert not any(a.lower().endswith((".cmd", ".bat", "cmd.exe")) for a in argv)


def test_default_alias_picks_its_version_not_another(tmp_path: Path) -> None:
    old = _install(tmp_path, "v22.0.0", windows=False)
    new = _install(tmp_path, "v26.0.0", windows=False)
    _alias(tmp_path, new)
    argv = resolve.resolve_codex("darwin", {"FNM_DIR": str(tmp_path)})
    assert "v26.0.0" in argv[0] and str(old) not in argv[0]


def test_root_defaults_per_platform(tmp_path: Path) -> None:
    assert resolve.fnm_root("win32", {"APPDATA": str(tmp_path)}) == tmp_path / "fnm"
    assert resolve.fnm_root("linux", {"XDG_DATA_HOME": str(tmp_path)}) == tmp_path / "fnm"
    assert resolve.fnm_root("darwin", {}) == Path.home() / ".local" / "share" / "fnm"
    assert resolve.fnm_root("linux", {"FNM_DIR": "/x/fnm", "XDG_DATA_HOME": "/y"}) == Path("/x/fnm")
    with pytest.raises(resolve.CodexUnavailable):
        resolve.fnm_root("win32", {})


def test_absent_alias_raises(tmp_path: Path) -> None:
    with pytest.raises(resolve.CodexUnavailable, match="alias"):
        resolve.resolve_codex("linux", {"FNM_DIR": str(tmp_path)})


def test_missing_codex_package_raises(tmp_path: Path) -> None:
    inst = _install(tmp_path, "v1.0.0", windows=False)
    (inst / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js").unlink()
    _alias(tmp_path, inst)
    with pytest.raises(resolve.CodexUnavailable, match="codex"):
        resolve.resolve_codex("linux", {"FNM_DIR": str(tmp_path)})


def test_missing_node_raises(tmp_path: Path) -> None:
    inst = _install(tmp_path, "v1.0.0", windows=False)
    (inst / "bin" / "node").unlink()
    _alias(tmp_path, inst)
    with pytest.raises(resolve.CodexUnavailable, match="node"):
        resolve.resolve_codex("linux", {"FNM_DIR": str(tmp_path)})


def _path_codex(tmp_path, name):
    if sys.platform == "win32":
        name += ".cmd"
    bindir = tmp_path / "pathbin"
    bindir.mkdir()
    exe = bindir / name
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    return bindir, exe


def test_path_fallback_when_alias_absent(tmp_path: Path) -> None:
    bindir, exe = _path_codex(tmp_path, "codex")
    env = {"FNM_DIR": str(tmp_path / "nofnm"), "PATH": str(bindir)}
    assert [os.path.normcase(p) for p in resolve.resolve_codex("linux", env)] == [os.path.normcase(str(exe))]


def test_path_fallback_when_node_or_package_absent(tmp_path: Path) -> None:
    bindir, exe = _path_codex(tmp_path, "codex")
    inst = _install(tmp_path, "v1.0.0", windows=False)
    (inst / "bin" / "node").unlink()
    _alias(tmp_path, inst)
    env = {"FNM_DIR": str(tmp_path), "PATH": str(bindir)}
    assert [os.path.normcase(p) for p in resolve.resolve_codex("linux", env)] == [os.path.normcase(str(exe))]


def test_fnm_wins_over_path(tmp_path: Path) -> None:
    bindir, _ = _path_codex(tmp_path, "codex")
    inst = _install(tmp_path, "v1.0.0", windows=False)
    _alias(tmp_path, inst)
    argv = resolve.resolve_codex("linux", {"FNM_DIR": str(tmp_path), "PATH": str(bindir)})
    assert len(argv) == 2 and argv[1].endswith("codex.js")


def test_both_miss_raises_naming_both(tmp_path: Path) -> None:
    with pytest.raises(resolve.CodexUnavailable, match="alias.*PATH"):
        resolve.resolve_codex("linux", {"FNM_DIR": str(tmp_path), "PATH": str(tmp_path)})


def test_windows_path_fallback_resolves_native_and_never_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake_which(cmd, path=None) -> str:
        seen["args"] = (cmd, path)
        return "npmbin/codex.cmd"

    monkeypatch.setattr(resolve.shutil, "which", fake_which)
    monkeypatch.setattr(resolve, "_native_npm_codex", lambda shim: "npmbin/native/codex.exe")
    argv = resolve.resolve_codex("win32", {"FNM_DIR": str(tmp_path / "nofnm"), "PATH": "P"})
    assert argv == ["npmbin/native/codex.exe"] and seen["args"] == ("codex", "P")
    import inspect
    assert "shell=True" not in inspect.getsource(resolve)


@pytest.mark.parametrize("family", ["codex", "agy", "grok"])
def test_native_cli_posix_is_plain_which(family, tmp_path: Path) -> None:
    assert resolve.resolve_native_cli(family, "linux", which=lambda n: f"/bin/{n}") == f"/bin/{family}"
    assert resolve.resolve_native_cli(family, "linux", which=lambda n: None, home=tmp_path) is None


@pytest.mark.parametrize("family", ["codex", "agy", "grok"])
@pytest.mark.parametrize("platform,name", [("linux", "{f}"), ("win32", "{f}.exe")])
def test_native_cli_falls_back_to_home_bin(family, platform, name, tmp_path: Path) -> None:
    exe = tmp_path / f".{family}" / "bin" / name.format(f=family)
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    got = resolve.resolve_native_cli(family, platform, which=lambda n: None, home=tmp_path)
    assert got == str(exe)
    on_path = resolve.resolve_native_cli(family, platform, which=lambda n: f"P/{n}", home=tmp_path)
    assert on_path.startswith("P/")


def test_native_cli_home_defaults_to_env_home(tmp_path: Path) -> None:
    exe = tmp_path / ".grok" / "bin" / "grok"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PATH": ""}
    assert resolve.resolve_native_cli("grok", "linux", env) == str(exe)


@pytest.mark.parametrize("family", ["codex", "agy", "grok"])
def test_native_cli_windows_exe_on_path_wins(family) -> None:
    seen = []
    got = resolve.resolve_native_cli(family, "win32", which=lambda n: seen.append(n) or f"X/{n}")
    assert got == f"X/{family}.exe" and seen == [f"{family}.exe"]


def test_native_cli_windows_absent_is_none(tmp_path: Path) -> None:
    assert resolve.resolve_native_cli("agy", "win32", which=lambda n: None, home=tmp_path) is None


def test_native_cli_windows_codex_shim_maps_without_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolve, "_native_npm_codex", lambda shim: f"native-of-{shim.name}")
    got = resolve.resolve_native_cli("codex", "win32", which=lambda n: "npm/codex.cmd" if n == "codex" else None)
    assert got == "native-of-codex.cmd"


@pytest.mark.parametrize("family", ["agy", "grok"])
def test_native_cli_windows_unknown_shim_layout_refuses(family, tmp_path: Path) -> None:
    with pytest.raises(resolve.CodexUnavailable, match=family):
        resolve.resolve_native_cli(
            family, "win32", which=lambda n: f"npm/{family}.cmd" if n == family else None, home=tmp_path
        )


@pytest.mark.parametrize("family", ["agy", "grok"])
def test_native_cli_windows_unusable_shim_falls_back_to_home_bin(family, tmp_path: Path) -> None:
    exe = tmp_path / f".{family}" / "bin" / f"{family}.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    got = resolve.resolve_native_cli(
        family, "win32", which=lambda n: f"npm/{family}.cmd" if n == family else None, home=tmp_path
    )
    assert got == str(exe)


def test_native_cli_default_which_reads_env_path(tmp_path: Path) -> None:
    exe = tmp_path / "agy.exe"
    exe.write_text("")
    got = resolve.resolve_native_cli("agy", "win32", {"PATH": str(tmp_path), "PATHEXT": ".EXE"})
    assert got is None or os.path.normcase(got) == os.path.normcase(str(exe))


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    path.chmod(0o755)
    return path


def test_native_cli_local_bin_beats_path_and_reports_untraversable_dir(
    _hermetic_user_prefixes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = _exe(_hermetic_user_prefixes / ".local" / "bin" / "grok")
    locked = tmp_path / "usr-local-bin"
    locked.mkdir()
    real_access = os.access
    monkeypatch.setattr(os, "access", lambda p, m: False if str(p) == str(locked) else real_access(p, m))
    env = {"HOME": str(_hermetic_user_prefixes), "PATH": str(locked)}
    with pytest.warns(resolve.UntraversableCliDir, match="usr-local-bin"):
        got = resolve.resolve_native_cli("grok", "linux", env)
    assert got == str(exe)


def test_native_cli_untraversable_dir_never_crashes_and_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    monkeypatch.setattr(os, "access", lambda p, m: False)
    with pytest.warns(resolve.UntraversableCliDir, match="locked"):
        assert resolve.resolve_native_cli("grok", "linux", {"PATH": str(locked)}, which=lambda n: None) is None


def test_native_cli_order_pin_then_local_then_npm_then_brew_then_path(
    _hermetic_user_prefixes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _hermetic_user_prefixes
    npm_prefix = tmp_path / "npm"
    brew = tmp_path / "brew"
    monkeypatch.setattr(resolve, "_BREW_BIN", str(brew))
    env = {"HOME": str(home), "npm_config_prefix": str(npm_prefix)}
    on_path = lambda n: f"P/{n}"  # noqa: E731
    assert resolve.resolve_native_cli("grok", "linux", env, which=on_path) == "P/grok"
    brew_exe = _exe(brew / "grok")
    assert resolve.resolve_native_cli("grok", "linux", env, which=on_path) == str(brew_exe)
    npm_exe = _exe(npm_prefix / "bin" / "grok")
    assert resolve.resolve_native_cli("grok", "linux", env, which=on_path) == str(npm_exe)
    local_exe = _exe(home / ".local" / "bin" / "grok")
    assert resolve.resolve_native_cli("grok", "linux", env, which=on_path) == str(local_exe)
    pin = _exe(tmp_path / "pinned" / "grok-pin")
    assert resolve.resolve_native_cli("grok", "linux", {**env, "COORDINATOR_GROK_CLI": str(pin)}, which=on_path) == str(pin)
    missing = {**env, "COORDINATOR_GROK_CLI": str(tmp_path / "nope")}
    assert resolve.resolve_native_cli("grok", "linux", missing, which=on_path) == str(local_exe)


def test_native_cli_npm_prefix_from_npmrc_and_node_location(
    _hermetic_user_prefixes: Path, tmp_path: Path
) -> None:
    home = _hermetic_user_prefixes
    (home / ".npmrc").write_text(f"prefix={tmp_path / 'rcprefix'}\n", encoding="utf-8")
    exe = _exe(tmp_path / "rcprefix" / "bin" / "agy")
    env = {"HOME": str(home)}
    assert resolve.resolve_native_cli("agy", "linux", env, which=lambda n: None) == str(exe)
    resolve._npm_global_bin.cache_clear()
    (home / ".npmrc").unlink()
    node_exe = _exe(tmp_path / "nodeprefix" / "bin" / "node")
    exe2 = _exe(tmp_path / "nodeprefix" / "bin" / "codex")
    got = resolve.resolve_native_cli("codex", "linux", env, which=lambda n: str(node_exe) if n == "node" else None)
    assert got == str(exe2)


def test_native_cli_npm_prefix_is_computed_once_per_process(
    _hermetic_user_prefixes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"HOME": str(_hermetic_user_prefixes)}
    resolve.resolve_native_cli("grok", "linux", env, which=lambda n: None)
    reads = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: reads.append(self) or real(self, *a, **k))
    resolve.resolve_native_cli("grok", "linux", env, which=lambda n: None)
    assert reads == []


class _Proc:
    def __init__(self, out: str = "", err: str = "") -> None:
        self.stdout, self.stderr = out, err


def _fake(out: str = "", err: str = "", calls=None):
    def run(cmd, **kw) -> _Proc:
        if calls is not None:
            calls.append((cmd, kw))
        return _Proc(out, err)
    return run


def test_chatgpt_signin_passes_and_runs_status_once() -> None:
    calls = []
    resolve.require_subscription(["node", "codex.js"], {}, _fake("Logged in using ChatGPT\n", calls=calls))
    assert len(calls) == 1 and calls[0][0] == ["node", "codex.js", "login", "status"]


def test_api_key_login_raises_naming_subscription() -> None:
    with pytest.raises(resolve.CodexUnavailable, match="subscription"):
        resolve.require_subscription(["n", "c"], {}, _fake("Logged in using an API key - sk-***"))


def test_api_key_login_passes_only_under_flag() -> None:
    run = _fake("Logged in using an API key")
    resolve.require_subscription(["n", "c"], {resolve.ALLOW_API_KEY_FLAG: "1"}, run)
    with pytest.raises(resolve.CodexUnavailable):
        resolve.require_subscription(["n", "c"], {}, run)


def test_allow_api_key_default_off_env_and_request() -> None:
    assert resolve.allow_api_key({}) is False
    assert resolve.allow_api_key({resolve.ALLOW_API_KEY_FLAG: "0"}) is False
    assert resolve.allow_api_key({resolve.ALLOW_API_KEY_FLAG: "1"}) is True
    assert resolve.allow_api_key({}, requested=True) is True


def test_request_flag_relaxes_subscription_preflight() -> None:
    run = _fake("Logged in using an API key")
    resolve.require_subscription(["n", "c"], {}, run, requested=True)
    with pytest.raises(resolve.CodexUnavailable):
        resolve.require_subscription(["n", "c"], {}, run)


def test_preflight_auth_refuses_apikey_by_default_and_accepts_with_flag(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text('{"auth_mode": "apikey"}', encoding="utf-8")
    env = {"CODEX_HOME": str(tmp_path)}
    with pytest.raises(resolve.ApiKeyRefused):
        resolve.preflight_auth(env)
    resolve.preflight_auth(env, requested=True)
    resolve.preflight_auth({**env, resolve.ALLOW_API_KEY_FLAG: "1"})
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt"}', encoding="utf-8")
    resolve.preflight_auth(env)


def test_logged_out_and_unknown_raise() -> None:
    assert resolve.login_status_mode(["n"], {}, _fake("", "Not logged in")) == "logged_out"
    for out in ("Not logged in", "gibberish"):
        with pytest.raises(resolve.CodexUnavailable):
            resolve.require_subscription(["n"], {}, _fake(out))


def test_spawn_failure_is_codex_unavailable() -> None:
    def boom(cmd, **kw):
        raise FileNotFoundError("node")
    with pytest.raises(resolve.CodexUnavailable, match="could not run"):
        resolve.require_subscription(["n"], {}, boom)
