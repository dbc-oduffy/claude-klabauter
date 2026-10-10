"""coordinator_core.bash_guards.guard_heavy_command_admission -- PreToolUse (Bash|PowerShell)
hard-deny: a heavy command is admitted by caller identity and measured box capacity only.

What this guard is, in four points (DoE tripwire A-BOX-CAP-COUNTS-COMMANDS-NOT-AGENTS, DoE 4289b1712):
  1. It never counts dispatched agents. An idle agent costs the box nothing; a cap on agents
     throttles the fleet and leaves the box-killers running.
  2. It keys on the command's class and the calling session's process tree, both measured.
  3. Heavy: typecheck (any tsc, a second one per session included), builds, unscoped test
     tiers, any vitest run whose workers are not capped, watch modes, `pnpm -r` and other
     every-workspace runs, UE launches, reindexes.
  4. A caller's claim of need never admits. No justification, priority or "light" field is read.

Legs, in order, each denying once (no hold, sleep or retry):
  identity     the main thread, or an agent_type exactly on heavy_command_allowlist.txt that a
               workflow did not spawn (read off where the agent's own transcript lives); a
               coordinator:executor running only one-shot `tsc --noEmit` is admitted here and
               bounded by the one-typecheck-per-session rule
  ram-floor    available RAM minus the reserve of every still-unattributed lease stays at or
               above the machine-local floor; scoped test runs skip it
  session-cap  the session's live heavy and background-shell descendants stay under the
               machine-local caps

A light, foreground command stops after classification and imports nothing else. Every
threshold is machine-local config (heavy_admission.*) seeded by setup; an absent or malformed
key is untrusted and denies. No caller-supplied field (justification, priority, light flag)
is read. The COORDINATOR_ALLOW_HEAVY_* keys are operator pre-launch overrides and every bypass
is logged. The lease is written only after every leg has passed.

Spawn-free: this module creates no process and calls no git; every process fact comes from
in-process host reads.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from coordinator_core._hook_envelope import deny as _deny
from coordinator_core.bash_guards._heavy_admission_contract import (
    ADVISORY_WORKER_RSS,
    GUARD_NAME,
    KEY_FREE_RAM_FLOOR_MB,
    KEY_LEASE_RESERVE_MB,
    KEY_SESSION_BACKGROUND_CAP,
    KEY_SESSION_HEAVY_CAP,
    KEY_VITEST_MAX_WORKERS,
    KEY_WORKER_RSS_CEILING_MB,
    LEG_IDENTITY,
    LEG_RAM_FLOOR,
    LEG_SESSION_CAP,
    ORPHAN_CLAIM_WINDOW_S,
    OVERRIDE_KEYS,
    HeavyClass,
    LeaseRecord,
)
from coordinator_core.bash_guards._heavy_command_class import classify
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
GENERATES: List[str] = []

_SETUP_REMEDY = "The box owner runs scripts/setup.py"
_LOG_CMD_CHARS = 120


# --------------------------------------------------------------------------- seams
# Module-level so tests substitute each reader without touching the host.

def _read_config() -> Mapping[str, Any]:
    from coordinator_core import machine_resolver

    return machine_resolver.merged_flat_registry()


_UNRESOLVED_RUN = "unresolved"


def _run_in_transcript_path(payload: Mapping[str, Any]) -> Optional[str]:
    """The workflow run id named in the subagent's own transcript path, or None. Path parse only,
    no filesystem access, so the log can carry it on every record."""
    transcript = payload.get("transcript_path")
    if not payload.get("agent_id") or not isinstance(transcript, str):
        return None
    parts = transcript.replace("\\", "/").split("/")
    if "subagents" not in parts:
        return None
    kind, *rest = parts[parts.index("subagents") + 1:] or [None]
    run, *agent = rest or [None]
    return run if kind == "workflows" and agent else None


def _workflow_runs_of(payload: Mapping[str, Any]) -> List[str]:
    """The workflow run that spawned this subagent: [] for an Agent-tool dispatch, [run_id] for a
    workflow spawn, [_UNRESOLVED_RUN] when its transcript is in neither place (fails closed).

    The harness keeps an Agent-tool dispatch's transcript at
    <session>/subagents/agent-<id>.jsonl and a workflow agent's at
    <session>/subagents/workflows/<run_id>/agent-<id>.jsonl, so this is a per-agent fact that a
    finished run cannot leave stale. Direct stats plus one listing of the session's few run dirs.
    """
    transcript = payload.get("transcript_path")
    agent_id = payload.get("agent_id")
    if not isinstance(transcript, str) or not transcript or not isinstance(agent_id, str):
        return [_UNRESOLVED_RUN]
    if not agent_id or any(c in agent_id for c in ("/", "\\", "\0")) or ".." in agent_id:
        return [_UNRESOLVED_RUN]
    parts = transcript.replace("\\", "/").split("/")
    if "subagents" in parts:
        below = parts[parts.index("subagents") + 1:]
        if below[:1] == ["workflows"] and len(below) > 1:
            return [below[1]]
        return []
    stem = os.path.splitext(os.path.basename(transcript))[0]
    subagents = os.path.join(os.path.dirname(transcript), stem, "subagents")
    name = "agent-%s.jsonl" % agent_id
    if os.path.isfile(os.path.join(subagents, name)):
        return []
    workflows = os.path.join(subagents, "workflows")
    try:
        runs = os.listdir(workflows)
    except OSError:
        runs = []
    hits = [r for r in runs if os.path.isfile(os.path.join(workflows, r, name))]
    return hits or [_UNRESOLVED_RUN]


def _primitives():
    from coordinator_core.bash_guards._host_probe import HostPrimitives

    return HostPrimitives


def _read_available():
    from coordinator_core.bash_guards._host_probe import read_available_mb

    return read_available_mb()


def _working_set_mb(pid: int) -> Optional[int]:
    from coordinator_core.bash_guards._host_probe import working_set_mb

    return working_set_mb(pid)


def _deny_log_path():
    from coordinator_core._settings_home import settings_home

    return settings_home() / "heavy-admission" / "would-deny.jsonl"


def _append_log(record: Dict[str, Any]) -> None:
    """One JSON line per deny or advisory; append-only, spawn-free, never raises."""
    import json

    try:
        path = _deny_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        print("%s: failed to write would-deny log: %s" % (GUARD_NAME, exc), file=sys.stderr)


# --------------------------------------------------------------------------- helpers

def _positive_int(config: Mapping[str, Any], key: str) -> Optional[int]:
    raw = config.get(key)
    if isinstance(raw, bool):
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _bypassed(leg: str, payload: Dict[str, Any], command: str) -> bool:
    """True when the operator's pre-launch key for `leg` is set; the bypass is logged."""
    from coordinator_core.bash_guards._rewrite_support import _override

    key = OVERRIDE_KEYS[leg]
    if not _override(key, payload=payload):
        return False
    _log_bypass(leg, key, payload, command)
    return True


