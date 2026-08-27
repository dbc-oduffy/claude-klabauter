"""
coordinator_core.ops.plan_status_transition — plan-lifecycle status stamp (stamp-implemented).

Purpose: native Python port of DoE-claude coordinator/bin/plan-status-transition.js —
the authorized writer of a plan's ``status:`` frontmatter field when a plan finishes
execution. Deliberately thin, single-verb CLI (mirrors the JS oracle's own framing:
a plan's lifecycle has exactly one mutating transition today, not the multi-verb
machinery handoffs/memos need).

Usage (argv, mirrors the node CLI verbatim):
    plan-status-transition stamp-implemented --plan <path>
    plan-status-transition stamp-implemented --plan <path> --override-reason "<why>"
    plan-status-transition stamp-superseded --plan <path> --by <successor-plan-path>
    plan-status-transition stamp-reopened --plan <path> --reason "<why>"

``--override-reason`` (2026-08-10, cross-repo memo example-retrieval-repo-em-close-out-
stamps-implemented-without-reading-the-ac-table.md): an explicit,
self-documenting override of `stamp-implemented`'s own completeness verdict
(the AC-open-rows advisory `_ac_open_rows_warning` prints) -- the sanctioned
successor to two independently-invented, mutually-divergent frontmatter
spellings (`status_stamped_by`/`status_stamped_reason` in one repo,
`status_stamped_by_hand` in another) a hand-edit introduced when this op had
no supported override path at all. A REQUIRED, non-empty reason string, not
a bare boolean `--force` -- see `_stamp_implemented`'s "Completeness-verdict
override" note for the field shape written (`status_override_by`,
`status_override_reason`, `status_override_at`) and why insertion order is
reversed. Omitting the flag leaves behaviour byte-for-byte unchanged from
before this addition: no new refusal, no new field, nothing silently
loosened. The live-foreign-holder refusal (`_refuse_if_live_foreign_holder`)
and the locked-write/commit-ownership machinery are UNCHANGED and still run
in full on this path -- the override only ever concerns the completeness
verdict, never who may hold the write. Rejected outright on
`stamp-superseded` (see `main()`): that verb asserts no completeness at all,
so there is nothing there for the flag to override.

NOTE (schema declaration, out of this change's scope): `status_override_by`
/ `status_override_reason` / `status_override_at` are written here but NOT
yet declared in the vendored `coordinator_core/frontmatter/schemas/
plan.schema.json` -- that file was outside this change's authorized file
scope (`coordinator_core/ops/plan_status_transition.py` +
`coordinator_core/ops/tests/` only). `plan.schema.json` has no
`additionalProperties: false` at the top level, so these fields validate
today without a schema entry, but they are UNDOCUMENTED there. Flagged for
a follow-up schema-declaration edit, not silently skipped.

``stamp-superseded`` (C2, docs/plans/2026-08-06-<this-plan>.md): the second
authorized writer of a plan's ``status:`` field, added because ``implemented``
is machine-derivable from git evidence while ``superseded`` is not -- it
requires naming WHAT superseded a plan, which is human/EM judgment this op
cannot infer. See ``_stamp_superseded``'s own docstring for the full
contract; it deliberately requires ``--by`` and refuses without it (no
inference from ``folded_into_workstream`` or any other field), is not
reachable from any sweep/cascade/ceremony tail, and is not present in the
(now-deleted) node oracle this module ports from -- see "Divergence from the
node oracle" below.

Divergence from the node oracle (2026-08-06, C2): ``plan-status-transition.js``
no longer exists on disk in DoE-claude (see cross-repo/archive/2026-07-22-
Claude-klabauter-em-plan-status-transition-wired-node-spawn-gone.md -- it was
declared deletable once this module's ``stamp-implemented`` had zero
remaining claude-klabauter callers routed through it). There is therefore no live
oracle to hold byte-parity against for ``stamp-superseded``; this verb is
Claude-klabauter-native by construction, not a port. Flagged for the EM/PM, not
decided silently here.

The ``stamp-implemented`` verb performs an atomic replace-not-append flip of the
plan's ``status:`` field to ``implemented``:
  - status in {draft, reviewed, approved, executing} -> replaced with 'implemented'
  - status in {implemented, superseded, abandoned, deferred} -> NO-OP, exit 0
    (idempotent re-run + respects terminal/deferred states -- does not resurrect
    a plan someone deliberately marked abandoned/deferred/superseded)

Halt-safety note: this CLI does NOT special-case 'executing' -- executing->implemented
DOES flip here. Any halt-mid-execution safety property belongs at the calling seam
(the thing that decides whether it's safe to declare a plan done), not this writer.

Exit codes:
    0 -- transition applied (cascade clean or nothing to cascade) OR
         already-at-target/terminal no-op
    1 -- error (bad args, missing --plan, file not found, missing/unparseable frontmatter)
    2 -- transition applied, but the terminal-state cascade (see below) resolved no
         downstream artifact -- the flip itself succeeded; the cascade needs attention.

Byte-parity with the node oracle (`coordinator/bin/plan-status-transition.js`) held through
2 -- superseded by the docs/plans/2026-08-04-terminal-state-propagation-join-keys.md § C6
Addendum (Q4, PM-ratified 2026-08-04): hanging a cascade off this CLI's `main()` retires
that claim deliberately -- this is now the true single writer both DoE's polyglot
trampoline and claude-klabauter's own ceremony callers meet at, so it owns the third outcome above.

Port source: coordinator/bin/plan-status-transition.js (DoE-claude)
Parity oracle: DoE-claude coordinator/bin/plan-status-transition.js (node, hand-run
    golden-oracle diff during port -- not the coordinator/bin/plan-status-transition.test.js
    file, which was not re-run by this port).

Negative-spec:
    - Does NOT do a full YAML parse -- uses the shared text-based frontmatter
      primitives (split_frontmatter/read_fm_field/replace_fm_field/rebuild), the
      same module handoff_transition.py and memo_transition.py already share --
      byte-identical rebuild behavior with the node oracle's own splitFrontmatter/
      readFmField/replaceFmField (bin/lib/schema.js), not a re-derivation.
    - No @register_op / no registry wiring -- this is a plain module with a
      main(argv) CLI entry point, called by the DoE-side polyglot trampoline via
      direct in-process import (template-variant #1: safe-leaf early-win port),
      NOT a JSON-RPC op. See docs/wiki (DoE-claude) port template for the
      direct-import-vs-registered-op discriminator.
    - RETIRED (2026-08-04, C6 Addendum Q4, PM-ratified): "no locking, single-writer,
      not a concurrent-mutation hot path" no longer holds now that a successful
      non-no-op flip fires the terminal-state cascade (deliverable.cascade_terminal)
      off this module's own main() -- both DoE's polyglot trampoline and claude-klabauter's
      ceremony callers meet here, so the plan's own flip write now routes through
      coordinator_core.locked_write.locked_rmw exactly like every other lifecycle
      frontmatter mutator in this repo (handoff_transition.py's _claim/_unclaim/_ship,
      etc.) -- see _stamp_implemented.

Commit ownership (2026-08-05): a real (non-no-op) flip is committed by THIS op,
scoped to exactly the plan path it just wrote, immediately after the write lands
(``_commit_plan_flip``, called from ``_stamp_implemented``). Follows the
writer-commits shape ``coordinator_core.ops.ceremony.consumed_handoff_stamp.
post_commit_stamp_and_ship`` already proves: that op's docstring is explicit
that the write and its commit land in ONE explicit follow-up commit "so the
op never exits with the stamp left as an unswept dirty working-tree edit" --
this op mirrors that, not a second invented shape. Only fires on the
fallback-plain-write branch's/``locked_rmw`` branch's real flip
(``_state["flipped"]``) AND when a git repo was resolved for the plan path
(``git_common_dir``/``worktree_root`` both non-None) -- the dry-run
scratch-copy branch (plan path outside any git worktree) never reaches a
commit attempt, by the same reasoning `_run_cascade` already documents for
that branch.

No-worktree-read commit (2026-08-06, C3, DR-272 Defect 1): commits now route
through ``coordinator_core.ops.ceremony.git_native.commit_authored_content``
(C2), not ``commit_scoped``. This op is DEFINITELY-MULTI-WRITER
(``completion_ops.py``'s ``completion.reconcile_commits``/``plan.append_session``
write the same plan files with no lock at all), so ``commit_scoped``'s AGREE
branch -- which stages and commits whatever is currently on the worktree path
-- could silently absorb a foreign edit that lands on the plan path between
this op's lock release and its commit call. ``commit_authored_content`` never
reads the worktree on ``path``; it only ever commits the exact ``content``
bytes handed to it -- ``locked_rmw``'s own return value on the fresh-write
branch (``written_text``), or the lock-held, re-validated on-disk text on the
resume branch (see ``_read_and_validate_resume_content`` -- THE RESUME BRANCH
HAS NO AUTHORED BYTES: those bytes were authored by a previous invocation,
never this one, so they are re-validated against this op's own terminal
state, ``status: implemented``, before being committed under this op's name).

Stranded-flip resume (2026-08-05, code-reviewer Finding 1, c6169575 review):
a commit-failure on a real flip is NOT the end of the story -- the write
already landed on disk before the commit was attempted, so a failed commit
leaves `status: implemented` stranded, uncommitted. Rather than gate solely
on "did THIS invocation flip the file" (which would make the very next
invocation read the now-frozen status and silently no-op forever, never
recovering the stranded write), the terminal/no-op branch also checks
whether the plan path is currently dirty in git (`_plan_path_dirty`) and,
if so, attempts the commit anyway -- reporting it distinctly ("committed the
stranded write now") from a genuine terminal no-op so a caller/ceremony can
tell the two apart.

DR-211 (`docs/decisions/DR-211-fleet-op-substrate-write-boundary.md`) D1 is
the standing category precedent this extends -- writer-side commit
ownership of coordinator substrate -- NOT a DR that already covers this op:
DR-211 ratifies `fleet.*` specifically (D1's "Ops sanctioned" list) and does
not itself admit `plan_status_transition`. The commit mechanism used here
(`git_native.commit_scoped`, the same D3-descended computed-selector every
other writer-commits site in this repo now uses) already satisfies D3's
FORWARD/FORWARD-B/REVERSE analysis without a bespoke re-derivation.

Byte-parity retirement note (2026-08-04, PM-ratified, see the Deviation
paragraph above): the repo-wide byte-parity obligation to the node oracle's
`splitFrontmatter`/`readFmField`/`replaceFmField` primitives and this CLI's
own stdout lines is what still survives -- a git commit of the already-
written plan file perturbs neither. The commit-failure diagnostic added
below is a NEW stderr line reached only on a git-commit failure; it does not
alter any existing stdout line the node-oracle byte-parity obligation, while
it held, ever covered.

``stamp-reopened`` (2026-08-16, PM-authorized directly, no plan file — see
this op's own git history for the dispatch that added it): the THIRD
authorized writer of a plan's ``status:`` field, and the only one that moves
a plan OUT of a frozen status. It exists to repair a since-fixed defect: a
now-closed gate could stamp a plan ``status: implemented`` (a member of
``_FROZEN_STATUSES``, terminal-by-convention) while its own plan-tasks spine
still carried row(s) at ``disposition: open`` — a corpus scan found 18 such
plans, and because ``implemented`` is frozen, nothing could ever move them
off that false-clean status again. The defect that produced this is fixed
(no NEW plan can be mis-stamped this way), but the engine had no way to
LEAVE the state by design; this verb is the exit, not a new way in.

Root cause the freeze rationale itself misses: ``_FROZEN_STATUSES`` exists
to protect HUMAN-DECLARED judgments (``abandoned``, ``deferred``,
``superseded`` — see ``_stamp_superseded``'s own docstring) from being
silently overwritten by this op's own machine-derived flip logic.
``implemented`` is the one member of that set THIS OP WRITES ITSELF — a rule
authored to guard a human's word is, for that one member, guarding this
op's own prior machine-derived output instead. ``stamp-reopened`` is scoped
to repairing exactly that one member, and only when the spine itself
disagrees with it (see the precondition-gate below) — it does not touch
``abandoned``/``deferred``/``superseded`` at all.

Negative-spec (the scope limits are the load-bearing half of this verb):
  - Moves ``implemented -> landed`` and NOTHING ELSE. There is no generic
    ``--to <status>`` flag and none will be added here — ``plan.schema.json``
    already defines ``landed`` as "every chunk's code is on the branch, but
    not every spine row has reached a disposition yet... NOT terminal",
    which is exactly the true state a false-``implemented`` plan with open
    spine rows is actually in; a plan reopened to ``landed`` re-stamps to
    ``implemented`` again by the ORDINARY ``stamp-implemented`` path, once
    (and only once) its spine rows resolve. Widening this verb's target set
    (``implemented -> draft``/``executing``, or any other pair) is
    reintroducing the false-clean this verb exists to repair — a status this
    op can move ANYWHERE is no longer evidence of anything.
  - Never reachable from a sweep, cascade, or ceremony tail — mirrors
    ``stamp-superseded``'s identical negative-spec below verbatim. No
    ``@register_op``, no registry entry, no caller anywhere in this module
    or its siblings invokes it programmatically. One plan at a time, with a
    stated reason, typed by a human/EM — never looped over the corpus by an
    automated sweep, however well-intentioned; a sweep that reopens 18
    plans unattended is the same failure mode as the gate that closed them
    wrongly in the first place, just pointed the other way.
  - Precondition-gated on a COMPUTED verdict
    (``directives_spine_worklist.compute_open_spine_row_gate``), never on
    the caller's bare assertion that a plan is wrongly stamped — see
    ``_stamp_reopened``'s own docstring for why a second, bespoke oracle is
    not written here.
  - Does NOT walk back the original cascade (see "Reverse cascade" below) —
    only reports the inconsistency it leaves behind.

Reverse cascade — report, never auto-reverse (2026-08-16): the ORIGINAL
false flip fired ``_run_cascade``, which may have advanced downstream
``state/handoffs/*.md``/sizing artifacts believing the plan's work was
fully done. Reopening the plan does not, and must not, walk any of that
back — precedent (commit ``2c5c7f907``) already ruled that artifacts
describing work that genuinely landed stay as they are; a handoff/sizing
that advanced is not FALSE just because its governing plan's own status
was. ``_stamp_reopened`` detects any live governing handoff (reusing
``_find_governing_handoff_paths``, the same reverse-join
``_refuse_if_live_foreign_holder`` already uses — not a second lookup) and
prints it as an advisory inconsistency note; it never mutates the handoff.

Path containment (2026-08-06, C2a, docs/plans/2026-08-06-writer-side-commit-
ownership-lock-gap.md): until this addition, this op had NO containment
guard of any kind -- it flipped `status:` and committed ANY path the caller
named, provided the file carried parseable frontmatter, unlike its sibling
ops `queue_close` (`contained_path`) and `memo_transition`
(`_containment_check`). This op is about to be handed a strictly more
powerful commit primitive (C2's no-worktree-read entrypoint) than the
`commit_scoped` it uses today -- a guardless op plus a more powerful commit
primitive is a materially different exposure, which is why C2a promotes
this from a previously-deferred item into the active spine ahead of C3.
`_stamp_implemented` now checks `--plan` against `coordinator_core.ops.
_path_guard.contained_path`, mirroring `queue_close`'s call site verbatim
(``contained_path(candidate, [allowed_root])``), with the resolved git
worktree root itself as the sole allowed root -- unlike `queue_close`
(one fixed well-known directory, `state/improvement-queue/`), a plan
document has no single conventional subdirectory this op can assume
(existing callers/tests exercise `--plan` pointed at arbitrary paths
inside the repo, not only `docs/plans/`), so the guard's containment
domain is the whole worktree: still closes off the caller-supplied-path
blast-radius this op's own family already guards against (a fat-fingered
absolute path landing a status flip + commit somewhere entirely outside
the repo), without narrowing what a legitimate `--plan` value may already
be. Only enforced when a git worktree was actually resolved for `--plan`:
the pre-existing no-repo fallback branch (a dry-run scratch copy
deliberately materialized outside any git worktree, see the "No
resolvable git repo root" comment below) has no worktree root to check
against, and is untouched.
"""

