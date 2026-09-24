"""coordinator_core.hooks.guard_doctrine_surface_ratio — PreToolUse
(Write|Edit|MultiEdit) op: Layer 1 leg 1a of the doctrinal surface weight
ratchet -- prices a byte-adding write to a doctrine surface against its D2
tier, ADVISORY ONLY on the ratio path; denies only a new file that overruns
its admission cap.

Arrival note (W4-C5, `docs/plans/2026-09-18-doe-holds-no-scripts.md`): ported
from DoE-claude `coordinator/hooks/scripts/guard-doctrine-surface-ratio.py`.
That script ran as an in-process guard body enrolled into a second,
doctrine-plane-resident guard registry (`_guard_runner.REAL_GUARD_REGISTRY`),
fired via `preuse-write-dispatch.py`'s own dispatch, with the same double-
registration hazard `guard_doctrine_changelog_prose`'s arrival note already
describes. None of that plumbing applies here: this lands as its own
`hooks.<name>` op per this row's body, called directly by whatever door
dials `hooks.guard_doctrine_surface_ratio`. The stdin/stdout JSON payload
read and `_message_envelope.emit()`'s stdout-writing channel dispatch are
replaced with the payload-dict-in/response-out `register_op` contract and
direct `allow_advisory`/`deny` envelope construction, same shape as the
sibling guards this same row lands. The pure `evaluate()` core (kept, for
the same reason the source kept it separable from stdin/envelope plumbing)
now returns `(Message, channel)` where `channel` is one of the two local
sentinels `_CHANNEL_ADVISORY`/`_CHANNEL_DENY` in place of the retired
`_message_envelope.CHANNEL_ADDITIONAL_CONTEXT`/`CHANNEL_DENY` constants --
same two-channel shape, no behaviour change. No other shape change: the
tier/admission-cap seam is `coordinator_core.doctrine_surface_tiers`
(already landed, W3-C6), the population scoper is
`coordinator_core.hooks.doctrine_changelog_prose.surface_of` (already
landed by this same chunk), and the byte-delta primitive is
`coordinator_core.write_guards._sentinel_write_guard.reconstruct_after` --
identical seams to `guard_doctrine_changelog_prose`'s own arrival.

Why this exists (unchanged from the source): D1: a per-commit net is the
mechanism that changes authoring behaviour, but a `PreToolUse` hook fires
before any commit exists -- it sees one tool call and cannot net across a
commit's other edits. Leg 1a's whole job is pricing: report what THIS edit
will cost at commit time and where the cut must land, before the paragraph
is even finished, which is when folding is still cheap. It never denies on
the ratio path -- the enforcing net is leg 1b
(`guard_doctrine_surface_ratio_precommit`, this same row), a native git
pre-commit hook, out of this module's scope.

Composes exactly the primitives the source names -- no new byte-delta
primitive, no second population scoper, no second ratchet, no second tier
owner:

  - Byte delta: `_sentinel_write_guard.reconstruct_after`, same contract as
    `guard_doctrine_changelog_prose`.
  - Population scoping: `doctrine_changelog_prose.surface_of`.
  - Tier lookup: `coordinator_core.doctrine_surface_tiers.tier_boundaries_for`,
    the ONE seam this guard reads Layer-2 ceiling data through. Never reads
    the doctrine-surface-weight baseline JSON directly and never imports
    anything from a test tree -- either would be an undeclared
    hooks-plane -> test-plane coupling the source forbids.
  - Verdict channel: `coordinator_core.hooks.support.message_envelope.compose`/
    `render`, plus `allow_advisory`/`deny`.

Ratio pricing scope -- three surfaces, not five
-------------------------------------------------
`tier_boundaries_for` is called on the STEP 5 (existing-file ratio) path
for `wiki`, `commands`, and `snippets` only. `agents` and `skills` keep
their own existing, unreplaced ratchets -- Step 5 is therefore a no-op
(silent allow) for `agents`/`skills`. Step 4 (new-file admission) is NOT
scoped this way -- the admission carve-out is priced across all five
surfaces (see `_ADMISSION_PRICED_SURFACES` below).

New-file admission cap (step 4)
---------------------------------
A file with no history has nothing to cut against, so a brand-new file is
priced by admission, not ratio, on ALL five surfaces:
`coordinator_core.doctrine_surface_tiers.admission_cap_for(surface)` -- the
wiki's own separate p90-derived `admission_cap` for `wiki`, falling through
to the surface's own `aspirational_ceiling` for every other surface. This
guard never reads either value from the baseline JSON itself and never
hardcodes either as a literal; both are read through this one export, the
same seam `tier_boundaries_for` is.

Advisory pricing and the 512 B floor (step 5)
----------------------------------------------------
On the ratio path, this leg reports what the edit will cost at commit AND
where the cut must land -- advisory only, NEVER a deny (this leg has no
enforcing authority). The price is the step-5 tier-plus-scope selection's
`ratio` times `delta_bytes` -- a single-edit estimate, not a
cross-invocation net; it does not attempt to track other edits in the same
uncommitted change. A single edit's `delta_bytes` under 512 B
(`_SUB_FLOOR_BYTES`) prices nothing: the guard returns `no_advisory()` --
silent, not advisory. Deferred cross-commit billing of sub-floor growth is
leg 1b's `sub_floor_accumulator`, out of this leg's scope.

ALGORITHM SKETCH (PreToolUse, target path resolved to one of the five
surfaces or no-op):
  1. Resolve target path via `extract_target_path`; no-op if unresolvable
     or `surface_of()` returns `None`.
  2. `before` = on-disk bytes ("" if new); `after = reconstruct_after(...)`.
     No-op if `reconstruct_after` returns `None` (fail-open).
  3. `delta_bytes = len(after.encode()) - len(before.encode())`.
     `delta_bytes <= 0` never prices anything -- allow silently.
  4. New file (`before == ""`) -- admission-cap carve-out: deny if
     `len(after.encode())` exceeds the surface's admission value.
  5. Existing file, `delta_bytes > 0`, surface in
     `_RATIO_PRICED_SURFACES` -- select the D2 tier (2:1/5:1/10:1) and its
     paired `credit_scope` off the file's PRE-EDIT size. Below the 512 B
     floor, silent. At or above it, emit the single-edit advisory price
     naming the pool owed. Leg 1b still owns per-commit netting, the
     reasoned-growth marker, and the sub-floor accumulator on top of this.

Fail-open (returns `no_advisory()`), in order: `tool_name` not in the
guarded set; no target path in `tool_input`; `surface_of()` is `None`
(wrong tree/extension, a `tests`/`fixtures` subdirectory); on-disk read
failure for an existing file; unreconstructable before/after;
`delta_bytes <= 0`.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C5
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core._hook_envelope import deny, payload_of
from coordinator_core.doctrine_surface_tiers import admission_cap_for, tier_boundaries_for
from coordinator_core.hooks._envelope import allow_advisory, no_advisory
from coordinator_core.hooks.doctrine_changelog_prose import surface_of
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.sentinel_write_guard import extract_target_path
from coordinator_core.ipc import register_op
from coordinator_core.write_guards._sentinel_write_guard import reconstruct_after

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit")

#: Surfaces D2's tier table (and this guard's step 5 ratio path) actually
#: prices. `agents`/`skills` keep their own existing, unreplaced ratchets
#: (see module docstring "Ratio pricing scope -- three surfaces, not
#: five").
_RATIO_PRICED_SURFACES = frozenset({"wiki", "commands", "snippets"})

#: All five surfaces are admission-priced on arrival -- see module
#: docstring "New-file admission cap (step 4)".
_ADMISSION_PRICED_SURFACES = frozenset({"wiki", "commands", "snippets", "agents", "skills"})

#: A single edit's `delta_bytes` under this floor prices nothing -- the
#: guard returns `no_advisory()`, silently. Below the floor is not
#: advisory, it is silent; deferred billing across commits is leg 1b's
#: `sub_floor_accumulator`, out of this leg's scope.
_SUB_FLOOR_BYTES = 512

#: Where the rule this guard enforces actually lives.
_RULE_ANCHOR = "the doctrinal surface weight ratchet"

_CHANNEL_ADVISORY = "advisory"
_CHANNEL_DENY = "deny"


def _select_tier(surface: str, pre_edit_size: int) -> "dict | None":
    """Step 5: select the D2 tier (ratio + `credit_scope`) `pre_edit_size`
    falls into for `surface`, or `None` if `surface` is not one of
    `_RATIO_PRICED_SURFACES`. Half-open band selection on the file's size
    BEFORE the edit, per `tier_boundaries_for`'s own docstring."""
    if surface not in _RATIO_PRICED_SURFACES:
        return None
    boundaries = tier_boundaries_for(surface)
    for band in boundaries["tiers"]:
        if band["max"] is None or pre_edit_size < band["max"]:
            return band
    return boundaries["tiers"][-1]


