"""
coordinator_core.hooks.postuse_stop_family_dispatch — PostToolUse(Write|
Edit|MultiEdit) Stop-family advisory fan-in op.

Purpose: warm command/native-door counterpart of DoE-claude's
`coordinator/hooks/scripts/postuse-stop-family-dispatch.py`, which folds
four write-path advisory guards (`derive-global-doctrine-live-copy.py`,
`derive-setup-copies.py`, `nudge-initiative-goals-ladder.py`,
`nudge-new-file-zero-budget-ratchets.py`) into one `python3` process via
`_stop_family_runner.run_registered_stop_family_guards`, batching over an
EMPTY `REAL_STOP_FAMILY_REGISTRY` the runner itself deliberately ships
(see `coordinator_core.hooks.support.stop_family_runner`'s own module
docstring — same reasoning as `guard_runner.REAL_GUARD_REGISTRY`).

None of that plumbing applies here, for the same reason
`preuse_write_dispatch`'s own docstring gives for its sibling PreToolUse
dispatcher: this module IS inside the engine boundary, so there is no
second, doctrine-plane-resident registry to fold — each of the four source
guards either already has (or, per this plan's own wave split, will land in
a sibling W4 chunk as) its own `hooks.<name>` op. This op reduces to
composing those THREE (not four — see Excluded, below) directly, in-process,
CONCATENATE-ALL, same aggregation contract the source dispatcher's own
`_stop_family_runner_contract.py` states (never first-fires-wins).

Excluded: `nudge-new-file-zero-budget-ratchets.py` STAYS DoE-resident per
DR-141 (this plan's own appendix, disposition `stays-doe-carve-out`) — it
reads `setup/publish-targets.portable`-rooted doctrine-plane publish/
locality policy as its own predicate, which is DR-141's stays-DoE-resident
test. No `hooks.nudge_new_file_zero_budget_ratchets` op exists or is built
by this chunk; DoE's own hooks.json registration for that one script is
unaffected by this op's landing (it keeps running as its own process,
outside this fan-in, per that appendix row's "-" chunk).

The other three land as their own `hooks.<name>` ops in sibling W4 chunks
(`derive_global_doctrine_live_copy`/`derive_setup_copies`: W4-C5;
`nudge_initiative_goals_ladder`: W4-C12) running concurrently with this one
— this module therefore imports each LAZILY, inside its own try/except, so
a not-yet-landed sibling degrades that one leg to silence (never crashes
this op) rather than requiring load-order between concurrent chunks.

Op contract: `params` reaches this op in either shape a `hooks.*` handler
receives — wrapped as `params["payload"]` by both engine doors, flat by the
cold chain; `_envelope.payload_of` reads both, supplying the flat
PostToolUse payload dict (`tool_name`, `tool_input`, `session_id`, `cwd`,
...). Returns one
`hookSpecificOutput` envelope: `post_advisory(<joined text>)` when one or
more composed leg fires (mirrors the source dispatcher's own stderr
concatenation — the Stop-family shape has no deny/advisory class split,
just N independent "did I have something to say" signals joined into one
channel); `no_advisory()` otherwise. Each leg is called in its own
try/except — one leg raising is isolated to that leg alone (fail-open for
it specifically), matching the source dispatcher's own per-guard isolation.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C14
DoE source: coordinator/hooks/scripts/postuse-stop-family-dispatch.py
"""

from __future__ import annotations

import inspect
from typing import Optional

from coordinator_core.hooks._envelope import no_advisory, payload_of, post_advisory
from coordinator_core.ipc import register_op

# One (module_path, op_attr) pair per composed leg — lazy per-call import so
# a not-yet-landed sibling chunk degrades that leg to silence rather than
# breaking import of this module.
_LEG_MODULES = (
    "coordinator_core.hooks.derive_global_doctrine_live_copy",
    "coordinator_core.hooks.derive_setup_copies",
    "coordinator_core.hooks.nudge_initiative_goals_ladder",
)


def _extract_text(result) -> "Optional[str]":
    """Read any `hooks.*` envelope-shaped or flat `{"message": str}`-shaped
    return uniformly — same reading `stop_dispatch._extract_advisory` uses
    for its own composed legs, narrowed to text-only since this fan-in's
    channel has no deny/advisory class distinction (module docstring)."""
    if not isinstance(result, dict):
        return None
    hso = result.get("hookSpecificOutput")
    if isinstance(hso, dict):
        text = hso.get("additionalContext") or hso.get("permissionDecisionReason")
        return text if isinstance(text, str) and text else None
    message = result.get("message")
    return message if isinstance(message, str) and message else None


async def _call_leg(module_path: str, params: dict) -> "Optional[str]":
    try:
        import importlib

        mod = importlib.import_module(module_path)
        handler = getattr(mod, "_handler", None)
        if handler is None:
            return None
        result = handler(params)
        if inspect.isawaitable(result):
            result = await result
        return _extract_text(result)
    except Exception:
        return None


@register_op("hooks.postuse_stop_family_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse(Write|Edit|MultiEdit): compose the three landed Stop-
    family write-path guards, CONCATENATE-ALL.

    `repo_root` (the framework-supplied handler argument) is unused — each
    composed leg resolves its own repo root from `params["payload"]["cwd"]`
    (or equivalent), matching every other payload-cwd-resolving `hooks.*` op
    in this family. Each composed op is `async def` (all three land as
    such in their own sibling W4 chunks); this handler is `async` too and
    awaits each in turn.
    """
    payload = payload_of(params)
    leg_params = dict(params)
    leg_params["payload"] = dict(payload)

    texts = []
    for module_path in _LEG_MODULES:
        text = await _call_leg(module_path, leg_params)
        if text:
            texts.append(text)

    if texts:
        return post_advisory("\n\n".join(texts))
    return no_advisory()