from __future__ import annotations

MUTATES = ["docs/plans/*.md"]  # flips any caller-supplied --plan file's status: frontmatter field (data-dependent target)

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    split_frontmatter,
    unquote_yaml_scalar,
)
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.ceremony import git_native
from coordinator_core.wire_paths import rel_id

_PROG = "plan-status-transition"

# YAML comment start: a `#` preceded by whitespace (space or tab). A `#` not
# preceded by whitespace is a literal character, not a comment start -- this
# lookbehind mirrors that rule exactly (single line value, so no MULTILINE
# needed -- read_fm_field already isolates one line's worth of value text).
_TRAILING_COMMENT_RE = re.compile(r'(?<=[ \t])#.*$')


def _strip_unquoted_trailing_comment(raw: Optional[str]) -> Optional[str]:
    """Strip a YAML-legal trailing inline comment from an UNQUOTED scalar.

    ``read_fm_field`` returns the raw on-disk text after ``key:``, so a line
    like ``status: executing  # authorized 2026-07-21`` yields the raw value
    ``"executing  # authorized 2026-07-21"`` -- YAML itself parses this as
    just ``executing`` (a ``#`` preceded by whitespace starts a comment that
    runs to end-of-line), but nothing upstream of this function strips it,
    so the value silently misses both ``_FROZEN_STATUSES`` and
    ``_FLIPPABLE_STATUSES`` and the CLI fail-louds on an otherwise-valid plan.

    Only strips when the value is UNQUOTED -- a quoted scalar
    (``status: "draft"`` / ``status: 'draft'``) has its quotes protecting
    everything inside, including any literal ``#``; this function leaves
    quoted values untouched and defers entirely to ``unquote_yaml_scalar``
    for those (order of operations: comment-strip first, unquote second, and
    this function is a no-op on anything that looks quoted so the two never
    fight over the same text).

    Negative-spec: does not STRIP a comment trailing a *closing* quote (e.g.
    ``"draft"  # note``) -- full quote-tracking (mirroring
    ``schema.js:stripInlineComment`` in DoE-claude) is out of scope for this
    bounded, module-local fix. Left untouched here (returned verbatim, comment
    included), that shape is detected and fail-louded with a clear diagnostic
    by the caller (``_stamp_implemented``) rather than surfaced as a garbled
    "unexpected current status" -- see the caller's quoted-scalar-plus-
    trailing-comment check, immediately after this function is called.
    """
    if raw is None:
        return raw
    if raw.startswith("'") or raw.startswith('"'):
        return raw
    return _TRAILING_COMMENT_RE.sub('', raw).rstrip()


# Question answered (C8b rename, 2026-07-27): "is this status frozen against
# the stamp-implemented flip?" (flippability) -- i.e. does `stamp-implemented`
# treat the plan's current status as a no-op rather than mutating it. This is
# NOT the same partition as the other "terminal"-named sets elsewhere in the
# codebase, and none of them are expected to agree:
#   - lifecycle_constants.PLAN_ARCHIVABLE_STATUS (alias: PLAN_TERMINAL_STATUS)
#     answers "can this plan's file be git-mv'd into archive/?" (archivability)
#     -- 'deferred' is deliberately excluded there but included here.
#   - ops.records_query.liveness()'s plan branch answers "what LIVE/BLOCKED/
#     DONE cockpit bucket does this status fall into?" -- 'deferred' maps to
#     BLOCKED there, not into a single terminal/non-terminal bucket.
# Three defensible, independent answers to three different questions about a
# plan's status, not a bug. Plan: 2026-07-27-plan-line-item-resolution-model.md § C8b.
_FROZEN_STATUSES = frozenset({"implemented", "superseded", "abandoned", "deferred"})
# Ordered list (not just a set) -- the error message below reproduces this exact
# insertion order verbatim, matching the node oracle's `[...FLIPPABLE_STATUSES]`
# (a JS Set iterates in insertion order; Python frozenset does not).
# 'landed' (C8a, plan-line-item-resolution-model, D9) sits between 'executing' and
# 'implemented' in the schema enum -- it is NOT terminal (spine rows may still be
# open) and IS flippable to 'implemented' once resolution completes, so it is
# placed in that same slot here.
_FLIPPABLE_STATUSES_ORDER = ["draft", "reviewed", "approved", "executing", "landed"]
_FLIPPABLE_STATUSES = frozenset(_FLIPPABLE_STATUSES_ORDER)


class _Opts:
    __slots__ = ("verb", "plan", "by", "override_reason", "reason", "findings")

    def __init__(
        self,
        verb: Optional[str],
        plan: Optional[str],
        by: Optional[str] = None,
        override_reason: Optional[str] = None,
        reason: Optional[str] = None,
        findings: Optional[str] = None,
    ) -> None:
        self.verb = verb
        self.plan = plan
        self.by = by
        self.override_reason = override_reason
        self.reason = reason
        self.findings = findings


class _CliError(Exception):
    """Raised for any fail-loud CLI condition; carries the message to print on stderr."""


def _parse_args(argv: List[str]) -> _Opts:
    """Parse argv (post-program-name, i.e. sys.argv[1:] equivalent).

    Mirrors the node oracle's parseArgs(process.argv) contract, but takes argv
    already stripped of the interpreter/script-path pair (this module's main()
    receives argv the same way handoff_gate_aging.main() does -- args only).

    ``--by`` (C2, ``stamp-superseded`` only) is parsed generically here rather
    than gated on ``verb`` -- ``stamp-implemented`` simply never receives it,
    and rejecting an out-of-verb flag is `main()`'s job, not the parser's,
    mirroring how `--plan` itself is unconditionally recognised regardless
    of verb.

    ``--override-reason`` (2026-08-10, cross-repo memo
    2026-08-10-example-retrieval-repo-em-close-out-stamps-implemented-without-reading-
    the-ac-table.md) is parsed the same way, generically, for the same
    reason: gating an out-of-verb flag belongs to `main()`, not the parser
    (see the rejection there for `stamp-superseded`).

    ``--reason`` (2026-08-16, `stamp-reopened` only) is parsed the same
    generic way, for the same reason -- `stamp-reopened` requires it
    (see `_stamp_reopened`), and `main()` rejects it out-of-verb, mirroring
    `--by`/`--override-reason` above.

    ``--findings`` (C6a, `stamp-review-verified` only, docs/plans/2026-08-20-
    the-rungs-get-writers.md) is parsed the same generic way, for the same
    reason -- `stamp-review-verified` requires it (see
    `_stamp_review_verified`; a review-trail PATH, not a bare count), and
    `main()` rejects it out-of-verb, mirroring `--by`/`--override-reason`/
    `--reason` above.
    """
    verb = argv[0] if argv else None
    opts = _Opts(verb=verb, plan=None)
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--plan":
            if i + 1 >= len(argv):
                raise _CliError(f"flag requires a value: {a}")
            i += 1
            opts.plan = argv[i]
        elif a == "--by":
            if i + 1 >= len(argv):
                raise _CliError(f"flag requires a value: {a}")
            i += 1
            opts.by = argv[i]
        elif a == "--override-reason":
            if i + 1 >= len(argv):
                raise _CliError(f"flag requires a value: {a}")
            i += 1
            opts.override_reason = argv[i]
        elif a == "--reason":
            if i + 1 >= len(argv):
                raise _CliError(f"flag requires a value: {a}")
            i += 1
            opts.reason = argv[i]
        elif a == "--findings":
            if i + 1 >= len(argv):
                raise _CliError(f"flag requires a value: {a}")
            i += 1
            opts.findings = argv[i]
        else:
            raise _CliError(f"unknown argument: {a}")
        i += 1
    return opts


#: Closed two-kind registry (AC1). The handler's own kind registry
#: (`deliverable_cascade._KIND_REGISTRY`) is closed at exactly these two names
#: and `_kind_descriptor` raises fail-loud on anything else -- enumerated here
#: rather than introduced as a plugin seam, per the plan's anti-scope.
_CASCADE_TARGET_KINDS = ("handoff", "sizing")


def _run_cascade(plan_path: str, deliverable_id: Optional[str]) -> int:
    """Fire the shared terminal-state cascade (deliverable.cascade_terminal, C6) once
    per target kind in `_CASCADE_TARGET_KINDS`, after a successful NON-no-op flip to
    status:implemented.

    Guarded on "a flip actually occurred" by construction: the only caller is the
    branch of _stamp_implemented that just wrote a real status change. The idempotent
    no-op path (status already frozen/terminal) never reaches this function, so a
    repeat `stamp-implemented` invocation on an already-implemented plan never
    re-cascades (AC6i) -- unchanged by targeting two kinds, since both runs still
    happen only from that same single non-no-op call site.

    Returns this leg's contribution to the CLI's own exit code (see module docstring):
    0 when the cascade is clean, there was nothing to cascade (no deliverable_id, or
    repo-root unresolvable), or AT LEAST ONE kind advanced something; 2 only when
    EVERY targeted kind ran and resolved no downstream artifact (AC2) -- the flip
    itself still succeeded, this is a "needs attention" signal, not a reversal of
    the flip.
    """
    deliverable_id = (deliverable_id or "").strip()
    if not deliverable_id:
        print(
            f"{_PROG}: {plan_path} carries no deliverable_id — nothing to cascade "
            "(join key absent; never falls back to the plan: handoff pointer)",
            file=sys.stderr,
        )
        return 0

    import asyncio

    # Function-local imports: archive_stamp.py imports this module at module level
    # (cs_stamp_plan_implemented), so a module-level import here would cycle.
    from coordinator_core.archive_stamp import _resolve_repo_root_for
    from coordinator_core.ops.deliverable_cascade import _handler as _cascade_handler

    # `_resolve_repo_root_for` returns (worktree_root, git_common_dir) -- the SECOND
    # element, bound here, is the git common dir (<worktree>/.git), never the
    # worktree root itself. `deliverable.cascade_terminal`'s own `repo_root` param
    # expects exactly this (its `main_worktree_root(repo_root)` call takes `.parent`,
    # which is only correct when the value passed in IS the common dir -- see that
    # module's own precondition note). Do not rename this back to `repo_root`: that
    # name previously caused a real false-negative (an actual repo root passed where
    # a common dir was expected silently scanned `<parent>/state/handoffs`, found
    # zero candidates, and reported a clean no-op).
    _, git_common_dir = _resolve_repo_root_for(Path(plan_path))
    if git_common_dir is None:
        print(
            f"{_PROG}: could not resolve git repo root for {plan_path} — cascade skipped",
            file=sys.stderr,
        )
        return 0

    # Per-kind results are collected before any "nothing advanced" narration is
    # emitted (AC4): if ANY kind advances, the flip is a clean success and the
    # OTHER kind's empty-corpus result is suppressed entirely -- printing it
    # unconditionally would inject a new stderr line into the handoff-only path
    # that no test, and no prior behaviour, ever produced.
    results: List["tuple[str, dict]"] = []
    any_advanced = False
    for target_kind in _CASCADE_TARGET_KINDS:
        result = asyncio.run(
            _cascade_handler(
                {
                    "deliverable_id": deliverable_id,
                    "source_kind": "plan",
                    "source_path": plan_path,
                    "target_kind": target_kind,
                },
                repo_root=git_common_dir,
            )
        )
        results.append((target_kind, result))
        advanced = result.get("advanced", [])
        if advanced:
            any_advanced = True
        for artifact in advanced:
            print(
                f"{_PROG}: cascade advanced {artifact.get('path') or artifact.get('handoff_path')}",
                file=sys.stderr,
            )
        for artifact in result.get("refused", []):
            print(
                f"{_PROG}: cascade refused "
                f"{artifact.get('path') or artifact.get('handoff_path')}: {artifact.get('reason')}",
                file=sys.stderr,
            )
        # Review: coordinator:code-reviewer -- `commit_error` (AC8) landed in
        # this result dict but was never read here; a commit failure with a
        # non-empty `advanced` list returned 0 in silence. Surface it without
        # touching exit_code, which stays keyed off `advanced` alone.
        commit_error = result.get("commit_error")
        if commit_error:
            print(f"{_PROG}: cascade commit failed: {commit_error}", file=sys.stderr)

    if any_advanced:
        return 0

    for target_kind, result in results:
        if result.get("exit_code") != 0:
            print(
                f"{_PROG}: cascade resolved no downstream artifact for {plan_path}: "
                f"{result.get('error')}",
                file=sys.stderr,
            )
    return 2


