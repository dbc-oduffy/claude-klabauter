"""machine_profile.py -- the two machine-local settings that gate author-only behaviour.

``coordinator.machine_profile`` is ``consumer`` or ``author``. An explicit
registry value wins; absent, the box is ``author`` when any registered
``repos.*`` path carries a ``.coordinator-dev-repo`` sentinel at its root,
else ``consumer``.

``coordinator.guard_level`` is ``strict``, ``warn`` or ``off``, with a
per-guard override ``coordinator.guard_level.<guard-name>``. Absent, a
``GUARD_DEFAULT_LEVEL`` guard takes its entry; otherwise it follows the profile:
``strict`` on an author box, ``warn`` on a consumer box. Warn is a consumer
courtesy; the box that builds coordinator holds the standard.
``FLOOR_GUARDS`` (irreversible-harm guards and the consumed-handoff freeze)
never consult it.

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

Change the level with ``machine-local set coordinator.guard_level warn``
(or ``strict`` / ``off``); one guard with
``machine-local set coordinator.guard_level.<guard-name> off``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from coordinator_core import machine_resolver

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
FEATURES = ("cross_repo_memos", "publishing", "doctrine_edit_gate")
_FEATURE_LABEL = {
    "cross_repo_memos": "cross-repo memos",
    "publishing": "publishing (percolate)",
    "doctrine_edit_gate": "the doctrine-edit approval gate",
}

#: Unset default per feature: ``"profile"`` follows the machine profile
#: (on for author, off for consumer); ``"on"``/``"off"`` hold on every profile.
_FEATURE_DEFAULT = {
    "cross_repo_memos": "profile",
    "publishing": "profile",
    "doctrine_edit_gate": "on",
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


def guard_level(guard_name: str) -> str:
    """``strict``, ``warn`` or ``off`` for ``guard_name``.

    Per-guard key, then the guard's ``GUARD_DEFAULT_LEVEL`` entry, then the
    global key; absent all three, ``strict`` on an author box and ``warn`` on a
    consumer box.
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
        or ("strict" if machine_profile() == "author" else "warn")
    )
    slot[memo] = level
    return level


#: Per-guard unset default that outranks the global ``coordinator.guard_level``.
#: Not the floor: the per-guard key still lowers these.
GUARD_DEFAULT_LEVEL = {
    # PM ruling 2026-10-04: a fast/full suite run for a one-line change is the
    # waste this guard exists to stop, so a box-wide `warn` must not open it.
    "check-test-suite-invocation": "strict",
    # Report-only until its census is tuned on live logs (Group EM coordinator-content-repo-6c, 2026-10-10):
    # every would-deny lands in <settings-home>/heavy-admission/would-deny.jsonl and nothing is
    # shown to the caller. The flip back to strict is
    # state/bug-backlog/2026-10-10-heavy-command-admission-guard-is-report-8e3c3b48d41a.yaml.
    "guard-heavy-command-admission": "off",
}

#: Guards whose deny is floor for a dispatched caller only: no level lowers it
#: for a subagent, while the EM's deny still follows ``guard_level``.
SUBAGENT_FLOOR_GUARDS = frozenset({
    "check-test-suite-invocation",
    # A subagent's UE build or editor run overlaps the EM's slot; the prose rule already failed
    # once (DoE bug 763a311d4442), so a box-wide `warn` must not reopen it.
    "guard-subagent-heavy-ue-launch",
})


