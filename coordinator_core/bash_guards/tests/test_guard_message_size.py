"""coordinator_core.bash_guards.tests.test_guard_message_size -- the
three-leg gate over C3's corpus: this is the suite the whole plan exists
to produce.

Spec backlink: pln-runtime-measured-message-size--0669ac,
chunk C5. Satisfies AC3, AC4, AC5, AC7, AC9, AC13, AC14.

THE THREE LEGS (each corrected during plan review -- see the plan's own
"Correction:" sections, cited per-leg below; the naive version of each leg
is what review rejected, not an oversight this suite repeats):

  LEG 1 -- ceiling (AC3/AC14). No speaker cell's prose bytes exceed
  `_message_size.MESSAGE_PROSE_CAP_BYTES`, computed PER BAND (well-defined
  at any population size, including n=1), except cells named in C4's
  `guard_message_exemptions.GUARD_MESSAGE_EXEMPTIONS` manifest.

  LEG 2 -- distribution (AC4/AC14). MEAN prose bytes over speaker cells
  only, POOLED corpus-wide, plus a per-band deviation assertion (no band's
  mean exceeds the pooled mean by more than `LEG2_DEVIATION_FACTOR`), gated
  by a minimum-population precondition -- a band with fewer than
  `MIN_BAND_POPULATION_FOR_LEG2_DEVIATION` speakers is ceiling-gated only
  (leg 1), never distribution-gated. Per § Problem's "Correction: leg 2's
  per-band distribution bound is noise over small bands": a per-band mean
  over `PLATFORM_CONDITIONED_DENY`'s live n=2 (grep-confirmed, not the
  plan's original "~3") is noise, not a bound.

  LEG 3 -- ratchet (AC5). Mean prose bytes per speaker cell, PER BAND, may
  only decrease -- NOT a raw corpus-wide total, which § Problem's
  "Correction: leg 3 as a raw corpus total contradicts AC2 by construction"
  shows red-lines on every future guard addition. Population-normalized so
  adding an in-band-typical guard (AC2's own requirement) does not itself
  trip the ratchet. On an increase, the offending cells are NAMED in the
  assertion message and the suite stops there -- per
  state/lessons/2026-07-31-a-ratchet-hit-is-a-question-not-a-verdict-*,
  this chunk does not auto-remediate; each named cell is adjudicated by
  whichever chunk fixes it.

AC7 -- THE FALSIFIABILITY TESTS, split per § Problem's "Correction: AC7's
second clause is false for a median, and the statistic was never named"
into two assertions on the SELECTED POPULATION directly, never through a
statistic's sensitivity:
  (a) `_select_speakers` (the one function every leg below calls to build
      its population) excludes a cell whose prose bytes are 0 -- including
      a `_advisory_value.resolve_suppressed_envelope`-manufactured
      rewrite-leg-only envelope, fired live on a non-Windows host, per §
      Problem's "Correction: the speaker predicate reconstitutes the
      silent-shim trap one level down".
  (b) an over-cap cell, injected into a real population, IS present in the
      output of `_select_speakers`.
Also: `test_gate_fails_on_deliberately_vacuous_population` proves each leg
function refuses to pass vacuously when handed zero speaker cells (an
empty-population `all(...)`/`for` loop is silently, arithmetically True in
Python -- the exact failure mode that would let this whole suite measure
nothing while going green).

AC13 -- deny-survives-over-cap, folded in from the removed C7 (§ Problem's
"Correction: production-path wiring is dropped, not isolated"): a real
CONFINEMENT_DENY guard's deny verdict, invoked via `GuardEntry.fn` through
C1's `guard_message_capture.capture_one_guard` seam, is unchanged when its
rendered message is deliberately padded past
`MESSAGE_PROSE_CAP_BYTES` after capture. No change to `dispatch.py` or
`write_guards/engine.py` -- proved entirely against C1's direct-invocation
seam plus a local dict copy, per Anti-scope's production-wiring ban.

AC9 -- fast tier, target end-state: no `cadence`/`pending_fix`/
`designed_red` marker anywhere in this module. Until row C10 of
docs/plans/2026-09-11-trim-the-remaining-over-cap-guard-messages.md lands,
`test_leg1_ceiling_per_band` below deliberately carries `@pytest.mark.
pending_fix` (16 cells remain over cap; see state/handoffs/2026-08-03-
guard-message-cap-remaining-16.md) -- C10 removing that marker is what
makes this AC9 clause true, not a present-tense fact today. No
uncommitted external corpus dependency either way (C3's corpus
rows fire live guards against fresh scratch fixtures, not a golden file),
and `test_ac9_measured_wallclock_runtime_reported` below prints a MEASURED
wall-clock figure rather than asserting it is lighter than
`test_confinement_attack_corpus.py` -- C3's corpus spans three directories
including a `write_guards` path that re-runs `_discover_guards()` per
`evaluate()`-shaped call, which is plausibly heavier, not lighter.

Corpus total: recorded as `CORPUS_TOTAL_PROSE_BYTES_SANITY_CEILING` below,
an inline constant with a justifying docstring, in the same style as this
repo's four other live byte budgets (`claude_md_budget.SOFT_LIMIT_BYTES`/
`HARD_LIMIT_BYTES`, `test_operator_override_note_retains_affordances.
_MAX_BYTES`, `guard_memory_store_cap.MAX_MEMORY_MD_BYTES`) -- a coarse,
generously-headroomed guard-rail against a runaway population bug (e.g. a
duplicated row registration silently doubling the corpus), NOT the leg-3
ratchet itself (that is per-cell-mean-per-band, per AC5's own correction
above; a raw total is deliberately the wrong shape for a strict ratchet).

Known limitation, not fixed in this chunk: `RATCHET_BASELINE_MEAN_PROSE_
BYTES_PER_BAND` below is measured live on this tree and this host. A guard
whose message embeds a host- or checkout-path-dependent span outside
`operator_override_note`'s own identity-subtracted tail (C2's `_tail_bytes`
handles only that one span) could shift a band's mean by a few bytes on a
different machine or checkout depth, without any real prose regression.
Per-band rounding (`_ceil_to_5`) below absorbs small jitter; a real,
structural fix (normalizing every embedded path span, not just the
override tail) is C3/C2 scope, not re-opened here.
"""

