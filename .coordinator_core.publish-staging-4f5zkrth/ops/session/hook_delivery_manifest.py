"""
coordinator_core.ops.session.hook_delivery_manifest — reader for the
hook-delivery manifest block DoE's carriers embed inside `hooks.json`.

Purpose: `guard_settings_integrity.detect_hook_delivery_duplication`
compares hook-delivery surfaces by raw script filename. That comparison
went blind the moment DoE's fan-in carriers started delivering many guards
under one filename (`preuse-write-dispatch.py`,
`postuse-stop-family-dispatch.py`, `postuse-advisory-dispatch.py`) — a
`settings.json` entry naming a carrier-delivered guard directly shares no
filename with any plugin-side entry, so it reads as "settings-only"
instead of "duplicate". This module parses the declared-export block that
lets the comparator (C3, `guard_settings_integrity.py`) difference
EFFECTIVE GUARD SETS instead of filenames. Full contract:
`docs/reference/hook-delivery-manifest.md` (C1).

Spec backlink: `pln-hook-delivery-duplication-dete-baf712`,
task C2, AC2.

Negative spec (SessionStart boot path, same discipline as this file
family's sibling predicate `_hook_layer_reachable` in
`guard_settings_integrity.py` — see that function's docstring for the
closest in-repo oracle for how this file family typed-degrades a boot-path
read):
  - Never raises. Every parse failure is a typed `state`, not an exception.
  - Never calls `resolve_content_root`, never opens a file. The whole
    input is the ALREADY-PARSED `hooks.json` dict handed in by the caller
    (`guard_settings_integrity.detect_hook_delivery_duplication` resolves
    and parses `hooks.json` once per boot; this module must not add a
    second resolution/read).
  - No import outside `coordinator_core`, no subprocess, no network.

Manifest block shape (see `docs/reference/hook-delivery-manifest.md` for
the authoritative contract; this docstring restates only what pins the
reader's behaviour):

    "x-effective-delivery": {
      "version": 1,
      "carriers": {
        "scripts/preuse-write-dispatch.py": {
          "guards": [{"id": "check_claude_md_size",
                       "script": "scripts/check-claude-md-size.py",
                       "tool_names": ["Bash"]}]
        }
      },
      "direct":  [{"id": "<guard id>", "script": "<script tail key>",
                    "tool_names": ["<tool name>", "..."]}],
      "retired": [{"id": "<guard id>", "script": "<script tail key>",
                    "reason": "<single line>"}]
    }

Every `script` field (and every carrier key) is a TAIL KEY in the SAME
normal form `guard_settings_integrity._tail_key` computes — the last two
path segments, forward-slash-joined, lower-cased. `_tail_key` is NOT
imported here (that would be a circular import: `guard_settings_integrity`
imports this module, not the reverse) — the caller passes
`declared_script_keys` already normalized to that form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

MANIFEST_KEY = "x-effective-delivery"
SUPPORTED_VERSIONS = frozenset({1})
MAX_FIELD_LEN = 200

# Same shape as `guard_settings_integrity._TAIL_KEY_RE`: last two
# path segments, forward-slash-joined. Used only to validate that a
# `script`/carrier-key field is already in tail-key normal form —
# never to compute one from a raw command token (that stays this
# module's caller's job, via `_tail_key`, to avoid a circular import).
_TAIL_KEY_SHAPE_RE = re.compile(r"^[^/\s]+/[^/\s]+$")

# Printable-only, single-line contract (C1): reject anything with a
# control/escape character, not merely `\n`/`\r`.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ManifestGuard:
    id: str
    script: str
    # Review: coordinator:code-reviewer — contract-mandated field (per
    # docs/reference/hook-delivery-manifest.md), stored/exposed only; no
    # matcher logic consumes this yet (a separate plan owns that).
    tool_names: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RetiredGuard:
    id: str
    script: str
    reason: str


@dataclass(frozen=True)
class HookDeliveryManifest:
    state: str
    carriers: Mapping[str, Tuple[ManifestGuard, ...]] = field(default_factory=dict)
    direct: Tuple[ManifestGuard, ...] = ()
    retired: Tuple[RetiredGuard, ...] = ()
    # A script tail key maps to ALL guard ids delivered under it, not one.
    # The contract's tail key is the last two path segments, so a fan-in
    # module hosting N distinct guards normalizes N ids onto one key by
    # design (`bash_guards/dispatch_checks.py`, 16 guards).
    script_index: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    unaccounted: Tuple[str, ...] = ()
    detail: str = ""


def _empty(state: str, detail: str = "") -> HookDeliveryManifest:
    return HookDeliveryManifest(state=state, detail=detail)


def _sanitize_field(value: object) -> Tuple[str, bool]:
    """Enforce the per-field string contract (single-line, printable-only,
    capped at `MAX_FIELD_LEN`). Returns `(sanitized, violated)` — a
    violating value is truncated-and-marked, never passed through
    unmodified, per C1's "truncate + mark, or degrade the entry" rule."""
    if not isinstance(value, str):
        return ("", True)
    violated = False
    out = value
    if _CONTROL_CHAR_RE.search(out):
        violated = True
        out = _CONTROL_CHAR_RE.sub("", out)
    if len(out) > MAX_FIELD_LEN:
        violated = True
        out = out[:MAX_FIELD_LEN]
    if not out:
        violated = True
    return (out, violated)


