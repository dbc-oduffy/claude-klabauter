"""coordinator_core.ops.docgen.volatility — the closed volatile-field set (C5, AC5).

Purpose: pin, BEFORE C6's byte-identity conformance harness exists, the complete
enumeration of every frontmatter field ``coordinator-doc-new`` populates from a
non-deterministic source, so C6 has a single authored contract to inject/redact
against instead of discovering volatility ad hoc per failing conformance case.

Verified against every ``_scaffold_*`` call site and the ``main()`` value-resolution
block in the oracle (``coordinator-doc-new``), cross-checked against every field
key actually declared in this plan's 22 extracted templates (``templates/*.json``,
C2) — not merely the plan body's own prose summary, which named only
``deliverable_id``/``branch``/``created``/``dispatched_at`` and omitted three
real volatile fields this audit found: ``handoff_id``, ``completion_id``, and
``plan_id`` (all minted via ``_mint_artifact_id``/``_mint_plan_id`` — the same
``sha1(slug|epoch|pid|random)`` non-determinism as an un-carried
``deliverable_id`` — with NO CLI carry/override flag; every call site comment at
``coordinator-doc-new`` reads "no carry path ... always minted fresh"). Both
gaps matter for C6: a volatile field absent from this registry would render
byte-identical BY LUCK on any given run and then flake the first time wall-clock
or PRNG state differs between the two sides.

Three non-deterministic MECHANISMS produce volatile values in the oracle, not
one:
  1. ``_mint_deliverable_id`` — carry-or-mint; mint path is
     ``sha1(slug|epoch|pid|random)``-based (non-deterministic) unless a caller
     supplies the value.
  2. ``_current_branch()`` — a git shellout; environment-dependent, not
     wall-clock-dependent, but still not reproducible across two independent
     invocations of the two sides being compared.
  3. ``_mint_artifact_id`` / ``_mint_plan_id`` (``handoff_id``, ``completion_id``,
     ``plan_id``) and ``datetime.now()`` (``created``, ``date``, ``dispatched_at``,
     ``spawned_at``) — always minted/derived fresh; no carry path exists for any
     of these six fields.

Two DISJOINT field families follow, matched to a HYBRID normalization contract:

  INJECTABLE_FIELDS (``deliverable_id``, ``branch``) — the oracle exposes a real
  argparse flag (``--deliverable-id`` / ``--branch``, confirmed present in the
  CLI's argument parser) that short-circuits minting with a caller-supplied
  value. C6 drives BOTH sides (this module's render + the live oracle) with the
  SAME injected value, producing literal byte-identity on these fields — no
  redaction needed where a type carries them.

  REDACT_ONLY_FIELDS (``created``, ``date``, ``dispatched_at``, ``spawned_at``,
  ``handoff_id``, ``completion_id``, ``plan_id``, ``run_id``) — no CLI flag exists
  for any of these eight; C6 must redact them identically on both sides before
  comparing. (``run_id`` — ``audit-record``'s ``run_id_placeholder`` field —
  closed a real registry gap: a code-review pass caught it patched only in the
  C6 test's private ``_LOCAL_EXTRA_REDACT``, not in this frozen registry;
  fixed here per the review finding.)

The set is CLOSED: ``VOLATILE_FIELDS`` is the only vocabulary this module or any
downstream conformance harness may treat as non-deterministic. Adding a field to
either family without also adding it to ``VOLATILE_FIELDS`` and to a per-type
policy entry is a defect this module's own tests catch (``test_volatility.py``
cross-validates the registry against ``VOLATILE_FIELDS`` AND against the field
keys template_format's loaded templates actually declare, in both directions —
a policy entry naming a field the template doesn't have, or a template field
name matching ``VOLATILE_FIELDS`` with no policy entry, both fail loud).

``strategic-self-description`` is the one type this audit found to be FULLY
DETERMINISTIC — zero volatile fields, confirmed against both the oracle's own
scaffolder body and this plan's extracted template (no field name coincides
with ``VOLATILE_FIELDS``). ``review-findings`` was a second such type until
the run-report/review-findings schema unification (docs/plans/2026-07-24-
reviewer-sidecar-provisioning-reconciliation.md) made it frontmatter-bearing;
it now carries ``spawned_at`` (mechanism 3, ``datetime.now()``, no CLI carry
path — redact-only) same as ``run-report``. ``lead_session_id`` is NOT tracked
here despite resolving from an env var: it follows the same non-volatile
treatment as ``run-report``'s ``dispatched_by`` (same
``COORDINATOR_SESSION_ID`` > ``CLAUDE_SESSION_ID`` > ``CLAUDE_CODE_SESSION_ID``
precedence chain) — one harness session, shared identity, deterministic across
a single test process's subprocess boundary, not wall-clock/mint-derived.

Negative-spec: this module does not read the live DoE clone, does not shell out,
and does not mint or resolve any value itself — it is a pure, static field-policy
registry plus small mapping/text redaction helpers. Injecting a shared value into
the oracle's own CLI invocation, and invoking the live oracle at all, is C6's
surface (the byte-identity conformance harness), not this one's.

Spec backlink: pln-strang-12-document-generation--75a7eb § C5 (AC5)
Oracle: /coordinator/bin/coordinator-doc-new.py (DoE clone) — ``_mint_deliverable_id``,
``_mint_artifact_id``, ``_mint_plan_id``, ``_current_branch``, the
``--branch``/``--deliverable-id`` argparse flags, and every ``_scaffold_*`` call
site's ``created``/``date``/``dispatched_at``/``spawned_at`` emission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import template_format

__all__ = [
    "FieldPolicy",
    "MINT_FIELDS",
    "TIME_FIELDS",
    "VOLATILE_FIELDS",
    "DETERMINISTIC_TYPES",
    "volatile_fields_for",
    "injectable_fields_for",
    "redact_only_fields_for",
    "is_fully_deterministic",
    "redact_values",
    "redact_text",
    "assert_closed_set",
]

REDACTED_TOKEN = "<REDACTED>"

# Volatility mechanism 1+2: flag-carryable (--deliverable-id / --branch).
MINT_FIELDS: frozenset[str] = frozenset({"deliverable_id", "branch"})

# Volatility mechanism 3: no carry path, always minted/derived fresh.
# Review: code-reviewer — "run_id" (audit-record's run_id_placeholder field,
# date-derived via _today() with no CLI carry path) was a real registry gap,
# previously patched only in the C6 test's private _LOCAL_EXTRA_REDACT.
TIME_FIELDS: frozenset[str] = frozenset(
    {
        "created",
        "date",
        "dispatched_at",
        "spawned_at",
        "handoff_id",
        "completion_id",
        "plan_id",
        "run_id",
    }
)

# The closed set (AC5). No field name outside this union may be treated as
# volatile by this module or any downstream conformance harness.
VOLATILE_FIELDS: frozenset[str] = MINT_FIELDS | TIME_FIELDS


@dataclass(frozen=True)
class FieldPolicy:
    """One volatile field's normalization contract for a single ``doc_type``.

    ``field`` is the template's emitted YAML *key* (``spec["key"]`` in
    ``template_format``/``render`` terms) — this is what ``redact_text`` matches
    against rendered output text, and what ``assert_closed_set`` cross-checks
    against each template's declared frontmatter field keys.

    ``value_field`` is the RESOLVED-VALUE MAPPING key — ``render.render_document``'s
    ``resolved_field_values`` input is keyed by the template's ``field`` attribute,
    which for most tracked fields coincides with ``field`` (the emitted key) but
    diverges for a few (``problem-set``'s emitted ``date:`` line is fed by a
    ``created`` mapping key; ``audit-record``'s emitted ``run_id:`` line is fed by
    a ``run_id_placeholder`` mapping key). Defaults to ``field`` when unset — the
    common case. ``redact_values`` (operating on the mapping, not rendered text)
    must use this, not ``field`` — using ``field`` there was a real bug (a
    code-review finding) that silently no-op'd for any type where the two diverge.

    ``injectable_flag`` is the oracle CLI flag that pins this field to a
    caller-supplied value (``--branch``, ``--deliverable-id``), or ``None`` when
    the field has no carry path and must instead be redacted identically on
    both sides post-render.
    """

    field: str
    injectable_flag: str | None
    value_field: str | None = None

    def resolved_field(self) -> str:
        """The key into a ``resolved_field_values`` mapping for this field."""
        return self.value_field if self.value_field is not None else self.field


def _p(field: str, flag: str | None = None, *, value_field: str | None = None) -> FieldPolicy:
    return FieldPolicy(field=field, injectable_flag=flag, value_field=value_field)


# Per-type volatile-field policy — the closed set, exhaustive over all 22
# extracted types (C2's templates/*.json). A type absent from this dict (or
# present with an empty tuple) carries NO volatile fields at all — see
# DETERMINISTIC_TYPES below for the two such types this audit found.
#
# Verified line-for-line against templates/*.json's declared frontmatter field
# keys (not merely the oracle's ``main()``) — e.g. ``goal-seed`` carries
# ``branch``/``handoff_id`` but NOT ``deliverable_id`` (excluded from the
# oracle's ``_spine_types`` set) — a real asymmetry, not an omission.
# ``roadmap-baton`` USED TO carry ``deliverable_id`` but NOT ``handoff_id``
# (excluded from the oracle's handoff-id-minting doc_type tuple) — that
# exclusion was a break-class defect (AC13, docs/plans/2026-08-01-baton-
# spine-information-integrity.md § A5): minted roadmap batons carried
# ``stub_id``/``deliverable_id`` with no ``handoff_id`` at all, so any
# fleet-side consumer joining on ``handoff_id`` missed the entire
# roadmap-baton record class. Fixed at the same commit as this comment —
# ``roadmap-baton`` now carries ``handoff_id`` like every other
# handoff-family doc_type below.
_POLICY: dict[str, tuple[FieldPolicy, ...]] = {
    "audit-record": (
        _p("created"),
        _p("run_id", value_field="run_id_placeholder"),
    ),
    "completion": (_p("created"), _p("completion_id")),
    "decision": (_p("created"),),
    "docs-check": (_p("created"),),
    "goal": (_p("created"),),
    "handoff": (
        _p("created"),
        _p("branch", "--branch"),
        _p("deliverable_id", "--deliverable-id"),
        _p("handoff_id"),
    ),
    "health-status": (_p("created"),),
    "memo": (_p("created"),),
    "plan": (
        _p("created"),
        _p("branch", "--branch"),
        _p("plan_id"),
        _p("deliverable_id", "--deliverable-id"),
    ),
    "plan-coverage-check": (_p("created"),),
    "prior-art-check": (_p("created"),),
    "problem-set": (_p("date", value_field="created"),),
    "recovery": (
        _p("created"),
        _p("branch", "--branch"),
        _p("deliverable_id", "--deliverable-id"),
        _p("handoff_id"),
    ),
    "research-synthesis": (_p("created"),),
    "review": (_p("created"),),
    "review-findings": (_p("spawned_at"),),
    "run-report": (_p("dispatched_at"), _p("spawned_at")),
    "spinoff": (
        _p("created"),
        _p("branch", "--branch"),
        _p("deliverable_id", "--deliverable-id"),
        _p("handoff_id"),
    ),
    "goal-seed": (
        _p("created"),
        _p("branch", "--branch"),
        _p("handoff_id"),
    ),
    "roadmap-baton": (
        _p("created"),
        _p("branch", "--branch"),
        _p("deliverable_id", "--deliverable-id"),
        _p("handoff_id"),
    ),
    "roadmap-seed": (
        _p("created"),
        _p("branch", "--branch"),
        _p("deliverable_id", "--deliverable-id"),
        _p("handoff_id"),
    ),
    "strategic-self-description": (),
}

# The one extracted type this audit found to be fully deterministic: no
# minted id, no shellout-derived value, no wall-clock read anywhere in its
# oracle scaffolder body or its extracted template. ``review-findings`` was
# a second member until the 2026-07-24 schema unification made it
# frontmatter-bearing (spawned_at is now wall-clock-derived) — see the
# module docstring.
DETERMINISTIC_TYPES: frozenset[str] = frozenset({"strategic-self-description"})


def volatile_fields_for(doc_type: str) -> tuple[FieldPolicy, ...]:
    """Return the closed volatile-field policy tuple for ``doc_type``.

    Returns an empty tuple for a fully-deterministic type (or any doc_type this
    registry has never heard of — callers distinguish the two via
    ``template_format.available_template_types()``).
    """
    return _POLICY.get(doc_type, ())


def injectable_fields_for(doc_type: str) -> dict[str, str]:
    """Return ``{field: cli_flag}`` for ``doc_type``'s flag-carryable fields."""
    return {
        p.field: p.injectable_flag
        for p in volatile_fields_for(doc_type)
        if p.injectable_flag is not None
    }