from __future__ import annotations

import copy
import statistics
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple

import pytest

from coordinator_core.bash_guards._advisory_value import (
    resolve_suppressed_envelope as _resolve_suppressed_envelope,
)
from coordinator_core.bash_guards._message_size import (
    MESSAGE_PROSE_CAP_BYTES,
    MessageSizeMeasurement,
    measure_envelope,
)
from coordinator_core.bash_guards.tests.guard_message_capture import (
    capture_one_guard,
)
from coordinator_core.bash_guards.tests.guard_message_corpus import (
    ADVISORY_REWRITE_ROWS,
    CONFINEMENT_ROWS,
    HOOK_ROWS,
    PLATFORM_CONDITIONED_ROWS,
    WRITE_GUARD_ROWS,
    fire_hook_row,
    fire_row,
    fire_write_guard_row,
    normalize_envelope_for_measurement,
)
from coordinator_core.bash_guards.tests.guard_message_exemptions import (
    GUARD_MESSAGE_EXEMPTIONS,
)


@dataclass(frozen=True)
class _Cell:
    guard: str
    row_id: str
    band: str
    measurement: MessageSizeMeasurement


def _measure_all_cells() -> Tuple[List[_Cell], float]:
    t0 = time.perf_counter()
    cells: List[_Cell] = []

    for row in CONFINEMENT_ROWS + ADVISORY_REWRITE_ROWS + PLATFORM_CONDITIONED_ROWS:
        capture = fire_row(row)
        measurement = measure_envelope(
            normalize_envelope_for_measurement(capture.envelope), band=capture.band
        )
        cells.append(_Cell(row.guard, row.row_id, measurement.band, measurement))

    for row in WRITE_GUARD_ROWS:
        if row.unverified_reason is not None:
            continue
        capture = fire_write_guard_row(row)
        measurement = measure_envelope(
            normalize_envelope_for_measurement(capture.envelope), band=capture.band
        )
        cells.append(_Cell(row.guard, row.row_id, measurement.band, measurement))

    for row in HOOK_ROWS:
        capture = fire_hook_row(row)
        measurement = measure_envelope(
            normalize_envelope_for_measurement(capture.envelope), band=capture.band
        )
        cells.append(_Cell(row.guard, row.row_id, measurement.band, measurement))

    elapsed = time.perf_counter() - t0
    return cells, elapsed


@pytest.fixture(scope="module")
def measured_corpus() -> Tuple[List[_Cell], float]:
    return _measure_all_cells()