def _sanitize_tail_key(value: object) -> Tuple[Optional[str], bool]:
    sanitized, violated = _sanitize_field(value)
    if violated or not _TAIL_KEY_SHAPE_RE.match(sanitized):
        return (None, True)
    return (sanitized.lower(), False)


def _sanitize_tool_names(raw: object) -> Tuple[Optional[Tuple[str, ...]], bool]:
    """Enforce `tool_names` as a REQUIRED list-of-strings field, per C1
    (`docs/reference/hook-delivery-manifest.md`). Returns
    `(sanitized_tuple_or_None, violated)` — `violated` is `True` (and the
    first element `None`) when `raw` is missing, not a list, or contains
    any non-string/violating element; the whole guard entry is malformed
    in that case, matching the `id`/`script` required-field contract."""
    if not isinstance(raw, list):
        return (None, True)
    names = []
    for item in raw:
        name, violated = _sanitize_field(item)
        if violated:
            return (None, True)
        names.append(name)
    return (tuple(names), False)


def _parse_guard_entry(raw: object) -> Tuple[Optional[ManifestGuard], bool]:
    """Returns `(guard_or_None, malformed)`. `malformed` is `True` when
    `raw` itself is present but fails a required-field check (`id`,
    `script`, or `tool_names` — C1 requires all three per entry); the
    caller escalates that to a manifest-level `malformed` state rather
    than silently dropping the entry."""
    if not isinstance(raw, dict):
        return (None, True)
    guard_id, id_violated = _sanitize_field(raw.get("id"))
    # `_script_violated` is redundant with `script is None` (`_sanitize_tail_key`
    # returns `None` exactly on violation) — discarded intentionally.
    script, _ = _sanitize_tail_key(raw.get("script"))
    tool_names, tool_names_violated = _sanitize_tool_names(raw.get("tool_names"))
    if id_violated or script is None or tool_names_violated:
        return (None, True)
    return (ManifestGuard(id=guard_id, script=script, tool_names=tool_names), False)


def _parse_retired_entry(raw: object) -> Optional[RetiredGuard]:
    if not isinstance(raw, dict):
        return None
    guard_id, id_violated = _sanitize_field(raw.get("id"))
    # `_script_violated` is redundant with `script is None` (`_sanitize_tail_key`
    # returns `None` exactly on violation) — discarded intentionally.
    script, _ = _sanitize_tail_key(raw.get("script"))
    # `_reason_violated` is not fatal to the entry — `reason` degrades via
    # truncate-and-mark, per C1's per-field contract.
    reason, _ = _sanitize_field(raw.get("reason"))
    if id_violated or script is None:
        return None
    return RetiredGuard(id=guard_id, script=script, reason=reason)


