"""Shared env shaping for every sandboxed codex spawn site.

Apply `shape_codex_child` to the FINAL child env, after every overlay and just before Popen, so the
preflight judges the env that is actually spawned.
"""

from __future__ import annotations

import os
import re
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


_HOOK_STATE_HEADER = re.compile(r"^(\[hooks\.state\.')(.*?)(:[a-z_]+:\d+:\d+'\])", re.MULTILINE)


def rekey_hook_trust(config_text: str, old_hooks: Path, new_hooks: Path) -> str:
    """Re-key `[hooks.state.'<hooks.json>:<event>:i:j']` headers from `old_hooks` to `new_hooks`.

    Codex keys hook trust by the hooks.json absolute path (the trusted_hash covers the hook body), so a
    copied hooks.json is untrusted until its entries carry the copy's path. Other paths are untouched.
    """
    old = os.path.normcase(os.path.normpath(str(old_hooks)))

    def swap(m: re.Match[str]) -> str:
        if os.path.normcase(os.path.normpath(m.group(2))) != old:
            return m.group(0)
        return f"{m.group(1)}{new_hooks}{m.group(3)}"

    return _HOOK_STATE_HEADER.sub(swap, config_text)


def _isolate_codex_home(env: dict[str, str], writable_root: Path, environ: Mapping[str, str]) -> None:
    """Point the child's CODEX_HOME at `<writable_root>/scratch/codex-home`, seeded with auth, config, hooks.

    Only when the parent is codex-sandboxed (CODEX_SANDBOX / CODEX_SANDBOX_NETWORK_DISABLED set) or the
    real home rejects a write; otherwise the env is untouched. Copies only auth.json, hooks.json and
    config.toml (hook trust re-keyed to the isolated hooks.json), never sessions, logs or tmp. Child env only.
    """
    real = _real_codex_home(env, environ)
    sandboxed = any(_key(environ, n) for n in ("CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"))
    if not sandboxed and _home_writable(real):
        return
    home = writable_root / "scratch" / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    # Trap: hooks.json carries codex's only guard (PreToolUse host-guard). Without it, or with its trust
    # entries still keyed to the real path, the isolated child runs every tool unguarded and silently.
    for name in ("auth.json", "config.toml", "hooks.json"):
        src, dst = real / name, home / name
        try:
            if src.is_file() and (not dst.is_file() or src.stat().st_mtime > dst.stat().st_mtime):
                part = home / f"{name}.{os.getpid()}.part"
                if name == "config.toml":
                    part.write_text(
                        rekey_hook_trust(src.read_text(encoding="utf-8"), real / "hooks.json", home / "hooks.json"),
                        encoding="utf-8",
                        newline="",
                    )
                    shutil.copystat(src, part)
                else:
                    shutil.copy2(src, part)
                os.replace(part, dst)
        except (OSError, UnicodeError):
            continue
    # Trap: a home seeded before re-keying existed keeps a config copy no older than the real one, so the
    # copy step skips it and its trust stays on the real path; re-key the existing copy in place too.
    cfg = home / "config.toml"
    try:
        text = cfg.read_text(encoding="utf-8")
        rekeyed = rekey_hook_trust(text, real / "hooks.json", home / "hooks.json")
        if rekeyed != text:
            cfg.write_text(rekeyed, encoding="utf-8", newline="")
    except (OSError, UnicodeError):
        pass
    env[_key(env, "CODEX_HOME") or "CODEX_HOME"] = str(home)
