"""
coordinator_core.ops.emit.skipped_stage — the ONE reuse-vs-null rule for skipped enrichments.

Purpose: hold, in exactly one place, the invariant every cadence-gated enrichment stage obeys.

    A SKIPPED ENRICHMENT REUSES LAST-KNOWN VALUES OR WRITES A CONTRACT-SANCTIONED NULL.
    IT NEVER WRITES AN EMPTY COLLECTION THAT WOULD READ AS "THIS DATA DOES NOT EXIST".

CURRENT REACHABILITY — THIS MODULE IS INERT IN PRODUCTION, AND THAT IS INTENDED (2026-07-29).
Both real ``envelope.emit`` callers run the FULL tier: ``ops/emit_cadence.py`` sets
``full_enrichment=True`` explicitly, and ``ops/artifact_emit.py`` defaults its own param to
True, over a dataclass default that is itself now True. Nothing in the fleet passes
``full_enrichment=False``, so no production path reaches the code below; it runs in tests and
for any caller that explicitly opts into the cheap tier. That is the deliberate resting state,
NOT an oversight and NOT a bug to fix by flipping ``EmitContext.full_enrichment`` back to
False: the reversal was a PM decision taken precisely because a default-cheap tier bought no
measured saving (both callers were already full) while functioning as a silent-staleness trap
— see that field's docstring for the full reasoning. The machinery is kept because the cheap
tier remains a legitimate explicit choice and because the reuse-vs-null rule below is the
thing that makes such a choice safe to offer at all. Whether a hot emit path should exist at
all is a separate question, dispositioned to its own spinoff rather than answered here.

Why this is a module and not a convention: an empty array is structurally schema-valid for
every required-array field in DoE's frozen ``snapshot-envelope.schema.json``, so a skipped
stage that emits ``[]`` passes validation, passes the emit gate, reports ``ok: True`` — and
lies to every consumer, which reads ``[]`` as "there are genuinely zero of these". That is
the exact silent-narrowing shape ``sections/file_attribution.py``'s F1/F2 fixes exist to
prevent, and re-deriving the rule at each new gated call site is how it gets re-broken. The
rule is therefore expressed once, as ``skipped_stage_value``, and each gated stage declares
only WHICH fallback its own contract sanctions.

Two fallback policies, and no third:

    ``NULL_SANCTIONED`` — the field is required-with-null and the contract explicitly
        permits null as a "not computed" state. Only then may a skip synthesise a value
        (``None``) rather than reusing one. ``ExecSummary.docs_staleness`` is this case: its
        own stamp already has a never-fail degrade path that writes null, so null on the
        skipped path is contract-legal by the same reading.

    ``MUST_COMPUTE`` — no sanctioned sentinel exists, so a skip is only permissible when a
        last-known value is actually available. With no prior emission on disk the helper
        returns "not satisfied" and the caller MUST fall through to a real computation.
        ``file_attributions`` is this case: a required array whose ``[]`` is a lie, so the
        gate reuses the previously-emitted rows or does the work.

Reuse source: the canonical on-disk ``cockpit-emission.json`` under ``ctx.central_state_root``
— deliberately NOT the caller's ``out`` override. The reuse source is the repo's last
published emission state, which is what a consumer would otherwise still be reading; a
one-off emission diverted to a scratch path (a timing harness, a parity fixture) must neither
seed itself from that scratch file nor be able to poison the reuse source for the next real
run. The file is only ever READ here.

Honest cost of reuse (stated, not hidden): reused rows carry the provenance and
``observed_at`` of the run that produced them, so a skipped stage's rows are as old as the
last full-enrichment emission. That is inherent to reuse-last-known and is the intended
trade — a correct-but-stale row is a truthful record of the last observation, where ``[]``
would be a false claim about the present.

VALIDATED REUSE (break-class fix, 2026-07-29, post-review):

    REUSE-LAST-KNOWN IS ONLY SAFE IF THE REUSED VALUE IS VALIDATED AGAINST THE SAME
    CONTRACT THE COMPUTED VALUE MUST SATISFY. Unvalidated reuse silently propagates
    whatever rot is in the last emission.

This is the ``[]``-trap's twin, and it is not obvious from the ``[]`` framing alone — it
narrows nothing and manufactures nothing, so every check above passes, yet the emitted
envelope is still wrong. It was found live: DoE-claude's on-disk emission (2026-07-28,
schema 3.7.0) carries 4027 ``file_attributions`` rows of which **3991 have absolute
``file_path`` values** and 0 exclusion markers — it predates the porter's F2
path-normalisation. The first version of this gate faithfully reused all of it, which on the
cheap tier emitted absolute paths into a field the contract declares "repo-relative,
directly comparable to ``git ls-files`` output" (a rag/cockpit JOIN KEY), resurrected the
out-of-repo/ephemeral rows F2 deliberately excludes, and shipped that re-widening with no
markers to show for it — defeating F1/F2's no-silent-narrowing guarantee from the opposite
direction.

Hence the ``validate`` argument. A prior value that fails its stage's contract check is
treated as ABSENT, and the stage's existing ``absent_policy`` decides — which for
``file_attributions`` means ``MUST_COMPUTE``, i.e. fall through and do the work. The
fall-through is logged to stderr so an operator can see why the cheap tier paid full cost.

``validate`` IS REQUIRED, keyword-only — not an optional kwarg with a fail-open default.
A caller certain its prior value cannot be contract-invalid passes an explicit no-op
(``validate=lambda _v: None``), which is a visible, auditable decision at the call site
rather than a silent omission. The first cut of this module made it optional and its own
sibling call site (``docs_staleness``) then shipped without one — the same optionality
machinery that manufactured the incident above, one layer up. Both current call sites now
pass a real validator; if a third one wants the no-op, it has to say so in the diff.

Negative-spec — do NOT repair or re-normalise a failing prior value. Running the porter's F2
normalisation over stale rows here would be a SECOND implementation of a normalisation the
computed path already performs correctly, and two implementations of a contract boundary is
how they drift apart. Recompute instead; the computed path is the one source of truth for
what a valid row looks like.

Spec backlink: emit() cost-lever work, 2026-07-29 (Levers 2 and 3 — cadence-gate
_stamp_docs_staleness and the file_attribution section).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

__all__ = [
    "MUST_COMPUTE",
    "NULL_SANCTIONED",
    "load_prior_emission",
    "skipped_stage_value",
]

# Fallback policies for "no last-known value is available". See module docstring.
NULL_SANCTIONED = "null_sanctioned"
MUST_COMPUTE = "must_compute"

# Sentinel distinguishing "the prior emission carried this field, and its value was None"
# from "the prior emission did not carry this field at all".
_ABSENT = object()

# Per-process memo, keyed on the resolved cache path: several gated stages consult the same
# prior emission within one build(), and that file is multi-megabyte on a real corpus. Safe
# to memo for the life of the process because claude-klabauter is spawn-per-call (DR-215) — the file is
# read before this run writes anything, and a fresh interpreter re-reads it next time.
_PRIOR_CACHE: dict[str, Optional[dict]] = {}


def _prior_emission_path(ctx: Any) -> Path:
    """Return the canonical emission path for *ctx* — the reuse source, never ``out``."""
    from coordinator_core.ops.emit.envelope import DEFAULT_OUTPUT_NAME

    return Path(ctx.central_state_root) / DEFAULT_OUTPUT_NAME


def load_prior_emission(ctx: Any) -> Optional[dict]:
    """Return the previously-emitted envelope as a dict, or None if there isn't a usable one.

    None covers absent, unreadable, non-JSON, and non-object payloads alike — every one of
    which means "no last-known value is available", which each caller's declared fallback
    policy then decides how to handle. Never raises: a broken prior emission must not be able
    to break the current one.
    """
    path = _prior_emission_path(ctx)
    key = str(path)
    if key in _PRIOR_CACHE:
        return _PRIOR_CACHE[key]
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        parsed = None
    _PRIOR_CACHE[key] = parsed
    return parsed


def clear_prior_emission_cache() -> None:
    """Drop the per-process prior-emission memo (test seam only)."""
    _PRIOR_CACHE.clear()


def _warn_declined(ctx: Any, stage: str, absent_policy: str, reason: str) -> None:
    """Report a declined reuse on stderr — the ONE framing both decline paths use.

    Loud, because the operator-visible symptom is "the cheap tier paid full cost" and the
    cause is either a stale artifact on disk or a broken extractor, neither of which is
    anything about the current run. A validator rejection and an extractor blowing up are
    equally capable of masking a real defect, so they get the same diagnostic trail.
    """
    print(
        f"emit: WARNING — skipped-stage {stage!r} declined to reuse the previously "
        f"emitted value: {reason}. Falling back to this stage's absent_policy "
        f"({absent_policy}); a MUST_COMPUTE stage will now recompute at full cost. "
        f"Reuse source: {_prior_emission_path(ctx)}.",
        file=sys.stderr,
    )


def skipped_stage_value(
    ctx: Any,
    *,
    stage: str,
    extract: Callable[[dict], Any],
    absent_policy: str,
    validate: Callable[[Any], Optional[str]],
) -> Tuple[bool, Any]:
    """Resolve the value a SKIPPED enrichment stage should write.

    Args:
        ctx: the ``EmitContext`` for this run — supplies the reuse source's location.
        stage: the stage's name. Used in the ValueError message on an unknown policy, and in
            the stderr line when a prior value fails validation.
        extract: given the prior envelope, return the last-known value for this stage, or
            ``skipped_stage.absent()`` when the prior emission does not carry it. Passing an
            extractor is what keeps this helper shape-neutral: ``file_attributions`` is a
            top-level array while ``docs_staleness`` lives per-record inside
            ``exec_summaries``, and neither shape belongs in this module.

            MUST NOT MUTATE ``prior``. The dict handed to *extract* is the live
            ``_PRIOR_CACHE``-resident object, not a private copy — every gated stage in this
            process shares it, so an in-place edit (``prior.pop(...)`` as a
            micro-optimisation, say) corrupts every later stage's view of "last known". The
            deep-copy guarantee below covers the value this function RETURNS, not the input.
            Deliberately a contract rather than a defensive ``copy.deepcopy(prior)``: the
            prior emission is ~9.6MB on a real corpus and copying it per gated stage would
            reintroduce most of the cost this gate exists to remove. Enforcement is
            reader-discipline; the tradeoff is stated here so it is a choice, not an
            oversight.
        absent_policy: ``NULL_SANCTIONED`` or ``MUST_COMPUTE`` — what to do when no
            last-known value exists.
        validate: REQUIRED contract check on the extracted prior value. Return ``None`` when
            the value satisfies the same contract the stage's COMPUTED value must satisfy, or
            a short reason string when it does not. A failing value is treated as ABSENT, so
            ``absent_policy`` decides what happens (``MUST_COMPUTE`` → recompute;
            ``NULL_SANCTIONED`` → write null), and the rejection is logged to stderr. Required
            rather than defaulted, because a silent default here IS the incident: pass an
            explicit ``lambda _v: None`` to assert "this stage's prior value cannot be
            contract-invalid" and make that claim visible in the diff. See the module
            docstring's VALIDATED REUSE section.

    Returns:
        ``(True, value)``  — write *value*. Either a genuine last-known value read out of the
            prior emission AND validated, or (``NULL_SANCTIONED`` only) the contract-sanctioned
            ``None``.
        ``(False, None)`` — the skip is not permissible; the caller MUST compute for real.

    This function never manufactures an empty collection. The only value it can invent is
    ``None``, and only under ``NULL_SANCTIONED``. An ``[]`` can come back from here solely
    because the prior emission genuinely recorded ``[]``, which is a truthful last-known
    observation rather than a synthesised claim.

    A rejected prior value is never repaired here — see the module docstring's negative-spec.

    The returned value is a DEEP COPY of what the prior emission held. Reused rows are placed
    into the live envelope and then mutated in place by the post-collect stamps
    (``_stamp_content_hash`` re-hashes every reused ``file_attributions`` row against the
    current working tree, which is exactly what should happen) — writing through to the shared
    prior-emission memo instead would let one stage's stamps leak into another stage's view of
    "last known".
    """
    if absent_policy not in (NULL_SANCTIONED, MUST_COMPUTE):
        raise ValueError(
            f"skipped_stage_value({stage!r}): absent_policy must be NULL_SANCTIONED or "
            f"MUST_COMPUTE, got {absent_policy!r}"
        )

    prior = load_prior_emission(ctx)
    if prior is not None:
        try:
            value = extract(prior)
        except Exception as exc:  # noqa: BLE001 -- a malformed prior emission is "no last-known value"
            # Logged on the same footing as a validator rejection: this also fires on a real
            # bug in the extractor itself (a typo'd key, a wrong type assumption), and a
            # silent swallow there is the next incident in this module's history.
            _warn_declined(ctx, stage, absent_policy, f"extractor raised {type(exc).__name__}: {exc}")
            value = _ABSENT
        if value is not _ABSENT:
            try:
                reason = validate(value)
            except Exception as exc:  # noqa: BLE001 -- a validator that raises rejects the value
                reason = f"validator raised {type(exc).__name__}: {exc}"
            if reason:
                _warn_declined(ctx, stage, absent_policy, reason)
                value = _ABSENT
        if value is not _ABSENT:
            return True, copy.deepcopy(value)

    if absent_policy == NULL_SANCTIONED:
        return True, None
    return False, None


def absent() -> Any:
    """The sentinel an ``extract`` callable returns for "the prior emission lacks this"."""
    return _ABSENT
