"""coordinator_core.hooks.sweep_boot — SessionStart(startup|compact) op:
forwarder self-heal, orientation-cache self-heal, and cadence-gated session
reap.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/sweep-boot.py` — a doctrine-plane
TRAMPOLINE whose whole job was resolving a sibling claude-klabauter checkout and
subprocess-exec'ing into it. Every leg it trampolined into is now a same-repo
sibling call, so the subprocess machinery this row exists to remove (Popen,
process-group kill, jitter, timeout reaping) collapses to plain in-process
calls:

  1. Forwarder self-heal — DoE's copy execed
     `<claude_klabauter_root>/coordinator/bin/sweep-boot.py`, whose OWN docstring
     records that the composite it used to run (`session.boot_sweep`) is a
     GRAVESTONE (killed 2026-08-xx, 30017ms against a 2000ms bar) and that
     CLI now does exactly one thing:
     `coordinator_core.install.forwarder_self_heal.self_heal_forwarders()`.
     Called directly here, best-effort (never affects this op's return).

  2. Orientation-cache self-heal — DoE's copy spawned the
     `regenerate-orientation-cache` forwarder as a detached child with a
     jittered thundering-herd mitigation and a process-group-kill timeout,
     because it was crossing a repo boundary through a resolved CLI.
     `coordinator_core.orientation.regenerate_cache.build_cache`/`write_cache`
     ARE that CLI's own implementation, already same-repo; this port calls
     them directly. `write_cache` already takes `_OrientationCacheLock`
     internally (module docstring "Concurrency"), so the jitter/Popen/
     process-group-kill machinery is DROPPED, not merely simplified — there
     is no subprocess left to time out or kill. The staleness check
     (`_cache_is_stale`/`_read_cache_head`/`_read_current_head`) is ported
     unchanged: it is a plain frontmatter read plus one `git rev-parse HEAD`,
     with no cross-repo dependency either way.

  3. Session reap — DoE's copy execed `reap-sessions.py` as a subprocess with
     a 60s timeout and process-group kill (for the identical repo-boundary
     reason as leg 2). The reap logic is the already-registered
     `session.reap` op (`coordinator_core/ops/session/reap.py`) — called
     in-process via its own handler, which self-gates on the SAME 12h cadence
     marker DoE's copy pre-gated against, so the cheap pre-gate
     (`_session_reap_due`) is kept to avoid paying even an in-process call on
     the overwhelmingly common "not due" boot.

  4. Global-doctrine-mirror derivation — NOT PORTED. DoE's fourth leg
     in-process-imported `derive-global-doctrine-live-copy.py`, a
     doctrine-plane-resident script (its OSS-clobber dev-repo gate, its
     tracked-source/live-copy pair) with no analogue in this engine's own
     footprint and no file in this chunk's `writes:`. Deferred to whichever
     future arrival lands that script, if any — this op never references it.

Op contract: `params` is unused. Every leg is independently exception-isolated
(one leg's failure never skips a sibling leg) and this handler always returns
`no_advisory()` — none of the four legs produces context-bound output;
mirrors the source script's own fully-async, stdout-discarded contract.

Negative-spec:
    Does NOT run `session.boot_sweep` — that op is a gravestone; see leg 1.
    Does NOT spawn a subprocess for the orientation-cache regen or the
    session reap — both are in-process calls now that this op lives inside
    the engine those CLIs themselves wrap.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from coordinator_core._hook_envelope import payload_of
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.ipc import register_op
from coordinator_core.win_portability import no_console_creationflags

_FRONTMATTER_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$")

#: Same numbers `session.reap`'s own `.last-reap` cadence marker uses;
#: deliberately SHORTER than the op's own 12h gate so the op, never this
#: op, stays authoritative on whether a reap runs.
_SESSION_REAP_PREGATE_SECONDS = 11 * 3600

#: Env opt-outs, mirrored from the source script.
_ORIENTATION_SELFHEAL_OFF = "COORDINATOR_ORIENTATION_SELFHEAL_OFF"
_SESSION_REAP_OFF = "COORDINATOR_SESSION_REAP_OFF"


def _resolve_this_repo_root() -> "Optional[str]":
    try:
        return show_toplevel(os.getcwd())
    except Exception:
        return None


def _selfheal_forwarders() -> None:
    if os.environ.get("COORDINATOR_FORWARDER_SELFHEAL_OFF"):
        return
    try:
        from coordinator_core.install.forwarder_self_heal import self_heal_forwarders

        self_heal_forwarders()
    except Exception:
        pass  # self-heal is best-effort background maintenance; boot must never block on it


def _read_cache_head(repo_root: str) -> "Optional[str]":
    cache_path = Path(repo_root, "state", "orientation_cache.md")
    try:
        text = cache_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    frontmatter = parts[1]
    for line in frontmatter.splitlines():
        match = _FRONTMATTER_KV_RE.match(line)
        if match and match.group(1) == "git_head_at_generation":
            value = match.group(2).strip()
            return value or None
    return None


def _read_current_head(repo_root: str) -> "Optional[str]":
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5.0,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def _cache_is_stale(repo_root: str) -> bool:
    cache_head = _read_cache_head(repo_root)
    if not cache_head:
        return True
    current_head = _read_current_head(repo_root)
    if not current_head:
        return True
    return not current_head.startswith(cache_head)


def _selfheal_orientation_cache(repo_root: "Optional[str]") -> None:
    if os.environ.get(_ORIENTATION_SELFHEAL_OFF):
        return
    if not repo_root:
        return
    try:
        stale = _cache_is_stale(repo_root)
    except Exception:
        return
    if not stale:
        return
    try:
        from coordinator_core.orientation.regenerate_cache import build_cache, write_cache

        output = build_cache("sweep-boot", Path(repo_root))
        if output.get("skipped"):
            return
        write_cache(Path(output["cache_file"]), output["output"])
    except Exception:
        pass  # self-heal is best-effort background maintenance; boot must never block on it


def _session_reap_due(repo_root: "Optional[str]") -> bool:
    if not repo_root:
        return True
    try:
        import time

        marker = Path(repo_root, ".git", "coordinator-sessions", ".last-reap")
        if not marker.exists():
            return True
        return (time.time() - marker.stat().st_mtime) >= _SESSION_REAP_PREGATE_SECONDS
    except Exception:
        return True


async def _reap_sessions(repo_root: "Optional[str]") -> None:
    if os.environ.get(_SESSION_REAP_OFF):
        return
    if not _session_reap_due(repo_root):
        return
    try:
        from coordinator_core.ops.session.reap import _handler as _reap_handler

        common_dir = None
        if repo_root:
            from coordinator_core.git.git_dir import resolve_git_common_dir

            common_dir = resolve_git_common_dir(repo_root)
        await _reap_handler({}, repo_root=common_dir)
    except Exception:
        pass  # self-heal is best-effort background maintenance; boot must never block on it


@register_op("hooks.sweep_boot")
async def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    del params  # unused -- see module docstring

    _selfheal_forwarders()

    this_repo_root = _resolve_this_repo_root()
    _selfheal_orientation_cache(this_repo_root)
    await _reap_sessions(this_repo_root)

    return no_advisory()
