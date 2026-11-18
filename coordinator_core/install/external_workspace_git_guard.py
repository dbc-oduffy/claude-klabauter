"""coordinator_core.install.external_workspace_git_guard -- install step that routes each
registered external workspace's git hooks to the engine's reference-transaction guard.

Purpose: renders the contract's includeIf stanza (one block per registered non-p4 workspace
path, missing paths included) and its hooks-path config into the settings home's machine-local
dir, then adds one ``[include] path=`` for the stanza file to the global git config. Only
registered paths then pay the hook; no repo-wide ``core.hooksPath`` is set.

Contract:
  - Registry reads are zero-spawn; the global-config step is at most two git spawns.
  - Idempotent: unchanged file content is not rewritten, an include line already present is
    not added again.
  - An unresolvable settings home is a typed refusal (`ExternalWorkspaceGuardRefusal`)
    naming COORDINATOR_SETTINGS_HOME; nothing is written.
  - Honours COORDINATOR_DISABLE_MACHINE_MUTATION: the global git config is never touched.

Negative-spec: does not clone, reset or otherwise write inside a workspace, and does not
touch `p4` workspaces (another guard's).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from coordinator_core.external_workspace import vcs_guard_contract as contract
from coordinator_core.git.run import run_git
from coordinator_core.install.write_surface import (
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

_ABSOLUTE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="external-workspace-git-guard",
    source_module="coordinator_core.install.external_workspace_git_guard",
    clauses=(
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings-home>/machine-local/{contract.INCLUDE_FILE_NAME}",
                    reason="apply(): one includeIf block per registered non-p4 external workspace",
                ),
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings-home>/machine-local/{contract.HOOKS_CONFIG_FILE_NAME}",
                    reason="apply(): the core.hooksPath the includeIf blocks apply",
                ),
                WriteSurfaceEntry(
                    kind="git-config-key",
                    key="include.path",
                    reason="apply(): one entry pointing at the include file, added to the global git config when absent",
                ),
            ),
        ),
    ),
)


class ExternalWorkspaceGuardRefusal(RuntimeError):
    """The install step cannot place the include file: the settings home does not resolve."""


def registered_paths() -> Dict[str, str]:
    """``{key: path}`` for every registered external workspace whose ``vcs`` is not ``p4``.

    Keeps a path that does not exist yet (the clone destination the guard must cover); skips
    an entry with no absolute path. Reads the merged registry only.
    """
    from coordinator_core.machine_resolver import external_workspace_registered_paths

    out: Dict[str, str] = {}
    for name, f in sorted(external_workspace_registered_paths().items()):
        if f["vcs"].lower() == "p4" or not _ABSOLUTE.match(f["path"]):
            continue
        out[name] = f["path"]
    return out


def _settings_dir() -> Path:
    from coordinator_core.machine_resolver import registry_dir

    try:
        return registry_dir()
    except Exception as exc:  # noqa: BLE001 -- any resolver failure is the same refusal
        raise ExternalWorkspaceGuardRefusal(
            f"settings home unresolved (COORDINATOR_SETTINGS_HOME): {exc}"
        ) from exc


def _write_if_changed(path: Path, content: str, *, dry_run: bool) -> str:
    try:
        current = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        current = None
    if current == content:
        return f"ok      {path} (unchanged)"
    if dry_run:
        return f"would   write {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    return f"wrote   {path}"


def _ensure_global_include(include_file: Path, *, dry_run: bool) -> str:
    from coordinator_core.install import substrate

    wanted = contract.to_git_posix(include_file)
    current = run_git(["config", "--global", "--get-all", "include.path"])
    if current.timed_out or current.returncode not in (0, 1):
        return "skip    include.path (could not read the global git config)"
    if wanted in {contract.to_git_posix(p) for p in current.stdout.splitlines() if p.strip()}:
        return f"ok      include.path = {wanted} (already set)"
    if dry_run:
        return f"would   include.path += {wanted}"
    refused = substrate._refuse_machine_mutation(
        str(include_file), what="global git include of the external-workspace guard",
        check_temp_path=False,
    )
    if refused is not None:
        return f"skip    include.path (machine mutation disabled: {refused})"
    added = run_git(["config", "--global", "--add", "include.path", wanted])
    if added.returncode != 0:
        return f"FAILED  include.path: {added.stderr.strip()}"
    return f"set     include.path += {wanted}"


def apply(*, dry_run: bool = False) -> List[str]:
    """Write the include and hooks-config files, then include the first from the global git config.

    Returns one report line per step. Raises `ExternalWorkspaceGuardRefusal` when the
    settings home does not resolve.
    """
    directory = _settings_dir()
    paths = registered_paths()
    include_file = directory / contract.INCLUDE_FILE_NAME
    report: List[str] = []
    if not paths and not include_file.exists():
        return ["ok      no registered non-p4 external workspace -- nothing to guard"]
    report.append(
        _write_if_changed(
            include_file, contract.render_include_stanza(list(paths.values())), dry_run=dry_run
        )
    )
    report.append(
        _write_if_changed(
            directory / contract.HOOKS_CONFIG_FILE_NAME,
            contract.render_hooks_config(),
            dry_run=dry_run,
        )
    )
    report.append(_ensure_global_include(include_file, dry_run=dry_run))
    report.append(f"guarded {len(paths)} registered external workspace path(s)")
    return report


if __name__ == "__main__":
    import sys

    for _line in apply(dry_run="--check-only" in sys.argv[1:]):
        print(_line)
    sys.exit(0)
