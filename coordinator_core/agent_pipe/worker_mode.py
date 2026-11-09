"""agent_pipe.worker_mode -- spec for PM-launched autonomous worker sessions of codex, grok and agy.

Three answers, no process spawn: WORKER_ARGV (what argv puts CLI X into autonomous execution),
worker_branch (the branch a worker owns), preflight (may a worker start now).

Traps:
  - The argv are INTERACTIVE-session flags appended to the CLI binary; they are not the headless
    `exec`/`-p` argv the adapters in this package build. The adapters stay bypass-free.
  - Codex flags are verified against installed codex-cli 0.167.0 help. Grok and agy flags
    are verified against their official CLI references (WORKER_ARGV_SOURCE); local binary
    availability is checked by the launcher before admission.
  - codex_runner.execute carries no argv constant (its `exec --sandbox workspace-write` is inline),
    so nothing is reused from it; the sandbox value here matches that call.
  - There is no per-agent seat record on the box. The only G-EM slot record is
    group_em.slot_grant (a memory-slot grant keyed by grantee session id and run), so the slot
    rule is: a live verified box holder (a Group EM exists, e.g. Machine-a) requires that grant;
    no live holder means a quiet box and no slot is owed.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, NamedTuple, Optional

WORKER_ENV = "COORDINATOR_AGENT_WORKER"

WORKER_ARGV: dict[str, tuple[str, ...]] = {
    "codex": ("--sandbox", "workspace-write", "--ask-for-approval", "never"),
    "grok": ("--always-approve",),
    "agy": ("--dangerously-skip-permissions",),
}

WORKER_ARGV_SOURCE: dict[str, str] = {
    "codex": "verified: `codex --help` on codex-cli 0.167.0 (-s/--sandbox, -a/--ask-for-approval never)",
    "grok": "verified official CLI permissions reference: https://docs.x.ai/build/features/permissions (2026-11-07)",
    "agy": "verified official CLI reference: https://antigravity.google/docs/cli/reference/ (2026-11-07)",
}

_PROTECTED_BRANCHES = frozenset({"main", "master"})
_SLUG_BAD = re.compile(r"[^a-z0-9]+")


class Preflight(NamedTuple):
    """`ok` with an empty `reason`, or not ok with a one-line `reason`."""

    ok: bool
    reason: str


def _slug(text: str) -> str:
    return _SLUG_BAD.sub("-", text.lower()).strip("-")


def worker_branch(agent: str, host: str, date: str) -> str:
    """`work/<host>/worker-<agent>-<date>`; raises ValueError for an unknown agent or an empty host or date."""
    if agent not in WORKER_ARGV:
        raise ValueError(f"unknown worker agent: {agent!r}")
    host_slug, date_slug = _slug(host), _slug(date)
    if not host_slug or not date_slug:
        raise ValueError("worker branch needs a non-empty host and date")
    return f"work/{host_slug}/worker-{agent}-{date_slug}"


def is_protected_branch(branch: str) -> bool:
    """True for a branch a worker must never run on (main, master)."""
    return branch in _PROTECTED_BRANCHES


_ENGINE_MARKER = ("coordinator_core", "agent_pipe", "worker_mode.py")


def is_engine_repo(start_dir) -> bool:
    """True when `start_dir` is inside the claude-klabauter engine checkout (an ancestor carries this module's own path)."""
    here = Path(start_dir).resolve()
    return any(Path(d, *_ENGINE_MARKER).is_file() for d in (here, *here.parents))


def preflight(
    agent: str,
    *,
    session_id: Optional[str] = None,
    run: Optional[str] = None,
    start_dir=None,
    read_load: Optional[Callable[[], dict]] = None,
    box_holder: Optional[Callable[[], Optional[dict]]] = None,
    slot_check: Optional[Callable[[str, str], tuple]] = None,
) -> Preflight:
    """May a worker of `agent` start now? Load admission first (one-shot, never holds), then the G-EM slot.

    Unconfigured admission admits (no threshold default lives in code). A live box holder requires a
    slot grant for (`session_id`, `run`); without both ids that is a refusal. A `start_dir` inside the engine repo is refused (None skips the check). Never raises.
    """
    if agent not in WORKER_ARGV:
        return Preflight(False, f"unknown worker agent: {agent!r}")
    if start_dir is not None and is_engine_repo(start_dir):
        return Preflight(False, "worker target is the claude-klabauter engine repo: workers do not land engine changes")
    try:
        from coordinator_core.ops.dispatch_emit import admission

        record = admission.await_admission(start_dir, hold_allowed=False, read_load=read_load)
        if record.get("reasons") and record.get("verdict") == "admitted_warm_no_hold":
            return Preflight(False, f"box over capacity: {'; '.join(record['reasons'])}")
    except Exception as exc:
        return Preflight(False, f"load admission unreadable: {exc}")
    try:
        if box_holder is None:
            from coordinator_core.group_em import nomination

            box_holder = nomination.box_holder
        holder = box_holder()
        if holder is None or not holder.get("live"):
            return Preflight(True, "")
        if not session_id or not run:
            return Preflight(False, "G-EM live on this box: worker needs a slot grant (session id and run)")
        if slot_check is None:
            from coordinator_core.group_em import slot_grant

            slot_check = slot_grant.check_slot_grant
        granted, _record = slot_check(session_id, run)
        if not granted:
            return Preflight(False, f"no G-EM slot grant for session {session_id} run {run}")
    except Exception as exc:
        return Preflight(False, f"G-EM slot unreadable: {exc}")
    return Preflight(True, "")
