"""
coordinator_core.workstream_complete.completion_verdict — per-gate readers
that translate `workstream-complete`'s five completeness gates into one
normalised vocabulary (`GateReading`), consumed by C2's rollup into
`gates.completion_verdict` on the `brief` envelope.

Spec backlink: pln-one-completion-verdict-for-wor-ea96e2, chunk C1a (this
module also hosts C1b's three remaining readers, appended on top).

Purpose: the close ceremony hands the EM five independently-computed
completeness facts and asks them to reduce it to one view by hand, every
time. This module is the reduction's honest half — normalising each
gate's own payload into a shared `GateReading` — while leaving the actual
judgment call (composition into one verdict) to C2, and the EM's genuinely
irreducible call (is the residue real work in disguise) to nobody but the
EM.

NEGATIVE SPEC — load-bearing, module-level, governs every reader in this
module including C1b's three:

1. No status is EVER inferred from a gate's `applies` field. Every reader
   declares its own TOTAL mapping: which payload key carries that gate's
   status, and an arm for every value that key can hold. A generic shared
   `applies`-reading helper is FORBIDDEN however much tidier it looks.
   Shared *dispatch machinery* over declared mappings is fine; shared
   *interpretation* of what a value means is not.
2. An unrecognised payload key shape, OR an unrecognised VALUE of a
   recognised key, yields `indeterminate` with a reason naming what was
   not recognised — NEVER `clean`. A reader that degrades an unknown shape
   to `clean` manufactures confidence it does not have.
3. This is not theoretical: on a live 2026-08-18 brief in this repo,
   `completeness_checklist` reported `applies: false` meaning "all
   verified / not applicable", while `open_spine_row_worklist` reported
   `applies: false` with `verdict: indeterminate` in the SAME envelope —
   the same boolean, two different meanings. A rollup reading `applies:
   false` uniformly as clean would have emitted a false-clean verdict on
   that very session. See also
   `state/lessons/2026-08-16-a-gate-that-keys-on-its-own-message-inst-a1b89e5202ad.yaml`
   (a judgment point keyed on `warn_text is not None` instead of
   `verdict`, silently passing exactly when the gate could not read its
   evidence) — the same failure class this module's readers exist to
   foreclose.

PURITY — this module is pure:
    - Reads payload dicts passed in by the caller.
    - Calls no gate-computation function (never recomputes a gate).
    - Spawns no subprocess.
    - Touches no disk.

Vocabulary ban (AC4): this module never emits doe-claude-em's next-step
vocabulary (trampoline / won't-do / backlog / spinoff). A residue item
carries only what this engine owns — the producing gate, a reference we
own, and the gate's own summary text — never a next-step verb. Mapping
residue to that vocabulary is a lookup doe-claude-em's ceremony prose
performs against data this module supplies either way; guessing it here
would make their later answer a breaking change instead of a lookup
table.
"""

from typing import Any, Mapping, NamedTuple


class GateReading(NamedTuple):

    status: str
    residue_items: tuple[Mapping[str, Any], ...]
    reason: str | None


def _unrecognised_value(gate: str, key: str, value: Any) -> GateReading:
    return GateReading(
        status="indeterminate",
        residue_items=(),
        reason=f"{gate}: unrecognised {key} value {value!r}.",
    )


def _unrecognised_shape(gate: str, missing_key: str) -> GateReading:
    return GateReading(
        status="indeterminate",
        residue_items=(),
        reason=f"{gate}: payload missing expected key {missing_key!r}.",
    )


def _row_reference(row: Any) -> str:
    if isinstance(row, Mapping):
        return str(row.get("id", ""))
    return str(getattr(row, "id", ""))


# `indeterminate`. This reader TRANSLATES that existing verdict; it does

_OPEN_SPINE_ROW_WORKLIST_MAPPING: dict[str, str] = {
    "applicable": "open",
    "not-applicable": "not-applicable",
    "indeterminate": "indeterminate",
}


