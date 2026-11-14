"""Shared env shaping for every sandboxed codex spawn site.

Apply `shape_codex_child` to the FINAL child env, after every overlay and just before Popen, so the
preflight judges the env that is actually spawned.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from coordinator_core.codex_runner.sandbox_preflight import check_pwsh_launchable, is_windowsapps_path

WRITABLE_TEMP_RELPATH = Path("scratch") / "codex-tmp"


def _key(env: Mapping[str, str], name: str) -> str | None:
    return next((k for k in env if k.upper() == name), None)


def _same_dir(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a.strip('"'))) == os.path.normcase(os.path.normpath(b.strip('"')))


def _holds_pwsh(entry: str) -> bool:
    return bool(entry) and os.path.isfile(os.path.join(entry.strip('"'), "pwsh.exe"))


def place_installed_pwsh(
    env: dict[str, str], environ: Mapping[str, str], *, windows: bool | None = None
) -> dict[str, str]:
    """Put the installed PowerShell 7 dir first among pwsh holders and drop WindowsApps pwsh alias dirs.

    No-op unless Windows and `%ProgramFiles%\\PowerShell\\7\\pwsh.exe` exists. Mutates and returns `env`.
    """
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return env
    root = next((v for k, v in environ.items() if k.upper() == "PROGRAMFILES" and v), None)
    if not root:
        return env
    pwsh_dir = os.path.join(root, "PowerShell", "7")
    if not os.path.isfile(os.path.join(pwsh_dir, "pwsh.exe")):
        return env
    path_key = _key(env, "PATH")
    entries = env[path_key].split(os.pathsep) if path_key else []
    first = next((i for i, e in enumerate(entries) if _holds_pwsh(e)), None)
    rest = [e for e in entries if not (e and _same_dir(e, pwsh_dir))]
    if first is None:
        shaped = [*rest, pwsh_dir]
    else:
        at = sum(1 for e in entries[:first] if not (e and _same_dir(e, pwsh_dir)))
        shaped = [*rest[:at], pwsh_dir, *rest[at:]]
    shaped = [e for e in shaped if not (is_windowsapps_path(e) and _holds_pwsh(e))]
    joined = os.pathsep.join(shaped)
    if joined != (env[path_key] if path_key else None):
        env[path_key or "PATH"] = joined
    return env


def shape_codex_child(
    env: dict[str, str],
    *,
    writable_root: str | os.PathLike[str] | None,
    environ: Mapping[str, str] | None = None,
    windows: bool | None = None,
) -> dict[str, str]:
    """Return the env a sandboxed codex child is spawned with; raise SandboxSetupRefused if unsafe.

    Mutates and returns `env`. On Windows, inserts the installed PowerShell 7 dir immediately before
    the first PATH entry holding a pwsh.exe (appended when none does), so a leading private-bin stays
    first, and drops every WindowsApps PATH entry holding a pwsh.exe alias once the installed pwsh
    exists. With `writable_root`, TEMP and TMP become the existing `<writable_root>/scratch/codex-tmp`;
    with None they are left as inherited. Ends with
    `check_pwsh_launchable(env)`, whose `SandboxSetupRefused` propagates. Stat and mkdir only; no spawn.
    """
    if windows is None:
        windows = os.name == "nt"
    place_installed_pwsh(env, os.environ if environ is None else environ, windows=windows)
    if writable_root is not None:
        tmp = Path(writable_root) / WRITABLE_TEMP_RELPATH
        tmp.mkdir(parents=True, exist_ok=True)
        for name in ("TEMP", "TMP"):
            env[_key(env, name) or name] = str(tmp)
    if writable_root is not None:
        _isolate_codex_home(env, Path(writable_root), os.environ if environ is None else environ)
    check_pwsh_launchable(env)
    return env


def _real_codex_home(env: Mapping[str, str], environ: Mapping[str, str]) -> Path:
    for source in (env, environ):
        k = _key(source, "CODEX_HOME")
        if k and source[k]:
            return Path(source[k])
    return Path.home() / ".codex"


def _home_writable(home: Path) -> bool:
    probe = home / "tmp" / f".probe-{os.getpid()}"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return False
    return True


def _isolate_codex_home(env: dict[str, str], writable_root: Path, environ: Mapping[str, str]) -> None:
    """Point the child's CODEX_HOME at `<writable_root>/scratch/codex-home`, seeded with auth and config.

    Only when the parent is codex-sandboxed (CODEX_SANDBOX / CODEX_SANDBOX_NETWORK_DISABLED set) or the
    real home rejects a write; otherwise the env is untouched. Copies only auth.json and config.toml,
    never sessions, logs or tmp. Child env only.
    """
    real = _real_codex_home(env, environ)
    sandboxed = any(_key(environ, n) for n in ("CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"))
    if not sandboxed and _home_writable(real):
        return
    home = writable_root / "scratch" / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    for name in ("auth.json", "config.toml"):
        src, dst = real / name, home / name
        try:
            if src.is_file() and (not dst.is_file() or src.stat().st_mtime > dst.stat().st_mtime):
                part = home / f"{name}.{os.getpid()}.part"
                shutil.copy2(src, part)
                os.replace(part, dst)
        except OSError:
            continue
    env[_key(env, "CODEX_HOME") or "CODEX_HOME"] = str(home)
