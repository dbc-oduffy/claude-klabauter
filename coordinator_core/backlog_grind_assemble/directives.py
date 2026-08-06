"""coordinator_core.backlog_grind_assemble.directives — shared directive/
judgment-point builders for the five backlog-grind readers (`readers_blitz`,
`readers_mise`, `readers_sweep`, `readers_debt`, `readers_dogfood`) and the
`brief()` seam that concatenates them.

This module builds DICTS only — it never mutates anything, never shells
out, and never imports `subprocess`. The dicts it returns are consumed two
ways downstream: `apply.py` (C4) dispatches `directives[].cli` through its
own closed, literally-named table (`commit-per-item`, `commit-per-wave`,
`tier-u-grant-cli`, `spinoff-handoff-template`,
`executor-dispatch-prompt-template`) via `coordinator_core.contract.
apply_base`; judgment points feed the same envelope's `judgment_points[]`
via `build_judgment_point`/`build_untrusted_gate_judgment_point`, imported
from the CONTRACT module (`coordinator_core.contract.decision_object.
judgment`), never re-derived and never `pickup_assemble`'s own
same-named-but-incompatible constructor — see the signature warning below.

Contract: example-doctrine-repo coordinator/docs/wiki/computed-skills.md
Spec backlink: docs/plans/2026-07-26-backlog-grind-computed-frontage.md,
chunk C2. D-1..D-6 (esp. D-3, the granularity policy this module encodes)
live in that plan's "Key decisions" section — read them before changing
`build_stage_and_commit`'s shape.

Signature warning (verbatim from the plan — do not grep-and-copy the first
hit): `pickup_assemble`'s own `build_judgment_point(id, question, evidence,
dispositions, recommendation, *, ...)` is a DIFFERENT, incompatible
signature from the contract's `build_judgment_point(recommendation, *, id,
question, dispositions, evidence, reason, ...)` — argument order differs,
`reason` is a REQUIRED keyword on the contract version, and
`revalidate_at_dispatch` defaults `True` there. This module imports and
uses ONLY the contract version.

AC28 — does any backlog-grind directive need the array (AND-semantics)
form of `depends_on` for a multi-gate judgment point? **No.** Every
builder in this module attaches at most ONE judgment-point id to a given
directive's `depends_on` (a bare `str`, per `apply_base.normalize_depends_on`'s
scalar branch) — `build_stage_and_commit`'s optional `depends_on` and
`build_tier_u_grant_flow`'s wired `jp_id` are both single ids, never a
list. `apply_base.normalize_depends_on`/`order_by_depends_on` already
support the list (AND-semantics) form generically — `baton_assemble`
exercises it today (`["d1"]`) — so nothing here would need to change if a
future backlog-grind directive genuinely gated on more than one
independently-resolving judgment point. It just doesn't happen across
this cluster's five surfaces today: every gate this module builds is a
single ask (PM Tier-U authorization, or the bug-blitz commit-readiness
judgment point below) with a single disposition that resolves the one
directive it gates.

D-3 policy encoded by `build_stage_and_commit` — granularity carries three
things, not a bare cardinality knob:
  (a) commit cardinality — expressed as the `cli` verb selected
      (`commit-per-item` vs `commit-per-wave`); the verbs themselves are
      C4's internal dispatch-table entries, never invoked here and never
      `coordinator-safe-commit` (forbidden by name, `bug-blitz.md:403`,
      off the 2026-05-06-22h42 concurrent-commit-absorption incident).
  (b) branch re-verification cadence — the `branch_recheck_cadence` field,
      set to `per-commit` for `per-item` and `per-wave` for `per-wave`,
      matching granularity 1:1 by construction (there is no code path
      that can produce a mismatched pair from this builder). C4's apply
      loop reads this field and hard-halts a mid-loop `git branch
      --show-current != expected_branch` mismatch rather than committing
      — the halt itself is C4's, this module only carries the cadence
      that tells C4 how often to check.
  (c) for `per-item` only, the non-PASS failure path as an explicit
      sub-directive nested at `directive["on_non_pass"]`
      (`git checkout -- <paths>` + a backlog note) — never emitted for
      `per-wave`, per D-3's own scope limit. This is nested rather than a
      second top-level `directives[]` entry because the fork it encodes
      is a runtime execution-result branch (did this item's own
      verification PASS?) internal to the `commit-per-item` handler, not
      an independently-dispatchable directive gated by a judgment-point
      disposition.

The raw `granularity` string is ALSO carried verbatim as `directive["granularity"]`
— AC7 requires it be a named field a caller can branch on directly, not merely
inferable by reverse-mapping `cli` back through `_CLI_BY_GRANULARITY`.

Negative-spec:
    - Do NOT call `subprocess`, `git`, or any mutating primitive from this
      module — every function here returns a plain dict (or a tuple of
      dicts); execution is entirely C4's (`apply.py`).
    - Do NOT collapse `per-item`/`per-wave` into a single code path that
      loses (b) or (c) — see D-3's own correction of exactly that
      collapse.
    - Do NOT invoke `coordinator-safe-commit` from anywhere in this
      module, by name or by proxy — `bug-blitz.md:403` forbids it from
      committer scope; the `cli` verbs this module emits are internal
      names C4's own closed dispatch table owns.
    - Do NOT attach a `recommendation` to a judgment point built with
      `build_untrusted_gate_judgment_point` — the PM Tier-U-authorization
      ask in `build_tier_u_grant_flow` is exactly the "untrusted gate"
      case the contract module's negative-spec describes; it structurally
      cannot carry one (no such parameter exists on that constructor).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
    build_untrusted_gate_judgment_point,
)

__all__ = [
    "build_stage_and_commit",
    "build_commit_readiness_gate",
    "build_tier_u_grant_flow",
    "build_spinoff_handoff_template_emission",
    "build_executor_dispatch_prompt_template_emission",
]

GRANULARITY_PER_ITEM = "per-item"
GRANULARITY_PER_WAVE = "per-wave"
_VALID_GRANULARITIES = (GRANULARITY_PER_ITEM, GRANULARITY_PER_WAVE)

#: cli verb selected by granularity — both names are C4's own
#: `apply.py::_CLI_DISPATCH` entries (mirroring `consolidate_assemble`'s
#: `delete-only`/`cherry-pick-and-delete`/`merge-and-delete` precedent),
#: never a call made from this module.
_CLI_BY_GRANULARITY = {
    GRANULARITY_PER_ITEM: "commit-per-item",
    GRANULARITY_PER_WAVE: "commit-per-wave",
}

#: branch-recheck cadence matched 1:1 to granularity — D-3(b).
_BRANCH_RECHECK_CADENCE_BY_GRANULARITY = {
    GRANULARITY_PER_ITEM: "per-commit",
    GRANULARITY_PER_WAVE: "per-wave",
}

#: the non-PASS failure-path verb nested under `on_non_pass` for
#: `per-item` directives only — C4's own dispatch-table entry, never
#: invoked here.
_ON_NON_PASS_CLI = "checkout-and-backlog-note"

#: the existing, already-live CLI this module's Tier-U grant directive
#: targets — claude-klabauter `coordinator/bin/tier-u-grant-cli`. Consumed
#: by name only; this module never imports or shells out to it.
_TIER_U_GRANT_CLI = "tier-u-grant-cli"

_SPINOFF_HANDOFF_TEMPLATE_CLI = "spinoff-handoff-template"
_EXECUTOR_DISPATCH_PROMPT_TEMPLATE_CLI = "executor-dispatch-prompt-template"


def build_stage_and_commit(
    *,
    id: str,
    paths: Sequence[str],
    message: str,
    granularity: str,
    branch: str,
    expected_branch: str,
    depends_on: Optional[str] = None,
    backlog_note: Optional[str] = None,
) -> dict[str, Any]:
    """Build ONE `directives[]` entry encoding D-3's granularity policy in
    full — commit cardinality (`cli`), branch-recheck cadence
    (`branch_recheck_cadence`), and, for `per-item` only, the non-PASS
    failure path as a nested `on_non_pass` sub-directive. See the module
    docstring's "D-3 policy encoded" section for the field-by-field
    rationale.

    `depends_on` is optional and, when supplied, is always a single
    judgment-point id (a bare `str`) — see AC28 in the module docstring.
    Bug-blitz's own commit directives pass this: per the plan's
    review-gate risk constraint, bug-blitz's `commit-per-item`/
    `commit-per-wave` directives are never execution-ready unconditionally
    — the caller (a `readers_blitz.py` builder, C3a) wires `depends_on` to
    an EM judgment point gating autonomous-fix commits, so C4's `apply()`
    halts before committing until that gate resolves.

    `backlog_note` is only meaningful (and only emitted, nested under
    `on_non_pass`) when `granularity == "per-item"`; it is silently
    ignored for `per-wave` — passing one is not an error, since a caller
    may share a note-formatting helper across both granularities and only
    have it consumed here for the one that uses it.
    """
    if granularity not in _VALID_GRANULARITIES:
        raise ValueError(
            f"build_stage_and_commit: granularity must be one of "
            f"{_VALID_GRANULARITIES}, got {granularity!r}"
        )
    if not paths:
        raise ValueError("build_stage_and_commit: paths must be non-empty")
    if not message:
        raise ValueError("build_stage_and_commit: message must be non-empty")
    if not branch or not expected_branch:
        raise ValueError(
            "build_stage_and_commit: branch and expected_branch are both required"
        )

    paths_list = list(paths)
    directive: dict[str, Any] = {
        "id": id,
        "granularity": granularity,
        "cli": _CLI_BY_GRANULARITY[granularity],
        "args": paths_list,
        "message": message,
        "branch": branch,
        "expected_branch": expected_branch,
        "branch_recheck_cadence": _BRANCH_RECHECK_CADENCE_BY_GRANULARITY[granularity],
        "depends_on": depends_on,
    }
    if granularity == GRANULARITY_PER_ITEM:
        directive["on_non_pass"] = {
            "cli": _ON_NON_PASS_CLI,
            "args": ["checkout", "--", *paths_list],
            "backlog_note": backlog_note,
        }
    return directive


def build_commit_readiness_gate(
    *,
    id: str,
    question: str,
    evidence: str,
    reason: str,
    resolves: Sequence[str],
    recommendation: Optional[Mapping[str, str]] = None,
    round_trip: str = "terminal",
) -> dict[str, Any]:
    """Build the trusted EM judgment point that gates a commit directive
    per the plan's Hard-constraints review-gate risk constraint: **bug-
    blitz's commit directives specifically carry a `depends_on` gate on an
    EM judgment point** rather than being emitted execution-ready — C4's
    `apply()` halts before committing autonomous bug-blitz fixes until
    this gate resolves. `resolves` is normally the single directive id
    `build_stage_and_commit` was (or will be) called with for `id`, wired
    together by the caller (a `readers_blitz.py` builder, C3a).

    Built with `build_judgment_point` — never
    `build_untrusted_gate_judgment_point` — because this is the EM's own
    judgment about commit-readiness, and the EM is a trusted caller
    allowed to attach a `recommendation` (may stay `None` for "no
    opinion"). This is the deliberate counterpoint to
    `build_tier_u_grant_flow` below, whose PM-authorization ask IS the
    untrusted-gate case: two judgment points in this same module, built
    from the two different contract constructors on purpose, not by
    oversight.
    """
    return build_judgment_point(
        recommendation,
        id=id,
        question=question,
        dispositions=[build_disposition("ready-to-commit", resolves=list(resolves))],
        evidence=evidence,
        reason=reason,
        round_trip=round_trip,
    )


def build_tier_u_grant_flow(
    *,
    jp_id: str,
    write_directive_id: str,
    subject: str,
    evidence: str,
    reason: str,
    note: str,
    granted_by: str = "pm",
    ceremony: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the (judgment_point, directive) pair for one Tier-U
    (full-suite) authorization ask, consuming the EXISTING
    `tier-u-grant-cli` (claude-klabauter `coordinator/bin/tier-u-grant-cli`
    — re-confirmed live on disk at authoring time: 6,406 b, `.cmd` twin
    present) by name only.

    The judgment point is built with `build_untrusted_gate_judgment_point`
    — never `build_judgment_point` — because PM authorization is exactly
    the "boundary this engine does not fully trust to judge its own
    situation" case the contract module's own docstring describes: this
    engine cannot attach a `recommendation` to "should the PM grant
    Tier-U," and the constructor makes that structurally impossible
    rather than a convention. Its `granted` disposition `resolves` ONLY
    `write_directive_id` — the `declined` disposition resolves nothing,
    so `apply_base.directive_gate_open` never treats a decline as
    ready-to-fire.

    The directive shells out to `tier-u-grant-cli grant <granted_by>
    <note> [--ceremony <ceremony>]` — the same subcommand/argv shape
    `bug-sweep`/`bug-blitz`'s own prose already uses today
    (`verb grant pm`, composed note = subject + PM's reply). This module
    never invokes the CLI itself; C4's dispatch table does, by this
    literal `cli` name.
    """
    jp = build_untrusted_gate_judgment_point(
        id=jp_id,
        question=(
            f"Authorize a Tier-U (full-suite) invocation for {subject}? "
            "(session-scoped grant, written via tier-u-grant-cli)"
        ),
        dispositions=[
            build_disposition("granted", resolves=[write_directive_id]),
            build_disposition("declined", resolves=[]),
        ],
        evidence=evidence,
        reason=reason,
    )
    args = ["grant", granted_by, note]
    if ceremony:
        args += ["--ceremony", ceremony]
    directive: dict[str, Any] = {
        "id": write_directive_id,
        "cli": _TIER_U_GRANT_CLI,
        "args": args,
        "depends_on": jp_id,
    }
    return jp, directive


def build_spinoff_handoff_template_emission(
    *,
    id: str,
    fields: Mapping[str, Any],
    depends_on: Optional[str] = None,
) -> dict[str, Any]:
    """Build the directive that emits the ~40-line spinoff-handoff
    authoring template (bug-blitz item #32, "the single largest Axis-1
    target") — a pure function of computed fields the assembler already
    holds (AC26), never new judgment. The caller (a compute-side reader,
    e.g. `readers_blitz.py`) is responsible for rendering the FULL template
    body into `fields` before calling this builder — `fields` is handed
    through verbatim as the directive's own `fields` payload, unvalidated
    and unreshaped. C4's dispatch-table handler for
    `spinoff-handoff-template` (`apply.py`) is a pure pass-through: it does
    NOT render anything, it only surfaces the already-rendered `fields` in
    its own report. Rendering lives entirely on the compute side.
    """
    return {
        "id": id,
        "cli": _SPINOFF_HANDOFF_TEMPLATE_CLI,
        "args": [],
        "fields": dict(fields),
        "depends_on": depends_on,
    }


def build_executor_dispatch_prompt_template_emission(
    *,
    id: str,
    fields: Mapping[str, Any],
    depends_on: Optional[str] = None,
) -> dict[str, Any]:
    """Build the directive that emits the fixed multi-part executor
    dispatch-prompt template (bug-blitz item #43; mise-en-place item #19
    is the same shape) — a pure function of computed fields (AC26), never
    new judgment. Same contract as
    `build_spinoff_handoff_template_emission`: the caller renders the FULL
    template body into `fields` before calling this builder; `fields` is
    passed through verbatim. C4's dispatch-table handler
    (`executor-dispatch-prompt-template` in `apply.py`) is a pure
    pass-through — rendering is never its job.
    """
    return {
        "id": id,
        "cli": _EXECUTOR_DISPATCH_PROMPT_TEMPLATE_CLI,
        "args": [],
        "fields": dict(fields),
        "depends_on": depends_on,
    }