def _admission_deny_reason(target: str, surface: str, new_size: int, threshold: int) -> str:
    return (
        f"{target} arrives at {new_size} B, over {surface}'s {threshold} B "
        "new-file admission cap. A file with no history has nothing "
        "to cut against -- split it before creating it, or start smaller."
    )


def _admission_deny_message(target: str, surface: str, new_size: int, threshold: int):
    return compose(
        _admission_deny_reason(target, surface, new_size, threshold),
        anchor=_RULE_ANCHOR,
    )


def _advisory_owed_bytes(delta_bytes: int, tier: dict) -> int:
    """Single-edit advisory price: this edit's `delta_bytes` times its
    selected tier's ratio -- a single-edit estimate, not a
    cross-invocation net (leg 1b owns the enforcing per-commit net)."""
    return delta_bytes * tier["ratio"]


def _advisory_reason(target: str, surface: str, delta_bytes: int, tier: dict) -> str:
    owed = _advisory_owed_bytes(delta_bytes, tier)
    ratio = tier["ratio"]
    if tier["credit_scope"] == "file":
        pool = f"THIS FILE ({target})"
    else:
        pool = f"`{surface}`"
    return (
        f"+{delta_bytes} B to a {ratio}:1 file -- {owed} B of cuts owed on "
        f"{pool} this commit."
    )


