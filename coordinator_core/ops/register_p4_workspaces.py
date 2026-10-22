"""
coordinator_core.ops.register_p4_workspaces -- install-path registrar for this host's
local Perforce client workspaces.

Purpose: seeds machine-local `external_workspaces.<key>.{path,vcs,kind,p4_client}` for each
p4 client whose Root is a directory on this host, so the read-only guards and cockpit's
venture join see a workspace a fresh box would otherwise never register.

Contract:
  - Two p4 spawns at most per run (`info`, then `clients -u <user>`), independent of client
    count. Reached only from `register_discovered_repos` (the install step); never a hot op.
  - Absent-only: an existing `external_workspaces.<key>.*` field is never overwritten or
    unset. A client already covered by an entry (same `p4_client`, or same directory as its
    `path`) gets no writes at all, so a hand-set `venture` survives every re-run.
  - `venture` is never written: no source resolves it, and guessing is forbidden.
  - A client that is a working repo (Root holds `.git`, equals a `repos.*` path, or is named
    by a `p4.*.client` key) is never registered external-readonly.
  - Known gap: the four field writes are not atomic; a failure mid-entry leaves a partial
    entry that the coverage check then treats as present, so it is never completed.
  - Never-block: no binary, no login, no server, a timeout -> zero entries, exit 0.

Negative-spec: does not touch `policy.p4_no_submit`, the p4 guards or the external-workspace
reader in machine_resolver.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from coordinator_core.install.resolution_journal import record_resolution
from coordinator_core.install.write_surface import (
    ShapedClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.machine_resolver import merged_flat_registry, registry_set
from coordinator_core.path_identity import same_dir

_PROG = "register-p4-workspaces"
_EXT_PREFIX = "external_workspaces."
_SHAPED_CLAUSE_INDEX = 0

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="register-p4-workspaces",
    source_module="coordinator_core.ops.register_p4_workspaces",
    clauses=(
        ShapedClause(
            discovered_by="p4 -ztag clients",
            entry_template=WriteSurfaceEntry(
                kind="machine-local-key",
                key="external_workspaces.<p4-derived-key>.<field>",
            ),
        ),
    ),
)
"""Not knowable in source: the set is this host's p4 clients with a local Root, minus those
already covered by an `external_workspaces.*` entry or that are working repos."""


def _run_discovery(args: List[str]):
    """Indirection so tests stub the spawn and nothing imports coordinator_core.p4 at module scope."""
    from coordinator_core.p4 import runner

    return runner.run_discovery(args)


def _derive_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _tagged_lines(stdout: str) -> List[Tuple[str, str]]:
    """`info1: <Tag> <value>` lines as (tag, value); the `exit:` trailer and others are dropped."""
    out: List[Tuple[str, str]] = []
    for line in stdout.splitlines():
        if not line.startswith("info1: "):
            continue
        tag, _, value = line[len("info1: "):].partition(" ")
        out.append((tag, value.strip()))
    return out


def _parse_info(stdout: str) -> Dict[str, str]:
    return {tag: value for tag, value in _tagged_lines(stdout)}


def _parse_clients(stdout: str) -> List[Dict[str, str]]:
    """One record per `client <name>` line, running to the next such line."""
    records: List[Dict[str, str]] = []
    for tag, value in _tagged_lines(stdout):
        if tag == "client":
            records.append({"client": value})
        elif records:
            records[-1].setdefault(tag, value)
    return records


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _refused() -> Optional[str]:
    from coordinator_core.install import substrate

    return substrate._refuse_machine_mutation(
        "registry.local.toml", what="p4 workspace registration", check_temp_path=False
    )


def main(argv: Sequence[str]) -> int:
    check_only = False
    for arg in argv:
        if arg == "--check-only":
            check_only = True
        else:
            print(f"{_PROG}: ignoring unknown argument: {arg}", file=sys.stderr)

    try:
        _run(check_only)
    except Exception as exc:  # noqa: BLE001 -- never-block contract
        print(f"{_PROG}: skipped: {exc}", file=sys.stderr)
    return 0


def _run(check_only: bool) -> None:
    info = _run_discovery(["info"])
    if not info.ok:
        print(f"{_PROG}: skipped, p4 info unavailable")
        return
    fields = _parse_info(info.stdout)
    user = fields.get("userName", "")
    if not user:
        print(f"{_PROG}: skipped, p4 reports no user")
        return
    host = fields.get("clientHost", "")

    clients = _run_discovery(["clients", "-u", user])
    if not clients.ok:
        print(f"{_PROG}: skipped, p4 clients unavailable")
        return

    registry = merged_flat_registry()
    ext: Dict[str, Dict[str, str]] = {}
    for full_key, value in registry.items():
        if full_key.startswith(_EXT_PREFIX) and isinstance(value, str):
            key, _, field = full_key[len(_EXT_PREFIX):].partition(".")
            ext.setdefault(key, {})[field] = value
    repo_paths = [v for k, v in registry.items() if k.startswith("repos.") and isinstance(v, str)]
    p4_repo_clients = {
        v for k, v in registry.items()
        if k.startswith("p4.") and k.endswith(".client") and isinstance(v, str)
    }

    pending: List[Tuple[str, str, str]] = []
    claimed: Dict[str, str] = {}
    for rec in _parse_clients(clients.stdout):
        name = rec["client"]
        rec_host = rec.get("Host", "")
        root = rec.get("Root", "")
        if rec_host and rec_host.lower() != host.lower():
            continue
        if not root or not os.path.isdir(root):
            continue
        if (
            os.path.exists(os.path.join(root, ".git"))
            or any(same_dir(root, p) for p in repo_paths)
            or name in p4_repo_clients
        ):
            print(f"{_PROG}: {name} -- working repo, not registered external-readonly")
            continue
        if any(
            f.get("p4_client") == name or ("path" in f and same_dir(root, f["path"]))
            for f in ext.values()
        ):
            continue
        key = "p4_" + _derive_key(name)
        if key == "p4_":
            continue
        if key in ext or key in claimed:
            print(
                f"{_PROG}: WARNING: external_workspaces.{key} already in use, skipping {name}",
                file=sys.stderr,
            )
            continue
        claimed[key] = name
        pending.append((key, name, _norm(root)))

    if check_only:
        for key, _name, root in pending:
            print(f"{_PROG}: would register external_workspaces.{key} = {root}")
        return
    if pending and _refused() is not None:
        print(f"{_PROG}: skipped, machine mutation disabled")
        return

    written: List[str] = []
    for key, name, root in pending:
        base = f"{_EXT_PREFIX}{key}"
        print(f"{_PROG}: registering external_workspaces.{key} = {root}")
        try:
            for field, value in (
                ("path", root),
                ("vcs", "p4"),
                ("kind", "external-readonly"),
                ("p4_client", name),
            ):
                registry_set(f"{base}.{field}", value)
        except (OSError, ValueError):
            print(f"{_PROG}: WARNING: failed to register {base} -- skipping.", file=sys.stderr)
        else:
            written.append(key)

    entries = tuple(
        WriteSurfaceEntry(kind="machine-local-key", key=f"{_EXT_PREFIX}{key}.{field}")
        for key in written
        for field in ("path", "vcs", "kind", "p4_client")
    )
    record_resolution(WRITE_SURFACE.writer_id, _SHAPED_CLAUSE_INDEX, entries)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
