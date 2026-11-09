"""codex_runner.resolve -- locate the codex entry: fnm [node_exe, codex_js], else PATH [codex].

Pure filesystem reads: never spawns fnm or a shell. The fnm route has no cmd.exe / .cmd shim on
Windows; the PATH fallback returns whatever which resolves (codex.cmd on Windows) as a bare argv.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

_CODEX_REL = ("@openai", "codex", "bin", "codex.js")

ALLOW_API_KEY_FLAG = "CODEX_RUNNER_ALLOW_API_KEY"
API_KEY_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY")
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class CodexUnavailable(RuntimeError):
    """fnm, its default node, node itself, or the @openai/codex package is absent."""


def fnm_root(platform: Optional[str] = None, env: Optional[dict] = None) -> Path:
    """FNM_DIR, else the platform default (Windows %APPDATA%\\fnm, else ~/.local/share/fnm)."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    override = env.get("FNM_DIR")
    if override:
        return Path(override)
    if platform.startswith("win"):
        appdata = env.get("APPDATA")
        if not appdata:
            raise CodexUnavailable("APPDATA is unset; cannot locate the fnm root")
        return Path(appdata) / "fnm"
    xdg = env.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "fnm"


def resolve_codex(platform: Optional[str] = None, env: Optional[dict] = None) -> list[str]:
    """Return [node_exe, codex_js] for the fnm `default` alias, else [which("codex")] from PATH.

    Raises CodexUnavailable only when both miss. The PATH hit is spawned directly, never via a shell;
    on Windows it is the codex.cmd that which resolves.
    """
    try:
        return _resolve_fnm(platform, env)
    except CodexUnavailable as fnm_miss:
        path = (os.environ if env is None else env).get("PATH", "")
        found = shutil.which("codex", path=path)
        if found:
            if (platform or sys.platform).startswith("win") and Path(found).suffix.lower() in {".cmd", ".bat"}:
                return [_native_npm_codex(Path(found))]
            return [found]
        raise CodexUnavailable(f"{fnm_miss}; codex not on PATH either") from fnm_miss


def _native_npm_codex(shim: Path) -> str:
    """Resolve npm's optional native package without executing its .cmd shim."""
    import platform

    arch = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
    triple = "aarch64-pc-windows-msvc" if arch == "arm64" else "x86_64-pc-windows-msvc"
    package = shim.parent / "node_modules" / "@openai" / "codex"
    candidates = (
        package / "node_modules" / "@openai" / f"codex-win32-{arch}" / "vendor" / triple / "bin" / "codex.exe",
        package / "vendor" / triple / "codex" / "codex.exe",
        package / "vendor" / triple / "bin" / "codex.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise CodexUnavailable(f"native Codex binary absent beside {shim}; reinstall @openai/codex with optional dependencies")


def _resolve_fnm(platform: Optional[str], env: Optional[dict]) -> list[str]:
    platform = platform or sys.platform
    windows = platform.startswith("win")
    root = fnm_root(platform, env)
    alias = root / "aliases" / "default"
    if not alias.exists():
        raise CodexUnavailable(f"fnm default alias not found: {alias}")
    # The alias is a symlink/junction to <version>/installation; realpath follows either.
    install = Path(os.path.realpath(alias))
    if windows:
        node = install / "node.exe"
        codex_js = install / "node_modules" / Path(*_CODEX_REL)
    else:
        node = install / "bin" / "node"
        codex_js = install / "lib" / "node_modules" / Path(*_CODEX_REL)
    if not node.is_file():
        raise CodexUnavailable(f"node not found under fnm default: {node}")
    if not codex_js.is_file():
        raise CodexUnavailable(f"@openai/codex not installed under fnm default: {codex_js}")
    return [str(node), str(codex_js)]


class ApiKeyRefused(RuntimeError):
    """codex is logged in with an API key and the allow_api_key flag is off."""


def allow_api_key(env: Optional[dict] = None, requested: bool = False) -> bool:
    """The allow_api_key feature flag: RunRequest.allow_api_key (`requested`) or env CODEX_RUNNER_ALLOW_API_KEY; default off."""
    if requested is True:
        return True
    env = os.environ if env is None else env
    return str(env.get(ALLOW_API_KEY_FLAG, "")).strip().lower() in _TRUTHY


def auth_mode(env: Optional[dict] = None) -> Optional[str]:
    """Normalised `auth_mode` of CODEX_HOME/auth.json ("chatgpt", "apikey"), None when unreadable."""
    env = os.environ if env is None else env
    home = env.get("CODEX_HOME")
    base = Path(home) if home else Path(env.get("HOME") or env.get("USERPROFILE") or Path.home()) / ".codex"
    try:
        data = json.loads((base / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mode = data.get("auth_mode") if isinstance(data, dict) else None
    return mode.replace("_", "").replace("-", "").lower() if isinstance(mode, str) else None


LOGIN_STATUS_TIMEOUT_S = 10


def login_status_mode(argv: Optional[list[str]] = None, env: Optional[dict] = None, run=subprocess.run) -> str:
    """Run `codex login status` once; return "chatgpt", "apikey" or "logged_out" ("unknown" if unclassifiable)."""
    argv = resolve_codex(env=env) if argv is None else argv
    try:
        proc = run(
            [*argv, "login", "status"],
            capture_output=True, text=True, timeout=LOGIN_STATUS_TIMEOUT_S, stdin=subprocess.DEVNULL,
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexUnavailable(f"codex login status could not run: {exc}") from exc
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
    if "not logged in" in text or "logged out" in text:
        return "logged_out"
    if "chatgpt" in text:
        return "chatgpt"
    if "api key" in text or "apikey" in text:
        return "apikey"
    return "unknown"


def require_subscription(
    argv: Optional[list[str]] = None, env: Optional[dict] = None, run=subprocess.run, requested: bool = False
) -> None:
    """Raise CodexUnavailable unless `codex login status` reports a ChatGPT sign-in.

    An API-key login passes only under the allow_api_key flag. Not wired into review/execute:
    their enforced gate is `preflight_auth` (auth.json); this check is for callers that can afford a spawn.
    """
    mode = login_status_mode(argv, env, run)
    if mode == "chatgpt" or (mode == "apikey" and allow_api_key(env, requested)):
        return
    raise CodexUnavailable(
        f"codex login is {mode}; a ChatGPT subscription sign-in is required "
        f"(run `codex login`; API-key billing is refused unless {ALLOW_API_KEY_FLAG}=1)"
    )


def preflight_auth(env: Optional[dict] = None, requested: bool = False) -> None:
    """Raise ApiKeyRefused on an API-key codex login unless the allow_api_key flag is set."""
    if auth_mode(env) == "apikey" and not allow_api_key(env, requested):
        raise ApiKeyRefused(
            f"codex auth_mode is API key; subscription login required (set {ALLOW_API_KEY_FLAG}=1 to allow)"
        )