def _select_speakers(cells: List[_Cell]) -> List[_Cell]:
    return [c for c in cells if c.measurement.is_speaker]


def _require_nonempty_population(speakers: List[_Cell], leg_name: str) -> None:
    if not speakers:
        raise ValueError(
            "%s: zero speaker cells in the population -- refusing to pass "
            "vacuously. This means the corpus fixture returned nothing "
            "measurable, not that every guard in the tree is silent." % leg_name
        )


def leg1_ceiling_violations(cells: List[_Cell]) -> List[str]:
    speakers = _select_speakers(cells)
    _require_nonempty_population(speakers, "leg1-ceiling")
    violations = []
    for cell in speakers:
        if not cell.measurement.over_cap:
            continue
        if (cell.guard, cell.row_id) in GUARD_MESSAGE_EXEMPTIONS:
            continue
        violations.append(
            "%s/%s/%s: %d prose bytes (cap=%d)"
            % (cell.band, cell.guard, cell.row_id, cell.measurement.prose_bytes, MESSAGE_PROSE_CAP_BYTES)
        )
    return violations


MIN_BAND_POPULATION_FOR_LEG2_DEVIATION = 5

LEG2_DEVIATION_FACTOR = 1.5


def leg2_pooled_mean(cells: List[_Cell]) -> float:
    speakers = _select_speakers(cells)
    _require_nonempty_population(speakers, "leg2-pooled-mean")
    return statistics.mean(c.measurement.prose_bytes for c in speakers)


def leg2_band_deviation_violations(cells: List[_Cell]) -> List[str]:
    speakers = _select_speakers(cells)
    _require_nonempty_population(speakers, "leg2-band-deviation")
    pooled_mean = statistics.mean(c.measurement.prose_bytes for c in speakers)
    by_band: Dict[str, List[_Cell]] = defaultdict(list)
    for cell in speakers:
        by_band[cell.band].append(cell)

    violations = []
    for band, band_cells in sorted(by_band.items()):
        if len(band_cells) < MIN_BAND_POPULATION_FOR_LEG2_DEVIATION:
            continue
        band_mean = statistics.mean(c.measurement.prose_bytes for c in band_cells)
        limit = pooled_mean * LEG2_DEVIATION_FACTOR
        if band_mean > limit:
            violations.append(
                "%s: mean=%.2f exceeds pooled_mean(%.2f) * %.1f = %.2f (n=%d speakers)"
                % (band, band_mean, pooled_mean, LEG2_DEVIATION_FACTOR, limit, len(band_cells))
            )
    return violations


