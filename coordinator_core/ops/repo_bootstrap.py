"""
coordinator_core.ops.repo_bootstrap — JSON-RPC "repo.clone_and_register" operation.

Purpose: one coherent bootstrap sequence for bringing a sibling repo onto this
machine and into the machine-local `repos.*` registry in a single call:
check (registry + on-disk) -> clone (delegates to
`coordinator_core.install.clone_sibling_repo.clone_idempotent`) -> register
(machine-local `set`) -> confirm (machine-local `get` read-back). Port of the
fence at `templates/handoffs/install-claude-klabauter.md:86` (clone claude-klabauter to a
sibling path and register it via machine-local set, guarded by a prior
machine-local get check so an already-registered/on-disk repo is skipped).

Op-key / contract (state/audits/2026-07-22-command-payload-inventory/
op-classification.tsv, row `clone-and-register-sibling-repo`):
    params:   {repo_key: str, clone_url: str, dest_path: str}
    response: {cloned: bool, registered: bool, path: str, already_present: bool}
scope-verdict: none — this op operates on a sibling repo path and the
OPERATOR'S OWN machine-local registry, neither of which is the caller's own
dispatching tree (matches the `plugin_health.*` / `register_discovered_repos`
precedent for operator-machine-scoped ops; no `_origin_worktree` injection
applies).

Composition, not duplication: this module owns only the check -> register ->
confirm wrapper around clone. The clone step itself (`.git`-directory
existence guard -> `git clone`) is
`coordinator_core.install.clone_sibling_repo.clone_idempotent`, built as a
disjoint Wave 2 chunk (w2-clone-sibling) and referenced here by import, not
reimplemented — one implementation of the clone-idempotency guard, not two.

Idempotency (AC7 — oracle-rated idempotency-hazard "none", closed by the
oracle's own guard design, not by this port): a second invocation with
identical `repo_key`/`clone_url`/`dest_path` is a safe no-op. The op reads
BOTH signals before doing any work — `machine-local get <repo_key>` (already
registered?) and `Path(dest_path in .git).is_dir()` (already on disk?) — and
short-circuits to a vacuous no-op (`cloned=False, registered=False,
already_present=True`) only when BOTH are already true. This is deliberately
narrower than "either is true": a repo present on disk but never registered
(e.g. a pre-existing manual clone) still completes registration on this call;
a registered key whose target directory has since vanished still re-clones.
Neither of those two partial-state repairs re-runs the OTHER half of the
work, so nothing is redone that is already correct — each of clone and
register is independently idempotent, and their conjunction is too.

Failure posture — fail loud, not advisory-skip: unlike
`register_discovered_repos` (a best-effort install-time offer with a
never-block contract), this op's whole job is the guarantee "the repo is
cloned AND registered when this returns 200". An unresolvable machine-local
CLI, a `machine-local set` failure, or a post-register confirm mismatch each
raise `RepoBootstrapError` rather than silently degrading — a caller that
gets a clean return can trust both halves happened (or were already true).

Machine-local CLI resolution: PATH first (`shutil.which`), then
`${CLAUDE_HOME:-$HOME}/.claude/bin/machine-local` — the same two-rung ladder
`coordinator_core.ops.register_discovered_repos._resolve_machine_local` uses,
duplicated locally (not imported) because that function's second parameter
(`self_dir`, a DoE-trampoline-supplied sibling-file directory) has no meaning
here; this op has no DoE-side trampoline in its call chain. Per the plan's
mandated-resolvers table, a `repos.*` key is not the `coordinator.*` hot path,
so resolution goes through the `machine-local` CLI (canonical), never the
direct-TOML `machine_resolver.registry_get` short-circuit.

Spec backlink: pln-coordinator-ops-buildout-from--903224 § Wave 2
Oracle backlink: state/audits/2026-07-22-command-payload-inventory/{op-classification,distinct-ops-new}.tsv
Self-registration: importing this module calls
`register_op("repo.clone_and_register", ...)` as a side effect (same pattern
as `ops/ping.py`). Wiring into `coordinator_core/ops/__init__.py`'s
`_EAGER_OP_MODULES` and `coordinator_core/ops/_registry_map.py` is a separate
serial tail-pass concern, not this chunk's.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import home_dir, normalize_native_path, settings_home
from coordinator_core.win_portability import is_executable, no_console_creationflags
from coordinator_core.install.clone_sibling_repo import (
    CloneSiblingRepoError,
    clone_idempotent,
)
from coordinator_core.ipc import register_op


_CREATIONFLAGS = no_console_creationflags()

_MACHINE_LOCAL_TIMEOUT_SECS = 15


class RepoBootstrapError(RuntimeError):
    """Raised when a required step of the check -> clone -> register ->
    confirm sequence cannot complete: the `machine-local` CLI is
    unresolvable, `git clone` itself fails (surfaced from
    `CloneSiblingRepoError`), a `machine-local set` call fails, or the
    post-register confirm read-back does not match what was written."""


def _resolve_machine_local_bin() -> Optional[str]:
    """PATH first, then `<settings-home>/bin/machine-local`, then
    `${CLAUDE_HOME:-$HOME}/.claude/bin/machine-local`.
    Returns None (never raises) if unresolvable — the caller decides whether
    that is fatal."""
    found = shutil.which("machine-local")
    if found:
        return found

    settings_home_candidate = settings_home() / "bin" / "machine-local"
    if settings_home_candidate.is_file() and is_executable(settings_home_candidate):
        return str(settings_home_candidate)

    fallback = home_dir() / ".claude" / "bin" / "machine-local"
    if fallback.is_file() and is_executable(fallback):
        return str(fallback)
    return None


def _machine_local_get(machine_local_bin: str, key: str) -> Optional[str]:
    """`machine-local get <key>` — returns the stripped stdout value, or None
    on any failure (missing key, non-zero exit, timeout, or a spawn error).
    A None return means "treat as not registered", never "error"."""
    try:
        proc = subprocess.run(
            [machine_local_bin, "get", key],
            capture_output=True,
            text=True,
            timeout=_MACHINE_LOCAL_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _machine_local_get: proc = subprocess.run(...) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    value = (proc.stdout or "").strip()
    return value or None


def _machine_local_set(machine_local_bin: str, key: str, value: str) -> bool:
    """`machine-local set <key> <value>` — returns whether the call
    succeeded (rc==0); never raises."""
    try:
        proc = subprocess.run(
            [machine_local_bin, "set", key, value],
            capture_output=True,
            text=True,
            timeout=_MACHINE_LOCAL_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _machine_local_set: proc = subprocess.run(...) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return proc.returncode == 0


def clone_and_register_sibling_repo(repo_key: str, clone_url: str, dest_path: str) -> dict:
    """Check -> clone -> register -> confirm.

    Returns ``{"cloned": bool, "registered": bool, "path": str,
    "already_present": bool}``. ``already_present`` is a whole-op-level flag:
    True only in the vacuous-no-op case (repo key already registered AND the
    destination already has a `.git` directory) — it is False whenever either
    half of this call actually did something, even if the OTHER half was a
    partial-state repair with nothing to do.

    Raises `RepoBootstrapError` on any hard failure — see module docstring
    § Failure posture.
    """
    target = normalize_native_path(dest_path)

    machine_local_bin = _resolve_machine_local_bin()
    if machine_local_bin is None:
        raise RepoBootstrapError(
            "repo.clone_and_register requires the machine-local CLI on PATH "
            "or at $CLAUDE_HOME/.claude/bin/machine-local — none found"
        )

    already_registered = _machine_local_get(machine_local_bin, repo_key) is not None
    already_on_disk = (target / ".git").is_dir()

    if already_registered and already_on_disk:
        return {
            "cloned": False,
            "registered": False,
            "path": str(target),
            "already_present": True,
        }

    try:
        clone_result = clone_idempotent(clone_url, str(target))
    except CloneSiblingRepoError as exc:
        raise RepoBootstrapError(str(exc)) from exc

    registered = False
    if not already_registered:
        if not _machine_local_set(machine_local_bin, repo_key, clone_result["path"]):
            raise RepoBootstrapError(
                f"repo.clone_and_register: cloned {repo_key!r} to "
                f"{clone_result['path']!r} but 'machine-local set' failed — the "
                "repo is on disk but unregistered; re-run to retry registration "
                "(the clone step is idempotent and will be skipped on retry)"
            )
        confirmed = _machine_local_get(machine_local_bin, repo_key)
        if confirmed != clone_result["path"]:
            raise RepoBootstrapError(
                f"repo.clone_and_register: post-register confirm mismatch for "
                f"{repo_key!r} — expected {clone_result['path']!r}, read back "
                f"{confirmed!r}"
            )
        registered = True

    return {
        "cloned": clone_result["cloned"],
        "registered": registered,
        "path": clone_result["path"],
        "already_present": False,
    }


@register_op("repo.clone_and_register")
def _clone_and_register_sibling_repo_op(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "repo.clone_and_register" handler.

    Params:
        repo_key (str, required) — the machine-local `repos.<key>` registry
        key this repo should be addressable under.
        clone_url (str, required) — the source to clone.
        dest_path (str, required) — the destination directory on this machine.

    `repo_root` (injected by ipc.dispatch_message) is unused — this op's
    scope-verdict is "none" (see module docstring); the signature is kept for
    dispatch-shape consistency with scoped ops (same as `ping`).
    """
    repo_key = params.get("repo_key")
    clone_url = params.get("clone_url")
    dest_path = params.get("dest_path")
    if not repo_key or not clone_url or not dest_path:
        raise ValueError(
            "repo.clone_and_register requires 'repo_key', 'clone_url', and "
            "'dest_path' params"
        )
    return clone_and_register_sibling_repo(repo_key, clone_url, dest_path)


if __name__ == "__main__":
    print("repo_bootstrap: this module is a registered JSON-RPC op (repo.clone_and_register), not a CLI entrypoint.", file=sys.stderr)
    sys.exit(1)