def _advisory_message(target: str, surface: str, delta_bytes: int, tier: dict):
    return compose(
        _advisory_reason(target, surface, delta_bytes, tier),
        anchor=_RULE_ANCHOR,
    )


def evaluate(payload: dict):
    """Pure core: given a parsed PreToolUse payload, returns the
    `(Message, channel)` pair to build a verdict from, or `None` for a
    silent no-op. Separable from the `register_op` handler so tests can
    drive it with synthesized payloads directly, same shape as the sibling
    guard's `_advisory_message`/`_deny_message` split."""
    if not isinstance(payload, dict):
        return None
    tool_name = payload.get("tool_name", "")
    if tool_name not in _GUARDED_TOOLS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    target_raw = extract_target_path(tool_input)
    if not target_raw:
        return None

    try:
        target = Path(target_raw).resolve()
    except Exception:
        return None

    surface = surface_of(target)
    if surface is None:
        return None

    try:
        before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    except Exception:
        return None

    after = reconstruct_after(tool_name, tool_input, before)
    if after is None:
        return None

    delta_bytes = len(after.encode()) - len(before.encode())
    if delta_bytes <= 0:
        return None

    if before == "":
        if surface not in _ADMISSION_PRICED_SURFACES:
            return None
        new_size = len(after.encode())
        threshold = admission_cap_for(surface)
        if new_size > threshold:
            return _admission_deny_message(target_raw, surface, new_size, threshold), _CHANNEL_DENY
        return None

    pre_edit_size = len(before.encode())
    tier = _select_tier(surface, pre_edit_size)
    if tier is None:
        return None

    # The 512 B floor: a single edit's delta under the floor prices
    # nothing -- the guard is SILENT, not advisory. Deferred cross-commit
    # billing of sub-floor growth is leg 1b's sub_floor_accumulator, out
    # of this leg's scope.
    if delta_bytes < _SUB_FLOOR_BYTES:
        return None

    # Advisory pricing: names the price and the pool it must be paid
    # from, ADVISORY ONLY -- never a deny. This leg has no enforcing
    # authority; leg 1b is the enforcing net.
    return _advisory_message(target_raw, surface, delta_bytes, tier), _CHANNEL_ADVISORY


@register_op("hooks.guard_doctrine_surface_ratio")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Write|Edit|MultiEdit) op: price a byte-adding write to a
    doctrine surface against its D2 tier, advisory-only on the ratio path,
    deny-only on new-file admission overrun."""
    params = payload_of(params)
    result = evaluate(params)
    if result is None:
        return no_advisory()

    message, channel = result
    env = params.get("env")
    if channel == _CHANNEL_DENY:
        return deny("PreToolUse", render(message, env=env))
    return allow_advisory("PreToolUse", render(message, env=env))