def open_spine_row_worklist(payload: Mapping[str, Any]) -> GateReading:
    if "verdict" not in payload:
        return _unrecognised_shape("open_spine_row_worklist", "verdict")

    verdict = payload["verdict"]
    status = _OPEN_SPINE_ROW_WORKLIST_MAPPING.get(verdict)
    if status is None:
        return _unrecognised_value("open_spine_row_worklist", "verdict", verdict)

    residue: tuple[Mapping[str, Any], ...] = ()
    if status == "open":
        summary_line = payload.get("summary_line", "")
        rows = payload.get("rows") or ()
        residue = tuple(
            {
                "gate": "open_spine_row_worklist",
                "reference": _row_reference(row),
                "summary": summary_line,
            }
            for row in rows
        )

    return GateReading(status=status, residue_items=residue, reason=None)


_LANDED_RECONCILIATION_MAPPING: dict[str, str] = {
    "applicable": "open",
    "not-applicable": "not-applicable",
    "indeterminate": "indeterminate",
}


def landed_reconciliation(payload: Mapping[str, Any]) -> GateReading:
    if "verdict" not in payload:
        return _unrecognised_shape("landed_reconciliation", "verdict")

    verdict = payload["verdict"]
    status = _LANDED_RECONCILIATION_MAPPING.get(verdict)
    if status is None:
        return _unrecognised_value("landed_reconciliation", "verdict", verdict)

    residue: tuple[Mapping[str, Any], ...] = ()
    if status == "open":
        residue = (
            {
                "gate": "landed_reconciliation",
                "reference": f"{payload.get('open_count')}/{payload.get('total_count')} unreconciled",
                "summary": payload.get("summary_line", ""),
            },
        )

    return GateReading(status=status, residue_items=residue, reason=None)


def _completeness_item_field(item: Any, field: str) -> str:
    if isinstance(item, Mapping):
        return str(item.get(field, ""))
    return str(getattr(item, field, ""))


# reader TRANSLATES that existing verdict; it does not re-derive a status

_COMPLETENESS_CHECKLIST_MAPPING: dict[str, str] = {
    "not-applicable": "not-applicable",
    "indeterminate": "indeterminate",
    "clean": "clean",
    "open": "open",
}


def completeness_checklist(payload: Mapping[str, Any]) -> GateReading:
    if "verdict" not in payload:
        return _unrecognised_shape("completeness_checklist", "verdict")

    verdict = payload["verdict"]
    status = _COMPLETENESS_CHECKLIST_MAPPING.get(verdict)
    if status is None:
        return _unrecognised_value("completeness_checklist", "verdict", verdict)

    residue: tuple[Mapping[str, Any], ...] = ()
    if status == "open":
        summary_line = payload.get("summary_line", "")
        items = payload.get("items") or ()
        residue = tuple(
            {
                "gate": "completeness_checklist",
                "reference": f"{_completeness_item_field(item, 'item_class')}: "
                             f"{_completeness_item_field(item, 'assertion')}",
                "summary": summary_line,
            }
            for item in items
            if not (item.get("verified") if isinstance(item, Mapping) else getattr(item, "verified", False))
        )

    return GateReading(status=status, residue_items=residue, reason=None)


_LEG_A_VERDICTS = frozenset({"open", "clean", "not-applicable", "indeterminate"})
_LEG_B_VERDICTS = frozenset({"live-child", "no-children", "indeterminate"})


def _consumed_handoff_element_status(element: Mapping[str, Any]) -> tuple[str, str | None]:
    handoff = element.get("handoff", "") if isinstance(element, Mapping) else ""
    leg_a = element.get("leg_a") if isinstance(element, Mapping) else None
    leg_b = element.get("leg_b") if isinstance(element, Mapping) else None

    if not isinstance(leg_a, Mapping) or "verdict" not in leg_a:
        return "indeterminate", f"consumed_handoff_completeness: element {handoff!r} missing leg_a.verdict."
    if not isinstance(leg_b, Mapping) or "verdict" not in leg_b:
        return "indeterminate", f"consumed_handoff_completeness: element {handoff!r} missing leg_b.verdict."

    leg_a_verdict = leg_a["verdict"]
    leg_b_verdict = leg_b["verdict"]
    if leg_a_verdict not in _LEG_A_VERDICTS:
        return "indeterminate", (
            f"consumed_handoff_completeness: element {handoff!r} unrecognised leg_a.verdict {leg_a_verdict!r}."
        )
    if leg_b_verdict not in _LEG_B_VERDICTS:
        return "indeterminate", (
            f"consumed_handoff_completeness: element {handoff!r} unrecognised leg_b.verdict {leg_b_verdict!r}."
        )

    if leg_a_verdict == "open" or leg_b_verdict == "live-child":
        return "open", None
    if leg_a_verdict == "indeterminate" or leg_b_verdict == "indeterminate":
        return "indeterminate", None
    if leg_a_verdict == "clean":
        return "clean", None
    return "not-applicable", None