def redact_only_fields_for(doc_type: str) -> tuple[str, ...]:
    """Return the field names for ``doc_type`` with no CLI carry path."""
    return tuple(p.field for p in volatile_fields_for(doc_type) if p.injectable_flag is None)


def is_fully_deterministic(doc_type: str) -> bool:
    """True when ``doc_type`` carries zero volatile fields (of either family)."""
    return len(volatile_fields_for(doc_type)) == 0


def redact_values(
    resolved_field_values: Mapping[str, Any], doc_type: str, *, token: str = REDACTED_TOKEN
) -> dict[str, Any]:
    """Redact ``doc_type``'s redact-only fields in a resolved-value mapping.

    Applies to the SAME flat mapping shape ``render.render_document`` consumes —
    intended for the internal-render side of a C6 comparison after rendering (or
    equivalently, redact the resolved inputs before rendering both sides; either
    application point yields the same redacted text since these fields are
    ``value``/``present_as_null`` kinds with no other line dependent on them).
    Leaves injectable fields (``deliverable_id``/``branch``) untouched — those
    are normalized by DRIVING both sides with the same injected value instead,
    not by redaction.

    Redacts by each policy's ``resolved_field()`` (the mapping key), NOT by
    ``field`` (the template's emitted YAML key) — a code-review finding caught
    this function silently no-op'ing for ``problem-set`` (mapping key
    ``created`` vs. emitted key ``date``), because it used to key off ``field``
    directly, which only coincidentally matched the mapping for every OTHER
    tracked type.
    """
    out = dict(resolved_field_values)
    for policy in volatile_fields_for(doc_type):
        if policy.injectable_flag is not None:
            continue
        key = policy.resolved_field()
        if key in out:
            out[key] = token
    return out


