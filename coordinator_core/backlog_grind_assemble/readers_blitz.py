"""
coordinator_core.backlog_grind_assemble.readers_blitz — C3a reader:
bug-blitz's backlog surface, self-gating on `cadence == "bug-blitz"`.

Purpose: expose `collect(cadence) -> ReaderResult` over `state/bug-backlog/`
(read via `coordinator_core.ops.queue_family.load_family_records` only —
no directory-glob, no raw-frontmatter-parse, per that module's own
negative-spec (D-6, AC6, scoped to this file)) plus the standing
commit-readiness gate
the plan's review-gate risk constraint requires: `bug-blitz` is the one
surface in this cluster with no code-review gate of any kind
(`coordinator/commands/bug-blitz.md` has zero occurrences of
`freeze-review-diff`/`code-reviewer`/`review-integrator`), so — until
`hnd-bug-blitz-commits-autonomous-f-a7cb8f` lands and this constraint's
removal condition fires — every commit directive this surface's runtime
builds must carry a `depends_on` gate on an EM judgment point rather than
ship execution-ready, so `apply.py` (C4) halts before committing
autonomous bug-blitz fixes and says why.

The seam (C3) calls `collect()` unconditionally for every cadence and
trusts this module to self-gate internally (mirrors
`orient_assemble.readers_health_reaper`'s day-cadence-only gating, applied
here to a surface-identity cadence instead of a session/day/week severity
knob — see `coordinator_core.test_backlog_grind_assemble`'s `_CADENCES`
for the five surface-identity cadence strings this cluster uses).

Four things live in this module, not one:

1. `collect(cadence)` — the boot-time read: bug-backlog presence/count via
   `load_family_records`, folded into the standing
   `j-bug-blitz-commit-readiness` judgment point's evidence, PLUS AC26's
   fixed executor dispatch-prompt template (item #43 below). This runs
   before any wave dispatch, so it never invents fix-target paths — it
   only asks "is there open bug-backlog work, and if this run commits
   anything, has the EM judged it ready?"
2. `build_commit_per_item` / `build_commit_per_wave` — the two granularity
   builders a live run's own commit machinery (the rebuilt
   `coordinator/commands/bug-blitz.md`, C7, or `apply.py`'s dispatch
   table, C4) calls AT WAVE-COMMIT TIME, once the actual touched-file
   paths are known. Both wrap `directives.build_stage_and_commit` with
   `depends_on` pre-wired to `COMMIT_READINESS_JP_ID` — this is the
   "readers_blitz.py builder" `directives.py`'s own module docstring
   names as the caller responsible for that wiring (see
   `build_stage_and_commit`'s docstring there). Bare
   `directives.build_stage_and_commit` calls for this surface that skip
   these wrappers (and therefore skip the `depends_on` wire) are the
   defect this module exists to prevent.
3. `build_spinoff_handoff` — bug-blitz item #32, "the single largest
   Axis-1 target": renders the ~40-line spinoff-handoff authoring
   template (frontmatter + canonical body-section skeleton + trailing
   marker, per `commands/bug-blitz.md` Phase 2.1's canonical schema and
   `commands/spinoff.md`'s frontmatter shape) as a pure function of
   fields a live run already holds once a `big` item is PM-authorized —
   title, run-id, item-id, footprint/scope, and the backlog entry's own
   `body`/`cross_ref`/`why_blocked`. Called AT SPINOFF-AUTHORING TIME
   (Phase 2.1, after PM authorization — never before; spinoffs are never
   EM-initiated, `skills/spinoff/SKILL.md` Step 0), never from
   `collect()`, which has no per-item data to offer. Per the ratified
   rendering-ownership decision (ties C4/C2's dueling attribution to a
   single place): this reader renders the full body text into the
   directive's `fields`; `apply.py`'s `spinoff-handoff-template` handler
   is a pure pass-through and does no further assembly.
4. `build_verifier_dispatch` — wraps `verifier.build_haiku_verifier_dispatch`
   with `BUG_BLITZ_VERIFIER_ENUM` and this surface's evidence/output-path
   shape (`coordinator/commands/bug-blitz.md` Phase 3 step 3: DONE
   summary + unstaged diff for the item's files + cited code;
   `state/scratch/bug-blitz/{run-id}/{item-id}.verify.md`). Called AT
   WAVE-VERIFY TIME (Phase 3 step 3), once a DONE summary exists — never
   from `collect()`.

Spec backlink: example-doctrine-repo docs/plans/2026-07-26-backlog-grind-computed-frontage.md,
chunk C3a.

Negative-spec:
    - Does NOT directory-glob `state/bug-backlog/` or raw-parse any queue
      entry's frontmatter itself — every queue read routes through
      `queue_family.load_family_records` (AC6, scoped to this file).
    - Does NOT re-derive `build_judgment_point` / `build_disposition` /
      `build_stage_and_commit` / `build_commit_readiness_gate` locally —
      imported from `directives.py` (C2) and
      `contract.decision_object.judgment`, never redefined here (AC1).
    - Does NOT reshape `readers_mise` / `readers_sweep` / `readers_debt` /
      `readers_dogfood`'s dicts — returns only this reader's own
      `ReaderResult`; concatenation is the seam's (C3) job alone.
    - Does NOT invoke `coordinator-safe-commit`, `git`, or any other
      mutating primitive — `build_commit_per_item`/`build_commit_per_wave`
      return plain dicts; execution is entirely `apply.py`'s (C4).
    - Does NOT ship a bug-blitz commit directive without a `depends_on`
      wire to `COMMIT_READINESS_JP_ID` — the review-gate risk constraint
      this module exists to satisfy has no opt-out on this surface until
      its named removal condition (the linked spinoff handoff) lands.
    - Does NOT gate on the Tier-U full-suite authorization ask
      (`build_tier_u_grant_flow`) — that ask is common cross-surface
      infra (bug-blitz Phase 0.6, bug-sweep Track B) already served by
      the existing `tier-u-grant-cli`; this file's remit is the backlog
      surface specifically, not the shared suite-authorization flow.
    - Does NOT call `build_spinoff_handoff` or `build_verifier_dispatch`
      from `collect()` — both need per-item data (`collect()` is a boot-
      time, cadence-only call with no item in scope) and both are called
      live by the wave-authoring/wave-verify caller, exactly like
      `build_commit_per_item`/`build_commit_per_wave`.
    - Does NOT widen or merge `BUG_BLITZ_VERIFIER_ENUM` with
      `MISE_VERIFIER_ENUM` — `verifier.py`'s own negative-spec forbids
      it; this module passes `BUG_BLITZ_VERIFIER_ENUM` through unchanged.
    - Does NOT invent judgment content (acceptance criteria, "what this
      covers" narrative) for the spinoff-handoff body beyond the fields
      the caller supplies — every section is either the backlog entry's
      own verbatim fields or a mechanical restatement of them (the
      classification reason, the run/item ids). A caller wanting richer
      judgment-authored sections composes them into `body`/`why_blocked`
      before calling; this module never guesses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from coordinator_core.backlog_grind_assemble.directives import (
    build_commit_readiness_gate,
    build_executor_dispatch_prompt_template_emission,
    build_spinoff_handoff_template_emission,
    build_stage_and_commit,
)
from coordinator_core.backlog_grind_assemble.verifier import (
    BUG_BLITZ_VERIFIER_ENUM,
    build_haiku_verifier_dispatch,
)
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ops.queue_family import load_family_records
from coordinator_core.orient_assemble.reader_result import ReaderResult

#: This reader's own cadence identity — one of the five surface-identity
#: cadences `coordinator_core.test_backlog_grind_assemble._CADENCES`
#: enumerates. `collect()` below is a no-op ReaderResult for every other
#: cadence string; the seam (C3) never branches on cadence itself.
_CADENCE = "bug-blitz"

#: The standing commit-readiness judgment-point id every bug-blitz commit
#: directive this surface's runtime builds must `depends_on` — see the
#: module docstring's review-gate risk constraint. Public and documented
#: (no leading underscore) because `apply.py`'s `_build_wave_path_directives`
#: reaches across the module boundary to wire the same gate onto its own
#: CLI-driven `--wave-path` commit path — cross-module use, not an
#: internal-only implementation detail.
#: Review: code-reviewer — F4: was module-private (`_COMMIT_READINESS_JP_ID`)
#: with no `__all__`/docstring export; `apply.py` reached across the module
#: boundary into it anyway, so a future rename here would silently break
#: that caller with no ImportError. Promoted to a public name.
COMMIT_READINESS_JP_ID = "j-bug-blitz-commit-readiness"

#: bug-blitz.md's own fixed template text for the Phase 3 step 1 executor
#: dispatch prompt, ported verbatim as constants rather than re-derived —
#: mirrors `readers_mise.py`'s identically-shaped constants (AC26, item
#: #43; "mise-en-place item #19 is the same shape" per the plan's C2
#: body). Only the parts that are genuinely fixed across every item live
#: here; the item-specific parts Phase 3 step 1 names (severity, file:line,
#: description, recommended fix, footprint) are NOT computed at
#: `collect()` boot time — they are filled in by whoever renders this
#: template at actual per-item dispatch time, exactly as
#: `readers_mise.py` leaves `[item-id]`/`[list]` unfilled.
_DISK_FIRST_VERIFICATION_PREAMBLE = (
    "Reply with `DONE: <path>` ONLY after you have confirmed the file "
    "exists at the path above (use Read or Bash `ls` to verify). If you "
    "find yourself about to summarize the deliverable inline in your "
    "reply, STOP — the coordinator reads from disk, not chat. Inline "
    "summary without a written file counts as task failure."
)
_VERIFICATION_GATE_CONSTRAINT = (
    "Before editing: confirm the described bug pattern is present at the "
    "cited file:line. For a `TF-*` test-failure item flagged "
    "`locus-underdetermined` (no cited file:line), this gate relaxes: "
    "reproduce the failing test first, confirm it fails, then fix to "
    "green — there is no cited line to anchor on."
)
_ASSERTION_WEAKENING_PROHIBITION = (
    "Never 'fix' a red test by weakening its assertion to green without "
    "evidence the assertion itself was wrong — that is the cardinal "
    "failure mode of automated test-chasing. A genuinely wrong assertion "
    "is reported `BLOCKED: assertion-weakening-without-evidence`, not "
    "edited. Required on every `TF-*` test-failure item; fixing the code "
    "under test is the default."
)
_FOOTPRINT_CONSTRAINT_TEMPLATE = (
    "You MUST NOT create or modify any file outside this footprint: "
    "[list]. If you discover you need to, STOP and report back via the "
    "DONE summary with status BLOCKED."
)
_EDIT_AND_REPORT_CONSTRAINT = (
    "Edit and report only — you do not stage or commit under any "
    "circumstance. Leave your changes uncommitted and unstaged; the EM "
    "commits at the wave gate, once per item (per-item granularity), "
    "after this item's own verification passes."
)
_DONE_SUMMARY_CONSTRAINT_TEMPLATE = (
    "Write a one-screen summary to "
    "`state/scratch/bug-blitz/[run-id]/[item-id].done.md` with: status "
    "(DONE | BLOCKED | PARTIAL), the changed-path list (`files`), "
    "`before`/`after` snippets, the verification result you observed, and "
    "any deviations from the recommended fix. Do not include a commit "
    "SHA — your changes are still uncommitted when you write this "
    "summary. Reply EXACTLY `DONE: state/scratch/bug-blitz/[run-id]/"
    "[item-id].done.md` (or `BLOCKED: <path>`)."
)

#: bug-blitz.md Phase 2.1's canonical spinoff frontmatter/body schema,
#: ported verbatim as a rendering template (AC26, item #32) — see
#: `commands/spinoff.md` for the generic `/spinoff` frontmatter shape this
#: mirrors, and the module docstring's `build_spinoff_handoff` section for
#: why `deployment_state: ready_to_fire` and `status: active` are
#: hard-coded here rather than caller-supplied.
_SPINOFF_FRONTMATTER_TEMPLATE = """---
title: {title}
created: {created}
branch: {branch}
status: active
kind: spinoff
predecessor: none
authoring_session: bug-blitz {run_id}
workstream: bug-backlog item {item_id} (state/bug-backlog/{item_id}.yaml)
deployment_state: ready_to_fire
scope:
{scope_yaml}
---"""


def _repo_root() -> Optional[str]:
    """Resolve the calling repo's worktree root via
    `coordinator_core.git.repo_root.show_toplevel` against process cwd —
    the shared cwd-keyed memoized resolution seam. Returns `None` on any
    resolution failure (not a git repo, `git` absent, timeout) rather than
    raising — a boot-time reader degrades to an empty `ReaderResult`, it
    never crashes the seam.
    """
    return show_toplevel()


def _read_backlog_readiness() -> ReaderResult:
    """Bug-backlog presence/count (via `load_family_records`, AC6) folded
    into the standing commit-readiness judgment point's evidence.

    Always emits `j-bug-blitz-commit-readiness` when this reader's cadence
    fires — even on an empty/absent backlog — because a run with zero
    backlog items can still commit `TF-*` test-failure fixes (Phase 0.7)
    that this reader has no visibility into at boot time; the gate is a
    standing precondition for ANY autonomous bug-blitz commit this run,
    not a backlog-count-conditional one. `apply.py` only ever needs the
    gate if a commit directive actually depends on it — an unresolved,
    unreferenced judgment point is inert, not an error.
    """
    repo_root_str = _repo_root()
    if repo_root_str is None:
        backlog_note = "state/bug-backlog/: repo root unresolved (not a git checkout?)"
    else:
        records = load_family_records(
            "bug-backlog", Path(repo_root_str), where="status=open"
        )
        backlog_note = f"state/bug-backlog/ open-item count={len(records)}"

    jp = build_commit_readiness_gate(
        id=COMMIT_READINESS_JP_ID,
        question=(
            "bug-blitz carries no code-review gate of any kind (survey A) — "
            "ready to commit this run's autonomous fixes?"
        ),
        evidence=(
            f"{backlog_note} | reason: bug-blitz's commit-per-item/commit-per-wave "
            "directives depend_on this judgment point per the plan's review-gate "
            "risk constraint, until hnd-bug-blitz-commits-autonomous-f-a7cb8f lands"
        ),
        reason="insufficient-evidence",
        resolves=[],
    )
    return ReaderResult(judgment_points=[jp])


def _read_executor_dispatch_template() -> ReaderResult:
    """Emits AC26's fixed executor dispatch-prompt template as a
    directive (bug-blitz item #43) — mirrors `readers_mise.py`'s
    `_read_executor_dispatch_template` exactly (same call site shape,
    surface-specific constant text). Fires unconditionally whenever this
    reader is asked, independent of backlog-emptiness above: the template
    is dispatch-time utility content, not gated on there being open
    backlog work right now."""
    return ReaderResult(
        directives=[
            build_executor_dispatch_prompt_template_emission(
                id="d-bug-blitz-executor-dispatch-prompt-template",
                fields={
                    "disk_first_verification_preamble": _DISK_FIRST_VERIFICATION_PREAMBLE,
                    "verification_gate_constraint": _VERIFICATION_GATE_CONSTRAINT,
                    "assertion_weakening_prohibition": _ASSERTION_WEAKENING_PROHIBITION,
                    "footprint_constraint_template": _FOOTPRINT_CONSTRAINT_TEMPLATE,
                    "edit_and_report_constraint": _EDIT_AND_REPORT_CONSTRAINT,
                    "done_summary_constraint_template": _DONE_SUMMARY_CONSTRAINT_TEMPLATE,
                },
            )
        ]
    )


def build_commit_per_item(
    *,
    id: str,
    paths: Sequence[str],
    message: str,
    branch: str,
    expected_branch: str,
    backlog_note: Optional[str] = None,
) -> dict[str, Any]:
    """Build one `per-item` bug-blitz commit directive, `depends_on`-wired
    to `COMMIT_READINESS_JP_ID` per this module's review-gate risk
    constraint. Called at wave-commit time (by C7's rebuilt
    `bug-blitz.md` body, or by `apply.py`) once the item's actual touched
    files are known — never called from `collect()`, which runs before
    any wave dispatch and has no fix-target paths to offer.
    """
    return build_stage_and_commit(
        id=id,
        paths=paths,
        message=message,
        granularity="per-item",
        branch=branch,
        expected_branch=expected_branch,
        depends_on=COMMIT_READINESS_JP_ID,
        backlog_note=backlog_note,
    )


def build_commit_per_wave(
    *,
    id: str,
    paths: Sequence[str],
    message: str,
    branch: str,
    expected_branch: str,
) -> dict[str, Any]:
    """Build one `per-wave` bug-blitz commit directive, `depends_on`-wired
    to `COMMIT_READINESS_JP_ID`. Same calling contract as
    `build_commit_per_item` above, for the `per-wave` granularity.
    """
    return build_stage_and_commit(
        id=id,
        paths=paths,
        message=message,
        granularity="per-wave",
        branch=branch,
        expected_branch=expected_branch,
        depends_on=COMMIT_READINESS_JP_ID,
    )


def _render_spinoff_handoff_body(
    *,
    title: str,
    created: str,
    branch: str,
    run_id: str,
    item_id: str,
    classification_reason: str,
    scope: Sequence[str],
    body: str,
    cross_ref: str,
    why_blocked: str,
) -> str:
    """Pure function rendering bug-blitz's ~40-line spinoff-handoff
    authoring template (frontmatter + canonical body-section skeleton +
    trailing marker) from fields a live run already holds. Every section
    is either the backlog entry's own verbatim `body`/`cross_ref`/
    `why_blocked` fields or a mechanical restatement of the ids/reason
    already passed in — see the module docstring's negative-spec on why
    this never invents judgment content beyond that.
    """
    scope_list = list(scope)
    scope_yaml = "\n".join(f"  - {path}" for path in scope_list) if scope_list else "  - []"
    frontmatter = _SPINOFF_FRONTMATTER_TEMPLATE.format(
        title=title,
        created=created,
        branch=branch,
        run_id=run_id,
        item_id=item_id,
        scope_yaml=scope_yaml,
    )
    sections = [
        frontmatter,
        f"# {title}",
        (
            f"This is its own session because bug-backlog item `{item_id}` "
            f"was classified `big` during bug-blitz run `{run_id}`: "
            f"{classification_reason}."
        ),
        "## What this covers",
        (
            f"Fix for bug-backlog item `{item_id}`, oversized for an in-wave "
            f"bug-blitz fix (footprint ≥3 files, or a new module/interface). "
            "See `## Specification` below for the original citation."
        ),
        "## Reference materials (read first)",
        (
            f"- `archive/bug-backlog/` — the closed `{item_id}.yaml` entry "
            "this spinoff was forked from"
            + (f"\n- {cross_ref}" if cross_ref else "")
        ),
        "## Specification",
        (
            f"Original backlog entry (`state/bug-backlog/{item_id}.yaml`, "
            "verbatim):\n\n"
            f"body: {body}\n\n"
            f"cross_ref: {cross_ref}\n\n"
            f"why_blocked: {why_blocked}\n\n"
            f"Classified `big` because: {classification_reason}"
        ),
        "## Acceptance criteria",
        (
            "The bug described above no longer reproduces, and no change "
            "lands outside the `scope` pathspecs declared in this "
            "handoff's frontmatter."
        ),
        "## Recommended next steps",
        (
            "Read the cited file(s) and the backlog entry's recommended "
            "fix above; this item was oversized for an in-wave fix, not "
            "uncertain in approach."
        ),
        "## Anti-scope",
        "Nothing beyond the `scope` pathspecs declared in this handoff's frontmatter.",
        f"<!-- spinoff: {created} by bug-blitz {run_id} -->",
    ]
    return "\n\n".join(sections)


def build_spinoff_handoff(
    *,
    id: str,
    title: str,
    created: str,
    branch: str,
    run_id: str,
    item_id: str,
    classification_reason: str,
    scope: Sequence[str],
    body: str,
    cross_ref: str,
    why_blocked: str,
    depends_on: Optional[str] = None,
) -> dict[str, Any]:
    """Build the `spinoff-handoff-template` directive for one PM-authorized
    `big` bug-backlog item (bug-blitz item #32) — the reader renders the
    full markdown body via `_render_spinoff_handoff_body` and hands it
    through `directives.build_spinoff_handoff_template_emission`'s
    `fields`, per the ratified rendering-ownership decision (see the
    module docstring's item 3). Called AT SPINOFF-AUTHORING TIME (Phase
    2.1), after PM authorization for this specific item — never from
    `collect()`, and never on the EM's own initiative (spinoffs are never
    EM-initiated, `skills/spinoff/SKILL.md` Step 0).

    `depends_on` is not wired to `COMMIT_READINESS_JP_ID` by default — a
    spinoff handoff is not an autonomous commit and is only ever built
    after its own, separate PM-authorization step has already resolved;
    a caller with an additional gate of its own may still pass one.
    """
    content = _render_spinoff_handoff_body(
        title=title,
        created=created,
        branch=branch,
        run_id=run_id,
        item_id=item_id,
        classification_reason=classification_reason,
        scope=scope,
        body=body,
        cross_ref=cross_ref,
        why_blocked=why_blocked,
    )
    return build_spinoff_handoff_template_emission(
        id=id,
        fields={"content": content},
        depends_on=depends_on,
    )


def build_verifier_dispatch(
    *,
    id: str,
    run_id: str,
    item_id: str,
    depends_on: Optional[str] = None,
) -> dict[str, Any]:
    """Build the Phase 3 step 3 Haiku-verifier dispatch directive for one
    bug-blitz item, wrapping `verifier.build_haiku_verifier_dispatch` with
    this surface's fixed evidence shape (DONE summary + the item's
    unstaged diff + cited code) and verdict vocabulary
    (`BUG_BLITZ_VERIFIER_ENUM`) per `commands/bug-blitz.md` Phase 3 step
    3. `output_path` is this reader's own computed string — verifier.py's
    negative-spec forbids hardcoding either call site's path pattern
    there. Called AT WAVE-VERIFY TIME, once a DONE summary exists for
    `item_id` — never from `collect()`.
    """
    return build_haiku_verifier_dispatch(
        id=id,
        evidence_source=["done-summary", "diff:files", "cited-code"],
        enum_set=BUG_BLITZ_VERIFIER_ENUM,
        output_path=f"state/scratch/bug-blitz/{run_id}/{item_id}.verify.md",
        depends_on=depends_on,
    )


def collect(cadence: str, *, run_id: Optional[str] = None) -> ReaderResult:
    """Compute this reader's directives/judgment_points for `cadence`.

    Self-gates internally: a no-op `ReaderResult()` for every cadence
    other than `"bug-blitz"` — the seam (C3) calls every C3a-C3e reader
    unconditionally for every cadence and trusts each to self-gate (mirrors
    `orient_assemble.readers_health_reaper`'s day-cadence-only gating).

    `run_id` names which run of the ASKING surface is asking. This reader
    has no per-run record family to resolve it against, so it accepts the
    parameter and ignores it: the seam threads it to all five readers
    uniformly for every cadence (`__init__.py`'s negative-spec against a
    per-surface branch), and self-gating on it is each reader's own job,
    exactly as self-gating on `cadence` is.
    """
    if cadence != _CADENCE:
        return ReaderResult()

    results = [_read_backlog_readiness(), _read_executor_dispatch_template()]

    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []
    for result in results:
        directives.extend(result.directives)
        judgment_points.extend(result.judgment_points)
    return ReaderResult(directives=directives, judgment_points=judgment_points)
