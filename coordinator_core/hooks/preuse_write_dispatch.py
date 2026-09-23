"""coordinator_core.hooks.preuse_write_dispatch — PreToolUse(Write|Edit|
MultiEdit|NotebookEdit) write-guard op.

Arrival note (W4-C7, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/preuse-write-dispatch.py`. That
script's whole job was PLUMBING: resolve a SIBLING claude-klabauter checkout from a
doctrine-plane repo (`_engine_root.resolve_claude_klabauter_root`), place it on
`sys.path`, import `coordinator_core.write_guards.engine` across that
boundary, and fold a SECOND, doctrine-plane-resident guard registry
(`_guard_runner.REAL_GUARD_REGISTRY`) into the engine's own verdict via
`_run_guards`/`_verdict_to_envelope`, because DoE used to host a handful of
guards the claude-klabauter engine did not know about. None of that plumbing applies
here: this module IS inside the engine boundary, `coordinator_core.
write_guards.engine` is a same-repo sibling import, and there is no second
guard registry to fold in — `evaluate()`'s own discovery already enumerates
every registered guard (`write_guards/engine.py :: discover_guard_names`).
So this op reduces to the part of the DoE script that was never plumbing:
call `evaluate()` with the payload the hook was already handed, and return
its verdict.

Op contract (matches the other landed `hooks.*` ops in this package): params
is the flat PreToolUse payload dict (`tool_name`, `tool_input`, `session_id`,
`cwd`, `agent_id`, …) — the SAME shape `write_guards.engine.evaluate()`
already accepts positionally, so no reshaping happens at this seam. Returns
one `hookSpecificOutput` envelope dict (deny, allow_advisory-shaped, or
`no_advisory()`) — never a list.

AGGREGATION IS DELIBERATELY NOT ENABLED HERE (deviation from the DoE
script, which turned `aggregate=True` on for its own dispatch). `evaluate(
aggregate=True)` returns `List[Dict]` when the advisory phase runs, which
would violate the singular "one hookSpecificOutput dict out" contract this
row's own body states for every op in this package, and no consumer of this
new op exists yet to define a list-of-envelopes wire shape (the entrypoint
that will actually dial this op, `hook-run`, lands in W4-C16). The DoE
script's `aggregate=True` reasoning (masking of a lower-priority advisory by
a higher-priority one that also fires) is a real behaviour and stays
available — a future caller that wants it passes `aggregate=True` itself
once a multi-envelope wire shape is defined for this door; this handler does
not invent one unasked. Default (first-fired-advisory-wins, hard-deny
short-circuits ahead of it) matches every other write-guard call site today.

Graceful degradation: any failure inside `evaluate()` fails OPEN (returns
`no_advisory()`, i.e. ALLOW) rather than raising — an edit must never be
bricked by a guard-engine defect, mirroring the DoE dispatcher's own
fail-open philosophy for engine-resolution failure (which cannot occur here,
since there is no cross-repo resolution step left to fail).

Negative-spec:
    Does NOT resolve a sibling engine checkout, place anything on `sys.path`,
    or import a `_engine_root`/`_guard_runner` analogue — there is nothing on
    the other side of a boundary to resolve; deleting that plumbing is the
    point of this port, not an omission.
    Does NOT read `subagent-sandbox-policy.yaml` or forward a `policy_path` —
    `write_guards/engine.py`'s own module docstring already records that the
    subagent-sandbox confinement was removed and `policy_path` is accepted-
    but-unused; forwarding a resolved path here would be dead plumbing for a
    parameter nothing reads.
    Does NOT accept raw JSON text — callers on this door already hand a parsed
    params dict (the JSON-RPC contract), so this calls `evaluate()` directly
    rather than the raw-text `evaluate_payload_json()` wrapper DoE's
    stdin-based script needed.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C7
"""

from __future__ import annotations

import sys

from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.ipc import register_op
from coordinator_core.write_guards.engine import evaluate


def _compose_skipped_guard_breadcrumb(skipped: "list[str]") -> str:
    """Best-effort stderr breadcrumb naming write-guard module(s) that failed
    to import and were skipped (fail-open for those guards only). Pure --
    takes the skipped-name list, returns the string; `_handler` is the only
    caller and the only place that prints it. Ported unchanged from the DoE
    script's own helper of the same name/shape.
    """
    return (
        "[preuse_write_dispatch] write-guard module(s) failed to import "
        f"and were skipped (fail-open for those guards only): {', '.join(skipped)}"
    )


@register_op("hooks.preuse_write_dispatch")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Write|Edit|MultiEdit|NotebookEdit) op: evaluate every
    registered write guard against this payload and return its verdict.

    `params` is the flat PreToolUse payload as received — `evaluate()` reads
    `tool_name`, `session_id`, `cwd`, `agent_id`, and whatever per-guard
    fields each guard's own `check(payload)` looks at directly out of it, so
    no field extraction happens at this seam.

    A guard module that fails to import during this call's discovery pass is
    named on stderr (best-effort, never affects the ALLOW/DENY decision) —
    the runtime-visible half of the silent-import-failure fix `evaluate()`'s
    own docstring describes (`skipped_out`).
    """
    skipped: list[str] = []
    try:
        out = evaluate(params, skipped_out=skipped)
    except Exception:
        return no_advisory()  # any engine failure -> fail-open ALLOW

    try:
        if skipped:
            print(_compose_skipped_guard_breadcrumb(skipped), file=sys.stderr)
    except Exception:
        pass

    if out is None:
        return no_advisory()
    return out