def redact_text(text: str, doc_type: str, *, token: str = REDACTED_TOKEN) -> str:
    """Redact ``doc_type``'s redact-only fields in ALREADY-RENDERED text.

    Line-oriented: for each redact-only field, replaces the value portion of any
    ``<optional indent><field>: <value>`` line with ``token``, regardless of the
    original quoting style — this is what makes redaction apply IDENTICALLY on
    both sides of a byte-identity comparison even when the internal renderer and
    the oracle CLI happen to quote a redacted value differently (they should not,
    per ``render``'s byte-for-byte ``_yaml_quote`` mirror, but redaction must not
    depend on that separately-tested guarantee to be correct).

    Intended for the oracle-CLI-output side of a C6 comparison (a raw markdown/
    YAML string), where there is no resolved-value mapping to redact directly.

    Negative-spec (break-class fix, 2026-07-28 — do NOT "simplify" the padding
    after the colon back to ``\\s*``): ``\\s`` matches a NEWLINE, so on a
    present-but-empty ``field:`` line the captured prefix walked past the line
    break and the trailing ``.*$`` then matched the FOLLOWING line — ``sub``
    replaced that innocent neighbour with the redaction token and dropped its
    content. ``redact_text("created:\\nstatus: open\\n", "decision")`` returned
    ``"created:\\n<REDACTED>\\n"``, silently destroying ``status: open`` on one
    side of a byte-identity comparison. The pad is therefore ``[ \\t]*``
    (horizontal whitespace only). The canonical statement of this defect and
    the pattern shape that avoids it lives in
    ``coordinator_core.frontmatter.primitives`` (``read_fm_field`` /
    ``replace_fm_field_raw``); this module cannot route through those helpers
    because it redacts INDENTED, non-frontmatter-block lines too (see the
    leading ``\\s*``, which is deliberate indent tolerance and stays).

    The LEADING ``\\s*`` is a different thing and is correct: it is captured
    into group 1 and re-emitted verbatim, so whatever it consumes is preserved
    rather than replaced. The trailing ``(\\r?)`` likewise re-emits a CRLF
    document's own carriage return, so redacting one line cannot leave the
    text with mixed line endings.
    """
    for field in redact_only_fields_for(doc_type):
        pattern = re.compile(rf"^(\s*{re.escape(field)}:[ \t]*).*?(\r?)$", flags=re.MULTILINE)
        text = pattern.sub(lambda m: m.group(1) + token + m.group(2), text)
    return text