def _find_git_root(cwd: Any) -> Optional[str]:
    if not isinstance(cwd, str) or not cwd:
        return None
    cur = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _log_bypass(leg: str, key: str, payload: Dict[str, Any], command: str) -> None:
    """Append one audit line to the session's overrides.log; a failed write is reported, never raised."""
    try:
        from coordinator_core.bash_guards._override_log_path import _override_log_path

        session_id = payload.get("session_id")
        sid = session_id if isinstance(session_id, str) and session_id else None
        git_root = _find_git_root(payload.get("cwd") or os.getcwd())
        if git_root is None:
            return
        path = _override_log_path(git_root, sid)
        if path is None:
            return
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "%s | %s | %s | %s | %s\n"
                % (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    sid or "no-session",
                    key,
                    leg,
                    command[:_LOG_CMD_CHARS].replace("\n", " "),
                )
            )
    except OSError as exc:
        print("%s: failed to write override audit log: %s" % (GUARD_NAME, exc), file=sys.stderr)


def _deny_text(leg: str, fact: str, alternative: str) -> Dict[str, Any]:
    return _deny("PreToolUse", "BLOCKED %s (%s leg): %s. %s." % (GUARD_NAME, leg, fact, alternative))


def _payload_pid(payload: Mapping[str, Any]) -> Optional[int]:
    raw = payload.get("pid")
    if isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _env_pid() -> Optional[int]:
    """CLAUDE_PID: the warm server binds the door's own pid here for the length of one request
    (warm/entry_seam._environ_identity_borrow) and pops it when the door sent none; a cold hook
    process inherits the harness's. Either one's ancestry reaches the session's claude."""
    raw = os.environ.get("CLAUDE_PID", "")
    return int(raw) if raw.isdigit() else None


