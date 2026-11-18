"""coordinator_core.ops.external_workspace_rehydrate -- op "external_workspace.rehydrate":
make one missing registered external workspace present again from its machine-local url/ref.

The only sanctioned VCS write into an ``external_workspaces.<key>.path``: the git spawns carry
``TOKEN_ENV`` in their own env (never in ``os.environ``), which the reference-transaction hook
reads as the sanctioned-route marker.

Params: exactly one of ``key`` or ``all_missing``. ``key`` clones ``url`` into the exact registered
path (then checks out ``ref`` when set), resets a path that holds a ``.git`` but no worktree, and
is a no-op on a healthy worktree. ``all_missing`` reports the missing workspaces and clones nothing.

Negative-spec: never clones anywhere but the key's registered path; never bulk-clones; does not
touch p4 workspaces, the registry, or any workspace that already has a worktree.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.external_workspace.vcs_guard_contract import TOKEN_ENV
from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.git.run import GitResult, run_git

OP_KEY = "external_workspace.rehydrate"

_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "minLength": 1,
                "description": "Registered external_workspaces key to clone or reset."},
        "all_missing": {"type": "boolean",
                        "description": "Report the missing registered workspaces; clones none."},
    },
    "additionalProperties": False,
}


def _registered() -> Dict[str, Dict[str, str]]:
    """``{key: {path, url, ref, vcs}}`` for every registered workspace, missing paths kept."""
    from coordinator_core.machine_resolver import external_workspace_registered_paths

    return external_workspace_registered_paths()


def _state(path: str) -> str:
    """``healthy`` (worktree present), ``reset`` (a ``.git`` and nothing else), ``empty`` (clone
    target), ``blocked`` (a non-empty directory without ``.git``, or a non-directory)."""
    if not os.path.lexists(path):
        return "empty"
    if not os.path.isdir(path):
        return "blocked"
    with os.scandir(path) as it:
        names = {entry.name for entry in it}
    if ".git" in names:
        return "healthy" if len(names) > 1 else "reset"
    return "empty" if not names else "blocked"


def _git(args: List[str], cwd: Optional[str] = None, remote: bool = False) -> GitResult:
    env = {**os.environ, TOKEN_ENV: "1", "GIT_TERMINAL_PROMPT": "0"}
    return run_git(args, cwd=cwd, env=env, remote=remote)


def _check(proc: GitResult, what: str) -> None:
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise CallerFacingValidationError(
            f"{OP_KEY}: {what} failed (exit {proc.returncode}): {' | '.join(tail)}"
        )


def _missing_rows(registry: Dict[str, Dict[str, str]]) -> List[dict]:
    rows = []
    for key in sorted(registry):
        f = registry[key]
        path = f.get("path", "").strip()
        if not path or f.get("vcs", "").lower() == "p4":
            continue
        if _state(path) != "healthy":
            rows.append({"key": key, "path": path, "url": f.get("url"), "ref": f.get("ref")})
    return rows


def _rehydrate_one(key: str, registry: Dict[str, Dict[str, str]]) -> dict:
    f = registry.get(key)
    if f is None:
        raise CallerFacingValidationError(f"{OP_KEY}: external_workspaces.{key} is not registered")
    path = f.get("path", "").strip()
    url = f.get("url", "").strip()
    ref = f.get("ref", "").strip()
    if not path:
        raise CallerFacingValidationError(f"{OP_KEY}: external_workspaces.{key}.path is not set")
    if f.get("vcs", "").lower() == "p4":
        raise CallerFacingValidationError(f"{OP_KEY}: external_workspaces.{key} is a p4 workspace")
    if not url:
        raise CallerFacingValidationError(f"{OP_KEY}: external_workspaces.{key}.url is not set")
    if url.startswith("-") or ref.startswith("-"):
        raise CallerFacingValidationError(f"{OP_KEY}: external_workspaces.{key} url/ref reads as an option")

    state = _state(path)
    result = {"key": key, "path": path, "url": url, "ref": ref or None}
    if state == "healthy":
        return {**result, "action": "noop"}
    if state == "blocked":
        raise CallerFacingValidationError(
            f"{OP_KEY}: {path} is not empty and holds no .git; clear it by hand, it is not cloned over"
        )
    if state == "reset":
        _check(_git(["reset", "--hard", ref or "HEAD", "--"], cwd=path), "git reset --hard")
        return {**result, "action": "reset"}
    _check(_git(["clone", "--", url, path], remote=True), "git clone")
    if ref:
        _check(_git(["checkout", ref, "--"], cwd=path), f"git checkout {ref}")
    return {**result, "action": "cloned"}


@register_op(OP_KEY, params_schema=_SCHEMA)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "external_workspace.rehydrate": params `key` xor `all_missing`.

    Returns {"key", "path", "url", "ref", "action"} with action "cloned", "reset" or "noop" for
    `key`; {"missing": [{key, path, url, ref}, ...]} for `all_missing`, with nothing cloned.
    """
    key = params.get("key")
    all_missing = params.get("all_missing")
    if bool(key) == bool(all_missing):
        raise CallerFacingValidationError(f"{OP_KEY}: pass exactly one of key or all_missing")
    registry = _registered()
    if all_missing:
        return {"missing": _missing_rows(registry)}
    return _rehydrate_one(str(key), registry)