def _relpath_for_commit(plan_path: Path, worktree_root: Path) -> "tuple[Optional[str], Optional[str]]":
    """Resolve the plan path's repo-relative id, without ever letting a raw
    ``ValueError`` escape into ``_stamp_implemented`` after the write has
    already landed (code-reviewer Finding 3, c6169575 review).

    ``rel_id`` raises ``ValueError`` when ``plan_path`` is not under
    ``worktree_root`` per ``Path.relative_to`` -- a live possibility when
    ``Path.resolve()`` and git's own ``rev-parse --show-toplevel`` disagree
    on symlink/mount-alias canonicalization (e.g. macOS ``/tmp`` vs
    ``/private/tmp``). Left unhandled, that raises AFTER the status flip is
    already on disk, bypassing the clean "flip succeeded, commit failed"
    stderr contract the rest of this module establishes and producing a raw
    traceback instead.

    Returns ``(relpath, None)`` on success or ``(None, error_message)`` on
    failure -- callers convert the latter into the same
    "succeeded but committing it failed"-shaped stderr line the
    ``commit_result.ok`` check already handles, never a bare traceback.
    """
    try:
        return rel_id(plan_path.resolve(), worktree_root.resolve()), None
    except ValueError as exc:
        return None, (
            f"could not resolve {plan_path} as repo-relative to {worktree_root}: {exc}"
        )


def _plan_path_dirty(worktree_root: Path, relpath: str) -> bool:
    """Return True iff ``relpath`` carries any uncommitted, PREVIOUSLY-TRACKED
    change (staged and/or unstaged) per ``git status --porcelain`` -- the
    stranded-flip detector for Finding 1 (code-reviewer, c6169575 review).

    Deliberately excludes a bare ``??`` (untracked) entry: a plan file that
    was never touched by this CLI and has simply never been committed at
    all (e.g. a fixture seeded directly with ``status: implemented``) must
    still take the genuine terminal no-op branch, not be mistaken for a
    stranded write this CLI itself left behind. A REAL stranded flip from a
    prior run always shows as a MODIFIED entry (staged and/or unstaged),
    never a fresh ``??``, because ``_commit_plan_flip``'s own
    ``commit_scoped`` call stages the path (via its AGREE branch's
    ``git add``) before a commit can fail on it.
    """
    result = git_native.status_porcelain(worktree_root)
    if not result.ok:
        return False
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        code, entry = line[:2], line[3:]
        if code == "??":
            continue
        if entry == relpath or entry.endswith(f" -> {relpath}"):
            return True
    return False


def _head_resolves(worktree_root: Path) -> bool:
    """Return True iff ``worktree_root``'s git repo has a resolvable ``HEAD``
    (i.e. at least one commit exists).

    ``commit_authored_content`` unconditionally requires ``HEAD`` to resolve
    (it runs ``git rev-parse HEAD`` as its first step) -- a brand-new repo
    with zero commits fails that call with a raw git diagnostic
    (``fatal: ambiguous argument 'HEAD'...``). This probe lets the caller
    detect that BEFORE ever attempting the commit, per the "detect before
    flipping the commit path, not after" requirement (2026-08-06, commit-
    authored-content reroute) -- so an operator sees a clear, module-styled
    diagnostic instead of a leaked raw git error.
    """
    return git_native.rev_parse_head(worktree_root).ok


def _plan_tracked_in_head(worktree_root: Path, relpath: str) -> bool:
    """Return True iff ``relpath`` exists in ``HEAD``'s tree.

    ``commit_authored_content`` deliberately refuses a path absent from
    ``HEAD`` (DR-272 § 3.3: it is built for in-place mutation of an
    existing reserved-noun file, not for first-committing a new one -- see
    that function's own docstring, and this module's docstring "No-
    worktree-read commit" section). This probe mirrors that refusal ahead
    of time so ``_stamp_implemented`` can skip the commit attempt cleanly
    rather than surface ``commit_authored_content``'s own raw refusal
    message as if it were a genuine commit failure.
    """
    result = git_native._git(["ls-tree", "HEAD", "--", relpath], cwd=worktree_root)
    return result.ok and bool(result.stdout.strip())


