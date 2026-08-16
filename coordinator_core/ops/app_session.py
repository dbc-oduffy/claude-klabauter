"""
coordinator_core.ops.app_session — JSON-RPC "app_session.launch" /
".census" / ".teardown" operations.

Purpose: start, inspect, and stop a local dev app in whatever CONSUMING repo
the operator is sitting in. This module owns the mechanism half of the
app-session skill; the skill/verb surface itself belongs to a peer repo (see
Anti-scope below) and is waiting on these entrypoint names.

Port lineage: ~900 lines of measured JavaScript from example-cockpit-repo's
`run-desktop`. The PM accepted the CAPABILITY and rejected the FORM (naked
Python, not a second JS runtime) — nothing here is a new design, it is a
translation plus one genuinely new config primitive
(`cs_read_local_md_mapping`, C1).

Spec backlink: docs/plans/2026-08-15-app-session-launch-census-teardown-ops.md § C3

Self-registration: importing this module calls register_op("app_session.launch",
...), register_op("app_session.census", ...), and register_op(
"app_session.teardown", ...) as a side-effect. Add this module to
coordinator_core/ops/_registry_map.py AND coordinator_core/ops/__init__.py —
both paths must reach it or registration becomes import-order dependent (see
coordinator_core/hooks/__init__.py for the drift-guard failure that shape
produces when only one path is updated).

Reuse, never re-implement (Hard constraint 2 — this is the finding that
shrank the whole plan):
  - `win_argv.win_safe_shlex_split` (coordinator/bin/lib/win_argv.py) for all
    argv parsing. Never bare `shlex.split`, never `shell=True`, no `VAR=`
    prefix shim (PM ruling 2026-08-06).
  - `redact_for_diag` / `metachar_warn` (coordinator_core.resolve_validation_cmd)
    for diagnostic redaction and the metachar advisory tripwire — a launch
    command is a spawned process built from operator-authored config, so both
    matter MORE here than at the ceremony seam, not less.
  - `no_console_creationflags()` (coordinator_core.win_portability) for
    Windows console-popup suppression — never a hand-rolled CREATE_NO_WINDOW.
  - `stable_pid_alive()` (coordinator_core.session.core) for the PID +
    second-identity-signal (create_time epoch) re-validation this whole
    plan exists to enforce (PIDs are recycled on Windows; a bare PID check
    would happily report a stranger's process as ours). This is the SAME
    primitive session liveness already uses — not a new implementation.
See coordinator/bin/coordinator-ceremony-hook.py for how the first three
compose today; it owns none of them itself.

Persisted-handle substrate: untracked, per-consuming-repo, mirrors the
`.git/coordinator-sessions/` convention (coordinator_core/ops/session/reap.py)
rather than living under the repo's tracked `state/` tree — a launch handle
is ephemeral process-liveness data, not durable substrate, and must never be
committed. One JSON file per launch-target key under
`<git-common-dir>/coordinator-app-sessions/<key>.json`:

    {
      "key": "<target key, e.g. 'desktop'>",
      "pid": <int>,
      "start_epoch": <int, psutil create_time() at spawn>,
      "argv": [<str>, ...],
      "cwd": "<str, spawn cwd>",
      "launched_at": "<ISO-8601 UTC timestamp>"
    }

`pid` ALONE is never sufficient to identify "our" process later — it is
paired with `start_epoch` (captured at spawn time via
`psutil.Process(pid).create_time()`) as the second identity signal that
`stable_pid_alive()` re-validates against on every census/teardown read.

Two deliberately isolated decision points (DP-1, DP-2 — see their own
predicates below): each lands as ONE named function, never inlined at a
call site, per PM ruling — so that when the peer's forthcoming contract
lands, updating it is a localised edit, not a re-cut.

Anti-scope (do NOT do here):
  - Build the skill surface, verb taxonomy, or invocation lines — peer's C3/C4.
  - Merge/reconcile the resolve_validation_cmd twins.
  - "Fix" git_root() to be zero-spawn — a sibling exists (_git_root_util).
  - Reap/census a process this op family did not start.

Negative-spec:
  - NEVER `shell=True`. NEVER a bare `shlex.split`. NEVER a `VAR=` prefix
    shim to fake env-setting through argv.
  - NEVER auto-download a missing runtime (Hard constraint 3) — that is the
    JS original's `_app_session_runtime` electron resolver's own concern,
    not this module's, but this module never works around a resolver's
    "not installed" report by trying anything else.
  - NEVER blanket-kill by process name in teardown — a shared runtime may
    belong to one of the 50-70 other concurrent agent sessions on this
    machine. Only PID+second-signal-verified, persisted-handle-owned
    processes are ever signalled.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._app_session_runtime import resolve_runtime
from coordinator_core.ops._git_root_util import git_root_zero_spawn
from coordinator_core.resolve_validation_cmd import (
    cs_read_local_md_mapping,
    metachar_warn,
    redact_for_diag,
)
from coordinator_core.win_portability import no_console_creationflags

try:
    from coordinator.bin.lib.win_argv import win_safe_shlex_split
except ImportError:  # pragma: no cover — sys.path bootstrap fallback
    import sys as _sys

    # C10 (staff-eng review finding 8): this block runs once, at module
    # import time — Python's import lock guarantees no concurrent second
    # run of this exact block in one process, so no refcounting is needed
    # here (contrast doctor.py's `_sys_path_push`/`_sys_path_pop`, which
    # guards a function called repeatedly and concurrently). The prior
    # shape inserted `_lib_dir` and never removed it, permanently widening
    # `sys.path` for the rest of the process's lifetime — under warm,
    # long-lived dispatch a LATER, unrelated import elsewhere in the
    # process could then silently resolve against this directory it never
    # asked for. Fixed additively: pop the entry again once this module's
    # own bootstrap import has resolved, leaving `sys.path` exactly as this
    # module found it.
    _lib_dir = str(Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "lib")
    _lib_dir_already_present = _lib_dir in _sys.path
    if not _lib_dir_already_present:
        _sys.path.insert(0, _lib_dir)
    try:
        from win_argv import win_safe_shlex_split  # type: ignore
    finally:
        if not _lib_dir_already_present:
            try:
                _sys.path.remove(_lib_dir)
            except ValueError:
                pass  # already absent — nothing to restore

APP_SESSION_CONFIG_KEY = "app_session"
_HANDLE_DIRNAME = "coordinator-app-sessions"


# --- DP-1: env rung -----------------------------------------------------


def _app_session_env_rung(key: str) -> Optional[str]:
    """DP-1 — whether app_session launch keys inherit an env-var override
    rung, mirroring `fast_test_cmd`'s COORDINATOR_FAST_TEST_CMD top rung.

    DEFAULT (until the peer's contract lands): NO env rung — this function
    always returns None. This is a DEVIATION from `fast_test_cmd` semantics,
    not a copy of them: `fast_test_cmd`'s env rung runs verbatim and
    unsanitized because its value is handed to a shell as a test command;
    an app_session launch key's value becomes a SPAWNED PROCESS (argv-only,
    Hard constraint 1), so unconditionally honouring an arbitrary env
    override here would hand an unaudited, non-repo-committed command
    straight to CreateProcess/execve. The peer's forthcoming plan is
    explicit that whether launch keys get an env rung at all is theirs to
    settle — this predicate is the single, localised edit point for that
    decision when it lands; nothing else in this module inlines the choice.
    """
    return None


# --- DP-2: absent-key branch --------------------------------------------


def _absent_key_result(op: str, key: str, reason: str) -> dict:
    """DP-2 — the absent-key branch: an app_session verb invoked in a repo
    (or against a target key) that never opted in.

    DEFAULT (until the peer's contract lands): a QUIET no-op returning a
    structured "not configured" result — never a raised error, and never a
    silent success. This is a DEVIATION from `fast_test_cmd` semantics, not
    a copy of them: `fast_test_cmd`'s absent-key case fails LOUD
    (FAST-TEST-CMD-MISSING-CHECK, DR-142 — silent skip is named there as the
    anti-pattern to avoid), because a repo that silently skips validation is
    a coverage hole. A launch/census/teardown verb invoked against a repo
    that never configured `app_session` is a different case: it is not a
    coverage gap, it is simply "there is nothing here to do," so the
    structured `configured: false` result lets a caller distinguish
    "nothing to do" from "it broke" without treating either as success OR
    failure. The peer's forthcoming contract supersedes this default; this
    predicate is its single, localised landing point.
    """
    return {"ok": True, "configured": False, "op": op, "key": key, "reason": reason}


# --- persisted-handle substrate -----------------------------------------


def _handle_dir(repo_root: str) -> Path:
    """Handle store, resolved via the git COMMON dir rather than <root>/.git.

    In a linked worktree `<root>/.git` is a FILE (a gitdir: pointer), so a naive
    join produces a path that cannot be mkdir'd — the same directory-vs-file trap
    the consuming-repo root resolver guards. `lifecycle.git_common_dir` already
    resolves this correctly, and does so WITHOUT spawning — it goes through the
    `coordinator_core.git.repo_root` seam's pure-Python `.git` walk, reaching
    `git rev-parse` only when no `.git` entry exists anywhere above `repo_root`
    (a case in which this op has already failed to resolve a repo root at all).
    Hard constraint 7's zero-spawn property therefore holds through this call,
    not just up to it.

    Keying on the common dir is also the behaviour we want: a launch from one
    worktree and a teardown from a sibling worktree must see the same handles, or
    teardown silently leaves the process running.
    """
    from coordinator_core.lifecycle import git_common_dir

    return git_common_dir(Path(repo_root)) / _HANDLE_DIRNAME


def _handle_path(repo_root: str, key: str) -> Path:
    return _handle_dir(repo_root) / f"{key}.json"


def _write_handle(repo_root: str, key: str, handle: dict) -> None:
    d = _handle_dir(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    tmp = _handle_path(repo_root, key).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(handle, indent=2), encoding="utf-8")
    tmp.replace(_handle_path(repo_root, key))


def _read_handle(repo_root: str, key: str) -> Optional[dict]:
    p = _handle_path(repo_root, key)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _remove_handle(repo_root: str, key: str) -> None:
    p = _handle_path(repo_root, key)
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def _list_handles(repo_root: str) -> list:
    d = _handle_dir(repo_root)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _handle_is_live(handle: dict) -> bool:
    """Re-validate a persisted handle's PID against its second identity
    signal (create_time epoch) before trusting it — PIDs recycle on
    Windows, so a bare PID check would happily report a stranger's process
    as ours. Reuses `stable_pid_alive()` (coordinator_core.session.core),
    the SAME primitive session liveness already uses; not re-implemented
    here (Hard constraint 8).
    """
    from coordinator_core.session.core import stable_pid_alive

    pid = handle.get("pid")
    start_epoch = handle.get("start_epoch")
    if pid is None:
        return False
    try:
        return stable_pid_alive(pid, stored_start_epoch=str(start_epoch or ""))
    except Exception:
        # Fail-closed-to-not-live: an ambiguous/erroring liveness read is
        # not a green light to report a possibly-stranger process as ours.
        return False


# --- config resolution ----------------------------------------------------


def _resolve_repo_root(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    return git_root_zero_spawn()


def _resolve_target_config(repo_root: str, key: str) -> Optional[dict]:
    mapping = cs_read_local_md_mapping(repo_root, APP_SESSION_CONFIG_KEY)
    if not mapping or key not in mapping:
        return None
    target = mapping[key]
    if not isinstance(target, dict):
        return None
    return target


def _resolve_argv(repo_root: str, key: str, target_config: dict) -> "tuple[Optional[list], Optional[str]]":
    """Resolve the argv to spawn for a target config.

    Delegates runtime resolution (electron vs. plain-argv fallback) to C2's
    `coordinator_core.ops._app_session_runtime.resolve_runtime(kind, config,
    repo_root) -> ResolvedRuntime` (`.ok`, `.argv`, `.error` — exactly one of
    `argv`/`error` set on return, never raises). That module already routes
    its own argv-ification through `win_safe_shlex_split` (Hard constraint 2)
    for both the electron and plain-argv resolvers, so this call site does
    not re-split anything.

    Does NOT run the metachar advisory tripwire itself — `_launch` runs it
    once, on the final argv, after BOTH this path and the DP-1 env-override
    path have converged and before the process is spawned. A tripwire called
    once per branch here and once again on DP-1's own argv would be two call
    sites that can silently drift apart the moment either branch changes;
    see `_launch`'s own docstring note on this.
    """
    kind = target_config.get("runtime")
    resolution = resolve_runtime(kind, target_config, repo_root)
    if resolution.ok and resolution.argv:
        return resolution.argv, None
    return None, resolution.error or f"app_session.{key}: runtime resolution failed"


# --- app_session.launch ---------------------------------------------------


@register_op("app_session.launch")
def _launch(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "app_session.launch" handler.

    Params:
      key         (str, required) — the app_session.<key> config target.
      repo_root   (str, optional) — consuming repo root override; defaults
                  to the zero-spawn resolver (Hard constraint 6/7).

    Resolves config via C1's `cs_read_local_md_mapping`, resolves the
    runtime via C2, spawns argv-only in the consuming repo, and persists a
    handle recording the PID plus a second identity signal (create_time
    epoch) so a later census/teardown never trusts a bare, recyclable PID.
    """
    key = params.get("key")
    if not key:
        return {"ok": False, "error": "app_session.launch requires 'key'"}

    root = _resolve_repo_root(params.get("repo_root") or (str(repo_root) if repo_root else None))
    if root is None:
        return {"ok": False, "error": "could not resolve consuming repo root"}

    target_config = _resolve_target_config(root, key)
    if target_config is None:
        return _absent_key_result(
            "app_session.launch", key, f"no app_session.{key} entry in coordinator.local.md"
        )

    env_override = _app_session_env_rung(key)  # DP-1 — always None today
    if env_override:
        argv = win_safe_shlex_split(env_override)
    else:
        argv, error = _resolve_argv(root, key, target_config)
        if error:
            return {"ok": False, "error": error}

    # A resolver returning neither argv nor error is a contract breach, not a
    # spawnable state — refuse here rather than letting Popen(None) surface it
    # as an unrelated TypeError from inside subprocess.
    if not argv:
        return {
            "ok": False,
            "error": f"runtime resolution for {key!r} produced no command",
        }

    # Metachar advisory tripwire (Hard constraint 2) — fires ONCE, on the
    # final argv, after BOTH the DP-1 env-override branch and the normal
    # _resolve_argv branch have converged and before the process is spawned.
    # A single post-convergence call site is deliberate: a tripwire honoured
    # on the branch that was named (_resolve_argv) and silently missed on
    # the branch that wasn't (DP-1) is exactly the constraint-leakage shape
    # captured in
    # state/lessons/2026-08-15-a-constraint-written-against-one-function-is-not-enforced-anywhere-else.yaml —
    # DP-1 is dead today (always returns None) but must not repeat that
    # shape the moment the peer's env-rung contract makes it live.
    metachar_warn(" ".join(argv), "app-session-argv", caller="app_session.launch")

    # Whole-command redaction, not per-token: `redact_for_diag` truncates
    # past 60 chars to bound secret leakage in a command STRING; applied
    # per-token, an embedded secret (e.g. a single `--token=...` argv
    # element) is almost never long enough to trip that truncation and
    # would leak into `argv_redacted` (and onward into review-trail JSON /
    # session transcripts) effectively unredacted.
    redacted_cmd = redact_for_diag(" ".join(argv))

    import subprocess

    proc = subprocess.Popen(
        argv,
        cwd=root,
        **no_console_creationflags(),
    )

    start_epoch = None
    try:
        import psutil

        start_epoch = int(psutil.Process(proc.pid).create_time())
    except Exception:
        start_epoch = None

    handle = {
        "key": key,
        "pid": proc.pid,
        "start_epoch": start_epoch,
        "argv": argv,
        "cwd": root,
        "launched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_handle(root, key, handle)

    return {
        "ok": True,
        "configured": True,
        "key": key,
        "pid": proc.pid,
        "argv_redacted": redacted_cmd,
    }


# --- app_session.census ---------------------------------------------------


@register_op("app_session.census")
def _census(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "app_session.census" handler.

    Enumerates from persisted handle state (never from in-memory session
    state), so it naturally finds orphans left by sessions that have
    already died — session death does not reap spawned workers, and a
    persisted handle survives its launching session regardless. Each
    recorded PID is re-validated against its stored second identity signal
    before being reported live (see `_handle_is_live`).
    """
    root = _resolve_repo_root(params.get("repo_root") or (str(repo_root) if repo_root else None))
    if root is None:
        return {"ok": False, "error": "could not resolve consuming repo root"}

    key = params.get("key")
    if key is not None:
        handle = _read_handle(root, key)
        if handle is None:
            return _absent_key_result("app_session.census", key, f"no persisted handle for key {key!r}")
        return {
            "ok": True,
            "configured": True,
            "sessions": [
                {
                    "key": handle.get("key"),
                    "pid": handle.get("pid"),
                    "live": _handle_is_live(handle),
                    "launched_at": handle.get("launched_at"),
                }
            ],
        }

    handles = _list_handles(root)
    sessions = [
        {
            "key": h.get("key"),
            "pid": h.get("pid"),
            "live": _handle_is_live(h),
            "launched_at": h.get("launched_at"),
        }
        for h in handles
    ]
    return {"ok": True, "configured": True, "sessions": sessions}


# --- app_session.teardown --------------------------------------------------


@register_op("app_session.teardown")
def _teardown(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "app_session.teardown" handler.

    Reaps ONLY what this op family started — a persisted handle whose PID
    re-validates against its own recorded second identity signal — and
    NEVER by process-name match (Hard constraint 8): a shared runtime may
    belong to one of the 50-70 other concurrent agent sessions on this
    machine. Releases the handle (its "lock") even when the process is
    already gone, so a dead-process handle never wedges a later launch.
    """
    key = params.get("key")
    if not key:
        return {"ok": False, "error": "app_session.teardown requires 'key'"}

    root = _resolve_repo_root(params.get("repo_root") or (str(repo_root) if repo_root else None))
    if root is None:
        return {"ok": False, "error": "could not resolve consuming repo root"}

    handle = _read_handle(root, key)
    if handle is None:
        return _absent_key_result("app_session.teardown", key, f"no persisted handle for key {key!r}")

    was_live = _handle_is_live(handle)
    if was_live:
        pid = handle.get("pid")
        try:
            import psutil

            # Re-compare create_time() immediately before signalling — the
            # earlier `_handle_is_live` check and this `.terminate()` call
            # are separated by a window in which the validated PID could
            # die and be recycled by an unrelated process (this machine
            # runs 50-70 concurrent sessions). A stale re-use of the
            # `_handle_is_live` result across that window is exactly the
            # TOCTOU this re-check closes.
            proc = psutil.Process(pid)
            start_epoch = handle.get("start_epoch")
            if start_epoch is None or int(proc.create_time()) == int(start_epoch):
                proc.terminate()
        except Exception:
            pass

    # Release the lock (remove the handle) unconditionally — even when the
    # process was already gone, a stale handle must never wedge a later
    # launch of the same key.
    _remove_handle(root, key)

    return {
        "ok": True,
        "configured": True,
        "key": key,
        "was_live": was_live,
        "reaped": was_live,
    }