def read_hook_delivery_manifest(
    hooks_json: object,
    declared_script_keys: Sequence[str],
) -> HookDeliveryManifest:
    """Parse the `x-effective-delivery` block out of the already-parsed
    `hooks.json` dict `hooks_json`, and degrade to a typed `state` for
    every bad case rather than raising. `declared_script_keys` are the
    plugin-side script tail keys `hooks.json` itself declares, already
    `_tail_key`-normalized by the caller — used only to compute `stale`
    (C1's exhaustiveness requirement)."""
    # Review: coordinator:code-reviewer — `declared_script_keys` is caller-
    # supplied like everything else this reader touches; a non-iterable
    # (e.g. `None`) must degrade, not raise, per the never-raise contract.
    # Element-level garbage (e.g. an unhashable `dict`/`list` element) is a
    # same-shaped hazard one level deeper: the `key not in accounted` set
    # membership test below hashes `key`, so a non-string, unhashable
    # element must be dropped here rather than surviving to that probe.
    if not isinstance(declared_script_keys, (list, tuple)):
        declared_script_keys = ()
    else:
        declared_script_keys = tuple(
            key for key in declared_script_keys if isinstance(key, str)
        )

    if not isinstance(hooks_json, dict):
        return _empty("absent", "hooks.json was not a parsed dict")

    if MANIFEST_KEY not in hooks_json:
        return _empty("absent", f"no {MANIFEST_KEY!r} key in hooks.json")
    block = hooks_json.get(MANIFEST_KEY)
    if not isinstance(block, dict):
        return _empty("malformed", f"{MANIFEST_KEY!r} is not an object")

    version = block.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return _empty("malformed", "version field missing or not an integer")
    if version not in SUPPORTED_VERSIONS:
        return _empty("version_unsupported", f"manifest version {version!r} not supported")

    carriers_raw = block.get("carriers", {})
    if not isinstance(carriers_raw, dict):
        return _empty("malformed", "carriers field is not an object")

    direct_raw = block.get("direct", [])
    if not isinstance(direct_raw, list):
        return _empty("malformed", "direct field is not a list")

    retired_raw = block.get("retired", [])
    if not isinstance(retired_raw, list):
        return _empty("malformed", "retired field is not a list")

    carriers: Dict[str, Tuple[ManifestGuard, ...]] = {}
    script_index: Dict[str, List[str]] = {}
    # A repeated script tail key is NOT a defect: the contract's tail key is
    # the last two path segments, so a fan-in module hosting N distinct
    # guards collapses N ids onto one key by construction, and a guard
    # delivered by two paths (a direct registration plus a carrier's carry)
    # is a real, declarable shape that must not be hidden by dropping either
    # side. What IS a defect is the same guard id declared twice within ONE
    # delivery surface — a double-registration the sender can act on.
    duplicate_within_surface: Optional[Tuple[str, str]] = None
    ids_by_surface: Dict[str, Set[str]] = {}

    def _note(surface: str, guard: ManifestGuard) -> Optional[Tuple[str, str]]:
        seen = ids_by_surface.setdefault(surface, set())
        collision = (surface, guard.id) if guard.id in seen else None
        seen.add(guard.id)
        bucket = script_index.setdefault(guard.script, [])
        if guard.id not in bucket:
            bucket.append(guard.id)
        return collision

    for carrier_key_raw, carrier_body in carriers_raw.items():
        carrier_key, key_violated = _sanitize_tail_key(carrier_key_raw)
        if key_violated or carrier_key is None or not isinstance(carrier_body, dict):
            continue
        guards_raw = carrier_body.get("guards", [])
        if not isinstance(guards_raw, list):
            continue
        guards = []
        for entry in guards_raw:
            guard, guard_malformed = _parse_guard_entry(entry)
            if guard_malformed:
                return _empty(
                    "malformed",
                    f"guard entry in carrier {carrier_key!r} is missing a required field "
                    "(id, script, or tool_names) or has a malformed tool_names value",
                )
            if guard is None:
                continue
            collision = _note(carrier_key, guard)
            if duplicate_within_surface is None and collision is not None:
                duplicate_within_surface = collision
            guards.append(guard)
        carriers[carrier_key] = tuple(guards)

    direct = []
    for entry in direct_raw:
        guard, guard_malformed = _parse_guard_entry(entry)
        if guard_malformed:
            return _empty(
                "malformed",
                "a direct guard entry is missing a required field (id, script, or "
                "tool_names) or has a malformed tool_names value",
            )
        if guard is None:
            continue
        collision = _note("direct", guard)
        if duplicate_within_surface is None and collision is not None:
            duplicate_within_surface = collision
        direct.append(guard)
    direct_t = tuple(direct)

    if duplicate_within_surface is not None:
        surface, guard_id = duplicate_within_surface
        where = "direct" if surface == "direct" else f"carrier {surface!r}"
        return _empty(
            "malformed",
            f"guard id {guard_id!r} is declared more than once within {where}",
        )

    retired = []
    for entry in retired_raw:
        r = _parse_retired_entry(entry)
        if r is None:
            continue
        retired.append(r)
    retired_t = tuple(retired)

    retired_scripts = {r.script for r in retired_t}
    # The contract requires each GUARD to appear in exactly one of
    # carriers/direct/retired; a guard both live and retired degrades to
    # `malformed` rather than silently coexisting. Keyed on the guard id,
    # not the script tail key: a fan-in module can legitimately host a
    # retired guard alongside live ones under the one key, and keying on
    # the key would call that a contradiction when it is the normal case.
    live_ids = {guard_id for ids in script_index.values() for guard_id in ids}
    live_and_retired_overlap = live_ids & {r.id for r in retired_t}
    if live_and_retired_overlap:
        offending = sorted(live_and_retired_overlap)[0]
        return _empty(
            "malformed",
            f"guard id {offending!r} appears in both a live guard entry and retired",
        )

    frozen_index: Dict[str, Tuple[str, ...]] = {
        key: tuple(ids) for key, ids in script_index.items()
    }
    accounted = set(script_index) | set(carriers) | retired_scripts
    unaccounted = tuple(
        key for key in declared_script_keys if key not in accounted
    )

    if unaccounted:
        return HookDeliveryManifest(
            state="stale",
            carriers=carriers,
            direct=direct_t,
            retired=retired_t,
            script_index=frozen_index,
            unaccounted=unaccounted,
            detail=f"hooks.json declares {unaccounted[0]!r}, unaccounted for by the manifest",
        )

    return HookDeliveryManifest(
        state="ok",
        carriers=carriers,
        direct=direct_t,
        retired=retired_t,
        script_index=frozen_index,
        unaccounted=(),
        detail="",
    )
