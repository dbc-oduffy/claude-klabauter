"""coordinator_core.ceremony_common.tail — the shared ceremony-close
directive pair every ceremony-complete computed-skill assembler emits as
its final two mechanical steps: the post-command hook, then cadence
emission.

Purpose: `workday_complete.brief` (C2, Step 10.5/10.6) and
`workweek_complete.brief` (C5, Step 13.5/13.6) both close with the exact
same two directives — `coordinator-ceremony-hook` with no args, then
`emit-cadence` with no args (the latter carrying `best_effort: True`),
both unconditional (`depends_on=None`, `already_satisfied=False`). AC9's
byte-identity check confirmed the two tails are identical in every
load-bearing field once workweek's separate `hard_block` bookkeeping
pass is accounted for (see negative-spec below) — this module factors
the shared pair so neither assembler hand-maintains its own copy.

Spec backlink: docs/plans/2026-07-24-b1-ceremony-complete-computed-conversion.md, chunk C5, AC9
Spec backlink: pln-a-best-effort-directive-cannot-be9948, chunk C4, AC8/AC9

Negative-spec:
    - Does NOT know about `hard_block` — that key is workweek-only
      bookkeeping (a uniform post-build pass `workweek_complete.brief`
      applies to EVERY directive it builds, tail included, so the
      skill-body render can preserve hard-block-vs-advisory granularity).
      Baking `hard_block` in here would leak workweek-specific metadata
      into workday's tail, which carries no such key. Callers that need
      `hard_block` stamp it onto the returned dicts themselves, same as
      they do for every other directive they build.
    - Does NOT read `CONSUMES_MANIFEST` or assert membership — each
      caller's own `_build_directives` assertion loop already checks every
      directive it emits (including the tail entries this module returns)
      against ITS OWN manifest; duplicating that check here would just be
      a second copy of the same assertion running against a value the
      caller already validates.
"""

from __future__ import annotations

from typing import Any


def build_ceremony_close_tail(
    *, post_command_hook_id: str, emit_cadence_id: str, ceremony_name: str
) -> list[dict[str, Any]]:
    """Builds the two ceremony-close directives (post-command hook, then
    emit-cadence) in that order. Callers pass their own step-numbered ids
    (workday: `d_step10_5_post_command_hook`/`d_step10_6_emit_cadence`;
    workweek: `d_step13_5_post_command_hook`/`d_step13_6_emit_cadence`) —
    this module owns the shape (cli/args/depends_on/already_satisfied),
    never the numbering. `ceremony_name` is the canonical dashed ceremony
    name (`"workday-complete"` / `"workweek-complete"`) —
    `coordinator-ceremony-hook.py` reads it as its one positional arg
    (`argv[0]`) to resolve which `coordinator.local.md` key to dispatch;
    omitting it (the pre-2026-07-26 shape, `args=[]`) always resolved an
    empty ceremony name and silently no-opped regardless of which ceremony
    ran it (arg-mismatch audit, class (c)). The emit-cadence entry alone
    carries `best_effort: True` (2026-08-08 plan, AC8) — a non-zero exit
    from it lands in the ceremony runner's `degraded` bucket rather than
    `failed`, so it cannot move the ceremony's exit code. The
    post-command-hook entry does not get the key: that step is not
    documented as best-effort.
    """
    return [
        {
            "id": post_command_hook_id,
            "cli": "coordinator-ceremony-hook",
            "args": [ceremony_name],
            "depends_on": None,
            "already_satisfied": False,
        },
        {
            "id": emit_cadence_id,
            "cli": "emit-cadence",
            "args": [],
            "depends_on": None,
            "already_satisfied": False,
            "best_effort": True,
        },
    ]
