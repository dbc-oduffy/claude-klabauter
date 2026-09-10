"""coordinator_core.environment — what this session's environment can actually DO.

Doctrine is written on a workstation and read everywhere, so it encodes the
author's environment as universal truth and a future self inherits asks it
cannot satisfy. This module lets a rule ask "is my mechanism real where I am
standing" before it fires.

CAPABILITIES, NOT A VENUE NAME. `is_cloud()` is the wrong primitive: a venue
name is a proxy, and a workstation with no reachable peers has the same memo
problem as a container. Every capability here is a real probe over real
evidence; none is an alias for another. `detect_venue()` exists for REPORTING
and is never what a guard branches on.

COST IS A CORRECTNESS PROPERTY. Probes are LAZY — asking for one never runs
another — because `fleet_present` is consulted from the PreToolUse hot path
while `peer_ems_reachable` reads files and is consulted only from `memo.send`.
No subprocess anywhere.

ENV IS PER CALL, NEVER MODULE-CACHED. The guard chain is hosted by a
long-lived warm server; caching an `os.environ` read at module scope would
freeze the first session's verdict for the daemon's life and make the
`COORDINATOR_CAP_*` override unreachable from every session it subsequently
serves. Callers thread `env` from `payload["env"]`, exactly as
`dispatch._override_env_identity` does and for the identical reason. The cache
below is keyed on that env's capability-relevant slice, so it is safe to share.

FAIL OPEN, AS A PROPERTY OF THIS MODULE. Every probe runs inside a handler
here, so a raising probe yields a permissive capability carrying the exception
— a consumer must not have to supply its own `try/except` to be safe on the
hot path.

RATIFICATION. The capability vocabulary is a DOCTRINE-PLANE contract implemented
here: DoE-claude `docs/decisions/DR-environment-scoped-guard-stand-down.md`. A
capability added without a real probe re-opens the defect review caught the first
time. Rationale and the measured incidents:
DoE-claude `docs/research/2026-09-05-environment-dependent-doctrine.md`; the
stand-down's own traps:
`coordinator/docs/wiki/coordinator-tripwires/tripwire-registry/a-guard-that-stands-down-must-still-be-in-the-chain.md`.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Mapping, NamedTuple, Optional

_OVERRIDE_PREFIX = "COORDINATOR_CAP_"
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

#: How recently a peer must have drained an inbox for the fleet to count as
#: staffed. Wide on purpose: this decides whether to WARN, and a false "no
#: reader" costs a warning the operator dismisses while a false "reader" costs
#: a memo nobody ever reads.
_DRAIN_HORIZON_DAYS = 30

#: Newest inbox entries to inspect. Filenames are date-prefixed, so the newest
#: by name are the newest by date without stat-ing the directory.
_DRAIN_SAMPLE = 12


class Capability(NamedTuple):
    """One capability, and the evidence that produced it. Consumers render
    `evidence` wherever a capability changes behaviour — a verdict that cannot
    explain itself gets the mechanism disabled the first time it surprises
    someone."""

    name: str
    value: bool
    evidence: str
    overridden: bool = False


def _env_map(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return env if isinstance(env, Mapping) else os.environ


def _override(name: str, env: Mapping[str, str]) -> Optional[bool]:
    raw = str(env.get(_OVERRIDE_PREFIX + name.upper(), "")).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return None  # unparseable is IGNORED, never read as false


def _cap(name: str, value: bool, evidence: str, env: Mapping[str, str]) -> Capability:
    forced = _override(name, env)
    if forced is None:
        return Capability(name, value, evidence)
    return Capability(
        name,
        forced,
        f"forced by {_OVERRIDE_PREFIX}{name.upper()} (inferred {value}: {evidence})",
        overridden=True,
    )


# ---------------------------------------------------------------------------
# Venue — reported, never branched on.
# ---------------------------------------------------------------------------

#: Markers a managed remote container sets. Read, not merely cited: an earlier
#: version of this module claimed these as "corroboration" while consulting
#: none of them.
_REMOTE_MARKERS = ("CLAUDE_CODE_CONTAINER_ID", "CCR_SESSION_PROFILE", "CCR_AGENT_PROXY_ENABLED")


def detect_venue(env: Optional[Mapping[str, str]] = None) -> str:
    """`remote` | `local` | `unknown`, for reporting."""
    e = _env_map(env)
    if str(e.get("CLAUDE_CODE_ENTRYPOINT", "")).strip().lower() == "remote":
        return "remote"
    if any(k in e for k in _REMOTE_MARKERS):
        return "remote"
    if e.get("CLAUDECODE"):
        return "local"
    return "unknown"


# ---------------------------------------------------------------------------
# Probes.
# ---------------------------------------------------------------------------


def _settings_home(env: Mapping[str, str]) -> Optional[Path]:
    raw = env.get("COORDINATOR_SETTINGS_HOME") or env.get("CLAUDE_HOME")
    if raw:
        return Path(str(raw))
    try:
        from coordinator_core._settings_home import settings_home

        return settings_home()
    except Exception:
        return None


def _probe_ephemeral_host(env: Mapping[str, str]) -> Capability:
    """Does this host's filesystem survive the session?

    States what is OBSERVED (a harness entrypoint label plus container
    markers), not what an earlier version inferred from it ("the filesystem is
    reclaimed"). A durable self-hosted runner sets the same label, so the
    evidence has to let a reader see the gap and override it.
    """
    e = env
    seen = [k for k in _REMOTE_MARKERS if k in e]
    entrypoint = str(e.get("CLAUDE_CODE_ENTRYPOINT", "")).strip().lower()
    if entrypoint == "remote" or seen:
        witness = ", ".join(["CLAUDE_CODE_ENTRYPOINT=remote"] if entrypoint == "remote" else [] + seen)
        return _cap(
            "ephemeral_host",
            True,
            f"managed-remote markers present ({witness or ', '.join(seen)}); a durable "
            "self-hosted runner sets the same markers and should override",
            e,
        )
    return _cap("ephemeral_host", False, f"no managed-remote markers (venue={detect_venue(e)})", e)


def _probe_engine_installed(env: Mapping[str, str]) -> Capability:
    """Is coordinator INSTALLED here, not merely checked out?

    Three independent witnesses, any one sufficient. An earlier version stat-ed
    one filename, and on a box whose settings home held `machine-local/`,
    `state/` and more — materially installed — it read "not installed" because
    `settings.json` happened to be absent.
    """
    home = _settings_home(env)
    if home is None:
        return _cap("engine_installed", True, "settings home unresolvable; assuming installed", env)
    witnesses = [
        name
        for name, p in (
            ("settings.json", home / "settings.json"),
            ("machine-local/", home / "machine-local"),
            (".doe-root", home / ".doe-root"),
        )
        if p.exists()
    ]
    if witnesses:
        return _cap("engine_installed", True, f"{home}: {', '.join(witnesses)}", env)
    return _cap(
        "engine_installed",
        False,
        f"{home} carries none of settings.json, machine-local/, .doe-root",
        env,
    )


def _probe_durable_repo(env: Mapping[str, str]) -> Capability:
    """Is there a durable sink — a git remote — for a record that must outlive
    this host? Consulted by the stand-down audit leg, which must not write its
    only copy inside `.git` on a container that gets reclaimed."""
    try:
        git_dir = Path(os.getcwd())
        for candidate in (git_dir, *git_dir.parents):
            config = candidate / ".git" / "config"
            if config.is_file():
                if "[remote " in config.read_text(encoding="utf-8", errors="replace"):
                    return _cap("durable_repo", True, f"{config} declares a remote", env)
                return _cap("durable_repo", False, f"{config} declares no remote", env)
    except OSError as exc:
        return _cap("durable_repo", True, f"unreadable git config ({exc}); assuming durable", env)
    return _cap("durable_repo", False, "no enclosing git repository", env)


def _iter_inbox_frontmatter(inbox: Path, limit: int):
    """Head-read the newest inbox entries. Filenames are date-prefixed, so
    newest-by-name is newest-by-date with no stat calls."""
    for entry in sorted(inbox.glob("*.md"), reverse=True)[:limit]:
        try:
            with open(entry, encoding="utf-8", errors="replace") as fh:
                yield [next(fh, "") for _ in range(24)]
        except OSError:
            continue


def _probe_peer_ems_reachable(
    env: Mapping[str, str], receiver_root: Optional[Path] = None
) -> Capability:
    """Would a memo written here ever be READ?

    A REAL PROBE, not an inference from the venue. The memo lifecycle stamps a
    drained memo `status: actioned` with a `picked_up_at:` timestamp, so a
    peer's inbox carries direct evidence of whether anyone is working it.
    `receiver_root` probes the repo a memo is actually addressed to; without
    one, this repo's own inbox stands in as a fleet-liveness signal.

    Errs toward "not reachable" only on real evidence of an undrained inbox: a
    false negative costs a warning the operator dismisses, a false positive
    costs a memo nobody reads.
    """
    root = receiver_root or Path(os.getcwd())
    inbox = None
    for candidate in (root / "cross-repo" / "inbox", root / "state" / "cross-repo" / "inbox"):
        if candidate.is_dir():
            inbox = candidate
            break
    if inbox is None:
        return _cap("peer_ems_reachable", False, f"no cross-repo inbox under {root}", env)

    import datetime as _dt

    newest: Optional[str] = None
    total = 0
    for head in _iter_inbox_frontmatter(inbox, _DRAIN_SAMPLE):
        total += 1
        for line in head:
            if line.startswith("picked_up_at:"):
                stamp = line.split(":", 1)[1].strip().strip("'\"")
                if newest is None or stamp > newest:
                    newest = stamp
                break
    if total == 0:
        return _cap("peer_ems_reachable", False, f"{inbox} is empty — no drain evidence", env)
    if newest is None:
        return _cap(
            "peer_ems_reachable",
            False,
            f"{inbox}: {total} memos sampled, none carries picked_up_at — nothing drained",
            env,
        )
    try:
        drained = _dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))
        age = (_dt.datetime.now(_dt.timezone.utc) - drained).days
    except ValueError:
        return _cap("peer_ems_reachable", True, f"{inbox}: drained at {newest} (unparsed)", env)
    if age <= _DRAIN_HORIZON_DAYS:
        return _cap("peer_ems_reachable", True, f"{inbox}: last drained {age}d ago", env)
    return _cap(
        "peer_ems_reachable",
        False,
        f"{inbox}: last drained {age}d ago, over the {_DRAIN_HORIZON_DAYS}d horizon",
        env,
    )


def _probe_fleet_present(env: Mapping[str, str]) -> Capability:
    """Is this session inside an installed coordinator fleet? Consulted from
    the PreToolUse hot path, so it stays env-and-stat only and never reads a
    memo corpus."""
    installed = _probe_engine_installed(env)
    ephemeral = _probe_ephemeral_host(env)
    if installed.value and not ephemeral.value:
        return _cap("fleet_present", True, f"installed ({installed.evidence}), durable host", env)
    reason = (
        f"not installed ({installed.evidence})"
        if not installed.value
        else f"ephemeral host ({ephemeral.evidence})"
    )
    return _cap("fleet_present", False, reason, env)


_PROBES: Dict[str, Callable[..., Capability]] = {
    "ephemeral_host": _probe_ephemeral_host,
    "engine_installed": _probe_engine_installed,
    "durable_repo": _probe_durable_repo,
    "fleet_present": _probe_fleet_present,
    "peer_ems_reachable": _probe_peer_ems_reachable,
}


def capability(
    name: str,
    env: Optional[Mapping[str, str]] = None,
    **probe_kwargs: Any,
) -> Capability:
    """One capability, probed lazily. Unknown name or raising probe both yield
    a permissive `True` carrying the reason — never an exception on a hot
    path."""
    e = _env_map(env)
    probe = _PROBES.get(name)
    if probe is None:
        return Capability(name, True, f"unknown capability {name!r}; defaulting permissive")
    try:
        return probe(e, **probe_kwargs)
    except Exception as exc:
        return Capability(
            name, True, f"probe raised {type(exc).__name__}: {exc}; defaulting permissive"
        )


def has(name: str, env: Optional[Mapping[str, str]] = None, **probe_kwargs: Any) -> bool:
    return capability(name, env, **probe_kwargs).value


def capabilities(env: Optional[Mapping[str, str]] = None) -> Mapping[str, Capability]:
    """Every capability, for REPORTING. Runs every probe including the
    file-reading one, so never call it from a guard — ask for the one you
    need."""
    e = _env_map(env)
    return MappingProxyType({name: capability(name, e) for name in _PROBES})