#: Guards whose deny prevents irreversible harm; ``apply_guard_level`` returns
#: their deny unchanged at every level.
FLOOR_GUARDS = frozenset(
    {
        "destructive-git-orphan",  # orphans unreferenced commits and uncommitted work, unrecoverable
        "destructive-rm",  # recursive delete has no undo
        "destructive-git-clean",  # removes untracked files git never stored
        "destructive-git-revert",  # discards working-tree changes git never committed
        "block-subagent-destructive-action",  # a subagent's destructive act on a shared tree
        "block-stash-destruction",  # drop/clear of a stash loses the only copy
        "block-topic-branch",  # fleet rule: commit only to the day branch; a level must not lower it
        "block-perforce-submit",  # PM box policy: nothing is submitted or shelved to Perforce
        "block-unreal-engine-resave",  # an engine-content rewrite needs a launcher Verify to undo
        "block-editor-kill-by-name",  # a name-based kill takes down every session's editor
        # PM-ratified invariant, never let it through (git-revertible, so not irreversible harm):
        # docs/wiki/pretooluse-write-guards.md § Guard policy permanence
        "block-consumed-handoff-edit",
        # a demotable sentinel guard lets an agent lower the level that gates it
        "block-approval-sentinel-creation",
        # PM load norm (CLAUDE.md § Load norm): a whole-drive scan occupies the box for
        # tens of minutes and runs on orphaned after a tool timeout
        "block-whole-filesystem-scan",
    }
)


def _deny_body(envelope: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(envelope, dict):
        return None
    hso = envelope.get("hookSpecificOutput")
    if isinstance(hso, dict) and hso.get("permissionDecision") == "deny":
        return hso
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


def apply_guard_level(
    guard_name: str,
    deny_envelope: Optional[Dict[str, Any]],
    *,
    risk: Optional[str] = None,
    once: Optional[Tuple[Any, str]] = None,
    subagent: bool = False,
) -> Optional[Dict[str, Any]]:
    """Map a guard's deny envelope through its configured level.

    Input that is not a ``permissionDecision: "deny"`` envelope is returned
    unchanged, so a seam may re-apply to an already-resolved envelope.
    ``guard_name`` is kebab-normalised. A ``FLOOR_GUARDS`` member's deny is
    returned unchanged at every level, as is a ``SUBAGENT_FLOOR_GUARDS``
    member's when ``subagent`` is true. strict returns the deny; off returns
    ``None``; warn returns a one-line advisory shaped by the envelope's
    ``hookEventName``: PreToolUse becomes ``allow`` plus ``additionalContext``,
    any other event ``additionalContext`` alone. ``risk=None`` derives the text
    from the deny's first reason paragraph. ``once=(gitdir, session_id)`` gives
    once-per-session delivery from a marker under ``gitdir`` (a repeat warn
    returns ``None``).
    """
    hso = _deny_body(deny_envelope)
    if hso is None:
        return deny_envelope
    name = guard_name.replace("_", "-")
    if name in FLOOR_GUARDS or (subagent and name in SUBAGENT_FLOOR_GUARDS):
        return deny_envelope
    level = guard_level(name)
    if level == "strict":
        return deny_envelope
    if level == "off":
        return None
    if once is not None and _already_warned(name, once):
        return None
    if risk is None:
        reason = str(hso.get("permissionDecisionReason") or "").strip()
        paras = reason.split("\n\n")
        risk = " ".join(paras[0].split())
        if risk.endswith(":") and len(paras) > 1:
            # A paragraph ending in ":" is a header; its list is the content.
            risk = "\n".join(
                [risk] + [" ".join(p.split()) for p in paras[1:] if p.strip()]
            ) + "\n"
        # A warn-level advisory never blocked anything; do not let the
        # embedded deny text claim it did.
        if risk.startswith("BLOCKED:"):
            risk = "would be blocked at strict level:" + risk[len("BLOCKED:"):]
    event = hso.get("hookEventName") or "PreToolUse"
    out: Dict[str, Any] = {
        "hookEventName": event,
        "additionalContext": (
            ADVISORY_PREFIX + risk.strip() + " " + advisory_tail(name)
        ),
    }
    if event == "PreToolUse":
        out["permissionDecision"] = "allow"
    return {"hookSpecificOutput": out}


def _already_warned(guard_name: str, once: Tuple[Any, str]) -> bool:
    """True when this session already received ``guard_name``'s warning;
    records the first delivery. Fails open (returns False) on any error."""
    gitdir, session_id = once
    try:
        from coordinator_core.bash_guards import _advisory_dedupe as dedupe

        key = guard_name + "__guard-level-warn"
        if dedupe.already_advised(gitdir, session_id, key):
            return True
        dedupe.mark_advised(gitdir, session_id, key)
    except Exception:  # noqa: BLE001 -- a marker fault must never swallow the warning
        return False
    return False
