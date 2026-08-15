"""
coordinator_core.ops._app_session_runtime — runtime-kind resolvers for app-session.

Purpose: given a launch config dict (one entry under the `app_session:` nested
mapping read by `cs_read_local_md_mapping`) and a `runtime` kind string, resolve
the argv the caller should spawn. Called by `coordinator_core.ops.app_session`'s
`launch` op (C3) — this module does resolution only, never spawns.

Spec backlink: docs/plans/2026-08-15-app-session-launch-census-teardown-ops.md,
    chunk C2 (source_memo: 2026-08-15-doe-claude-em-launch-ops-amendment-runtime-axis-and-mapping-reader.md)

Shape: a resolver REGISTRY keyed by runtime kind, with `electron` as the first
entry — not the mechanism. The registry exists because the op family was born
with a runtime-kind axis rather than growing one later; adding a second kind is
"add a function, add a registry entry," never a rewrite of `launch`.

An unrecognised OR ABSENT runtime kind degrades to the PLAIN-ARGV fallback —
this is both the generic case and the simplest one, and it means a repo that
just wants "run this command and track it" needs zero resolver machinery.

Negative-spec:
    - Does NOT spawn anything. Resolution only — argv out, no `subprocess.run`.
    - Does NOT auto-download a missing Electron binary (Hard constraint 3). The
      JS original (example-cockpit-repo `run-desktop`) `spawnSync`s a download here;
      this port reports `electron not installed at <path>` and stops. A census
      call silently pulling ~100MB is a surprise, and this is the one place the
      port deliberately diverges from the JS original.
    - Does NOT add `electron` as a dependency of THIS repo to resolve it — that
      would resolve OUR electron (different version, different native ABI), a
      green result from the wrong anchor, which is the failure mode here, not
      an error (Hard constraint 5).
    - Does NOT split/redact the fallback command string beyond
      `win_safe_shlex_split` — the shared spawn substrate (Hard constraint 2),
      reused here rather than re-implemented, since resolving a plain-argv
      fallback command means turning its config string into argv.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from coordinator.bin.lib.win_argv import win_safe_shlex_split


@dataclass
class ResolvedRuntime:
    """Result of resolving a launch config's runtime kind to a spawnable argv.

    Exactly one of (`argv` non-None) or (`error` non-None) holds on return —
    never both, never neither. Callers distinguish "resolved" from "not
    installed" (or any other resolution failure) by checking `ok`, never by
    truthiness of `argv` alone (an empty-but-non-None argv is not a valid
    resolved state and never produced by any resolver here).
    """

    ok: bool
    argv: Optional[List[str]] = None
    error: Optional[str] = None
    binary: Optional[str] = None


# Resolver signature: (config: dict, repo_root: str) -> ResolvedRuntime
RuntimeResolver = Callable[[dict, str], ResolvedRuntime]


def _resolve_plain_argv(config: dict, repo_root: str) -> ResolvedRuntime:
    """Generic fallback resolver — NO resolution step, just argv-ify `command`.

    This is what an unrecognised or absent runtime kind degrades to. Reads
    `config["command"]` (a shell-shaped string, e.g. "pnpm dev") and splits it
    with `win_safe_shlex_split` — the shared spawn substrate (Hard constraint
    2), not a bespoke split. An absent or blank `command` is a resolution
    failure, reported the same way a missing binary is: `ok=False` with a
    structured `error`, never a raised exception.
    """
    command = config.get("command") if isinstance(config, dict) else None
    if not command or not str(command).strip():
        return ResolvedRuntime(ok=False, error="no command configured")
    argv = win_safe_shlex_split(str(command))
    if not argv:
        return ResolvedRuntime(ok=False, error="no command configured")
    return ResolvedRuntime(ok=True, argv=argv)


def _electron_binary_path(repo_root: str) -> Path:
    """Resolve the electron binary path for the CONSUMING repo `repo_root`.

    Precedence (Hard constraint 4, read from `electron/index.js` at source by
    the peer):
      1. `ELECTRON_OVERRIDE_DIST_PATH` env var, if set — takes precedence over
         `path.txt` unconditionally. Honouring only `path.txt` silently
         resolves the wrong binary, which is a wrong success, not an error.
      2. `<repo_root>/node_modules/electron/path.txt` — the file's contents
         (one line, the binary's path relative to the `electron/` package
         dir) named `<repo_root>/node_modules/electron/<contents>`.

    Never anchors on this file's own location (Hard constraint 6) — always
    the CONSUMING repo's `node_modules`, resolved by the caller via
    `git_root_zero_spawn` and passed in as `repo_root`.
    """
    override = os.environ.get("ELECTRON_OVERRIDE_DIST_PATH")
    if override:
        return Path(override)

    electron_pkg = Path(repo_root) / "node_modules" / "electron"
    path_txt = electron_pkg / "path.txt"
    rel = path_txt.read_text(encoding="utf-8").strip()
    return electron_pkg / rel


def _resolve_electron(config: dict, repo_root: str) -> ResolvedRuntime:
    """Electron runtime resolver — first entry in the registry, not the
    mechanism (see module docstring).

    Resolves the electron binary per `_electron_binary_path` (following
    symlinks — pnpm layouts store the real package elsewhere, so the
    existence/realpath check below MUST resolve through a symlinked
    `node_modules/electron`), then builds argv as `[binary] + extra_args`
    where `extra_args` comes from `config["args"]` (a list) or, if absent,
    `config["command"]` split via `win_safe_shlex_split`.

    On a missing binary: returns `ok=False` with
    `error="electron not installed at <path>"` and does NOT attempt any
    download (Hard constraint 3) — the one place this port deliberately
    diverges from the JS original, which `spawnSync`s a download here.
    """
    try:
        binary = _electron_binary_path(repo_root)
    except (OSError, ValueError):
        # path.txt missing, unreadable, or containing invalid UTF-8
        # (UnicodeDecodeError is a ValueError, not an OSError) — same
        # terminal outcome as a missing binary: report and stop, never
        # download. Matches the fail-closed style already used in
        # `_handle_is_live` (app_session.py); resolve_runtime's own
        # docstring promises "never raises" and a raw UnicodeDecodeError
        # here would break that contract.
        missing = Path(repo_root) / "node_modules" / "electron" / "path.txt"
        return ResolvedRuntime(
            ok=False, error=f"electron not installed at {missing}"
        )

    # Follow symlinks (pnpm layouts store the real package elsewhere) before
    # deciding whether the binary exists.
    resolved_binary = binary
    try:
        resolved_binary = binary.resolve(strict=False)
    except OSError:
        pass

    if not resolved_binary.is_file():
        return ResolvedRuntime(ok=False, error=f"electron not installed at {binary}")

    extra_args: List[str]
    config_args = config.get("args") if isinstance(config, dict) else None
    if isinstance(config_args, list):
        extra_args = [str(a) for a in config_args]
    else:
        command = config.get("command") if isinstance(config, dict) else None
        extra_args = win_safe_shlex_split(str(command)) if command else []

    argv = [str(resolved_binary)] + extra_args
    return ResolvedRuntime(ok=True, argv=argv, binary=str(resolved_binary))


# Registry keyed by runtime kind. `electron` is the FIRST resolver added, not
# the mechanism — the whole point of this shape is that the runtime-kind axis
# was there from the start, not grown on later. Extend by adding a function
# plus one entry here; never inline a new kind's logic at a call site.
RUNTIME_RESOLVERS: Dict[str, RuntimeResolver] = {
    "electron": _resolve_electron,
}


def resolve_runtime(kind: Optional[str], config: dict, repo_root: str) -> ResolvedRuntime:
    """Resolve a launch config's runtime kind to a spawnable argv.

    `kind` absent (None/empty) OR not a key in `RUNTIME_RESOLVERS` degrades to
    the plain-argv fallback (`_resolve_plain_argv`) — no resolution step,
    just `config["command"]` split into argv. This is deliberate: it is both
    the generic case and the simplest one, so a repo that just wants "run
    this command and track it" needs no resolver at all.

    `repo_root` is the CONSUMING repo's root (resolved by the caller via
    `coordinator_core.ops._git_root_util.git_root_zero_spawn`) — never this
    file's own location (Hard constraint 6).

    Never raises. Every failure path returns `ResolvedRuntime(ok=False,
    error=...)`.
    """
    resolver = RUNTIME_RESOLVERS.get(kind) if kind else None
    if resolver is None:
        return _resolve_plain_argv(config, repo_root)
    return resolver(config, repo_root)