def _pid_and_source(payload: Mapping[str, Any]) -> Tuple[int, str]:
    pid = _payload_pid(payload)
    if pid is not None:
        return pid, "payload"
    pid = _env_pid()
    if pid is not None:
        return pid, "caller-env"
    # TRAP: in the warm server this is the server itself, which no session owns; it anchors
    # nothing and the session-cap leg fails closed.
    return os.getpid(), "guard-process"


def _caller_pid(payload: Mapping[str, Any]) -> int:
    return _pid_and_source(payload)[0]


def _record(
    payload: Mapping[str, Any],
    command: str,
    cls,
    kind: str,
    reason: str,
    anchor=None,
    census=None,
) -> Dict[str, Any]:
    """The would-deny log line. pid_source and the census names are what the morning review
    needs to tell a real deny from a mis-anchored or MCP-inflated one."""
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": kind,
        "reason": reason,
        "command": command[:_LOG_CMD_CHARS].replace("\n", " "),
        "heavy_class": cls.heavy_class.value if cls.heavy_class is not None else None,
        "scoped": cls.scoped,
        "background": cls.background,
        "session_id": payload.get("session_id"),
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
        "workflow_run": _run_in_transcript_path(payload),
        "cwd": payload.get("cwd"),
        "caller_pid": _caller_pid(payload),
        "pid_source": _pid_and_source(payload)[1],
        "anchor_pid": anchor.pid if anchor is not None else None,
        "census_heavy": sorted(r.name for r in census.heavy) if census is not None else None,
        "census_shells": len(census.shells) if census is not None else None,
    }


def _deny_reason(envelope: Mapping[str, Any]) -> str:
    hso = envelope.get("hookSpecificOutput")
    return str(hso.get("permissionDecisionReason", "")) if isinstance(hso, dict) else ""


def _flag_runaway_workers(config, payload, command, cls, anchor, census) -> None:
    """Advisory only: log each session heavy process whose working set is over the ceiling."""
    ceiling = _positive_int(config, KEY_WORKER_RSS_CEILING_MB)
    if ceiling is None or census is None:
        return
    for row in census.heavy:
        mb = _working_set_mb(row.pid)
        if mb is not None and mb > ceiling:
            _append_log(
                _record(
                    payload, command, cls, ADVISORY_WORKER_RSS,
                    "pid %d (%s) holds %d MB, ceiling %d MB" % (row.pid, row.name, mb, ceiling),
                    anchor, census,
                )
            )


# --------------------------------------------------------------------------- legs

_EXECUTOR_TYPE = "coordinator:executor"
ADMITTED_VERIFIER = "admitted-verifier"
# Group EM ruling 2026-10-10: an execute run's own test phase and terminal judge verify inside
# the workflow; they get test-tier and typecheck only, never builds, UE, watch modes or an
# uncapped vitest/jest. Being inside a workflow run admits nothing by itself.
_VERIFIER_TYPES = frozenset({"coordinator:test-runner", "coordinator:exit-criterion-judge"})