def consumed_handoff_completeness(payload: Mapping[str, Any]) -> GateReading:
    if "elements" not in payload:
        return _unrecognised_shape("consumed_handoff_completeness", "elements")

    elements = payload["elements"] or ()
    statuses: list[str] = []
    reasons: list[str] = []
    residue: list[Mapping[str, Any]] = []
    for element in elements:
        status, reason = _consumed_handoff_element_status(element)
        statuses.append(status)
        if reason is not None:
            reasons.append(reason)
        if status == "open" and isinstance(element, Mapping):
            leg_a = element.get("leg_a") or {}
            leg_b = element.get("leg_b") or {}
            detail = leg_a.get("detail") if leg_a.get("verdict") == "open" else leg_b.get("detail")
            residue.append(
                {
                    "gate": "consumed_handoff_completeness",
                    "reference": element.get("handoff", ""),
                    "summary": detail or "",
                }
            )

    if "open" in statuses:
        gate_status = "open"
    elif "indeterminate" in statuses:
        gate_status = "indeterminate"
    elif "clean" in statuses:
        gate_status = "clean"
    else:
        gate_status = "not-applicable"

    reason = "; ".join(reasons) if reasons else None
    return GateReading(status=gate_status, residue_items=tuple(residue), reason=reason)


def review_scale(payload: Mapping[str, Any]) -> GateReading:
    """Reader for `gates.review_scale` — NARRATION ONLY.

    TOTAL mapping (`ReviewScaleDecision.resolved` -> `GateReading.status`):
        True  -> "not-applicable"  (a review-scale decision was resolved;
                                     this reader answers a different
                                     question than completeness, so a
                                     resolved decision carries no
                                     open/clean signal of its own)
        False -> "indeterminate"   (row 4 not yet resolved — no decision
                                     to narrate)
        anything else (non-bool)   -> "indeterminate", reason names the
                                       unrecognised value.
    Missing "resolved" key -> "indeterminate", reason names the missing key.

    Residue: always empty — `review_scale` never contributes to C2's
    residue[]; its own `commit_slices`/`chain_slices` are a different
    concern (trail-readiness), not unresolved completeness work.

    CALLER CONTRACT (restated for C2): this reading is for `readings[]`
    narration only. Exclude `review_scale` from `verdict` composition,
    `indeterminate_gates[]`, and the clean/not-applicable reading census —
    it does not answer the completeness question the other four readers
    do.
    """
    if "resolved" not in payload:
        return _unrecognised_shape("review_scale", "resolved")

    resolved = payload["resolved"]
    if resolved is True:
        return GateReading(status="not-applicable", residue_items=(), reason=None)
    if resolved is False:
        return GateReading(status="indeterminate", residue_items=(), reason=None)

    return _unrecognised_value("review_scale", "resolved", resolved)


# CALLER CONTRACT paragraph above) and is carried in `readings[]` for

_CENSUS_GATE_NAMES: tuple[str, ...] = (
    "completeness_checklist",
    "open_spine_row_worklist",
    "consumed_handoff_completeness",
    "landed_reconciliation",
)


