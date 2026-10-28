"""machine_profile.py -- the two machine-local settings that gate author-only behaviour.

``coordinator.machine_profile`` is ``consumer`` or ``author``. An explicit
registry value wins; absent, the box is ``author`` when any registered
``repos.*`` path carries a ``.coordinator-dev-repo`` sentinel at its root,
else ``consumer``.

``coordinator.guard_level`` is ``strict``, ``warn`` or ``off``, with a
per-guard override ``coordinator.guard_level.<guard-name>``; ``guard_level``
reads it. It no longer decides a deny: ``resolved_level`` returns ``floor`` for
a guard registered in ``deny_registry`` and ``log`` for every other guard, so a
guard outside the registry never refuses work at any level or profile.

Reads go through the in-process registry reader
(``machine_resolver.registry_get``): no subprocess, no ``machine-local`` CLI.
Results are cached per process, keyed on the registry directory, the two
registry files' mtimes and the ``MACHINE_LOCAL_COORDINATOR_*`` env
overrides, so a ``machine-local set`` is visible to a resident engine on its
next call.

``coordinator.feature.cross_repo_memos`` and ``coordinator.feature.publishing``
are ``on`` or ``off``; absent, ``on`` for an author box and ``off`` for a
consumer box (``feature_enabled``). ``coordinator.feature.doctrine_edit_gate``
is ``on`` on every profile until set ``off``: the doctrine-edit approval gate
and its sentinel-creation guards fire unless disabled.
``coordinator.feature.gem_cross_repo_commit`` is likewise ``on`` on every
profile until set ``off``: a session holding live Group EM standing may make
scoped commits in a foreign repo. It mirrors coordinator-content-repo's
``coordinator/hooks/scripts/_machine_profile.py`` ``_FEATURE_DEFAULT``, which
must agree.
``coordinator.feature.out_of_repo_write_block`` is ``on`` for an author box and
``off`` for a consumer box; the bump guards read it to decide whether a marker
clears their verdict. It never makes a guard deny.

Change the level with ``machine-local set coordinator.guard_level warn``
(or ``strict`` / ``off``); one guard with
``machine-local set coordinator.guard_level.<guard-name> off``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from coordinator_core import deny_registry, machine_resolver

PROFILE_KEY = "coordinator.machine_profile"
LEVEL_KEY = "coordinator.guard_level"

PROFILES = ("consumer", "author")
LEVELS = ("strict", "warn", "off")

DEV_REPO_SENTINEL = ".coordinator-dev-repo"

#: The plain verb that lowers a guard, quoted verbatim in advisory and deny text.
LEVEL_VERB = "machine-local set coordinator.guard_level warn"

_ENV_PREFIX = "MACHINE_LOCAL_COORDINATOR_"

_cache: Dict[Tuple[Any, ...], Dict[str, Any]] = {}


def reset_cache() -> None:
    _cache.clear()


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _cache_key() -> Tuple[Any, ...]:
    try:
        reg_dir = machine_resolver.registry_dir()
    except ValueError:  # an unresolvable CLAUDE_HOME must not raise out of a guard
        reg_dir = Path("<unresolvable-registry-dir>")
    env = tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith(_ENV_PREFIX)))
    return (
        str(reg_dir),
        _mtime(reg_dir / "registry.toml"),
        _mtime(reg_dir / "registry.local.toml"),
        env,
    )


def _slot() -> Dict[str, Any]:
    key = _cache_key()
    slot = _cache.get(key)
    if slot is None:
        _cache.clear()
        slot = _cache[key] = {}
    return slot


def _sentinel_repo() -> Optional[str]:
    """First registered ``repos.*`` path carrying the dev-repo sentinel, else ``None``."""
    try:
        flat = machine_resolver.merged_flat_registry()
    except Exception:  # noqa: BLE001 -- unreadable registry degrades to consumer
        return None
    for key, value in flat.items():
        if not key.startswith("repos.") or not isinstance(value, str) or not value:
            continue
        try:
            if (Path(value) / DEV_REPO_SENTINEL).exists():
                return value
        except OSError:
            continue
    return None


def _explicit(key: str, allowed: Tuple[str, ...]) -> Optional[str]:
    try:
        raw = machine_resolver.registry_get(key)
    except Exception:  # noqa: BLE001
        return None
    value = (raw or "").strip().lower()
    return value if value in allowed else None


def machine_profile_source() -> Tuple[str, str, str | None]:
    """``(profile, rung, evidence)``: which rung decided the profile.

    ``rung`` is ``explicit-key`` (evidence: the key), ``sentinel`` (evidence:
    the registered repo path carrying ``.coordinator-dev-repo``) or
    ``default`` (no evidence; consumer). Cached like ``machine_profile``.
    """
    slot = _slot()
    cached = slot.get("profile-source")
    if cached is not None:
        return cached
    explicit = _explicit(PROFILE_KEY, PROFILES)
    if explicit is not None:
        result: Tuple[str, str, str | None] = (explicit, "explicit-key", PROFILE_KEY)
    else:
        repo = _sentinel_repo()
        result = ("author", "sentinel", repo) if repo else ("consumer", "default", None)
    slot["profile-source"] = result
    return result


def machine_profile() -> str:
    """``consumer`` or ``author``; explicit registry key, else sentinel-derived."""
    slot = _slot()
    cached = slot.get("profile")
    if cached is not None:
        return cached
    value = machine_profile_source()[0]
    slot["profile"] = value
    return value


FEATURE_KEY = "coordinator.feature."
FEATURES = (
    "cross_repo_memos",
    "publishing",
    "doctrine_edit_gate",
    "gem_cross_repo_commit",
    "out_of_repo_write_block",
)
_FEATURE_LABEL = {
    "out_of_repo_write_block": "the out-of-repo write block",
    "cross_repo_memos": "cross-repo memos",
    "publishing": "publishing (percolate)",
    "doctrine_edit_gate": "the doctrine-edit approval gate",
    "gem_cross_repo_commit": "Group EM cross-repo commit",
}

#: Unset default per feature: ``"profile"`` follows the machine profile
#: (on for author, off for consumer); ``"on"``/``"off"`` hold on every profile.
_FEATURE_DEFAULT = {
    "cross_repo_memos": "profile",
    "publishing": "profile",
    "doctrine_edit_gate": "on",
    "gem_cross_repo_commit": "on",
    "out_of_repo_write_block": "profile",
}


def _feature_default(name: str) -> str:
    default = _FEATURE_DEFAULT[name]
    if default == "profile":
        return "on" if machine_profile() == "author" else "off"
    return default


def feature_enabled(name: str) -> bool:
    """Whether feature ``name`` (see ``FEATURES``) is on.

    Explicit ``coordinator.feature.<name>`` (``on``/``off``) wins; absent, the
    ``_FEATURE_DEFAULT`` entry decides.
    """
    if name not in _FEATURE_LABEL:
        raise KeyError(name)
    slot = _slot()
    memo = "feature:" + name
    cached = slot.get(memo)
    if cached is None:
        cached = _explicit(FEATURE_KEY + name, ("on", "off")) or _feature_default(name)
        slot[memo] = cached
    return cached == "on"


def feature_refusal(name: str) -> Optional[str]:
    """One-line refusal when feature ``name`` is off on this machine, else ``None``."""
    if feature_enabled(name):
        return None
    return (
        f"{_FEATURE_LABEL[name]} is off on this machine; "
        f"enable it with: machine-local set {FEATURE_KEY}{name} on"
    )


def out_of_repo_write_blocked() -> bool:
    """True when ``out_of_repo_write_block`` is on: the author-box default."""
    return feature_enabled("out_of_repo_write_block")


def guard_level(guard_name: str) -> str:
    """``strict``, ``warn`` or ``off`` for ``guard_name``.

    Per-guard key, then the guard's ``GUARD_DEFAULT_LEVEL`` entry, then the
    global key; absent all three, ``warn`` on every profile.
    """
    slot = _slot()
    memo = "level:" + guard_name
    cached = slot.get(memo)
    if cached is not None:
        return cached
    level = (
        _explicit(LEVEL_KEY + "." + guard_name, LEVELS)
        or GUARD_DEFAULT_LEVEL.get(guard_name)
        or _explicit(LEVEL_KEY, LEVELS)
        or "warn"
    )
    slot[memo] = level
    return level


#: Per-guard unset default for ``guard_level``. Empty: ``resolved_level`` clamps
#: every unregistered guard to ``log``, so no default can raise one to a deny.
GUARD_DEFAULT_LEVEL: Dict[str, str] = {}

#: Derived views of ``deny_registry``: the guards whose deny ``apply_guard_level``
#: returns unchanged. The registry is the only place to add one.
FLOOR_GUARDS = frozenset(deny_registry.GUARD_DENY_CLASS)
SUBAGENT_FLOOR_GUARDS = frozenset(deny_registry.SUBAGENT_DENY_CLASS)


def _deny_body(envelope: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(envelope, dict):
        return None
    hso = envelope.get("hookSpecificOutput")
    if isinstance(hso, dict) and hso.get("permissionDecision") == "deny":
        return hso
    if envelope.get("decision") == "block":
        # Stop-event deny (see _hook_envelope.deny), read through the same body.
        return {"hookEventName": "Stop", "permissionDecisionReason": envelope.get("reason")}
    return None


def is_deny_envelope(envelope: Any) -> bool:
    """True for a ``permissionDecision: "deny"`` envelope; total over any input."""
    return _deny_body(envelope) is not None


#: The warn-level advisory frame: mechanism-owned, not guard copy. The size
#: harness classifies ``ADVISORY_PREFIX`` and ``advisory_tail`` bytes as tail.
ADVISORY_PREFIX = "Advisory: "


def advisory_tail(name: str) -> str:
    """The fixed escalate/silence route that closes every warn advisory."""
    return (
        "Stricter: `machine-local set coordinator.guard_level strict`; "
        "silence: `machine-local set coordinator.guard_level.%s off`." % name
    )


#: Top-level key on a returned envelope naming the level that decided it
#: (``floor`` or ``log``), so a canary reads it from the hook output.
LEVEL_RECORD_KEY = "guardLevel"


def _record_level(envelope: Dict[str, Any], level: str) -> Dict[str, Any]:
    """Stamp ``level`` on ``envelope`` in place; identity is preserved."""
    envelope[LEVEL_RECORD_KEY] = level
    return envelope


def resolved_level(guard_name: str, *, subagent: bool = False) -> str:
    """``floor`` for a guard registered in ``deny_registry``, else ``log``.

    The single resolution ``apply_guard_level`` and decision records share. No
    registry key, level key or profile lifts an unregistered guard above ``log``.
    """
    name = guard_name.replace("_", "-")
    if name in FLOOR_GUARDS or (subagent and name in SUBAGENT_FLOOR_GUARDS):
        return "floor"
    return "log"


def apply_guard_level(
    guard_name: str,
    deny_envelope: Optional[Dict[str, Any]],
    *,
    risk: Optional[str] = None,
    once: Optional[Tuple[Any, str]] = None,
    subagent: bool = False,
    session_id: str = "",
    cwd: Optional[Any] = None,
    event_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Resolve a guard's deny envelope at the one policy point.

    Input that is not a ``permissionDecision: "deny"`` envelope is returned
    unchanged, so a seam may re-apply to an already-resolved envelope.
    ``guard_name`` is kebab-normalised. A ``deny_registry`` guard's deny is
    returned unchanged, stamped ``guardLevel: floor``. Any other guard resolves
    to ``log``: the would-be deny is recorded through ``guard_advisory_counter``
    and becomes one ``ADVISORY_PREFIX`` line (an allow plus context on
    PreToolUse), at every ``guard_level`` and profile. With ``event_id`` (the
    hook payload's ``tool_use_id``) and a resolvable ``session_id``, only the
    first call per event across all processes returns the line; later ones
    return ``None``. ``risk`` overrides the line's text; ``once=(gitdir,
    session_id)`` or ``session_id``/``cwd`` locate the record, and no record is
    written without a session id.
    """
    hso = _deny_body(deny_envelope)
    if hso is None:
        return deny_envelope
    name = guard_name.replace("_", "-")
    level = resolved_level(name, subagent=subagent)
    if level == "floor":
        return _record_level(deny_envelope, level)
    sid = session_id or (once[1] if once else "")
    where = cwd or (once[0] if once else None)
    _record_log(name, sid, where)
    if event_id and not _claim_event(sid, where, event_id):
        return None
    return _nudge_envelope(name, hso, risk)