def _commit_plan_flip(
    worktree_root: Path,
    relpath: str,
    message: str,
    content: str,
    deliverable_id: Optional[str],
) -> git_native.GitResult:
    """Commit the plan's own status flip, scoped to exactly the plan path
    just written -- the writer-side commit-ownership shape this module's
    docstring "Commit ownership" section documents.

    Uses ``git_native.commit_authored_content`` (C2, DR-272) rather than
    ``commit_scoped`` (2026-08-06, C3, Defect 1): ``content`` is always
    caller-supplied bytes -- either the text ``locked_rmw`` itself returned
    on the fresh-write branch, or the lock-held, re-validated on-disk text
    on the resume branch (see ``_read_and_validate_resume_content``) --
    never a re-read of the worktree at commit time. This closes the
    DEFINITELY-MULTI-WRITER vector `commit_scoped`'s AGREE branch left open
    (``completion_ops.py`` writes the same plan files with no lock at all):
    a foreign unstaged/staged edit landing on this exact path between the
    lock release and the commit call can no longer be silently absorbed
    into this op's own commit, because this entrypoint never reads the
    worktree on ``path`` at all -- it only ever commits the bytes it was
    handed.

    ``relpath``, ``message``, ``content`` and ``deliverable_id`` are all
    precomputed by the caller -- this function no longer resolves the
    repo-relative id itself, so a symlink/mount-alias ``ValueError`` is
    handled once, uniformly, at the call site rather than inside this
    helper. ``deliverable_id`` is passed straight through to
    ``commit_authored_content`` so its trailer tier-0 resolution reads it
    from this op's own already-parsed frontmatter state rather than
    re-opening the committed file itself (DR-272 bound 2).

    Returns the raw ``GitResult`` -- the caller decides how a failure maps
    to this CLI's own exit code (see _stamp_implemented).

    Attribution (state/bug-backlog/2026-08-18-scoped-git-commit-stamps-a-
    foreign-session-id-8d21f0c4e7b9.yaml): no caller of this function has a
    trusted, caller-resolved session identity to hand it -- ``_stamp_
    implemented``'s own ``--by`` flag is rejected outright by ``main()``
    before ``_stamp_implemented`` ever runs (an unauthenticated session-id
    override would let a caller disarm ``_refuse_if_live_foreign_holder``'s
    own-session check), and the other verbs' ``--by`` flags mean something
    else entirely (``_stamp_superseded``'s is a PLAN PATH, not a session
    id) or have no override at all (``_stamp_reopened``). This function
    therefore never passes ``attributed_session_id`` to
    ``commit_authored_content`` and falls through to that function's own
    blind env-var read, same as every other caller of it.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(message)
        msg_path = fh.name
    try:
        return git_native.commit_authored_content(
            relpath, content, msg_path, worktree_root, deliverable_id=deliverable_id,
        )
    finally:
        try:
            Path(msg_path).unlink()
        except OSError:
            pass


def _read_and_validate_resume_content(
    plan_path: Path, git_common_dir: Path
) -> "tuple[Optional[str], Optional[str]]":
    """Re-read ``plan_path`` inside a lock-held read and confirm it still
    carries this op's own authored terminal state (``status: implemented``)
    before a resume commit is allowed to fire.

    THE RESUME BRANCH HAS NO AUTHORED BYTES (2026-08-06, C3, Defect 1): on
    a resume, ``locked_rmw`` either did not run this invocation or returned
    ``old_text`` unwritten -- the bytes that would be committed exist only
    on disk, authored by a PREVIOUS process, not by this invocation.
    Committing them unvalidated would re-open exactly the defect this
    module's Commit ownership section closes for the fresh-write branch.
    So the resume branch re-opens the lock, re-reads, and re-validates the
    frontmatter's ``status`` field is EXACTLY ``implemented`` -- the one
    terminal state this op's own ``stamp-implemented`` verb could itself
    have authored -- before treating the on-disk bytes as safe to commit
    under this op's own name.

    Deliberately narrower than ``_FROZEN_STATUSES``: a plan sitting at
    ``abandoned``/``superseded``/``deferred`` and independently dirty in
    git (for reasons unrelated to this CLI) is frozen, but it is NOT a
    state this op's own ``stamp-implemented`` verb could have produced --
    committing it under this op's commit message would launder unrelated
    dirty content as if it were this op's own stranded write. Only
    ``implemented`` is accepted (AC10).

    Uses a no-write ``locked_rmw`` mutate closure (returns ``old_text``
    unchanged) so the read is lock-held and race-free against a concurrent
    writer on the same path, without performing a second write.

    Returns ``(content, None)`` on success or ``(None, error_message)`` on
    a failed validation -- the caller converts the latter into a fail-loud
    stderr line and a non-zero exit (AC10), never a silent commit of
    unauthenticated content.
    """
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    captured: dict = {}

    def _validate(old_text: str) -> str:
        text = old_text.replace("\r\n", "\n")
        split = split_frontmatter(text)
        if split is None:
            raise MutateAbort(
                f"{_PROG}: resume commit aborted -- no parseable YAML frontmatter in "
                f"{plan_path} (expected status: implemented)"
            )
        status = read_fm_field(split.fm_text, "status")
        status = _strip_unquoted_trailing_comment(status)
        status = unquote_yaml_scalar(status) if status is not None else status
        if status != "implemented":
            raise MutateAbort(
                f"{_PROG}: resume commit aborted -- {plan_path} no longer carries "
                f"status: implemented on disk (found {status!r}); refusing to commit "
                "unauthenticated content"
            )
        captured["text"] = text
        return old_text  # validation-only read; never writes

    try:
        locked_rmw(plan_path, _validate, repo_root=git_common_dir)
    except FileNotFoundError:
        return None, f"{_PROG}: resume commit aborted -- plan not found: {plan_path}"
    except LockTimeout as exc:
        return None, (
            f"{_PROG}: resume commit aborted -- timed out waiting for file lock on "
            f"{plan_path}: {exc}"
        )
    except MutateAbort as exc:
        return None, (exc.args[0] if exc.args else f"{_PROG}: resume validation aborted")
    return captured.get("text"), None


def _find_governing_handoff_paths(root: Path, plan_path: Path, plan_deliverable_id: Optional[str]) -> List[Path]:
    """Reverse join: given a plan (identified by its own `deliverable_id`
    frontmatter and its path), find every `state/handoffs/*.md` baton that
    governs it — either by carrying a matching `deliverable_id`, or by an
    explicit `governing_plan:` field naming this plan path.

    This is the REVERSE of `workstream_complete._resolve_session_handoff_
    plan_by_deliverable_id` (handoff -> plan); that function is not reused
    directly (it walks `docs/plans/*.md` keyed off ONE handoff's
    `deliverable_id`, the opposite direction), but shares its
    frontmatter-primitive idiom rather than re-deriving it.

    Never raises: an unreadable/non-UTF-8 handoff is skipped, not fatal to
    the scan — mirrors the forward join's own resilience. Only scans the
    live `state/handoffs/` directory (never `state/handoffs/.archive/` or
    `archive/handoffs/`) — an archived handoff is not a live claim holder by
    construction.
    """
    handoffs_dir = root / "state" / "handoffs"
    if not handoffs_dir.is_dir():
        return []

    canonical_plan_id = plan_deliverable_id if plan_deliverable_id else None

    try:
        plan_resolved = plan_path.resolve()
    except OSError:
        plan_resolved = plan_path

    matches: List[Path] = []
    for handoff_path in sorted(handoffs_dir.glob("*.md")):
        try:
            handoff_text = handoff_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        split = split_frontmatter(handoff_text)
        if split is None:
            continue

        governing_plan_field = read_fm_field_unquoted(split.fm_text, "governing_plan")
        if governing_plan_field:
            candidate = Path(governing_plan_field)
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                if candidate.resolve() == plan_resolved:
                    matches.append(handoff_path)
                    continue
            except OSError:
                pass

        if canonical_plan_id is not None:
            handoff_deliverable_id = read_fm_field_unquoted(split.fm_text, "deliverable_id")
            if handoff_deliverable_id and handoff_deliverable_id == canonical_plan_id:
                matches.append(handoff_path)

    return matches


def _refuse_if_live_foreign_holder(plan_path: Path, worktree_root: Path, closing_sid: Optional[str]) -> Optional[str]:
    """Refuse a `stamp-implemented` write when a DIFFERENT, LIVE session
    still holds this plan's governing handoff with in-flight
    `deployment_state` — defence-in-depth for the incident this closes: a
    session-shape misdetection resolved a LIVE PEER's plan as the closing
    session's own governing plan and stamped it `implemented` (cross-repo
    memo `2026-08-10-example-retrieval-repo-em-wsc-misdetection-wrote-to-a-live-peers-
    plan.md`).

    Returns a human-readable refusal reason (non-None -> caller must fail
    loud, no write, no cascade) or `None` when the write may proceed.

    Negative-spec — WHY this is keyed on LIVENESS, not provenance equality,
    and must not be "simplified" back to either:
      - The ceremony claims the plan itself one directive earlier via
        `d-claim-plan-execution-lock` (`directives_lessons_plan.
        build_plan_claim_and_stamp_directives`), so a check against the
        PLAN-CLAIM holder reads as self-held and is vacuous.
      - The misdetection this guards against corrupts the plan<->handoff
        provenance JOIN itself (a wrong plan was resolved as "governing"
        in the first place), so a provenance-EQUALITY check is also
        vacuous under the exact failure this guards against.
      - Only "a DIFFERENT session is alive right now and still holds this
        work in flight" survives both failure modes. That is the entire
        condition below — nothing more, nothing less.

    Terminal-safe by construction: every ambiguity (zero handoffs found,
    more than one, unreadable frontmatter, unresolvable/self sid, dead
    holder, terminal `deployment_state`) returns `None` (proceed). This
    guard only fires on a POSITIVELY-established live foreign holder — a
    guard that blocked on absence of evidence would wedge every plan-less
    or handoff-less session, which is not this incident's shape.
    """
    from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
    from coordinator_core.session.core import resolve_session_id
    from coordinator_core.session.liveness import session_live

    plan_deliverable_id = None
    try:
        plan_text = plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        plan_text = None
    if plan_text is not None:
        split = split_frontmatter(plan_text.replace("\r\n", "\n"))
        if split is not None:
            plan_deliverable_id = read_fm_field_unquoted(split.fm_text, "deliverable_id")

    candidates = _find_governing_handoff_paths(worktree_root, plan_path, plan_deliverable_id)
    if len(candidates) != 1:
        # Zero -> nothing governs this plan (or it is genuinely plan-less
        # from a handoff's perspective); more than one -> ambiguous join,
        # same "do not guess" discipline as the forward join. Both proceed.
        return None

    handoff_path = candidates[0]
    try:
        handoff_text = handoff_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    split = split_frontmatter(handoff_text)
    if split is None:
        return None

    claimed_by = read_fm_field_unquoted(split.fm_text, "claimed_by")
    if not claimed_by:
        return None

    try:
        resolved_closing_sid = closing_sid or resolve_session_id()
    except Exception:
        resolved_closing_sid = None
    if resolved_closing_sid is None or claimed_by == resolved_closing_sid:
        return None

    if not session_live(claimed_by, cwd=str(worktree_root)):
        return None

    deployment_state = read_fm_field_unquoted(split.fm_text, "deployment_state")
    if deployment_state and deployment_state in HANDOFF_TERMINAL_DEPLOYMENT:
        return None

    return (
        f"{plan_path} is governed by {handoff_path} (deployment_state={deployment_state!r}), "
        f"claimed by LIVE session {claimed_by} (this session: {resolved_closing_sid!r}) — "
        "refusing to stamp implemented over a foreign in-flight claim"
    )


def _ac_open_rows_warning(plan_path: str, plan_text: str) -> None:
    """Emit an advisory-only WARNING to stderr naming any still-open
    `## Acceptance Criteria` table rows, immediately after a successful
    (non-no-op) `stamp-implemented` flip -- cross-repo memo
    `2026-08-10-example-retrieval-repo-em-close-out-stamps-implemented-without-
    reading-the-ac-table.md` from example-retrieval-repo-em: this CLI's own flip gate
    is entirely the `_FROZEN_STATUSES`/`_FLIPPABLE_STATUSES` vocabulary
    matrix; it never opened the plan's AC table at all.

    Deliberately NOT gated on `spine_fully_resolved` (unlike
    `close_out_and_stamp`'s own caller-side gate on `_ac_table_desync_
    finding`) -- this path has no spine verdict to gate on: the
    workstream-complete directive route (`cs_stamp_plan_implemented` ->
    `plan_status_transition.main(["stamp-implemented", ...])`) bypasses
    `close_out_and_stamp` entirely, so this is the ONLY AC-awareness this
    route gets, and it must not silently wait on a verdict it cannot see.

    Advisory only, never blocking (per the driving memo: "a warning that
    can be acknowledged is probably right; a refusal that forces the table
    to lie would be worse than what exists now"): never raises, never
    changes the exit code, never skips or reverts the write already on
    disk. An unparseable/absent/malformed AC table stays silent, mirroring
    `_ac_table_desync_finding`'s own degrade-quietly posture -- this
    function's own broad `except Exception` is a second, redundant
    backstop on top of that one, not a substitute for it.

    Coupling decision: reuses `close_out_and_stamp`'s existing AC-table
    parser (`_ac_table_desync_finding` and its own helpers) rather than
    hand-rolling a second, divergent AC oracle. Imported FUNCTION-LOCALLY,
    not at module scope: `execute_plan_assemble.close_out_and_stamp`
    already imports THIS module (`plan_status_transition`) at ITS OWN
    module scope (`from coordinator_core.ops.plan_status_transition import
    ...`), so a module-level import here in the other direction would be a
    genuine import cycle, not merely a new `ops` -> `execute_plan_assemble`
    edge. A function-local import resolves cleanly at call time (both
    modules are always fully loaded by the time a real `stamp-implemented`
    flip runs) -- mirrors `_run_cascade`'s own identical function-local
    avoidance of the sibling `archive_stamp` cycle already documented at
    that call site in this same file.
    """
    try:
        from coordinator_core.execute_plan_assemble.close_out_and_stamp import (
            _ac_table_desync_finding,
        )

        finding = _ac_table_desync_finding(plan_text, spine_fully_resolved=True)
        if finding is None:
            return
        unresolved = ", ".join(finding.get("unresolved_ac_ids", []) or [])
        print(
            f"{_PROG}: WARNING: {plan_path} was stamped implemented but its "
            f"## Acceptance Criteria table still has open row(s): {unresolved}",
            file=sys.stderr,
        )
    except Exception:
        return


def _read_and_normalize_status(fm_text: str, plan_display: str) -> str:
    """Read the ``status`` field, comment-strip, and unquote it -- the
    genuinely-identical status-parsing primitive (C1, docs/plans/2026-08-20-
    the-rungs-get-writers.md) that opens ``_stamp_implemented``'s,
    ``_stamp_superseded``'s, and ``_stamp_reopened``'s own ``mutate()``
    closures verbatim, byte-for-byte (same four edge cases, same fail-loud
    messages): a missing ``status:`` key, a comment-only value
    (``status: #note``), a YAML-legal unquoted trailing inline comment
    (``status: executing  # note``, see ``_strip_unquoted_trailing_comment``),
    and a quoted scalar with a trailing inline comment
    (``"draft"  # note`` -- detected and fail-louded, not silently mishandled
    by ``unquote_yaml_scalar``). Raises ``coordinator_core.locked_write.
    MutateAbort`` on any of the first three failure shapes; returns the
    fully comment-stripped, unquoted status string otherwise.

    ``plan_display`` is the caller's own ``opts.plan`` (or equivalent
    display string) -- reproduced verbatim in every raised message, matching
    each verb's own pre-extraction wording exactly.

    Not wired into the three existing verbs -- they keep their own inline
    copies verbatim (see this chunk's dispatch brief: "Do not re-express the
    three EXISTING verbs on these primitives... leave _stamp_implemented,
    _stamp_reopened, _stamp_superseded exactly as they are"). This exists
    for C2's new verbs to build on.
    """
    from coordinator_core.locked_write import MutateAbort

    status = read_fm_field(fm_text, "status")
    if status is None:
        raise MutateAbort(f"{_PROG}: no \"status\" field found in frontmatter of {plan_display}")
    # A comment-only value (`status: #note`, no real value before the `#`) is,
    # per YAML, an absent/null status -- see the identical inline comment on
    # each pre-extraction verb's own copy of this check.
    if status.startswith("#"):
        raise MutateAbort(f"{_PROG}: no \"status\" field found in frontmatter of {plan_display}")
    status = _strip_unquoted_trailing_comment(status)
    if status and status[0] in ("'", '"') and not status.endswith(status[0]):
        raise MutateAbort(
            f"{_PROG}: status value appears to carry a quoted-scalar-plus-trailing-comment, "
            f"which this CLI does not support -- remove the inline comment or the quotes "
            f"({plan_display})"
        )
    unquoted = unquote_yaml_scalar(status)
    # `unquote_yaml_scalar` is typed `str | None` for its own callers that pass
    # a raw `read_fm_field` result; `status` is a non-empty `str` by here, so the
    # None arm is unreachable -- kept explicit rather than annotated away.
    return status if unquoted is None else unquoted


def _resolve_worktree_root_and_check_containment(
    plan_path: Path, plan_display: str
) -> "tuple[Optional[Path], Optional[Path], Optional[str]]":
    """Resolve the plan's git worktree root + common dir, and -- when a
    worktree was resolved -- enforce path containment against it: the
    genuinely-identical worktree-resolution-plus-containment primitive (C1,
    docs/plans/2026-08-20-the-rungs-get-writers.md) that opens
    ``_stamp_implemented``'s, ``_stamp_superseded``'s, and
    ``_stamp_reopened``'s bodies verbatim (containment is scoped to the
    whole resolved worktree, not ``docs/plans/`` specifically -- see the
    module docstring's "Path containment" section for why a plan document
    has no single conventional subdirectory this op can assume).

    Returns ``(worktree_root, git_common_dir, refusal_message)``:
    ``worktree_root``/``git_common_dir`` mirror ``_resolve_repo_root_for``'s
    own contract (either may be ``None`` when no git repo was resolved for
    ``plan_path``); a non-``None`` ``refusal_message`` means the caller must
    print it to stderr and return exit code 1 without proceeding further --
    mirrors each verb's own pre-extraction ``--plan escapes the resolved git
    worktree`` fail-loud verbatim.

    Not wired into the three existing verbs -- they keep their own inline
    copies verbatim (see this chunk's dispatch brief). This exists for C2's
    new verbs to build on.
    """
    from coordinator_core.archive_stamp import _resolve_repo_root_for

    worktree_root, git_common_dir = _resolve_repo_root_for(plan_path)

    if worktree_root is not None:
        if contained_path(plan_path, [worktree_root]) is None:
            return (
                worktree_root,
                git_common_dir,
                f"{_PROG}: --plan escapes the resolved git worktree: {plan_display!r}",
            )

    return worktree_root, git_common_dir, None


def _stamp_implemented(opts: _Opts) -> int:
    """Perform the stamp-implemented verb; returns the exit code.

    Prints its own stdout/stderr lines (byte-parity with the node oracle for the plan's
    own flip; the cascade's own messages are new, see _run_cascade) rather than
    returning a structured result -- this CLI has no IPC/op-result contract, only a
    stdout/stderr + exit-code contract (see module docstring).

    The read-check-write sequence for the plan's own status field runs inside ONE
    coordinator_core.locked_write.locked_rmw mutate closure (2026-08-04, C6 Addendum
    Q4 retirement of this module's former "not a concurrent-mutation hot path" claim
    -- see the module docstring's Negative-spec) so a concurrent stamp attempt on the
    same plan file cannot race the read against the write.

    A real (non-no-op) flip is committed by this function immediately after the
    write lands, scoped to exactly the plan path (see the module docstring's
    "Commit ownership" section and `_commit_plan_flip`) -- the op takes ownership
    of committing its own terminal write rather than leaving it for a later
    archival sweep to find.
    """
    if not opts.plan:
        print(f"{_PROG}: stamp-implemented requires --plan <path>", file=sys.stderr)
        return 1
    # `--override-reason` (2026-08-10) fails loud on an EMPTY reason (flag
    # given with a blank/whitespace-only value) the same way `--by` fails
    # loud without `--override-reason` on stamp-superseded above: the
    # override is a judgment call the caller must state, not a bare
    # boolean toggle -- see module docstring "Override" note and this
    # function's own "Completeness-verdict override" section below.
    override_reason = opts.override_reason
    if override_reason is not None and not override_reason.strip():
        print(
            f"{_PROG}: --override-reason requires a non-empty reason "
            "(state WHY the completeness verdict is being overridden -- "
            "no bare/blank override)",
            file=sys.stderr,
        )
        return 1
    if not os.path.exists(opts.plan):
        print(f"{_PROG}: plan not found: {opts.plan}", file=sys.stderr)
        return 1

    plan_path = Path(opts.plan)

    from coordinator_core.archive_stamp import _resolve_repo_root_for
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    # `_resolve_repo_root_for` returns (worktree_root, git_common_dir) -- see the
    # identical rename note in `_run_cascade` above for why the second element is
    # bound to `git_common_dir`, never `repo_root`. `locked_rmw`'s own `repo_root`
    # kwarg below accepts either a worktree root or a `.git` dir (see its
    # docstring), so passing the common dir here is correct either way -- this
    # rename is honesty, not a behaviour change. `worktree_root` (the first
    # element, unused before the 2026-08-05 commit-ownership addition) is now
    # needed by `_commit_plan_flip` below -- `commit_scoped`'s own `cwd` and its
    # relpath-computation both want the worktree root, never the `.git` dir.
    worktree_root, git_common_dir = _resolve_repo_root_for(plan_path)

    # Path containment (C2a, see module docstring "Path containment" section):
    # mirrors queue_close's `contained_path(candidate, [queue_dir])` call site
    # verbatim, with docs/plans/ under the resolved worktree root as the sole
    # allowed root. Only enforced when a git worktree was actually resolved --
    # the no-repo dry-run-scratch-copy fallback below has no worktree-rooted
    # docs/plans/ to check against and is deliberately left out of this guard's
    # scope (same reasoning `_run_cascade` already documents for that branch).
    if worktree_root is not None:
        if contained_path(plan_path, [worktree_root]) is None:
            print(
                f"{_PROG}: --plan escapes the resolved git worktree: {opts.plan!r}",
                file=sys.stderr,
            )
            return 1

    if worktree_root is not None:
        refusal_reason = _refuse_if_live_foreign_holder(plan_path, worktree_root, opts.by)
        if refusal_reason is not None:
            print(f"{_PROG}: refusing to stamp implemented: {refusal_reason}", file=sys.stderr)
            return 1

    _state: dict = {
        "flipped": False,
        "prior_status": None,
        "deliverable_id": None,
        "override_applied": False,
    }

    def mutate(old_text: str) -> str:
        text = old_text.replace("\r\n", "\n")

        split = split_frontmatter(text)
        if split is None:
            raise MutateAbort(f"{_PROG}: no parseable YAML frontmatter in {opts.plan}")

        status = read_fm_field(split.fm_text, "status")
        if status is None:
            raise MutateAbort(f"{_PROG}: no \"status\" field found in frontmatter of {opts.plan}")
        # A comment-only value (`status: #note`, no real value before the `#`) is,
        # per YAML, an absent/null status -- read_fm_field's own `\s*` already
        # consumed the whitespace before the `#`, so the raw captured text starts
        # at index 0 with `#`. Route this to the same "no status field" diagnostic
        # as a genuinely missing key, rather than letting the literal "#note"
        # fall through to the matrix lookup and produce an opaque
        # `unexpected current status "#note"` for what is really a missing value.
        if status.startswith("#"):
            raise MutateAbort(f"{_PROG}: no \"status\" field found in frontmatter of {opts.plan}")
        # read_fm_field returns the RAW on-disk text, quotes included; without this
        # unquote a `status: "draft"` line fails the _FROZEN_STATUSES /
        # _FLIPPABLE_STATUSES set lookup silently on an otherwise well-formed plan
        # -- the exact regression the node oracle's A-F3 review fix called out.
        # 2026-07-21: the local _unquote_scalar copy is retired in favour of the
        # shared primitive, which mirrors schema.js unquoteScalar identically.
        # 2026-07-21: comment-strip added ahead of the unquote -- a YAML-legal
        # trailing inline comment (`status: executing  # authorized ...`) is
        # equally raw-text-only from read_fm_field and equally invisible to the
        # set lookup below until stripped. See _strip_unquoted_trailing_comment
        # docstring for why this runs BEFORE unquote_yaml_scalar (quoted values
        # must not have their protected contents touched by the comment strip).
        status = _strip_unquoted_trailing_comment(status)
        # A quoted value with a trailing inline comment (e.g. `"draft"  # note`) is
        # left untouched by the comment-strip above (it no-ops on anything that
        # looks quoted -- see _strip_unquoted_trailing_comment docstring), so the
        # raw text still carries the trailing comment and no longer ends with the
        # matching quote character. unquote_yaml_scalar would silently fail to
        # strip the quotes on that shape too, surfacing a garbled
        # `unexpected current status "\"draft\"  # note"`. Detect the shape and
        # fail loud with a clear diagnostic instead.
        if status and status[0] in ("'", '"') and not status.endswith(status[0]):
            raise MutateAbort(
                f"{_PROG}: status value appears to carry a quoted-scalar-plus-trailing-comment, "
                f"which this CLI does not support -- remove the inline comment or the quotes "
                f"({opts.plan})"
            )
        status = unquote_yaml_scalar(status)

        # UNQUOTED, not raw (break-class fix, 2026-08-04). This value is not
        # echoed or rewritten -- it is handed to `deliverable.cascade_terminal`
        # as a JOIN KEY and exact-string-compared against each live handoff's
        # own parsed `deliverable_id`. The handoff side of that join reads a
        # PARSED frontmatter mapping (quotes already gone); this side read the
        # RAW on-disk text, so a conventionally-quoted `deliverable_id: "dlv-…"`
        # arrived carrying its quote characters and matched nothing. Observed
        # live: a plan stamped `implemented` reported "no live handoff carries
        # deliverable_id='\"dlv-…\"'" while the matching handoff sat in
        # state/handoffs/, so the cascade silently advanced nothing and the
        # handoff had to be shipped by hand.
        #
        # Silent by construction, which is why it survived: an empty candidate
        # set is indistinguishable from "correctly nothing to cascade", and the
        # op's own contract treats that as success. `status` above already gets
        # the comment-strip + unquote treatment for the same reason; this field
        # was simply missed. `read_fm_field_unquoted` is the documented
        # comparison-safe sibling and does both steps.
        _state["deliverable_id"] = read_fm_field_unquoted(split.fm_text, "deliverable_id")

        if status in _FROZEN_STATUSES:
            _state["flipped"] = False
            _state["prior_status"] = status
            return old_text  # byte-identical -> locked_rmw skips the write; no cascade

        if status not in _FLIPPABLE_STATUSES:
            expected = ", ".join(_FLIPPABLE_STATUSES_ORDER)
            raise MutateAbort(
                f"{_PROG}: unexpected current status \"{status}\" for stamp-implemented — "
                f"expected one of: {expected}"
            )

        fm_text = replace_fm_field(split.fm_text, "status", "implemented")

        # Completeness-verdict override (2026-08-10): record WHO overrode
        # the AC-open-rows advisory (`_ac_open_rows_warning` below) and WHY,
        # as canonical schema'd frontmatter -- the sanctioned successor to
        # the two ad-hoc, mutually-divergent hand-authored spellings
        # (`status_stamped_by`/`status_stamped_reason` in one repo,
        # `status_stamped_by_hand` in another) this closes. Only written on
        # a REAL flip (this branch), never on the frozen/no-op branch above
        # -- an override with nothing to override (the plan was already
        # terminal) writes nothing. Inserted in reverse (`at`, then
        # `reason`, then `by`) because `insert_fm_field(..., after_key=
        # "status")` anchors each new line immediately after `status:`,
        # pushing the previously-inserted line down -- the last insertion
        # ends up first, giving the on-disk order status -> by -> reason ->
        # at (see `_stamp_superseded`'s identical single-field use of this
        # anchor for the established idiom this mirrors).
        if override_reason is not None:
            from datetime import datetime, timezone

            from coordinator_core.session.core import resolve_session_id

            override_by = resolve_session_id() or "unknown-session"
            override_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            fm_text = insert_fm_field(fm_text, "status_override_at", override_at, after_key="status")
            fm_text = insert_fm_field(fm_text, "status_override_reason", override_reason, after_key="status")
            fm_text = insert_fm_field(fm_text, "status_override_by", override_by, after_key="status")
            _state["override_applied"] = True
            _state["override_by"] = override_by
            _state["override_at"] = override_at

        _state["flipped"] = True
        _state["prior_status"] = status
        return rebuild(split, fm_text)

    written_text: Optional[str] = None
    if git_common_dir is not None:
        try:
            written_text = locked_rmw(plan_path, mutate, repo_root=git_common_dir)
        except FileNotFoundError:
            print(f"{_PROG}: plan not found: {opts.plan}", file=sys.stderr)
            return 1
        except LockTimeout as exc:
            print(f"{_PROG}: timed out waiting for file lock on {opts.plan}: {exc}", file=sys.stderr)
            return 1
        except MutateAbort as exc:
            print(exc.args[0] if exc.args else f"{_PROG}: mutation aborted", file=sys.stderr)
            return 1
    else:
        # No resolvable git repo root -- falls back to the ORIGINAL (pre-C6)
        # plain read-modify-write, byte-for-byte. This is the correct behaviour,
        # not a compromise: locking exists to serialise concurrent writers
        # sharing one lock-sidecar namespace keyed off the repo's git common
        # dir, and a path outside any git worktree (e.g. close_out_and_stamp's
        # `--dry-run` scratch copy, deliberately materialized in the system
        # temp directory so the LIVE plan is never touched) has no such
        # namespace and no concurrent-writer hazard by construction -- it is
        # read and deleted by exactly one process, one call. _run_cascade below
        # independently re-resolves git_common_dir from the same path and no-ops
        # the same way, so a repo-less plan path never reaches a real write
        # against state/handoffs/ either.
        with open(plan_path, "r", encoding="utf-8", newline="") as f:
            old_text = f.read()
        try:
            new_text = mutate(old_text)
        except MutateAbort as exc:
            print(exc.args[0] if exc.args else f"{_PROG}: mutation aborted", file=sys.stderr)
            return 1
        if new_text != old_text:
            with open(plan_path, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)
        written_text = new_text

    # Only reachable when a git repo was actually resolved (`worktree_root` is
    # non-None only when `git_common_dir` is, per `_resolve_repo_root_for`'s own
    # contract) -- the dry-run scratch-copy branch (no git repo) never reaches
    # this, matching `_run_cascade`'s identical no-repo no-op below.
    relpath: Optional[str] = None
    # Untracked-plan / no-HEAD skip-commit detection (2026-08-06, commit-
    # authored-content reroute): `commit_authored_content` REFUSES a path
    # that does not exist in HEAD, and requires HEAD to exist at all -- both
    # correct, DR-272-bound refusals this op must never route around (see
    # module docstring "No-worktree-read commit"). Detected here, BEFORE any
    # commit attempt is made, exactly mirroring `_plan_path_dirty`'s own
    # deliberate exclusion of `??` (untracked) entries one section below: an
    # untracked plan is never resumed by this op, so it must also never be
    # first-committed by this op. `untracked_reason` is None when a commit
    # is safe to attempt; otherwise it names the reason for the operator-
    # facing skip diagnostic below.
    untracked_reason: Optional[str] = None
    if worktree_root is not None:
        relpath, relpath_err = _relpath_for_commit(plan_path, worktree_root)
        if relpath_err is not None:
            print(
                f"{_PROG}: {opts.plan} status flip succeeded but committing it failed: "
                f"{relpath_err}",
                file=sys.stderr,
            )
            return 1
        if not _head_resolves(worktree_root):
            untracked_reason = "in a git repo with no commits yet (HEAD does not resolve)"
        elif not _plan_tracked_in_head(worktree_root, relpath):
            untracked_reason = "not tracked in git (absent from HEAD)"

    if not _state["flipped"]:
        # Finding 1 (code-reviewer, c6169575 review): the ORIGINAL no-op branch
        # here gated committing entirely on "did THIS invocation flip the
        # file" -- a commit failure on a real flip leaves `status: implemented`
        # stranded on disk, uncommitted; the very next invocation reads that
        # frozen status, sets flipped=False, and would take this branch
        # unconditionally, never resuming the stranded commit. Any caller of
        # this CLI has no other path back in to recover it. Distinguish the
        # genuine terminal no-op (nothing on disk to commit) from a stranded
        # dirty write (a prior run's flip that never got committed) by
        # checking git status on this exact path -- only the latter attempts
        # a commit here.
        if (
            worktree_root is not None
            and relpath is not None
            and untracked_reason is None
            and _plan_path_dirty(worktree_root, relpath)
        ):
            resume_content, resume_err = _read_and_validate_resume_content(
                plan_path, git_common_dir
            )
            if resume_err is not None:
                print(resume_err, file=sys.stderr)
                return 1
            message = (
                f"{_PROG}: stamp status (resumed) {_state['prior_status']} on {relpath}\n"
            )
            commit_result = _commit_plan_flip(
                worktree_root, relpath, message, resume_content, _state["deliverable_id"],
            )
            if not commit_result.ok:
                print(
                    f"{_PROG}: {opts.plan} has a stranded uncommitted status flip from a prior "
                    f"run, and committing it now failed too: {commit_result.stderr}",
                    file=sys.stderr,
                )
                return 1
            print(
                f"{_PROG}: {opts.plan} status \"{_state['prior_status']}\" was already flipped "
                "on disk by a prior run but never committed — committed the stranded write now"
            )
            return 0
        print(f"{_PROG}: {opts.plan} status \"{_state['prior_status']}\" is terminal/deferred — no-op")
        return 0

    # Commit ownership (see module docstring "Commit ownership" section): a real
    # flip just landed on disk -- commit it now, scoped to exactly this plan path.
    if worktree_root is not None and relpath is not None:
        if untracked_reason is not None:
            # The EM's decision (2026-08-06, commit-authored-content
            # reroute): an op whose contract is in-place mutation must not
            # be the thing that first-commits a file into git. The flip has
            # already landed on disk (above) -- report that plainly and
            # skip the commit rather than let `commit_authored_content`'s
            # own refusal (or a raw "ambiguous argument HEAD" git error)
            # leak through as if the flip itself had failed.
            print(
                f"{_PROG}: {opts.plan} is {untracked_reason} -- status flip landed on "
                "disk but was left uncommitted (this op mutates an existing tracked "
                "file in place; it does not first-commit a new one into git)",
                file=sys.stderr,
            )
        else:
            # ASCII `->`, not the Unicode `→` the stdout success line below uses --
            # deliberate (code-reviewer Finding 5, c6169575 review): a commit
            # message is read by `git log`/`git show` in arbitrary terminals and
            # tooling (some of which still mangle non-ASCII), while the stdout
            # line is this process's own output, encoded and flushed by this
            # interpreter. Same event, two different portability requirements.
            message = f"{_PROG}: stamp status \"{_state['prior_status']}\" -> implemented on {relpath}\n"
            commit_result = _commit_plan_flip(
                worktree_root, relpath, message, written_text, _state["deliverable_id"],
            )
            if not commit_result.ok:
                print(
                    f"{_PROG}: {opts.plan} status flip succeeded but committing it failed: "
                    f"{commit_result.stderr}",
                    file=sys.stderr,
                )
                return 1

    if written_text is not None:
        _ac_open_rows_warning(opts.plan, written_text)

    if _state.get("override_applied"):
        # Loud on purpose (stdout, not stderr): this is the audit trail the
        # cross-repo memo asked for -- an operator overriding the
        # completeness verdict must see it echoed back, not have it land
        # silently alongside the ordinary flip line below.
        print(
            f"{_PROG}: {opts.plan} completeness verdict OVERRIDDEN by "
            f"{_state['override_by']} at {_state['override_at']} — reason: "
            f"{override_reason}"
        )

    print(f"{_PROG}: {opts.plan} status \"{_state['prior_status']}\" → implemented")

    return _run_cascade(opts.plan, _state["deliverable_id"])


def _stamp_superseded(opts: _Opts) -> int:
    """Perform the stamp-superseded verb (C2, docs/plans/2026-08-06-<this-
    plan>.md); returns the exit code.

    Purpose: `implemented` is machine-derivable from git evidence (did the
    work land) -- `superseded` is not. It requires naming WHAT superseded a
    plan, which is human/EM judgment this op cannot infer (not from
    `folded_into_workstream`, not from any other frontmatter field already on
    the plan). So this verb takes that judgment as a REQUIRED argument
    (``--by <successor-plan-path>``) and refuses outright without it, rather
    than defaulting or guessing -- the same discipline that keeps
    `stamp-implemented` itself from writing any status a caller merely
    asserts (see this module's docstring and `_FLIPPABLE_STATUSES`).

    Mirrors `_stamp_implemented`'s authorized-writer discipline (path
    containment, locked read-modify-write, writer-side commit ownership,
    exit-code conventions) exactly -- see that function's docstring for the
    shared machinery this one reuses verbatim. Diverges only where the verb
    itself differs:
      - requires `--by`, validated to exist on disk, before touching `--plan`
        at all (fail loud, no inference -- see module docstring "C2" note);
      - writes `superseded_by: <--by value>` alongside the status flip, the
        durable record of the judgment call this verb exists to capture;
      - never fires the terminal-state cascade (`_run_cascade` is specific to
        `deliverable.cascade_terminal`'s `stamp-implemented` semantics; a
        superseded plan has nothing live left to cascade).

    Negative-spec: NOT reachable from any automated sweep, cascade, or
    ceremony tail -- this function is only ever invoked from `main()`'s
    explicit `stamp-superseded` verb dispatch, itself only reachable via a
    human/EM-typed CLI invocation naming both `--plan` and `--by` by hand.
    No `@register_op`, no registry entry, no caller anywhere in this module
    or its siblings invokes it programmatically -- an operator must always
    supply the judgment call live.
    """
    if not opts.plan:
        print(f"{_PROG}: stamp-superseded requires --plan <path>", file=sys.stderr)
        return 1
    if not opts.by:
        print(
            f"{_PROG}: stamp-superseded requires --by <successor-plan-path> "
            "(no inference -- name what superseded this plan)",
            file=sys.stderr,
        )
        return 1
    if not os.path.exists(opts.by):
        print(f"{_PROG}: --by path not found: {opts.by}", file=sys.stderr)
        return 1
    if not os.path.exists(opts.plan):
        print(f"{_PROG}: plan not found: {opts.plan}", file=sys.stderr)
        return 1

    plan_path = Path(opts.plan)

    from coordinator_core.archive_stamp import _resolve_repo_root_for
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    worktree_root, git_common_dir = _resolve_repo_root_for(plan_path)

    if worktree_root is not None:
        if contained_path(plan_path, [worktree_root]) is None:
            print(
                f"{_PROG}: --plan escapes the resolved git worktree: {opts.plan!r}",
                file=sys.stderr,
            )
            return 1

    by_value = opts.by
    _state: dict = {"flipped": False, "prior_status": None}

    def mutate(old_text: str) -> str:
        text = old_text.replace("\r\n", "\n")

        split = split_frontmatter(text)
        if split is None:
            raise MutateAbort(f"{_PROG}: no parseable YAML frontmatter in {opts.plan}")

        status = read_fm_field(split.fm_text, "status")
        if status is None:
            raise MutateAbort(f"{_PROG}: no \"status\" field found in frontmatter of {opts.plan}")
        if status.startswith("#"):
            raise MutateAbort(f"{_PROG}: no \"status\" field found in frontmatter of {opts.plan}")
        status = _strip_unquoted_trailing_comment(status)
        if status and status[0] in ("'", '"') and not status.endswith(status[0]):
            raise MutateAbort(
                f"{_PROG}: status value appears to carry a quoted-scalar-plus-trailing-comment, "
                f"which this CLI does not support -- remove the inline comment or the quotes "
                f"({opts.plan})"
            )
        status = unquote_yaml_scalar(status)

        if status == "superseded":
            _state["flipped"] = False
            _state["prior_status"] = status
            return old_text  # byte-identical -> locked_rmw skips the write

        if status in _FROZEN_STATUSES:
            # Terminal, but not already-superseded (implemented/abandoned/
            # deferred) -- refused, not silently no-op'd: those are distinct
            # judgment calls this verb must not overwrite quietly.
            raise MutateAbort(
                f"{_PROG}: {opts.plan} is already terminal at status \"{status}\" -- "
                "stamp-superseded refuses to overwrite a different terminal status"
            )

        if status not in _FLIPPABLE_STATUSES:
            expected = ", ".join(_FLIPPABLE_STATUSES_ORDER)
            raise MutateAbort(
                f"{_PROG}: unexpected current status \"{status}\" for stamp-superseded — "
                f"expected one of: {expected}"
            )

        fm_text = replace_fm_field(split.fm_text, "status", "superseded")
        if read_fm_field(fm_text, "superseded_by") is not None:
            fm_text = replace_fm_field(fm_text, "superseded_by", by_value)
        else:
            fm_text = insert_fm_field(fm_text, "superseded_by", by_value, after_key="status")
        _state["flipped"] = True
        _state["prior_status"] = status
        return rebuild(split, fm_text)

    written_text: Optional[str] = None
    if git_common_dir is not None:
        try:
            written_text = locked_rmw(plan_path, mutate, repo_root=git_common_dir)
        except FileNotFoundError:
            print(f"{_PROG}: plan not found: {opts.plan}", file=sys.stderr)
            return 1
        except LockTimeout as exc:
            print(f"{_PROG}: timed out waiting for file lock on {opts.plan}: {exc}", file=sys.stderr)
            return 1
        except MutateAbort as exc:
            print(exc.args[0] if exc.args else f"{_PROG}: mutation aborted", file=sys.stderr)
            return 1
    else:
        with open(plan_path, "r", encoding="utf-8", newline="") as f:
            old_text = f.read()
        try:
            new_text = mutate(old_text)
        except MutateAbort as exc:
            print(exc.args[0] if exc.args else f"{_PROG}: mutation aborted", file=sys.stderr)
            return 1
        if new_text != old_text:
            with open(plan_path, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)

    if not _state["flipped"]:
        print(f"{_PROG}: {opts.plan} status \"{_state['prior_status']}\" is already superseded — no-op")
        return 0

    if worktree_root is not None:
        relpath, relpath_err = _relpath_for_commit(plan_path, worktree_root)
        if relpath_err is not None:
            print(
                f"{_PROG}: {opts.plan} status flip succeeded but committing it failed: "
                f"{relpath_err}",
                file=sys.stderr,
            )
            return 1
        untracked_reason: Optional[str] = None
        if not _head_resolves(worktree_root):
            untracked_reason = "in a git repo with no commits yet (HEAD does not resolve)"
        elif not _plan_tracked_in_head(worktree_root, relpath):
            untracked_reason = "not tracked in git (absent from HEAD)"

        if untracked_reason is not None:
            print(
                f"{_PROG}: {opts.plan} is {untracked_reason} -- status flip landed on "
                "disk but was left uncommitted (this op mutates an existing tracked "
                "file in place; it does not first-commit a new one into git)",
                file=sys.stderr,
            )
        else:
            message = (
                f"{_PROG}: stamp status \"{_state['prior_status']}\" -> superseded "
                f"(by {by_value}) on {relpath}\n"
            )
            commit_result = _commit_plan_flip(
                worktree_root, relpath, message, written_text, None,
            )
            if not commit_result.ok:
                print(
                    f"{_PROG}: {opts.plan} status flip succeeded but committing it failed: "
                    f"{commit_result.stderr}",
                    file=sys.stderr,
                )
                return 1

    print(
        f"{_PROG}: {opts.plan} status \"{_state['prior_status']}\" → superseded (by {by_value})"
    )
    return 0


def _stamp_reopened(opts: _Opts) -> int:
    """Perform the stamp-reopened verb (2026-08-16, PM-authorized directly);
    returns the exit code.

    Purpose: the ONLY writer that moves a plan OUT of ``implemented`` — see
    this module's docstring "stamp-reopened" section for the incident this
    repairs (a since-fixed defect stamped ``implemented`` while the plan's
    own spine still had open rows; 18 plans found by corpus scan). Moves
    ``implemented -> landed`` and NOTHING ELSE — no generic ``--to``, no
    other source/target pair; see the module docstring's negative-spec for
    why widening this verb's target set is how it becomes the false-clean
    it exists to repair.

    Precondition-gated on a COMPUTED verdict, never the caller's assertion:
    reuses ``directives_spine_worklist.compute_open_spine_row_gate`` (the
    same oracle `workstream_complete/__init__.py` already gates
    ``d-stamp-plan-implemented`` on) rather than writing a second, bespoke
    "does this plan have open spine rows" check — the standing convention
    this repo already follows for the AC-table oracle
    (`_ac_open_rows_warning` reuses `_ac_table_desync_finding` rather than
    re-deriving it). The plan itself is its own "governing plan" for this
    gate's purposes (it is the spine being reopened, not some other
    session's inherited baton) -- `governing_plan_slug` is the plan path's
    stem, used only for the gate's own human-readable summary/warn text,
    never for gate logic. Refuses unless the gate's `verdict` is exactly
    `"applicable"` with `open_count > 0` -- `"not-applicable"` (spine
    resolved, genuinely nothing open) and `"indeterminate"` (could not
    resolve the spine at all) both refuse: this verb requires POSITIVE
    evidence of an open row, not merely the absence of a clean verdict.

    Negative-spec: NOT reachable from any automated sweep, cascade, or
    ceremony tail -- mirrors `_stamp_superseded`'s identical negative-spec
    verbatim (see that function's docstring). Only ever invoked from
    `main()`'s explicit `stamp-reopened` verb dispatch, itself only
    reachable via a human/EM-typed CLI invocation naming both `--plan` and
    `--reason` by hand -- never looped over the corpus.

    Reverse cascade -- report, never auto-reverse (module docstring's
    "Reverse cascade" section): after a successful flip, this function
    reuses `_find_governing_handoff_paths` (the same reverse plan-
    ->handoff join `_refuse_if_live_foreign_holder` already performs, not a
    second lookup) to detect any handoff that may have been advanced by the
    original (now-known-false) cascade, and prints it as an advisory
    inconsistency note on stdout. It never mutates, claims, or reverses
    that handoff -- precedent (commit `2c5c7f907`) already ruled that
    artifacts describing work that genuinely landed stay as they are.

    Mirrors `_stamp_superseded`'s authorized-writer discipline (path
    containment, locked read-modify-write, writer-side commit ownership,
    exit-code conventions) exactly -- see that function's docstring for the
    shared machinery this one reuses verbatim. Diverges only where the verb
    itself differs:
      - requires `--reason`, a non-empty string, before touching `--plan`
        at all (fail loud, no bare `--force` -- mirrors `--override-reason`
        on `stamp-implemented`, not `--by`'s "path that must exist" shape,
        since a reopen reason is prose, not a filesystem reference);
      - refuses unless the CURRENT on-disk status is exactly `"implemented"`
        (not any member of `_FROZEN_STATUSES` -- a `superseded`/`abandoned`/
        `deferred` plan is a human judgment call this verb does not touch);
      - refuses unless `compute_open_spine_row_gate` confirms an open row;
      - writes `status: landed` plus `reopened_by`/`reopened_reason`/
        `reopened_at`, the durable audit trail of the judgment call this
        verb records (mirrors `superseded_by`'s role on `stamp-superseded`);
      - never fires the terminal-state cascade (`_run_cascade` is specific
        to `stamp-implemented`'s own semantics; a plan moving OUT of
        `implemented` has nothing new to cascade forward) -- instead prints
        the reverse-cascade advisory note described above.
    """
    if not opts.plan:
        print(f"{_PROG}: stamp-reopened requires --plan <path>", file=sys.stderr)
        return 1
    if not opts.reason or not opts.reason.strip():
        print(
            f"{_PROG}: stamp-reopened requires --reason \"<why>\" "
            "(state WHY this plan is being reopened -- no bare/blank reason)",
            file=sys.stderr,
        )
        return 1
    if not os.path.exists(opts.plan):
        print(f"{_PROG}: plan not found: {opts.plan}", file=sys.stderr)
        return 1

    plan_path = Path(opts.plan)

    from coordinator_core.archive_stamp import _resolve_repo_root_for
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
    from coordinator_core.workstream_complete.directives_spine_worklist import (
        compute_open_spine_row_gate,
    )

    worktree_root, git_common_dir = _resolve_repo_root_for(plan_path)

    if worktree_root is not None:
        if contained_path(plan_path, [worktree_root]) is None:
            print(
                f"{_PROG}: --plan escapes the resolved git worktree: {opts.plan!r}",
                file=sys.stderr,
            )
            return 1

    gate = compute_open_spine_row_gate(plan_path.stem, plan_path)
    if gate.verdict != "applicable" or gate.open_count <= 0:
        print(
            f"{_PROG}: refusing to reopen {opts.plan}: the computed spine-row gate found "
            f"no open row ({gate.summary_line}) -- stamp-reopened only repairs a plan whose "
            "spine genuinely has an open row; the caller's own assertion is not evidence",
            file=sys.stderr,
        )
        return 1

    reason_value = opts.reason
    _state: dict = {"flipped": False, "prior_status": None, "deliverable_id": None}

    def mutate(old_text: str) -> str:
        text = old_text.replace("\r\n", "\n")

        split = split_frontmatter(text)
        if split is None:
            raise MutateAbort(f"{_PROG}: no parseable YAML frontmatter in {opts.plan}")

        status = read_fm_field(split.fm_text, "status")
        if status is None:
            raise MutateAbort(f"{_PROG}: no \"status\" field found in frontmatter of {opts.plan}")
        if status.startswith("#"):
            raise MutateAbort(f"{_PROG}: no \"status\" field found in frontmatter of {opts.plan}")
        status = _strip_unquoted_trailing_comment(status)
        if status and status[0] in ("'", '"') and not status.endswith(status[0]):
            raise MutateAbort(
                f"{_PROG}: status value appears to carry a quoted-scalar-plus-trailing-comment, "
                f"which this CLI does not support -- remove the inline comment or the quotes "
                f"({opts.plan})"
            )
        status = unquote_yaml_scalar(status)
        _state["deliverable_id"] = read_fm_field_unquoted(split.fm_text, "deliverable_id")

        if status != "implemented":
            raise MutateAbort(
                f"{_PROG}: {opts.plan} is at status \"{status}\", not \"implemented\" -- "
                "stamp-reopened only moves implemented -> landed, no other transition"
            )

        from datetime import datetime, timezone

        from coordinator_core.session.core import resolve_session_id

        reopened_by = resolve_session_id() or "unknown-session"
        reopened_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        fm_text = replace_fm_field(split.fm_text, "status", "landed")
        fm_text = insert_fm_field(fm_text, "reopened_at", reopened_at, after_key="status")
        fm_text = insert_fm_field(fm_text, "reopened_reason", reason_value, after_key="status")
        fm_text = insert_fm_field(fm_text, "reopened_by", reopened_by, after_key="status")

        _state["flipped"] = True
        _state["prior_status"] = status
        _state["reopened_by"] = reopened_by
        _state["reopened_at"] = reopened_at
        return rebuild(split, fm_text)

    written_text: Optional[str] = None
    if git_common_dir is not None:
        try:
            written_text = locked_rmw(plan_path, mutate, repo_root=git_common_dir)
        except FileNotFoundError:
            print(f"{_PROG}: plan not found: {opts.plan}", file=sys.stderr)
            return 1
        except LockTimeout as exc:
            print(f"{_PROG}: timed out waiting for file lock on {opts.plan}: {exc}", file=sys.stderr)
            return 1
        except MutateAbort as exc:
            print(exc.args[0] if exc.args else f"{_PROG}: mutation aborted", file=sys.stderr)
            return 1
    else:
        with open(plan_path, "r", encoding="utf-8", newline="") as f:
            old_text = f.read()
        try:
            new_text = mutate(old_text)
        except MutateAbort as exc:
            print(exc.args[0] if exc.args else f"{_PROG}: mutation aborted", file=sys.stderr)
            return 1
        if new_text != old_text:
            with open(plan_path, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)
        written_text = new_text

    if not _state["flipped"]:
        # Unreachable in practice (`mutate` either flips or raises MutateAbort
        # above), kept only as a defensive mirror of the sibling verbs' shape.
        return 1

    if worktree_root is not None:
        relpath, relpath_err = _relpath_for_commit(plan_path, worktree_root)
        if relpath_err is not None:
            print(
                f"{_PROG}: {opts.plan} status flip succeeded but committing it failed: "
                f"{relpath_err}",
                file=sys.stderr,
            )
            return 1
        untracked_reason: Optional[str] = None
        if not _head_resolves(worktree_root):
            untracked_reason = "in a git repo with no commits yet (HEAD does not resolve)"
        elif not _plan_tracked_in_head(worktree_root, relpath):
            untracked_reason = "not tracked in git (absent from HEAD)"

        if untracked_reason is not None:
            print(
                f"{_PROG}: {opts.plan} is {untracked_reason} -- status flip landed on "
                "disk but was left uncommitted (this op mutates an existing tracked "
                "file in place; it does not first-commit a new one into git)",
                file=sys.stderr,
            )
        else:
            message = (
                f"{_PROG}: stamp status \"{_state['prior_status']}\" -> landed "
                f"(reopened, reason: {reason_value}) on {relpath}\n"
            )
            commit_result = _commit_plan_flip(
                worktree_root, relpath, message, written_text, _state["deliverable_id"],
            )
            if not commit_result.ok:
                print(
                    f"{_PROG}: {opts.plan} status flip succeeded but committing it failed: "
                    f"{commit_result.stderr}",
                    file=sys.stderr,
                )
                return 1

    print(
        f"{_PROG}: {opts.plan} status \"{_state['prior_status']}\" → landed (reopened by "
        f"{_state['reopened_by']} at {_state['reopened_at']} — reason: {reason_value})"
    )

    # Reverse-cascade advisory (report only -- see module docstring "Reverse
    # cascade" and this function's own docstring): the original flip may have
    # advanced a governing handoff. Detected here, AFTER the write/commit
    # above already landed -- this note never gates or reverses the reopen.
    if worktree_root is not None:
        governing = _find_governing_handoff_paths(worktree_root, plan_path, _state["deliverable_id"])
        for handoff_path in governing:
            try:
                handoff_text = handoff_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            split = split_frontmatter(handoff_text)
            if split is None:
                continue
            deployment_state = read_fm_field_unquoted(split.fm_text, "deployment_state")
            print(
                f"{_PROG}: NOTE: {handoff_path} governs this plan and carries "
                f"deployment_state={deployment_state!r} -- it may have been advanced by the "
                "original (now-reopened) implemented flip's cascade; this op does NOT walk "
                "that back, reported for operator awareness only",
                file=sys.stderr,
            )

    return 0


def _stamp_rung(
    opts: _Opts, verb: str, target_status: str, allow_landed_reentry: bool = False
) -> int:
    """Shared body for the three rung-advance verbs (C2, docs/plans/2026-08-20-
    the-rungs-get-writers.md): ``stamp-reviewed`` (target ``reviewed``),
    ``stamp-approved`` (target ``approved``), and ``stamp-executing`` (target
    ``executing``). Templated on ``_stamp_implemented``/``_stamp_superseded``'s
    programmatic-caller and commit-ownership contract (path containment,
    locked read-modify-write, writer-side commit ownership via
    ``_commit_plan_flip``, exit-code conventions) -- see those functions'
    docstrings for the shared machinery this one reuses verbatim. Uses the
    genuinely-identical status-parsing (``_read_and_normalize_status``) and
    worktree-resolution-plus-containment (``_resolve_worktree_root_and_check_
    containment``) primitives C1 extracted specifically for these three new
    verbs to build on (AC7's four edge cases survive by construction, not by
    a fourth hand-copy).

    Positively ceremony-reachable, unlike ``_stamp_superseded``/``_stamp_
    reopened``'s negative-spec (never copy their "not reachable from any
    sweep/cascade/ceremony tail" wording onto these three): ``stamp-reviewed``
    fires from inside the ``review-exec-auth-stamp`` binary (AC10),
    ``stamp-approved`` fires from inside the exec-auth stamp path (AC8), and
    ``stamp-executing`` fires from inside ``session.claims.claim_plan(...,
    for_execution=True)`` (AC11) -- each a genuine ceremony call site this
    module gains, not merely a human-typed CLI line.

    Uniform semantics (AC2-AC6), all keyed off ``target_status``'s own index
    in ``_FLIPPABLE_STATUSES_ORDER``:
      - a `_FROZEN_STATUSES` source refuses, non-zero, naming the status
        (AC2) -- these three verbs assert no completeness, so a terminal
        plan is never silently reinterpreted as "still climbing the rungs";
      - a source outside `_FLIPPABLE_STATUSES` (and not frozen either)
        aborts non-zero, naming the status and the expected set (AC5);
      - a source already AT OR PAST `target_status`'s rung is an rc-0 no-op
        that WRITES NOTHING (AC4) -- the same byte-identical-return shape
        `_stamp_implemented`'s frozen branch and `_stamp_reopened`'s
        already-implemented-only gate establish;
      - ``allow_landed_reentry`` (``stamp-executing`` only, AC6): `landed`
        sits PAST `executing` in `_FLIPPABLE_STATUSES_ORDER`, so the generic
        at-or-past rule above would treat it as a no-op -- but `landed` is
        exactly `stamp-reopened`'s own re-park target, and the re-execution
        flow that follows a reopen re-enters the plan claim and fires
        `stamp-executing` on that landed plan. Treating that as "already
        past" leaves the plan reading `landed` while executors are actively
        running it -- reintroducing the exact status lie this plan removes.
        So `landed` is special-cased here as a legitimate re-entry source
        that DOES flip, for this one target only.

    Never fires the terminal-state cascade (`_run_cascade` is specific to
    `stamp-implemented`'s `deliverable.cascade_terminal` semantics) and never
    accepts `--by`/`--reason`/`--override-reason` -- `main()` rejects all
    three per its existing argument-rejection idiom before this function is
    ever called (AC3: none of these verbs assert completeness, so none may
    claim to override a completeness verdict).
    """
    if not opts.plan:
        print(f"{_PROG}: {verb} requires --plan <path>", file=sys.stderr)
        return 1
    if not os.path.exists(opts.plan):
        print(f"{_PROG}: plan not found: {opts.plan}", file=sys.stderr)
        return 1

    plan_path = Path(opts.plan)
    # Bound once after the guard above so the mutate closure carries a `str`
    # rather than `opts.plan`'s declared `str | None`.
    plan_display: str = opts.plan

    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    worktree_root, git_common_dir, refusal = _resolve_worktree_root_and_check_containment(
        plan_path, plan_display
    )
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1

    target_index = _FLIPPABLE_STATUSES_ORDER.index(target_status)
    _state: dict = {"flipped": False, "prior_status": None, "deliverable_id": None}

    def mutate(old_text: str) -> str:
        text = old_text.replace("\r\n", "\n")

        split = split_frontmatter(text)
        if split is None:
            raise MutateAbort(f"{_PROG}: no parseable YAML frontmatter in {opts.plan}")

        status = _read_and_normalize_status(split.fm_text, plan_display)
        _state["deliverable_id"] = read_fm_field_unquoted(split.fm_text, "deliverable_id")

        if status in _FROZEN_STATUSES:
            raise MutateAbort(
                f"{_PROG}: {opts.plan} is at frozen status \"{status}\" -- {verb} refuses "
                "a frozen source"
            )

        if status not in _FLIPPABLE_STATUSES:
            expected = ", ".join(_FLIPPABLE_STATUSES_ORDER)
            raise MutateAbort(
                f"{_PROG}: unexpected current status \"{status}\" for {verb} — "
                f"expected one of: {expected}"
            )

        status_index = _FLIPPABLE_STATUSES_ORDER.index(status)
        is_reentry = allow_landed_reentry and status == "landed"

        if not is_reentry and status_index >= target_index:
            _state["flipped"] = False
            _state["prior_status"] = status
            return old_text  # byte-identical -> locked_rmw skips the write

        fm_text = replace_fm_field(split.fm_text, "status", target_status)
        _state["flipped"] = True
        _state["prior_status"] = status
        return rebuild(split, fm_text)

    written_text: Optional[str] = None
    if git_common_dir is not None:
        try:
            written_text = locked_rmw(plan_path, mutate, repo_root=git_common_dir)
        except FileNotFoundError:
            print(f"{_PROG}: plan not found: {opts.plan}", file=sys.stderr)
            return 1
        except LockTimeout as exc:
            print(f"{_PROG}: timed out waiting for file lock on {opts.plan}: {exc}", file=sys.stderr)
            return 1
        except MutateAbort as exc:
            print(exc.args[0] if exc.args else f"{_PROG}: mutation aborted", file=sys.stderr)
            return 1
    else:
        with open(plan_path, "r", encoding="utf-8", newline="") as f:
            old_text = f.read()
        try:
            new_text = mutate(old_text)
        except MutateAbort as exc:
            print(exc.args[0] if exc.args else f"{_PROG}: mutation aborted", file=sys.stderr)
            return 1
        if new_text != old_text:
            with open(plan_path, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)
        written_text = new_text

    if not _state["flipped"]:
        print(
            f"{_PROG}: {opts.plan} status \"{_state['prior_status']}\" is already at or past "
            f"\"{target_status}\" — no-op"
        )
        return 0

    if worktree_root is not None:
        relpath, relpath_err = _relpath_for_commit(plan_path, worktree_root)
        if relpath_err is not None:
            print(
                f"{_PROG}: {opts.plan} status flip succeeded but committing it failed: "
                f"{relpath_err}",
                file=sys.stderr,
            )
            return 1
        untracked_reason: Optional[str] = None
        if not _head_resolves(worktree_root):
            untracked_reason = "in a git repo with no commits yet (HEAD does not resolve)"
        elif not _plan_tracked_in_head(worktree_root, relpath):
            untracked_reason = "not tracked in git (absent from HEAD)"

        if untracked_reason is not None:
            print(
                f"{_PROG}: {opts.plan} is {untracked_reason} -- status flip landed on "
                "disk but was left uncommitted (this op mutates an existing tracked "
                "file in place; it does not first-commit a new one into git)",
                file=sys.stderr,
            )
        else:
            message = (
                f"{_PROG}: stamp status \"{_state['prior_status']}\" -> {target_status} "
                f"on {relpath}\n"
            )
            commit_result = _commit_plan_flip(
                worktree_root, relpath, message, written_text, _state["deliverable_id"],
            )
            if not commit_result.ok:
                print(
                    f"{_PROG}: {opts.plan} status flip succeeded but committing it failed: "
                    f"{commit_result.stderr}",
                    file=sys.stderr,
                )
                return 1

    print(
        f"{_PROG}: {opts.plan} status \"{_state['prior_status']}\" → {target_status}"
    )
    return 0


def _stamp_reviewed(opts: _Opts) -> int:
    """Perform the stamp-reviewed verb (C2): flips ``draft -> reviewed``.

    See ``_stamp_rung`` for the shared contract (AC2-AC5). Fires from inside
    the ``review-exec-auth-stamp`` binary (AC10) -- from its ``mark-reviewed``
    verb at review-integration completion (the producer that actually leaves a
    plan observable at this rung) and, redundantly and as an at-or-past no-op
    by then, from its ``stamp`` verb at PM execution-approval -- ceremony-
    reachable by design, not a human-typed CLI line (see ``_stamp_rung``'s own docstring
    for why this positively diverges from ``_stamp_superseded``/``_stamp_
    reopened``'s negative-spec).
    """
    return _stamp_rung(opts, "stamp-reviewed", "reviewed")


def _stamp_approved(opts: _Opts) -> int:
    """Perform the stamp-approved verb (C2): flips ``draft``/``reviewed`` ->
    ``approved``.

    See ``_stamp_rung`` for the shared contract (AC2-AC5). Fires from inside
    the exec-auth stamp path, in a separate lock+commit after that path
    returns 0 (AC8) -- ceremony-reachable by design, not a human-typed CLI
    line (see ``_stamp_rung``'s own docstring for why this positively
    diverges from ``_stamp_superseded``/``_stamp_reopened``'s negative-spec).
    """
    return _stamp_rung(opts, "stamp-approved", "approved")


def _stamp_executing(opts: _Opts) -> int:
    """Perform the stamp-executing verb (C2): flips ``draft``/``reviewed``/
    ``approved`` -> ``executing``, and additionally accepts ``landed`` as a
    legitimate re-entry source (AC6) -- see ``_stamp_rung``'s own docstring
    for why ``landed``, which sits PAST ``executing`` in
    ``_FLIPPABLE_STATUSES_ORDER``, must flip here rather than no-op.

    See ``_stamp_rung`` for the shared contract (AC2-AC5). Fires from inside
    ``session.claims.claim_plan(..., for_execution=True)``, anchored on
    claim success (AC11) -- ceremony-reachable by design, not a human-typed
    CLI line (see ``_stamp_rung``'s own docstring for why this positively
    diverges from ``_stamp_superseded``/``_stamp_reopened``'s negative-spec).
    """
    return _stamp_rung(opts, "stamp-executing", "executing", allow_landed_reentry=True)


def _stamp_review_verified(opts: _Opts) -> int:
    """Perform the stamp-review-verified verb (C6a, docs/plans/2026-08-20-
    the-rungs-get-writers.md); returns the exit code.

    Writes ``review_verified_by`` / ``review_verified_at`` /
    ``review_verified_findings`` alongside a plan already sitting at the
    terminal ``implemented`` status -- mirrors ``_stamp_implemented``'s
    existing ``status_override_{by,reason,at}`` write shape verbatim (same
    ``insert_fm_field(..., after_key="status")`` idiom, same reversed
    insertion order so the last-inserted field ends up first on disk,
    giving on-disk order ``status -> review_verified_by ->
    review_verified_findings -> review_verified_at``), in ONE
    ``locked_rmw`` closure (AC14) -- the read-check-write sequence for the
    attest fields cannot race a concurrent attest/flip on the same plan.

    ``review_verified_findings`` carries the REVIEW-TRAIL **path**
    (e.g. ``state/review-trail/findings/...``), never a bare count -- a
    count drifts from the record already on disk and cannot distinguish a
    P1 from a nitpick (dispatch brief, C6a).

    IDEMPOTENCE (AC14) -- the named key is ``review_verified_findings``,
    the review-trail path itself (stated explicitly per the dispatch
    brief: "the sender asked which key we use and their tripwire will be
    built against the answer"): re-running this verb with the SAME
    ``--findings`` path already on disk is a byte-identical no-op
    (``locked_rmw`` skips the write, AC14's "re-runs the same wrap -- no
    second write"); a genuine SECOND review produces a NEW review-trail
    record at a different path, so a different ``--findings`` value always
    writes (AC14's "a second review -- writes"), overwriting the prior
    attest's by/at/findings with the new review's -- this field means "the
    most recently verified review", not "the first one ever recorded".

    Refuses (non-zero, no write) unless the plan's on-disk ``status`` is
    EXACTLY ``implemented`` -- this attest asserts a completed review OF a
    terminal implementation; a plan not yet (or no longer) ``implemented``
    has nothing for this verb to attest against. Deliberately narrower
    than ``_FROZEN_STATUSES``: ``superseded``/``abandoned``/``deferred``
    are refused too, same as any non-``implemented`` status, per the same
    reasoning ``_read_and_validate_resume_content`` already applies (only
    ``implemented`` is a state this attest can mean anything against).

    Never flips ``status`` itself (that is ``stamp-implemented``'s job
    alone, unchanged) and never fires the terminal-state cascade
    (``_run_cascade`` is specific to ``stamp-implemented``'s own flip
    semantics; an attest write has nothing new to cascade). A real
    (non-no-op) write is committed by this function immediately after it
    lands, scoped to exactly the plan path -- mirrors
    ``_stamp_superseded``'s identical writer-side commit-ownership shape
    (``_commit_plan_flip``), not a third invented commit path.

    Negative-spec: does NOT accept ``--by``/``--reason``/
    ``--override-reason`` -- ``main()`` rejects all three out-of-verb, the
    same existing argument-rejection idiom every other verb already uses
    (see the module's Anti-scope: "Do not add ``--by`` or ``--reason`` to
    the new verbs").
    """
    if not opts.plan:
        print(f"{_PROG}: stamp-review-verified requires --plan <path>", file=sys.stderr)
        return 1
    if not opts.findings:
        print(
            f"{_PROG}: stamp-review-verified requires --findings <review-trail-path> "
            "(the record already on disk under state/review-trail/findings/ -- "
            "not a bare count)",
            file=sys.stderr,
        )
        return 1
    if not os.path.exists(opts.plan):
        print(f"{_PROG}: plan not found: {opts.plan}", file=sys.stderr)
        return 1

    plan_path = Path(opts.plan)

    from coordinator_core.archive_stamp import _resolve_repo_root_for
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

    worktree_root, git_common_dir = _resolve_repo_root_for(plan_path)

    if worktree_root is not None:
        if contained_path(plan_path, [worktree_root]) is None:
            print(
                f"{_PROG}: --plan escapes the resolved git worktree: {opts.plan!r}",
                file=sys.stderr,
            )
            return 1

    findings_value = opts.findings
    _state: dict = {"flipped": False, "prior_status": None}

    def mutate(old_text: str) -> str:
        text = old_text.replace("\r\n", "\n")

        split = split_frontmatter(text)
        if split is None:
            raise MutateAbort(f"{_PROG}: no parseable YAML frontmatter in {opts.plan}")

        status = read_fm_field(split.fm_text, "status")
        if status is None:
            raise MutateAbort(f"{_PROG}: no \"status\" field found in frontmatter of {opts.plan}")
        if status.startswith("#"):
            raise MutateAbort(f"{_PROG}: no \"status\" field found in frontmatter of {opts.plan}")
        status = _strip_unquoted_trailing_comment(status)
        if status and status[0] in ("'", '"') and not status.endswith(status[0]):
            raise MutateAbort(
                f"{_PROG}: status value appears to carry a quoted-scalar-plus-trailing-comment, "
                f"which this CLI does not support -- remove the inline comment or the quotes "
                f"({opts.plan})"
            )
        status = unquote_yaml_scalar(status)
        _state["prior_status"] = status

        if status != "implemented":
            raise MutateAbort(
                f"{_PROG}: {opts.plan} is at status \"{status}\", not \"implemented\" -- "
                "stamp-review-verified only attests a plan already terminal at implemented"
            )

        existing_findings = read_fm_field_unquoted(split.fm_text, "review_verified_findings")
        if existing_findings == findings_value:
            _state["flipped"] = False
            return old_text  # byte-identical -> locked_rmw skips the write; idempotent re-run

        from datetime import datetime, timezone

        from coordinator_core.session.core import resolve_session_id

        verified_by = resolve_session_id() or "unknown-session"
        verified_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        fm_text = split.fm_text
        if read_fm_field(fm_text, "review_verified_by") is not None:
            # A second review after fixes (a DIFFERENT findings path already
            # established above) -- replace all three fields in place rather
            # than re-inserting, leaving their existing on-disk position
            # (immediately after `status:`) untouched.
            fm_text = replace_fm_field(fm_text, "review_verified_by", verified_by)
            fm_text = replace_fm_field(fm_text, "review_verified_findings", findings_value)
            fm_text = replace_fm_field(fm_text, "review_verified_at", verified_at)
        else:
            # Inserted in reverse (`at`, then `findings`, then `by`) because
            # `insert_fm_field(..., after_key="status")` anchors each new
            # line immediately after `status:`, pushing the
            # previously-inserted line down -- the last insertion ends up
            # first, giving the on-disk order status -> by -> findings ->
            # at. Mirrors `_stamp_implemented`'s identical
            # `status_override_{by,reason,at}` insertion idiom verbatim.
            fm_text = insert_fm_field(fm_text, "review_verified_at", verified_at, after_key="status")
            fm_text = insert_fm_field(fm_text, "review_verified_findings", findings_value, after_key="status")
            fm_text = insert_fm_field(fm_text, "review_verified_by", verified_by, after_key="status")

        _state["flipped"] = True
        _state["by"] = verified_by
        _state["at"] = verified_at
        return rebuild(split, fm_text)

    written_text: Optional[str] = None
    if git_common_dir is not None:
        try:
            written_text = locked_rmw(plan_path, mutate, repo_root=git_common_dir)
        except FileNotFoundError:
            print(f"{_PROG}: plan not found: {opts.plan}", file=sys.stderr)
            return 1
        except LockTimeout as exc:
            print(f"{_PROG}: timed out waiting for file lock on {opts.plan}: {exc}", file=sys.stderr)
            return 1
        except MutateAbort as exc:
            print(exc.args[0] if exc.args else f"{_PROG}: mutation aborted", file=sys.stderr)
            return 1
    else:
        with open(plan_path, "r", encoding="utf-8", newline="") as f:
            old_text = f.read()
        try:
            new_text = mutate(old_text)
        except MutateAbort as exc:
            print(exc.args[0] if exc.args else f"{_PROG}: mutation aborted", file=sys.stderr)
            return 1
        if new_text != old_text:
            with open(plan_path, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)
        written_text = new_text

    if not _state["flipped"]:
        print(
            f"{_PROG}: {opts.plan} already carries review_verified_findings={findings_value!r} — no-op"
        )
        return 0

    if worktree_root is not None:
        relpath, relpath_err = _relpath_for_commit(plan_path, worktree_root)
        if relpath_err is not None:
            print(
                f"{_PROG}: {opts.plan} review-verified attest succeeded but committing it failed: "
                f"{relpath_err}",
                file=sys.stderr,
            )
            return 1
        untracked_reason: Optional[str] = None
        if not _head_resolves(worktree_root):
            untracked_reason = "in a git repo with no commits yet (HEAD does not resolve)"
        elif not _plan_tracked_in_head(worktree_root, relpath):
            untracked_reason = "not tracked in git (absent from HEAD)"

        if untracked_reason is not None:
            print(
                f"{_PROG}: {opts.plan} is {untracked_reason} -- review-verified attest landed "
                "on disk but was left uncommitted (this op mutates an existing tracked "
                "file in place; it does not first-commit a new one into git)",
                file=sys.stderr,
            )
        else:
            message = (
                f"{_PROG}: attest review_verified (by {_state['by']}, findings "
                f"{findings_value}) on {relpath}\n"
            )
            commit_result = _commit_plan_flip(
                worktree_root, relpath, message, written_text, None,
            )
            if not commit_result.ok:
                print(
                    f"{_PROG}: {opts.plan} review-verified attest succeeded but committing it "
                    f"failed: {commit_result.stderr}",
                    file=sys.stderr,
                )
                return 1

    print(
        f"{_PROG}: {opts.plan} review verified by {_state['by']} at {_state['at']} "
        f"(findings: {findings_value})"
    )
    return 0


def main(argv: List[str]) -> int:
    """CLI entry: dispatch on the verb (``stamp-implemented`` / ``stamp-superseded``).

    Mirrors the node oracle's main()/switch(opts.verb) shape. Neither verb is
    registered anywhere else in this codebase -- both are only reachable
    through this explicit dispatch, invoked by a human/EM-typed CLI call
    (see `_stamp_superseded`'s negative-spec).
    """
    try:
        opts = _parse_args(argv)
    except _CliError as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1

    if opts.verb == "stamp-implemented":
        if opts.by is not None:
            # Hardening (latent, not live -- see module docstring/dispatch
            # brief): `_refuse_if_live_foreign_holder` takes an unauthenticated
            # `closing_sid` override with NO verification against the actual
            # resolved session identity. The only programmatic caller
            # (`cs_stamp_plan_implemented`) never supplies `--by`, so this is
            # unreached in production today -- but `_parse_args`'s own
            # docstring already promises "rejecting an out-of-verb flag is
            # main()'s job", and until now that promise was undischarged.
            print(
                f"{_PROG}: stamp-implemented does not accept --by "
                "(it has no successor-plan judgment call to record, and "
                "silently accepting it would let a caller disarm "
                "_refuse_if_live_foreign_holder's own-session check with an "
                "unauthenticated session id)",
                file=sys.stderr,
            )
            return 1
        if opts.reason is not None:
            print(
                f"{_PROG}: stamp-implemented does not accept --reason "
                "(it has no reopening judgment call to record -- that is "
                "stamp-reopened's own flag)",
                file=sys.stderr,
            )
            return 1
        return _stamp_implemented(opts)

    if opts.verb == "stamp-superseded":
        if opts.reason is not None:
            print(
                f"{_PROG}: stamp-superseded does not accept --reason "
                "(it has no reopening judgment call to record -- name the "
                "successor via --by instead)",
                file=sys.stderr,
            )
            return 1
        if opts.override_reason is not None:
            # Symmetric rejection to `--by` on stamp-implemented above:
            # `--override-reason` names an explicit judgment call about the
            # COMPLETENESS verdict this op's `stamp-implemented` verb
            # (advisorily) checks via `_ac_open_rows_warning` -- a
            # `stamp-superseded` plan is not being asserted "done" at all,
            # so there is no completeness verdict here for the flag to
            # override. Rejected rather than silently ignored so a
            # caller's typo/copy-paste is never absorbed quietly.
            print(
                f"{_PROG}: stamp-superseded does not accept --override-reason "
                "(it has no completeness verdict to override -- name the "
                "successor via --by instead)",
                file=sys.stderr,
            )
            return 1
        return _stamp_superseded(opts)

    if opts.verb == "stamp-reopened":
        if opts.by is not None:
            print(
                f"{_PROG}: stamp-reopened does not accept --by "
                "(it has no successor-plan judgment call to record -- use --reason)",
                file=sys.stderr,
            )
            return 1
        if opts.override_reason is not None:
            print(
                f"{_PROG}: stamp-reopened does not accept --override-reason "
                "(it has no completeness verdict to override -- use --reason)",
                file=sys.stderr,
            )
            return 1
        return _stamp_reopened(opts)

    if opts.verb in ("stamp-reviewed", "stamp-approved", "stamp-executing"):
        # AC3: none of the three rung-advance verbs assert completeness, so
        # none may claim to override a completeness verdict -- symmetric
        # rejection to `stamp-superseded`/`stamp-reopened` above, same
        # existing argument-rejection idiom.
        if opts.override_reason is not None:
            print(
                f"{_PROG}: {opts.verb} does not accept --override-reason "
                "(it has no completeness verdict to override)",
                file=sys.stderr,
            )
            return 1
        if opts.verb == "stamp-reviewed":
            return _stamp_reviewed(opts)
        if opts.verb == "stamp-approved":
            return _stamp_approved(opts)
        return _stamp_executing(opts)

    if opts.verb == "stamp-review-verified":
        # Same existing argument-rejection idiom as every verb above: none
        # of --by/--reason/--override-reason mean anything to this attest
        # write (see module Anti-scope: "Do not add --by or --reason to
        # the new verbs" -- this verb has no successor-plan judgment call,
        # no reopening judgment call, and no completeness verdict to
        # override).
        if opts.by is not None:
            print(
                f"{_PROG}: stamp-review-verified does not accept --by "
                "(it has no successor-plan judgment call to record)",
                file=sys.stderr,
            )
            return 1
        if opts.reason is not None:
            print(
                f"{_PROG}: stamp-review-verified does not accept --reason "
                "(it has no reopening judgment call to record -- name the "
                "review-trail path via --findings instead)",
                file=sys.stderr,
            )
            return 1
        if opts.override_reason is not None:
            print(
                f"{_PROG}: stamp-review-verified does not accept --override-reason "
                "(it has no completeness verdict to override -- name the "
                "review-trail path via --findings instead)",
                file=sys.stderr,
            )
            return 1
        return _stamp_review_verified(opts)

    print(
        f"{_PROG}: unknown verb: {opts.verb or '(none)'} — supported: stamp-implemented, "
        "stamp-superseded, stamp-reopened, stamp-reviewed, stamp-approved, stamp-executing, "
        "stamp-review-verified",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
