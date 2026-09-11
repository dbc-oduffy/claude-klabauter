"""coordinator_core.executor_return_contract — the one definition of what a
dispatched executor's return shape looks like: its footprint constraint, its
self-verify step, and its DONE-summary shape.

Three consumers across two packages read this today
(`backlog_grind_assemble.readers_mise`, `backlog_grind_assemble.readers_blitz`,
and `ops.dispatch_emit.emit`, wired in C2/C3), and the live dependency
direction is already `backlog_grind_assemble -> ops`
(`readers_mise` imports `ops.queue_family`, `ops.ceremony.wsc_disposition`,
`ops.review_brightline_gate`) — homing the contract in the assembler would
invert that edge, so it lives here as a top-level leaf instead, matching the
repo's dominant idiom (~40 such modules).

Follows the same shape this repo already uses twice for exactly this
problem (`directives.build_executor_dispatch_prompt_template_emission`,
`verifier.build_haiku_verifier_dispatch`): one shared shape, caller-supplied
per-surface parameters — never one frozen text.

`FOOTPRINT_CONSTRAINT_TEMPLATE` is a genuine constant, moved verbatim: its
own `[list]` placeholder is filled by whoever dispatches, and a report path
outside a row's own `writes:` is handled by including that path IN the
list at dispatch time — no carve-out clause belongs in this module.

`self_verify_constraint` stays a builder because its commit/verification
clauses are surface-dependent, and takes TWO separate keyword parameters
(`commit_authority`, `deferred_verification_authority`) rather than one
shared value, because those two clauses do not always name the same
authority. Today's hand-dispatch text says "leave it to the EM" and "Only
the EM commits, once per wave" — TRUE for mise hand-dispatch (one
authority, both clauses), FALSE on the emitted path, where a
`coordinator:git-commit-agent` phase commits alone while broader
verification is deferred to that phase together with the run's terminal
`coordinator:test-runner` phase.

`done_summary_constraint` stays a builder because its output path and its
own DONE-summary field list are per-surface (mise's AC checklist vs.
blitz's before/after snippets), and the "Reply EXACTLY `<STATUS>: <path>`"
sentence is not surface-invariant either — it carries that same per-surface
path, so it lives inside this builder's own rendered text rather than as a
standalone constant.
"""

from typing import Sequence

__all__ = [
    "FOOTPRINT_CONSTRAINT_TEMPLATE",
    "self_verify_constraint",
    "done_summary_constraint",
]

#: `readers_mise._FOOTPRINT_CONSTRAINT_TEMPLATE` /
#: `readers_blitz._FOOTPRINT_CONSTRAINT_TEMPLATE`, byte-identical today,
#: moved verbatim. `[list]` is filled in by whoever renders the final
#: prompt; a surface whose own report lands outside a row's declared
#: `writes:` (the emitted path) includes that report path IN the list it
#: substitutes for `[list]`, rather than this module carrying a carve-out.
FOOTPRINT_CONSTRAINT_TEMPLATE = (
    "You MUST NOT create or modify any file outside this footprint: "
    "[list]. If you discover you need to, STOP and report back via the "
    "DONE summary with status BLOCKED."
)


def self_verify_constraint(
    *, commit_authority: str, deferred_verification_authority: str | None = None
) -> str:
    """Render the self-verify constraint: re-read the spec, confirm the
    footprint, verify scoped-only, then leave broader verification to
    `deferred_verification_authority` and commit to `commit_authority`.

    Today's hand-dispatch text (`readers_mise._SELF_VERIFY_CONSTRAINT`) is
    written for mise hand-dispatch specifically — "leave it to the EM" and
    "Only the EM commits, once per wave" — both TRUE there (one authority
    does both jobs), but not on every surface: on the emitted path a
    `coordinator:git-commit-agent` phase commits, while broader
    verification is deferred to that phase AND the run's terminal
    `coordinator:test-runner` phase together. Those are not the same
    authority, so this builder takes two separate keyword parameters
    instead of one shared one — sharing one would force slot (4) ("Only
    <X> commits...") to also name the phase that never commits, which is
    exactly the false-both-commit defect measured on the emitted path
    (Review: coordinator:code-reviewer, finding 1, EM-agreed break-class
    fix). `deferred_verification_authority` defaults to `commit_authority`
    when omitted, so a caller with one shared authority (every
    hand-dispatch surface today) supplies one value and gets identical
    behaviour to a single-parameter builder.

    Both values MUST be bare noun phrases. This builder, not the caller,
    supplies the "commits, once per wave, after every item in the wave
    passes verification" tail — `commit_authority` is spliced into that
    sentence, never composes it. A value carrying its own parenthetical
    gloss of what it does renders that tail a second time and reads as
    garbled duplication: measured at 8b0ee94908, where an emitted
    `commit_authority` describing itself mid-value ended "...(which runs
    broader verification) commits, once per wave, after every item in the
    wave passes verification." Every caller of this shared builder is
    bound by this, not just the one that tripped it.
    """
    if deferred_verification_authority is None:
        deferred_verification_authority = commit_authority
    return (
        "After implementation: (1) re-read the spec's `## Tasks` spine row "
        "for this item -- plus its `prime_exit_criterion` if the spec "
        "carries one -- and confirm each is discharged; (2) run `git "
        "status --porcelain -- <footprint paths> | cut -c4-` and confirm "
        "every changed AND created path is inside the declared footprint; "
        "(3) run verification scoped to the files/dirs you touched only — "
        "never the repo's fast-test command, the full suite, or any "
        "unscoped runner invocation; note in the DONE summary if the spec "
        "calls for broader verification and leave it to "
        f"{deferred_verification_authority}; "
        "(4) leave your changes uncommitted and unstaged — you do not "
        f"invoke git under any circumstance. Only {commit_authority} "
        "commits, once per wave, after every item in the wave passes "
        "verification."
    )


def done_summary_constraint(
    *,
    output_path_template: str,
    extra_fields: Sequence[str] = (),
) -> str:
    """Render the DONE-summary constraint: a one-screen summary at
    `output_path_template` carrying the invariant spine (status enum and
    the no-commit-SHA rule), each caller's own `extra_fields` (the
    changed-path-list clause included — mise's is the `git status
    --porcelain` invocation, blitz's own directive already carries a
    computed `files` list and names that instead), and the "Reply EXACTLY"
    sentence spliced against the same path.

    `_DONE_SUMMARY_CONSTRAINT_TEMPLATE` is NOT byte-identical between
    `readers_mise` and `readers_blitz` today — different output paths,
    different required fields, different changed-path-list clauses — and
    that asymmetry is exactly what `extra_fields` carries; this function
    does not tidy it away. `extra_fields` entries are joined with `", "`
    verbatim (the trailing Oxford "and" a caller wants before its last
    entry is the caller's own text, not list-formatting this function
    performs).

    The reply leads with the summary's own status, never a fixed `DONE`: the
    reply is the only text a workflow reads without opening the summary
    file, so a reply that says DONE over a PARTIAL summary is how a run
    reported `completed: true` with an unfinished chunk (example-store-repo,
    2026-09-11).
    """
    fields_text = ", ".join(extra_fields)
    return (
        f"Write a one-screen summary to `{output_path_template}` with: "
        f"status (DONE | BLOCKED | PARTIAL), {fields_text}. Do not include "
        "a commit SHA — your changes are still uncommitted when you write "
        f"this summary. Reply EXACTLY `<STATUS>: {output_path_template}`, "
        "where <STATUS> is the status your summary records — `DONE`, "
        "`PARTIAL`, or `BLOCKED`."
    )