_NUDGE_MAX = 400


def _nudge_envelope(name: str, hso: Dict[str, Any], risk: Optional[str]) -> Dict[str, Any]:
    """The one-line advisory for a would-be deny; no override key."""
    if risk is None:
        risk = str(hso.get("permissionDecisionReason") or "")
    line = " ".join(risk.split())
    if line.startswith("BLOCKED:"):
        line = line[len("BLOCKED:"):].strip()
    if len(line) > _NUDGE_MAX:
        line = line[: _NUDGE_MAX - 3] + "..."
    advisory = ADVISORY_PREFIX + (line or name)
    event = hso.get("hookEventName") or "PreToolUse"
    if event in ("Stop", "SubagentStop"):
        return _record_level({"systemMessage": advisory}, "log")
    out: Dict[str, Any] = {"hookEventName": event, "additionalContext": advisory}
    if event == "PreToolUse":
        out["permissionDecision"] = "allow"
    return _record_level({"hookSpecificOutput": out}, "log")


def _claim_event(session_id: str, cwd: Optional[Any], event_id: str) -> bool:
    """True when this call is the first to claim ``event_id``: an ``O_CREAT|O_EXCL``
    marker under the session share dir. Fails open (True) when it cannot be placed."""
    if not session_id:
        return True
    try:
        import hashlib

        from coordinator_core.session.machinery_paths import share_dir
        from coordinator_core.subagent_sandbox import resolve_git_root_cheap

        root = resolve_git_root_cheap(str(cwd) if cwd else None)
        if not root:
            return True
        key = hashlib.sha256((session_id + "\0" + event_id).encode("utf-8")).hexdigest()[:24]
        d = Path(share_dir(root, session_id)) / "advisory-event-claims"
        d.mkdir(parents=True, exist_ok=True)
        os.close(os.open(str(d / key), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        return True
    except FileExistsError:
        return False
    except Exception:  # noqa: BLE001 -- a marker fault must never swallow the nudge
        return True


def _record_log(guard_name: str, session_id: str, cwd: Optional[Any]) -> None:
    """Append the would-be deny to the advisory counter; a fault never reaches the caller."""
    try:
        from coordinator_core.guard_advisory_counter import record_advisory_fire

        record_advisory_fire(guard_name, str(session_id or ""), str(cwd) if cwd else None)
    except Exception:  # noqa: BLE001 -- telemetry must never turn a guard into a crash
        return
