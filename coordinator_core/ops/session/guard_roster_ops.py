"""
coordinator_core.ops.session.guard_roster_ops — eager listing seam for the
*historically-ported* advisory hook op population in `coordinator_core.hooks`:
the six ops brought over from the `~/.claude` advisory/nudge command hooks.

NEGATIVE SPEC — this is NOT carrier membership. It does not enumerate what
DoE-claude's `postuse-advisory-dispatch.py` delivers, and must never be read
as though it does. Verified against DoE's live `hooks.json` on 2026-08-13
(memo 2026-08-13-doe-claude-em-advisory-carrier-boundary-diverges-and-script-tail-answer.md):
that carrier issues exactly two `dispatch_message()` calls —
`hooks.postuse_advisory_dispatch` and `hooks.track_touched_files` (the latter
bookkeeping, not advisory) — so exactly one of the six names below is
carrier-delivered. Of the rest, `suggest_sonnet_research` and
`nudge_named_agent_report_delivery` have their own separate live
registrations; `nudge_foreground_agent_dispatch`'s DoE script is fully
deregistered and its logic now runs as a pure-Python port with no claude-klabauter call
at all; `nudge_em_code_dispatch` has zero registrations under any name;
`nudge_unauthorized_handoff`'s logic is reached by direct function import
(`nudge_unauthorized_handoff.advisory_text(...)`), bypassing the RPC op key
named here. A consumer that wants carrier membership reads
`x-effective-delivery`'s own `carriers['postuse-advisory-dispatch.py']` op
list — generated from `hooks.json`, so self-correcting on the next
registration edit — never this tuple.

→ docs/decisions/DR-297-the-ported-advisory-grouping-is-not-carrier-membership.md

Purpose: `coordinator_core.ipc::_REGISTRY` only reflects whatever has been
imported so far in this process — under `COORDINATOR_CORE_LAZY_OPS=1` a
naive read of it is silently PARTIAL. DoE's `x-effective-delivery` manifest
treats an incomplete listing as worse than an absent one (`stale` outranks
`absent` in its own reader semantics —
docs/reference/hook-delivery-manifest.md § The five states). This module
forces the advisory hook modules to import before reading, so the listing
it returns is exhaustive rather than import-order-dependent.

Spec backlink: pln-guard-roster-export-minus-the-a4dec3 § C3 / AC5.
See also: docs/reference/hook-delivery-manifest.md § Natural emitter source
("postuse-advisory-dispatch.py ... resolves into coordinator_core.hooks,
whose ops register by import-time side effect with no listing API").

Scope of "the advisory op set" (read from claude-klabauter's tree, not asserted from
outside it): `coordinator_core/hooks/__init__.py`'s own module docstring
names exactly six ops under "Advisory hook ops ported from ~/.claude
advisory/nudge command hooks (pcore-04, D4)" —
nudge_foreground_agent_dispatch, suggest_sonnet_research,
nudge_em_code_dispatch, nudge_unauthorized_handoff,
postuse_advisory_dispatch, nudge_named_agent_report_delivery. That prose
grouping is the only boundary claude-klabauter's tree draws between "advisory" and
the package's other two populations — bookkeeping ops that write
`.git/coordinator-sessions/`, and the zero-tool-use/arrival-check detection
ops — there is no data structure encoding it anywhere in this tree, only
that docstring. `_PORTED_ADVISORY_HOOK_OP_NAMES` below mirrors it as data
rather than re-deriving it structurally — and that grouping is *originating*,
not current: three of the six were given their own independent DoE
registrations after the port, and one has none at all, which is precisely the
divergence the NEGATIVE SPEC above records.

Deliberately does NOT use `coordinator_core.authz.classification.OP_CLASSIFICATION`
as the boundary — that table's COMPUTE_ONLY/MUTATING axis is orthogonal to
carrier membership. `hooks.postuse_advisory_dispatch` and
`hooks.nudge_foreground_agent_dispatch` are both grouped "advisory" above
despite being classified MUTATING there; `hooks.subagent_zero_tool_use_resolve`
is COMPUTE_ONLY but belongs to the separate zero-tool-use-detection
population, not this one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

_PORTED_ADVISORY_HOOK_OP_NAMES: Tuple[str, ...] = (
    "hooks.nudge_foreground_agent_dispatch",
    "hooks.suggest_sonnet_research",
    "hooks.nudge_em_code_dispatch",
    "hooks.nudge_unauthorized_handoff",
    "hooks.postuse_advisory_dispatch",
    "hooks.nudge_named_agent_report_delivery",
)


class AdvisoryRosterUnavailable(RuntimeError):
    """Raised when eager resolution of the advisory hook op set cannot be
    completed or verified.

    Never swallowed into a short list — a caller must be able to tell
    "there are no advisory ops" (impossible here; `_PORTED_ADVISORY_HOOK_OP_NAMES`
    is non-empty by construction) from "eager resolution failed", per this
    module's own purpose: a silently-truncated roster reproduces exactly
    the `stale`-worse-than-`absent` failure the listing seam exists to
    prevent.
    """


@dataclass(frozen=True)
class AdvisoryOpEntry:
    """Plain-data identity for one resolved advisory op.

    `id` is the registered op key (e.g. "hooks.postuse_advisory_dispatch").
    `module`/`qualname` are the handler callable's own `__module__` and
    `__qualname__` — the only stable identity the op-registry carries
    beyond the key itself (`register_op` stores bare callables, no
    metadata object). `qualname` degrades to `__name__` for a callable
    with no `__qualname__`, and further to `""` if neither attribute is
    present (e.g. a `functools.partial` or a C-extension callable) — this
    module never fails a lookup over an exotic handler's missing identity.
    """

    id: str
    module: str
    qualname: str


def list_ported_advisory_ops() -> Tuple[AdvisoryOpEntry, ...]:
    """Eagerly resolve and return every op named in `_PORTED_ADVISORY_HOOK_OP_NAMES`.

    No payload argument. Forces `coordinator_core.hooks._eager_import_all()`
    up front — the same full-load routine
    `coordinator_core.ipc._lazy_import_and_lookup` itself calls on a
    `hooks.*` registry miss (its "HOOKS-SCOPED FALLBACK" step) — then
    resolves each name via `coordinator_core.ipc.get_op_handler`, which
    applies that same lazy-import-and-retry path per key as a second line
    of defense. No new import machinery is added; both calls reuse the
    existing resolution path verbatim.

    Raises `AdvisoryRosterUnavailable` if the eager import itself raises,
    or if any name in `_PORTED_ADVISORY_HOOK_OP_NAMES` is still unregistered
    afterward. Never returns a truncated tuple.
    """
    from coordinator_core.hooks import _eager_import_all, get_poisoned_modules
    from coordinator_core.ipc import get_op_handler

    try:
        _eager_import_all()
    except Exception as exc:
        raise AdvisoryRosterUnavailable(
            f"eager import of coordinator_core.hooks failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    entries = []
    missing = []
    for name in _PORTED_ADVISORY_HOOK_OP_NAMES:
        handler = get_op_handler(name)
        if handler is None:
            missing.append(name)
            continue
        entries.append(
            AdvisoryOpEntry(
                id=name,
                module=getattr(handler, "__module__", ""),
                qualname=getattr(
                    handler, "__qualname__", getattr(handler, "__name__", "")
                ),
            )
        )

    if missing:
        poisoned = get_poisoned_modules()
        detail = (
            f"; poisoned module(s) recorded during eager import: {poisoned!r}"
            if poisoned
            else "; no poisoned modules recorded -- eager import completed "
            "without error, these names are simply not registered"
        )
        raise AdvisoryRosterUnavailable(
            f"{len(missing)} advisory op(s) still unregistered after eager "
            f"import: {missing!r}{detail}"
        )

    return tuple(entries)
