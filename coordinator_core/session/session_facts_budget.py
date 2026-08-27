"""
coordinator_core.session.session_facts_budget — the fact layer's per-ceremony
hot-path ceiling (X), armed from measurement.

Purpose: carries ONE dial, `FACT_LAYER_PER_CEREMONY_GIT_SPAWN_BUDGET`, and its
derivation backlink to the artifact the figure was read off. Homed on the layer
it bounds rather than beside `composition_budget.py`'s two `FLEET_*` constants —
a semantically unrelated third dial named next to them reads as fleet-wide to
the next person regardless of prose (plan § "Where X lives, and what it is not").
This module does not import, read, or modify `composition_budget.py`.

DECLARATIVE-ONLY, AND UNREAD ON PURPOSE. Nothing in this repo reads this
constant, no gate fires on it, and no caller is wired. That is the decision, not
an unfinished edge (settled 2026-08-27, EM, on PM delegation — plan § "Where X
lives"). The facade has exactly ONE production consumer
(`quick_wrap_assemble/__init__.py :: brief`), so a gate armed off this sample
would fire, or fail to fire, on a population far too small to carry a
fleet-wide conclusion — DR-344's "no conclusion rests on 'a caller exists'"
points the same way. This is a RECORDED MEASUREMENT, not an enforcing ceiling.
Do not add a consumer here on the reasoning that an unread constant looks
unfinished; wiring one belongs after the facade has a denominator worth gating,
which is the `fact-layer` roadmap's question, not this stub's.

THE AXIS IS SPAWN COUNT, NOT TIME. DR-344 (amended 2026-08-21) makes process
time and spawn count the unit and closes with "no wall-clock budget re-entering
under a different name". A per-fact wall-clock figure exists and is recorded in
the artifact as CONTEXT ONLY; it is deliberately not what this dial is
expressed in. Spawn count is the load-independent measure here: it is
deterministic, it cannot flake under peer load, and on this facade it is also
the dominant cost — `time.process_time()` under-reports every one of these
facts because it excludes the `git` child processes that do the actual work.

Derivation: docs/research/2026-08-27-fact-layer-hot-path-measured.md
Spec backlink: docs/plans/2026-08-27-the-fact-layer-is-measured-on-the-one-hot-path.md § C4
"""

from __future__ import annotations

#: Worst-case git spawns for ONE ceremony's pass through the fact layer, as
#: served on the one production path (`brief`, five of the six facts). Read off
#: the artifact's structural leg: 3 unconditional spawns, 8 with every
#: conditional branch taken. Deterministic — a counted property of the call
#: graph, not a sampled one.
MEASURED_WORST_GIT_SPAWNS_PER_CEREMONY = 8

#: Headroom multiplier, stated rather than assumed. DR-325 armed
#: `FLEET_AGGREGATE_ELAPSED_BUDGET` at 3.7x its worst observation; that
#: multiplier is DEPARTED FROM here, deliberately, and the reason is the
#: measure's own nature. 3.7x absorbs sampling variance in a wall-clock
#: distribution — a spawn count has no sampling variance to absorb, since 8 is
#: the enumerated maximum over every branch of the call graph, not the tail of
#: a sample. A 2x allowance covers a future fact or a new conditional branch
#: without pre-authorising a doubling of the count twice over.
HEADROOM_MULTIPLIER = 2.0

#: X. Armed at the measured worst case times the stated headroom.
#: 8 * 2.0 == 16.
FACT_LAYER_PER_CEREMONY_GIT_SPAWN_BUDGET = int(
    MEASURED_WORST_GIT_SPAWNS_PER_CEREMONY * HEADROOM_MULTIPLIER
)

#: Companion structural figure, same derivation, same declarative-only status:
#: worst-case frontmatter/file reads for one ceremony's pass (1 unconditional,
#: 3 with every conditional branch taken). Recorded beside the spawn budget
#: because a read is not free on this box either, and a future change that
#: trades a spawn for ten reads should be visible as such rather than reading
#: as an improvement.
MEASURED_WORST_FILE_READS_PER_CEREMONY = 3
FACT_LAYER_PER_CEREMONY_FILE_READ_BUDGET = int(
    MEASURED_WORST_FILE_READS_PER_CEREMONY * HEADROOM_MULTIPLIER
)

#: The aggregate does NOT decompose into per-fact ceilings, and a reader must
#: not treat the per-fact definition-of-done ceilings in `fl-core-01`/
#: `fl-core-02` as discharging it (COORDINATOR-RESOLUTIONS R-04). Five
#: individually-cheap facts on one hot path is precisely the amplification
#: shape behind state/audits/2026-08-15-fleet-degradation-forensics.md. This
#: constant bounds the COMPOSITION, and nothing about it is implied by any
#: per-fact number.
PER_FACT_CEILINGS_DO_NOT_DISCHARGE_THIS = True
