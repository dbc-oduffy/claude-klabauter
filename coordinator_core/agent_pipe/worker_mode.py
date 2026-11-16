"""agent_pipe.worker_mode -- spec for PM-launched autonomous worker sessions of codex, grok and agy.

Three answers, no process spawn: WORKER_ARGV (what argv puts CLI X into autonomous execution),
worker_branch (the branch a worker owns), preflight (may a worker start now).

Traps:
  - The argv are INTERACTIVE-session flags appended to the CLI binary; they are not the headless
    `exec`/`-p` argv the adapters in this package build. The adapters stay bypass-free.
  - Codex flags are verified against installed codex-cli 0.167.0 help. Grok and agy flags
    are verified against their official CLI references (WORKER_ARGV_SOURCE); local binary
    availability is checked by the launcher before admission.
  - Every engine codex launch (this table, codex_runner.execute, codex_runner.workflow) widens the
    workspace-write sandbox by the settings home (`settings_home_add_dir_argv`) plus the machine lock
    root and the repo git common dir (`lock_and_gitdir_add_dir_argv`), so engine ops a worker runs
    can write their settings-home state, foreign-target locks and git objects.
  - There is no per-agent seat record on the box. The only G-EM slot record is
    group_em.slot_grant (a memory-slot grant keyed by grantee session id and run), so the slot
    rule is: a live verified box holder (a Group EM exists, e.g. Machine-a) requires that grant;
    no live holder means a quiet box and no slot is owed.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterator, Mapping, NamedTuple, Optional

from coordinator_core._settings_home import settings_home

WORKER_ENV = "COORDINATOR_AGENT_WORKER"


def settings_home_add_dir_argv() -> list[str]:
    """`["--add-dir", <settings home>]`: the codex flag that makes the settings home a writable root beside the repo."""
    return ["--add-dir", str(settings_home())]


def lock_and_gitdir_add_dir_argv(repo=None) -> list[str]:
    """`--add-dir` grants for the machine lock root and the git common dir of `repo` (default cwd).

    Both are roots the engine's own ops write (locked_write foreign-target locks, git object writes).
    Walk-resolved, zero spawn; outside a git repo only the lock root is granted.
    """
    from coordinator_core.git import repo_root as _repo_root
    from coordinator_core.locked_write import _machine_lock_dir

    argv = ["--add-dir", str(_machine_lock_dir())]
    common = _repo_root.git_common_dir(None if repo is None else str(repo))
    if common:
        argv += ["--add-dir", common]
    return argv


class _WorkerArgv(Mapping):
    """WORKER_ARGV view: static agent flags, with the codex writable roots resolved on each read."""

    _STATIC: dict[str, tuple[str, ...]] = {
        "codex": ("--sandbox", "workspace-write", "--ask-for-approval", "never"),
        "grok": ("--always-approve",),
        "agy": ("--dangerously-skip-permissions",),
    }

    def __getitem__(self, agent: str) -> tuple[str, ...]:
        base = self._STATIC[agent]
        if agent != "codex":
            return base
        return (*base, *settings_home_add_dir_argv(), *lock_and_gitdir_add_dir_argv())

    def __iter__(self) -> Iterator[str]:
        return iter(self._STATIC)

    def __len__(self) -> int:
        return len(self._STATIC)


WORKER_ARGV: Mapping[str, tuple[str, ...]] = _WorkerArgv()

WORKER_ARGV_SOURCE: dict[str, str] = {
    "codex": "verified: `codex --help` on codex-cli 0.167.0 (-s/--sandbox, -a/--ask-for-approval never, --add-dir <DIR> additional writable directory)",
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