class VolatilitySetError(AssertionError):
    """Raised by ``assert_closed_set`` when the registry and the on-disk
    templates disagree about which fields are volatile for some type.
    """


def assert_closed_set(templates_directory: str | None = None) -> None:
    """Assert this module's registry is closed AND consistent with the extracted
    templates on disk, in both directions:

      1. Every field named in ``_POLICY`` is a member of ``VOLATILE_FIELDS``
         (no policy entry may reference an untracked field name).
      2. Every field named in ``_POLICY`` for a ``doc_type`` is actually a
         declared frontmatter field key in that type's template (no policy
         entry may reference a field the template doesn't have).
      3. No template field key that MATCHES ``VOLATILE_FIELDS`` by name is
         missing a corresponding ``_POLICY`` entry for its type (no volatile
         field may go untracked).

    Raises ``VolatilitySetError`` carrying every violation found (not just the
    first), matching this repo's fail-loud-with-detail convention.
    """
    errors: list[str] = []

    for doc_type, policies in _POLICY.items():
        for policy in policies:
            if policy.field not in VOLATILE_FIELDS:
                errors.append(
                    f"{doc_type}: policy field {policy.field!r} is not a member of VOLATILE_FIELDS"
                )

    d = template_format.templates_dir() if templates_directory is None else Path(templates_directory)
    for path in sorted(d.glob("*.json")):
        data = template_format.load_template(path)
        doc_type = data["doc_type"]
        frontmatter = data.get("frontmatter")
        template_field_keys = (
            {f["key"] for f in frontmatter["fields"] if f.get("key")} if frontmatter else set()
        )

        policy_fields = {p.field for p in _POLICY.get(doc_type, ())}

        missing_from_template = policy_fields - template_field_keys
        if missing_from_template:
            errors.append(
                f"{doc_type}: policy references field(s) {sorted(missing_from_template)} "
                "not declared in the extracted template"
            )

        untracked_volatile = (template_field_keys & VOLATILE_FIELDS) - policy_fields
        if untracked_volatile:
            errors.append(
                f"{doc_type}: template declares volatile-named field(s) "
                f"{sorted(untracked_volatile)} with no policy entry"
            )

        if doc_type not in _POLICY and template_field_keys & VOLATILE_FIELDS:
            errors.append(
                f"{doc_type}: has no _POLICY entry at all but declares volatile-named "
                f"field(s) {sorted(template_field_keys & VOLATILE_FIELDS)}"
            )

    if errors:
        raise VolatilitySetError("; ".join(errors))
