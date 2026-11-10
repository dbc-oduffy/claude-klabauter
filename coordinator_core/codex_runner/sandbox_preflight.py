"""Windows preflight for codex workspace-write launches and the child pwsh.

The unelevated Windows sandbox spends ~13 minutes stamping a capability ACE on a root that is new
to it, then ~0.3s per launch. A root not yet proven is never refused: one bounded probe runs, and a
probe that outlives its cap is left running as the detached warm-up, never killed.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

PROVEN_ROOTS_RELPATH = Path(".coordinator-local") / "codex-sandbox-proven-roots.json"
WARMUP_RELPATH = Path(".coordinator-local") / "codex-sandbox-warmup.json"
PROBE_CAP_S = 1.5
_DETACHED = (
    getattr(subprocess, "DETACHED_PROCESS", 0)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
)


class SandboxSetupRefused(RuntimeError):
    """Raised when a workspace-write launch would hang in unelevated sandbox setup."""


def _is_windows() -> bool:
    return os.name == "nt"


def _normalise(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _codex_home(codex_home: str | os.PathLike[str] | None) -> Path:
    if codex_home is not None:
        return Path(codex_home)
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def _sandbox_mode(codex_home: Path) -> str | None:
    try:
        with open(codex_home / "config.toml", "rb") as fh:
            cfg = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    windows = cfg.get("windows")
    if not isinstance(windows, dict):
        return None
    mode = windows.get("sandbox")
    return mode if isinstance(mode, str) else None


def _proven_roots(repo_root: Path) -> set[str]:
    try:
        data = json.loads((repo_root / PROVEN_ROOTS_RELPATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {_normalise(item) for item in data if isinstance(item, str) and item}


def _first_pwsh(env: Mapping[str, str]) -> str | None:
    path = next((v for k, v in env.items() if k.upper() == "PATH"), "")
    for entry in path.split(os.pathsep):
        candidate = os.path.join(entry.strip('"'), "pwsh.exe") if entry else ""
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def check_pwsh_launchable(env: Mapping[str, str]) -> None:
    """Raise SandboxSetupRefused when the child PATH resolves no launchable pwsh.exe on Windows.

    The sandbox's restricted token cannot activate the WindowsApps pwsh alias
    (CreateProcessAsUserW failed: 5), so a first hit under a `WindowsApps` dir, or no hit,
    is refused. One stat per PATH entry, no spawn; no-op off Windows.
    """
    if not _is_windows():
        return
    found = _first_pwsh(env)
    if found is not None and "windowsapps" not in os.path.normcase(found).split(os.sep):
        return
    root = next((v for k, v in env.items() if k.upper() == "PROGRAMFILES" and v), "%ProgramFiles%")
    install = os.path.join(root, "PowerShell", "7")
    seen = f"resolves {found}, an unlaunchable WindowsApps alias" if found else "resolves no pwsh.exe"
    raise SandboxSetupRefused(
        f"refused: codex launch blocked: child PATH {seen}. "
        f"Install PowerShell 7 to {install} (it is then prepended to the child PATH per launch)."
    )


def _atomic_write_json(path: Path, data: object) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _warming_pid(repo_root: Path, key: str) -> int | None:
    try:
        data = json.loads((repo_root / WARMUP_RELPATH).read_text(encoding="utf-8"))
        pid = data.get(key)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(pid, int):
        return None
    try:
        import psutil

        return pid if psutil.Process(pid).name().lower().startswith("codex") else None
    except Exception:
        return None


def _set_warming(repo_root: Path, key: str, pid: int | None) -> None:
    try:
        data = json.loads((repo_root / WARMUP_RELPATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if pid is None:
        data.pop(key, None)
    else:
        data[key] = pid
    _atomic_write_json(repo_root / WARMUP_RELPATH, data)


def _record_proven(repo_root: Path, key: str) -> None:
    roots = sorted(_proven_roots(repo_root) | {key})
    _atomic_write_json(repo_root / PROVEN_ROOTS_RELPATH, roots)


def check_workspace_write(
    workspace_root: str | os.PathLike[str],
    *,
    repo_root: str | os.PathLike[str] | None = None,
    codex_home: str | os.PathLike[str] | None = None,
    codex_argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return an advisory line when `workspace_root` may be slow on its first sandbox launch, else None.

    Never refuses. Acts only on Windows with `[windows].sandbox == "unelevated"` and a root absent
    from the proven-roots record. With `codex_argv`, runs one probe (`codex sandbox ... cmd /c exit 0`)
    capped at PROBE_CAP_S: success records the root proven; a probe still running at the cap is left
    as the detached warm-up (pid kept in the warm-up record, never killed). A live warm-up is not
    respawned. Without `codex_argv` the root is allowed with an advisory.
    """
    if not _is_windows():
        return None
    if _sandbox_mode(_codex_home(codex_home)) != "unelevated":
        return None
    root = Path(workspace_root)
    state = Path(repo_root) if repo_root is not None else root
    key = _normalise(root)
    if key in _proven_roots(state):
        return None
    if _warming_pid(state, key) is not None:
        return f"advisory: codex sandbox warm-up for {root} is still running; launches may be slow until it ends."
    if codex_argv is None:
        return f"advisory: codex workspace-write on {root} is not proven; the first launch may be slow."
    cmd = [*codex_argv, "sandbox", "-c", 'sandbox_mode="workspace-write"', "--", "cmd", "/c", "exit", "0"]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(root),
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_DETACHED,
        )
    except OSError:
        return f"advisory: codex sandbox probe for {root} could not start; launching unprobed."
    try:
        rc = proc.wait(timeout=PROBE_CAP_S)
    except subprocess.TimeoutExpired:
        _set_warming(state, key, proc.pid)
        return (
            f"advisory: codex sandbox is stamping {root} (about 13 minutes once); "
            "a detached warm-up continues and is never killed."
        )
    if rc == 0:
        _record_proven(state, key)
        return None
    return f"advisory: codex sandbox probe for {root} exited {rc}; launching unprobed."