def _identity_leg(payload: Dict[str, Any], command: str, heavy_name: str, cls) -> Optional[Dict[str, Any]]:
    from coordinator_core.bash_guards._heavy_identity import identity_verdict, load_allowlist

    allowlist = load_allowlist()
    listed = bool(payload.get("agent_id")) and payload.get("agent_type") in allowlist
    verdict = identity_verdict(
        payload,
        allowlist=allowlist,
        workflow_runs=_workflow_runs_of(payload) if listed else (),
    )
    if verdict.allowed:
        return None
    # Group EM ruling 2026-10-10: an executor verifies its own chunk with `tsc --noEmit`; the
    # session-cap leg's one-typecheck rule still bounds it.
    if payload.get("agent_type") == _EXECUTOR_TYPE and payload.get("agent_id") and cls.noemit_tsc:
        return None
    if payload.get("agent_type") in _VERIFIER_TYPES and payload.get("agent_id") and cls.bounded_verify:
        # Logged so a run's verifier traffic is attributable: a clean run shows admits, not silence.
        _append_log(_record(payload, command, cls, ADMITTED_VERIFIER, verdict.reason))
        return None
    return _deny_text(
        LEG_IDENTITY,
        "%s: %s" % (heavy_name, verdict.reason),
        "The EM or coordinator:test-runner runs it",
    )


def _ram_leg(config: Mapping[str, Any], primitives) -> Optional[Dict[str, Any]]:
    from coordinator_core.bash_guards import _heavy_lease_store as leases

    floor = _positive_int(config, KEY_FREE_RAM_FLOOR_MB)
    reserve = _positive_int(config, KEY_LEASE_RESERVE_MB)
    if floor is None or reserve is None:
        return _deny_text(
            LEG_RAM_FLOOR,
            "heavy_admission RAM keys unconfigured",
            _SETUP_REMEDY,
        )
    reading = _read_available()
    if not reading.trusted or reading.avail_mb is None:
        return _deny_text(
            LEG_RAM_FLOOR,
            "available RAM is unreadable or untrusted (%s)" % reading.source,
            "Retry once the host reading recovers",
        )
    leases.reap(primitives)
    reserved = leases.unattributed_count() * reserve
    effective = reading.avail_mb - reserved
    if effective >= floor:
        return None
    held = leases.holders()
    return _deny_text(
        LEG_RAM_FLOOR,
        "%d MB available, %d MB reserved by unattributed leases, floor %d MB; holders: %s"
        % (reading.avail_mb, reserved, floor, "; ".join(held) if held else "none"),
        "Wait for a holder to finish, then retry",
    )


def _chain_pids(by_pid: Mapping[int, Any], start: int, anchor_pid: int) -> set:
    """The caller's own wrapper chain, caller pid up to the session anchor."""
    chain = set()
    node = by_pid.get(start)
    while node is not None and node.pid != anchor_pid and node.pid not in chain:
        chain.add(node.pid)
        node = by_pid.get(node.ppid)
    return chain


def _second_typecheck(anchor, primitives) -> List[int]:
    """Holder pids of the session's live, attributed typecheck leases."""
    from coordinator_core.bash_guards import _heavy_lease_store as leases

    leases.reap(primitives)
    return [
        r.holder_pid
        for r in leases.live_leases()
        if r.heavy_class == HeavyClass.TYPECHECK.value
        and r.session_pid == anchor.pid
        and r.session_ctime == anchor.ctime
        and (r.holder_pid, r.holder_ctime) != (r.session_pid, r.session_ctime)
    ]