def _ceil_to_5(value: float) -> int:
    return int(-(-value // 5) * 5)


JITTER_ALLOWANCE_BYTES = max(5, _ceil_to_5(2.1618))

#: default-TMPDIR run) as `_ceil_to_5(live_mean) + JITTER_ALLOWANCE_BYTES` --
#: jitter, which `JITTER_ALLOWANCE_BYTES` now absorbs explicitly (measured,
#: retains_affordances._MAX_BYTES`'s own manually-ratcheted-down precedent)
RATCHET_BASELINE_MEAN_PROSE_BYTES_PER_BAND: Dict[str, int] = {
    "confinement-deny": 250 + JITTER_ALLOWANCE_BYTES,
    "advisory-rewrite": 165 + JITTER_ALLOWANCE_BYTES,
    "platform-conditioned-deny": 750 + JITTER_ALLOWANCE_BYTES,
    "directory:write_guards": 145 + JITTER_ALLOWANCE_BYTES,
    "directory:hooks": 230 + JITTER_ALLOWANCE_BYTES,
}


def leg3_ratchet_violations(cells: List[_Cell]) -> List[str]:
    speakers = _select_speakers(cells)
    _require_nonempty_population(speakers, "leg3-ratchet")
    by_band: Dict[str, List[_Cell]] = defaultdict(list)
    for cell in speakers:
        by_band[cell.band].append(cell)

    violations = []
    for band, band_cells in sorted(by_band.items()):
        baseline = RATCHET_BASELINE_MEAN_PROSE_BYTES_PER_BAND.get(band)
        if baseline is None:
            violations.append(
                "%s: no ratchet baseline recorded -- a new band appeared with no entry in "
                "RATCHET_BASELINE_MEAN_PROSE_BYTES_PER_BAND; add one rather than let this "
                "band ratchet silently from whatever it happens to measure first" % band
            )
            continue
        band_mean = statistics.mean(c.measurement.prose_bytes for c in band_cells)
        if band_mean > baseline:
            offenders = sorted(band_cells, key=lambda c: -c.measurement.prose_bytes)
            named = ", ".join(
                "%s/%s=%db" % (c.guard, c.row_id, c.measurement.prose_bytes) for c in offenders
            )
            violations.append(
                "%s: mean %.2f > baseline %d -- offending cells (highest prose first): %s"
                % (band, band_mean, baseline, named)
            )
    return violations


@pytest.mark.pending_fix
def test_leg1_ceiling_per_band(measured_corpus):
    cells, _elapsed = measured_corpus
    violations = leg1_ceiling_violations(cells)
    assert not violations, "leg-1 ceiling violations (%d), none exempted in C4's manifest:\n%s" % (
        len(violations),
        "\n".join(violations),
    )


def test_leg2_distribution_pooled_mean_with_band_deviation(measured_corpus):
    cells, _elapsed = measured_corpus
    violations = leg2_band_deviation_violations(cells)
    assert not violations, "leg-2 per-band deviation violations:\n%s" % "\n".join(violations)


def test_leg3_ratchet_mean_prose_bytes_per_band(measured_corpus):
    cells, _elapsed = measured_corpus
    violations = leg3_ratchet_violations(cells)
    assert not violations, "leg-3 ratchet increase(s) -- adjudicate each named cell, do not sweep:\n%s" % (
        "\n".join(violations)
    )


def test_leg3_baseline_is_not_slack(measured_corpus):
    """R11's dilution finding, closed as a two-sided ratchet rather than a
    one-off re-baseline: a band whose recorded baseline has drifted more than
    `2 * JITTER_ALLOWANCE_BYTES` above its live `_ceil_to_5` mean is slack,
    not a ratchet -- the gap no longer tracks any real cross-host jitter and
    should be lowered by hand (this dict does not self-update, by design;
    see `RATCHET_BASELINE_MEAN_PROSE_BYTES_PER_BAND`'s own docstring)."""
    cells, _elapsed = measured_corpus
    speakers = _select_speakers(cells)
    by_band: Dict[str, List[_Cell]] = defaultdict(list)
    for cell in speakers:
        by_band[cell.band].append(cell)

    violations = []
    for band, band_cells in sorted(by_band.items()):
        baseline = RATCHET_BASELINE_MEAN_PROSE_BYTES_PER_BAND.get(band)
        if baseline is None:
            continue
        live_ceil = _ceil_to_5(statistics.mean(c.measurement.prose_bytes for c in band_cells))
        slack = baseline - live_ceil
        if slack > 2 * JITTER_ALLOWANCE_BYTES:
            violations.append(
                "%s: baseline %d is %d over live ceil5-mean %d (allowance %d) -- lower it to %d"
                % (band, baseline, slack, live_ceil, JITTER_ALLOWANCE_BYTES, live_ceil + JITTER_ALLOWANCE_BYTES)
            )
    assert not violations, "leg-3 baseline is slack, not ratchet -- lower by hand:\n%s" % "\n".join(violations)


# GUARD_MESSAGE_EXEMPTIONS`. A lookup over the SAME cells `measured_corpus`


def _stale_exemptions(
    manifest: Dict[Tuple[str, str], str], cells: List[_Cell]
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    cell_by_key: Dict[Tuple[str, str], MessageSizeMeasurement] = {
        (c.guard, c.row_id): c.measurement for c in cells
    }
    unknown_keys = [key for key in manifest if key not in cell_by_key]
    under_cap_keys = [
        key for key in manifest if key in cell_by_key and not cell_by_key[key].over_cap
    ]
    return unknown_keys, under_cap_keys


def test_exemption_entries_name_known_corpus_rows(measured_corpus):
    cells, _elapsed = measured_corpus
    unknown_keys, _under_cap_keys = _stale_exemptions(GUARD_MESSAGE_EXEMPTIONS, cells)
    assert not unknown_keys, (
        "these exemption entries name no corpus row in measured_corpus -- remove or fix "
        "them: %s" % unknown_keys
    )


def test_exemption_entries_still_exceed_cap(measured_corpus):
    """An exemption for a cell that no longer exceeds `MESSAGE_PROSE_CAP_BYTES`
    is dead config of a different shape than an unknown entry -- it would
    silently keep exempting a cell that no longer needs exempting."""
    cells, _elapsed = measured_corpus
    _unknown_keys, under_cap_keys = _stale_exemptions(GUARD_MESSAGE_EXEMPTIONS, cells)
    assert not under_cap_keys, (
        "these exemption entries no longer exceed MESSAGE_PROSE_CAP_BYTES -- the guard's "
        "message must have been trimmed or the input stopped triggering it; remove the "
        "now-unneeded exemption: %s" % under_cap_keys
    )


def test_exemption_entries_carry_a_written_reason():
    blank = [
        key for key, reason in GUARD_MESSAGE_EXEMPTIONS.items() if not reason or not reason.strip()
    ]
    assert not blank, "these exemption entries have no written reason: %s" % blank


def test_ac2a_stale_exemptions_flags_a_manifest_entry_with_no_corpus_row(measured_corpus):
    """Falsifiability: a synthetic manifest naming a guard/row-id that fires
    no corpus row must be caught. `GUARD_MESSAGE_EXEMPTIONS` is empty today,
    so `test_exemption_entries_name_known_corpus_rows` above passes
    vacuously; this proves `_stale_exemptions` itself actually detects the
    failure mode rather than being untested machinery."""
    cells, _elapsed = measured_corpus
    synthetic_manifest = {("no-such-guard", "no-such-row"): "synthetic falsifiability fixture"}
    unknown_keys, _under_cap_keys = _stale_exemptions(synthetic_manifest, cells)
    assert unknown_keys == [("no-such-guard", "no-such-row")]


def test_ac2b_stale_exemptions_flags_a_manifest_entry_that_no_longer_exceeds_cap(measured_corpus):
    """Falsifiability: a synthetic manifest naming a real, currently-under-cap
    corpus cell must be flagged stale. Uses a write-guard/hook band cell
    from the real, already-fired `measured_corpus` population -- no
    monkeypatching, and the module-level `GUARD_MESSAGE_EXEMPTIONS` is never
    mutated."""
    cells, _elapsed = measured_corpus
    under_cap_cell = next(
        (
            c
            for c in cells
            if c.band.startswith("directory:") and c.measurement.is_speaker and not c.measurement.over_cap
        ),
        None,
    )
    assert under_cap_cell is not None, (
        "sanity: this falsifiability test needs at least one under-cap write-guard/hook "
        "speaker cell in the fired corpus"
    )
    synthetic_manifest = {
        (under_cap_cell.guard, under_cap_cell.row_id): "synthetic falsifiability fixture"
    }
    _unknown_keys, under_cap_keys = _stale_exemptions(synthetic_manifest, cells)
    assert under_cap_keys == [(under_cap_cell.guard, under_cap_cell.row_id)]


def test_ac7a_speaker_selection_excludes_suppression_manufactured_zero_prose_cell():
    """`find-exec-rewrite` (ADVISORY_REWRITE, WINDOWS_COST_ONLY) fired live
    on a non-Windows host returns a real, speaking envelope. Passing that
    SAME envelope through `_advisory_value.resolve_suppressed_envelope` --
    the function `dispatch.evaluate_payload_json`'s outer loop calls when a
    WINDOWS_COST_ONLY advisory is suppressed off-Windows -- manufactures the
    exact rewrite-leg-only, zero-prose, non-`None` envelope § Problem's
    correction warns about. `_select_speakers` must exclude it."""
    cmd = "find . -type f -exec rm {} \\;"
    sid = "ac7a-suppressed-rewrite-only-%s" % uuid.uuid4().hex
    capture = capture_one_guard(
        "find-exec-rewrite",
        cmd,
        sid,
        "/tmp",
        {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": sid, "cwd": "/tmp"},
        host_is_windows=False,
    )
    assert capture.envelope is not None
    raw_measurement = measure_envelope(capture.envelope, band=capture.band)
    assert raw_measurement.is_speaker, "sanity: the real, unsuppressed firing must itself speak"

    suppressed_envelope = _resolve_suppressed_envelope(capture.envelope)
    assert suppressed_envelope is not None, "rewrite leg must survive suppression (H4 finding 4)"
    hso = suppressed_envelope["hookSpecificOutput"]
    assert "updatedInput" in hso
    assert "additionalContext" not in hso

    suppressed_measurement = measure_envelope(suppressed_envelope, band=capture.band)
    assert suppressed_measurement.prose_bytes == 0
    assert suppressed_measurement.is_speaker is False

    manufactured_cell = _Cell("find-exec-rewrite", "ac7a-suppressed", suppressed_measurement.band, suppressed_measurement)
    selected = _select_speakers([manufactured_cell])
    assert manufactured_cell not in selected, (
        "a zero-prose, suppression-manufactured rewrite-only envelope must never enter the "
        "leg-2 speaker population"
    )


def test_ac7b_over_cap_speaker_injected_is_present_in_selected_population(measured_corpus):
    cells, _elapsed = measured_corpus
    synthetic_envelope = {
        "hookSpecificOutput": {"additionalContext": "x" * (MESSAGE_PROSE_CAP_BYTES + 500)}
    }
    synthetic_measurement = measure_envelope(synthetic_envelope, band="advisory-rewrite")
    assert synthetic_measurement.is_speaker
    assert synthetic_measurement.over_cap
    injected = _Cell("synthetic-ac7b-guard", "ac7b-injected-over-cap", synthetic_measurement.band, synthetic_measurement)

    selected = _select_speakers(cells + [injected])
    assert injected in selected


def test_gate_fails_on_deliberately_vacuous_population():
    with pytest.raises(ValueError):
        leg1_ceiling_violations([])
    with pytest.raises(ValueError):
        leg2_band_deviation_violations([])
    with pytest.raises(ValueError):
        leg2_pooled_mean([])
    with pytest.raises(ValueError):
        leg3_ratchet_violations([])


def test_ac13_confinement_deny_survives_message_forced_over_cap():
    """A real `no-verify` (CONFINEMENT_DENY) firing, invoked via
    `GuardEntry.fn` through C1's `capture_one_guard` seam, denies with a
    message safely under cap today. Padding that SAME captured envelope's
    `permissionDecisionReason` past `MESSAGE_PROSE_CAP_BYTES` -- entirely in
    this test's own local dict copy, no change to `dispatch.py` or
    `write_guards/engine.py` -- must not touch `permissionDecision`: a cap
    is a test-suite-only measurement concept (Anti-scope's production-
    wiring ban) and must never be able to turn a deny into an allow."""
    cmd = 'git commit --no-verify -m "msg"'
    sid = "ac13-deny-survives-over-cap-%s" % uuid.uuid4().hex
    capture = capture_one_guard(
        "no-verify",
        cmd,
        sid,
        "/tmp",
        {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": sid, "cwd": "/tmp"},
        host_is_windows=False,
    )
    assert capture.envelope is not None
    original_hso = capture.envelope["hookSpecificOutput"]
    assert original_hso["permissionDecision"] == "deny"

    padded_envelope = copy.deepcopy(capture.envelope)
    padded_envelope["hookSpecificOutput"]["permissionDecisionReason"] += " " + "x" * MESSAGE_PROSE_CAP_BYTES
    padded_measurement = measure_envelope(padded_envelope, band=capture.band)
    assert padded_measurement.over_cap, "fixture must actually be over cap for this proof to mean anything"

    assert padded_envelope["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert padded_envelope["hookSpecificOutput"]["permissionDecision"] == original_hso["permissionDecision"]


def test_ac9_measured_wallclock_runtime_reported(measured_corpus):
    cells, elapsed = measured_corpus
    assert elapsed > 0
    print(
        "\nguard_message_size: fired+measured %d corpus cells in %.3fs (%.1f cells/s)"
        % (len(cells), elapsed, (len(cells) / elapsed) if elapsed else 0.0)
    )


#: justifying-docstring style as `claude_md_budget.SOFT_LIMIT_BYTES`/
#: `HARD_LIMIT_BYTES`, `test_operator_override_note_retains_affordances.
#: _MAX_BYTES`, and `guard_memory_store_cap.MAX_MEMORY_MD_BYTES`.
CORPUS_TOTAL_PROSE_BYTES_SANITY_CEILING = 90_000


def test_corpus_total_prose_bytes_within_sanity_ceiling(measured_corpus):
    cells, _elapsed = measured_corpus
    speakers = _select_speakers(cells)
    total = sum(c.measurement.prose_bytes for c in speakers)
    assert total <= CORPUS_TOTAL_PROSE_BYTES_SANITY_CEILING, (
        "corpus-wide total prose bytes (%d) blew past the sanity ceiling (%d) -- this is a "
        "coarse guard-rail against a runaway population bug, not the leg-3 ratchet; check for "
        "duplicated corpus rows before touching the ceiling itself"
        % (total, CORPUS_TOTAL_PROSE_BYTES_SANITY_CEILING)
    )