def compose_completion_verdict(readings: Mapping[str, GateReading]) -> dict[str, Any]:
    """C2's rollup: reduces the five gates' `GateReading`s to one
    `gates.completion_verdict` payload.

    `readings` carries all five gate names as keys (the four census gates
    plus `review_scale`, narration-only) mapped to that gate's own
    `GateReading`, produced upstream by this module's per-gate readers.
    This function calls no gate-computation function and recomputes no
    gate's own status (AC7) — it only reduces readings already computed.

    Composition rule, over the four census gates in `_CENSUS_GATE_NAMES`
    only:
        any reading "open"                            -> "incomplete"
        elif >=1 "clean" AND no "indeterminate"        -> "complete"
        elif every reading "not-applicable"            -> "not-applicable"
        else                                           -> "indeterminate"
    AC8: `complete` requires POSITIVE EVIDENCE — a census with zero
    `clean` readings never reads `complete`, because the `elif` guard
    requires a `clean_count` of 1 or more. That guarantee is unchanged.

    `not-applicable` (2026-08-31, cross-repo/inbox/2026-08-30-example-retrieval-repo-
    em-brightline-gate-discards-computed-scale-inputs.md follow-up) is the
    fourth headline value, and it is AC8's own distinction carried up to
    the rollup rather than a relaxation of it. An all-`not-applicable`
    census used to read `indeterminate`, which made this rollup the one
    layer that contradicts the rule every per-gate reader here obeys:
    `not-applicable` is nothing to look at and stays silent, same as
    `clean`; `indeterminate` is the gate tried to look and could not (see
    the tripwire `NOT-APPLICABLE-SPANS-TWO-SILENCES`). Collapsing the two
    at the headline threw away a distinction this payload's own
    `clean_count`/`not_applicable_count`/`indeterminate_gates[]` still
    carried, and it did it on the close where nothing applied — so that
    close could never read anything but `indeterminate`, and a verdict
    that is always `indeterminate` is one an EM learns to skip past. That
    training effect is the harm, not the word.

    The positive-evidence guarantee survives intact because this arm is
    NOT `complete` and never becomes it: it asserts only that the census
    found nothing to check, which is exactly what it measured. An EMPTY
    census (no census gate present in `readings` at all) stays
    `indeterminate` — nothing was read, which is a different fact from
    everything having been read and found inapplicable.

    `indeterminate_gates[]` names every census gate whose reading is
    `indeterminate`, ALWAYS populated regardless of the top-level
    `verdict` — including when `verdict` is `incomplete` (a concrete
    `open` reading elsewhere must not make other unreadable gates
    disappear from the envelope; `open` outranks `indeterminate` for the
    top-level verdict only, a consumer-side rendering choice, not engine-
    resident semantics).

    `clean_count`/`not_applicable_count`: census over the same four gates,
    so "complete, and here is what was actually checked" is
    distinguishable from "complete because nothing applied".

    `residue[]`: concatenation of every reading's own `residue_items`
    (all five gates, `review_scale`'s is always empty by construction),
    in `readings`' iteration order. Each item already carries only what
    its producing reader owns (gate, an owned reference, the gate's own
    summary text) — no next-step verb (AC4).

    `readings[]`: one entry per gate in `readings` (all five, `review_
    scale` included for narration per F5), `{"gate", "status", "reason"}`.

    AC6: this function gates nothing — no `depends_on` edge, no judgment
    point, no halt, no block. Advisory-only emission.
    """
    census_statuses = [readings[name].status for name in _CENSUS_GATE_NAMES if name in readings]

    indeterminate_gates = [
        name for name in _CENSUS_GATE_NAMES if name in readings and readings[name].status == "indeterminate"
    ]
    clean_count = sum(1 for status in census_statuses if status == "clean")
    not_applicable_count = sum(1 for status in census_statuses if status == "not-applicable")

    if "open" in census_statuses:
        verdict = "incomplete"
    elif clean_count >= 1 and not indeterminate_gates:
        verdict = "complete"
    elif census_statuses and not_applicable_count == len(census_statuses):
        verdict = "not-applicable"
    else:
        verdict = "indeterminate"

    residue: list[Mapping[str, Any]] = []
    for reading in readings.values():
        residue.extend(reading.residue_items)

    readings_out = [
        {"gate": name, "status": reading.status, "reason": reading.reason}
        for name, reading in readings.items()
    ]

    return {
        "verdict": verdict,
        "indeterminate_gates": indeterminate_gates,
        "readings": readings_out,
        "residue": residue,
        "clean_count": clean_count,
        "not_applicable_count": not_applicable_count,
    }