def _cap_leg(
    config: Mapping[str, Any],
    payload: Dict[str, Any],
    primitives,
    *,
    heavy: bool,
    background: bool,
    typecheck: bool = False,
):
    """Returns (deny_envelope_or_None, anchor_or_None, census_or_None, launch_ctime)."""
    from coordinator_core.bash_guards import _session_census as census_mod

    heavy_cap = _positive_int(config, KEY_SESSION_HEAVY_CAP)
    bg_cap = _positive_int(config, KEY_SESSION_BACKGROUND_CAP)
    if (heavy and heavy_cap is None) or (background and bg_cap is None):
        return (
            _deny_text(
                LEG_SESSION_CAP,
                "heavy_admission session cap unconfigured",
                _SETUP_REMEDY,
            ),
            None,
            None,
            0,
        )
    rows = primitives.snapshot()
    if rows is None:
        return (
            _deny_text(LEG_SESSION_CAP, "the process table is unreadable", "Retry once the host reading recovers"),
            None,
            None,
            0,
        )
    caller = _caller_pid(payload)
    anchor = census_mod.resolve_anchor({"pid": caller}, primitives, snapshot=rows)
    if anchor is None:
        return (
            _deny_text(
                LEG_SESSION_CAP,
                "no session anchor found above pid %d" % caller,
                "Launch from inside a Claude session",
            ),
            None,
            None,
            0,
        )
    by_pid = {r.pid: r for r in rows}
    chain = _chain_pids(by_pid, caller, anchor.pid)
    launch = by_pid[caller].ctime if caller in by_pid else 0
    census = census_mod.session_census(anchor, primitives, snapshot=rows, exclude_pids=chain)
    if census is not None:
        census = _fold_claimed(census, rows, anchor, chain)
    if census is None:
        return (
            _deny_text(LEG_SESSION_CAP, "the process table is unreadable", "Retry once the host reading recovers"),
            None,
            None,
            0,
        )
    running = _second_typecheck(anchor, primitives) if typecheck else []
    if running:
        return (
            _deny_text(
                LEG_SESSION_CAP,
                "session already runs a typecheck: pids %s" % ", ".join(str(p) for p in running),
                "Wait for it to finish",
            ),
            anchor,
            census,
            launch,
        )
    commands = census_mod.heavy_roots(census.heavy, rows) if heavy else ()
    if heavy and len(commands) >= heavy_cap:
        pids = ", ".join(str(r.pid) for r in commands)
        return (
            _deny_text(
                LEG_SESSION_CAP,
                "session holds %d heavy commands (cap %d): pids %s" % (len(commands), heavy_cap, pids),
                "Wait for one to finish",
            ),
            anchor,
            census,
            launch,
        )
    if background and len(census.shells) >= bg_cap:
        pids = ", ".join(str(r.pid) for r in census.shells)
        return (
            _deny_text(
                LEG_SESSION_CAP,
                "session holds %d background shells (cap %d): pids %s" % (len(census.shells), bg_cap, pids),
                "Stop an idle shell first",
            ),
            anchor,
            census,
            launch,
        )
    return None, anchor, census, launch


def _fold_claimed(census, rows, anchor, chain):
    """census with the session's claimed orphan commands added to heavy.

    Open leases first claim the orphan trees created after their launch marks; then every tree
    a lease of this session holds counts once.
    """
    from dataclasses import replace

    from coordinator_core.bash_guards import _heavy_lease_store as leases
    from coordinator_core.bash_guards import _session_census as census_mod
    from coordinator_core.bash_guards._host_probe import ctime_units_per_second

    since = leases.oldest_open_launch()
    if since is not None:
        trees = census_mod.orphan_trees(rows, since)
        if trees:
            leases.claim_orphans(trees, ORPHAN_CLAIM_WINDOW_S * ctime_units_per_second())
    session = (anchor.pid, anchor.ctime)
    roots = {
        (r.holder_pid, r.holder_ctime)
        for r in leases.live_leases()
        if (r.session_pid, r.session_ctime) == session and (r.holder_pid, r.holder_ctime) != session
    }
    have = {r.pid for r in census.heavy}
    extra = [r for r in census_mod.claimed_heavy(rows, roots) if r.pid not in have and r.pid not in chain]
    return replace(census, heavy=tuple(census.heavy) + tuple(extra)) if extra else census


def _write_lease(heavy_name: str, anchor, census, primitives, launch_ctime: int = 0) -> None:
    from coordinator_core.bash_guards import _heavy_lease_store as leases

    if census is not None and census.heavy:
        newest = max(census.heavy, key=lambda r: r.ctime)
        leases.attribute(anchor.pid, anchor.ctime, newest.pid, newest.ctime)
    leases.write_lease(
        LeaseRecord(
            holder_pid=anchor.pid,
            holder_ctime=anchor.ctime,
            session_pid=anchor.pid,
            session_ctime=anchor.ctime,
            heavy_class=heavy_name,
            admitted_at=time.time(),
            launch_ctime=launch_ctime,
        )
    )


# --------------------------------------------------------------------------- entry

def check(
    payload: Dict[str, Any],
    resolve_wiki_citation: Optional[Callable[[str], str]] = None,
) -> Optional[Dict[str, Any]]:
    """None (allow) or the hard-deny envelope. Every deny, and every crash, is appended to the
    would-deny log whatever the guard's level. `resolve_wiki_citation` is accepted for the
    chain's call signature and unused."""
    del resolve_wiki_citation
    if not isinstance(payload, dict) or payload.get("tool_name") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    cls = classify(command, tool_input, cwd)
    if cls.heavy_class is None and not cls.background:
        return None

    config = None
    # Any heavy command naming vitest re-classifies with the cap: a capped scoped vitest segment
    # beside a `tsc --noEmit` must not hold the executor carve-out shut.
    if cls.heavy_class is not None and ("vitest" in command.lower() or "jest" in command.lower()):
        config = _read_config()
        cls = classify(command, tool_input, cwd, _positive_int(config, KEY_VITEST_MAX_WORKERS))
        if cls.heavy_class is None and not cls.background:
            return None

    try:
        return _admit(payload, command, cls, config)
    except Exception as exc:
        _append_log(_record(payload, command, cls, "crash", "%s: %s" % (type(exc).__name__, exc)))
        raise


def _admit(payload: Dict[str, Any], command: str, cls, config) -> Optional[Dict[str, Any]]:
    heavy = cls.heavy_class is not None
    heavy_name = cls.heavy_class.value if heavy else "background"

    def denied(leg: str, envelope, anchor=None, census=None):
        _append_log(_record(payload, command, cls, leg, _deny_reason(envelope), anchor, census))
        if census is not None:
            _flag_runaway_workers(config, payload, command, cls, anchor, census)
        return envelope

    if heavy and not _bypassed(LEG_IDENTITY, payload, command):
        envelope = _identity_leg(payload, command, heavy_name, cls)
        if envelope is not None:
            return denied(LEG_IDENTITY, envelope)

    if config is None:
        config = _read_config()
    primitives = _primitives()

    if heavy and not _bypassed(LEG_RAM_FLOOR, payload, command):
        envelope = _ram_leg(config, primitives)
        if envelope is not None:
            return denied(LEG_RAM_FLOOR, envelope)

    anchor = census = None
    launch = 0
    if not _bypassed(LEG_SESSION_CAP, payload, command):
        envelope, anchor, census, launch = _cap_leg(
            config, payload, primitives, heavy=heavy, background=cls.background,
            typecheck=cls.heavy_class is HeavyClass.TYPECHECK,
        )
        if envelope is not None:
            return denied(LEG_SESSION_CAP, envelope, anchor, census)

    if heavy:
        _flag_runaway_workers(config, payload, command, cls, anchor, census)
    if heavy and anchor is not None:
        _write_lease(heavy_name, anchor, census, primitives, launch)
    return None
