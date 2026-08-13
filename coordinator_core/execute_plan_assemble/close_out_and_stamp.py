"""
coordinator_core.execute_plan_assemble.close_out_and_stamp — mutating
assembler for `/execute-plan` Phase 4's close-out sequence.

Purpose: `/execute-plan`'s Phase 4 (coordinator-claude
`coordinator/skills/execute-plan/SKILL.md` § "Phase 4: Commit, Report, and
Offer the Next Step", item 1) narrates a hand-sequenced git close-out as
inline prose -- decide whether every wave-map chunk landed, stamp the plan's
`status:` frontmatter field to `implemented` only on the full-shipped path,
then stage and land one scoped commit covering every changed path plus the
plan doc itself. This module collapses that to ONE named op --
`close_out_and_stamp(plan_path, repo_root=None)` -- so the skill invokes a
single CLI (`close-out-and-stamp <plan-path>`) instead of hand-sequencing
`git add`/`git commit` plus a separate stamp step.

Full-shipped vs. halted determination: reads the plan's `## Tasks`
machine-parseable spine (the single fenced ```yaml plan-tasks``` block
directly under `## Tasks` -- coordinator-claude `docs/wiki/writing-plans.md` §
Machine-Parseable Task Spine), takes every row with `deferred` absent or
`false`, and cross-references each row's `id` against
`git log --oneline <range>` commit subjects using the `<chunk-id>: ...` /
`<id-1>[,+/]<id-2>[,+/]...: ...` prefix convention the skill's own DEC-2
recovery triple (Phase 1.6) already relies on for "which chunks shipped"
after a crash or compaction -- the id-list separator may be `,`, `+`, or
`/` (corpus-derived, `_extract_chunk_ids`'s own docstring). Every
non-deferred chunk-id having a matching commit subject (directly, via a
multi-id subject, or via a sub-chunk-suffixed id -- see
`_committed_chunk_ids`'s docstring) =
full-shipped; any gap = halted (stamp skipped, remaining chunk-ids
reported).

Range choice (Defect 2(a) fix, 2026-07-27) -- NOT path-scoped to the plan
doc: chunk commits touch the WORK files the chunk implements, not the plan
doc, and on the now-default background-Workflow execution vehicle
executors are structurally barred from writing plan bodies at all (a
PreToolUse guard keeps `docs/plans/*.md` read-only to them) -- so
path-scoping to the plan doc could never find a chunk commit on that path
and reported every chunk missing on fully-shipped plans. The replacement
range is `git merge-base origin/main HEAD` (exclusive) through `HEAD`
(inclusive) -- "every commit this branch has added since it diverged from
origin/main" -- chosen because it is the same merge-base-against-origin/main
primitive `coordinator_core.ops.completion_ops` already uses for its own
chain-commit resolution (see that module's `_reality_git(worktree_root,
["merge-base", "origin/main", "HEAD"])` call), so this does not introduce a
second range convention. When `origin/main` cannot be resolved (a fresh
local-only repo with no remote, a detached-HEAD sandbox, or a merge-base
failure) the range falls back to the whole `HEAD` history rather than
failing the query outright -- a workstream branch's own full history is
still a defensible (if wider than strictly necessary) search space, and
"wider than necessary" is the safe failure direction here (a chunk-id can
still be found; it just isn't excluded by branch-divergence scoping).

Range choice, widened again (range-fix, 2026-08-07 -- see bug-backlog entry
`2026-08-07-close-out-and-stamp-s-chunk-evidence-joi-8b6a7a32d833.yaml`,
two independent sightings): `merge-base origin/main HEAD` is DEGENERATE on
a shared-main workflow (this fleet's normal case -- the branch gate refuses
to cut a feature branch when live peers share the worktree, so plan
execution on `main` is not an operator error): `origin/main` advances as
peers push, so a session's own already-landed chunk commits fall BEHIND
that merge-base within minutes of landing, and the range this oracle
searches collapses toward empty. `_chunk_evidence_log_range` now tries,
in order, before falling back to the `merge-base`/`HEAD` ladder above: (1)
the plan's own `execution_authorized_sha:` frontmatter, resolved as a
literal commit-ish (works only for a plan hit by the historical mis-
stamping bug this module's docstring already records elsewhere -- see
`_plan_execution_authorized_sha`'s own docstring for why this is a no-op
for an ordinary, correctly-stamped plan, whose `execution_authorized_sha`
is a `canonical_body_sha` content hash, never a real git object); (2) the
earliest commit anywhere in `HEAD`'s own history carrying a `Deliverable-
Id` trailer that canonicalizes equal to this plan's own `deliverable_id`
(`_first_deliverable_commit_range_base`) -- deliberately NOT bounded by
`origin/main` at all. Widening the RANGE this far is safe specifically
BECAUSE the `Deliverable-Id:` exact-equality join below (see § Deliverable
scoping) is what scopes evidence to THIS plan, not the range -- a wider
range still cannot attribute a commit carrying a DIFFERENT plan's
Deliverable-Id, so this does not reopen the 2026-07-27 false-positive
incident § Deliverable scoping records. Do not re-narrow this back toward
`origin/main`-bounding without re-reading that incident.

Deliverable scoping (Defect, 2026-07-27 -- chunk-ids collide ACROSS plans):
`<chunk-id>: ...` is only unique WITHIN a single plan's own spine -- `C1`,
`C2`, `C8b`, etc. are reused by convention across every plan on the shared
workstream branch, and the merge-base..HEAD range above deliberately spans
EVERY plan's commits, not just this one's. Unscoped chunk-id matching was
therefore a FALSE-POSITIVE machine: a genuinely uncommitted `C8b` row in
plan A read as shipped because plan B and plan C each happen to have their
own, unrelated `C8b` commits on the same branch (observed live,
`work/machine-b/2026-07-21to26`, 2026-07-27 -- plan
`2026-07-27-plan-line-item-resolution-model`'s `C8b` falsely reported
shipped via `68256445`/`acce8d68`, neither of which belongs to that plan).
A false-positive completeness verdict is worse than no oracle at all -- it
silently closes a plan over unfinished work, the exact "scope leaves
without a trace" failure this whole close-out mechanism exists to catch.

The fix scopes every commit-subject match to the plan being closed out via
the `Deliverable-Id:` git trailer chunk commits already carry (the commit
pipeline stamps it; see `run_commit_pipeline`) cross-referenced against the
plan's own frontmatter `deliverable_id:` field. A commit counts as evidence
for a chunk-id ONLY IF its `Deliverable-Id` trailer equals the plan's
`deliverable_id` AND its subject matches that chunk-id -- both conditions,
not either alone. This is deliberately narrower than the raw subject match
alone, which is why the docstring calls it OUT explicitly rather than
letting a future reader assume subject-matching was always sufficient.

Chosen failure direction -- false negative over false positive, ON PURPOSE:
only commits made after the `Deliverable-Id` trailer convention shipped
carry it at all (roughly 71 of 245 chunk commits on the branch, as of this
fix); every older commit is UNATTRIBUTABLE and therefore never counts as
evidence, even when its subject matches. That means this oracle will now
sometimes report "not complete" for a plan that a human would recognize as
actually complete, if that plan's chunk commits predate the trailer. Do
NOT "fix" this back toward unscoped subject-matching to eliminate those
false negatives -- a false negative just makes a human look twice; a false
positive silently ships an incomplete plan as done. The asymmetry is the
whole point of this fix.

No `deliverable_id` in the plan's own frontmatter -- also handled
conservatively, NOT by falling back to the old unscoped behavior (that
would silently reinstate the exact bug this fix closes): the git-log query
still runs (so `query_ok` still distinguishes a broken query from an empty
result, Defect 2(d)'s existing contract), but nothing is ever added to the
committed-id set, so every commit-required chunk-id reads as missing and
the plan reports halted/not-shipped. This is the same "wider than
necessary is the safe failure direction" posture the merge-base fallback
above already uses -- just applied to attribution scope instead of commit
range.

Composition, not duplication (Wave-2 substrate-gap remit): this module does
NOT re-derive the `status:` transition logic or hand-roll a second
frontmatter writer -- it calls `coordinator_core.archive_stamp.
cs_stamp_plan_implemented` (itself a thin wrapper over the already-native
`coordinator_core.ops.plan_status_transition` port) for the stamp. The
commit leg calls `coordinator_core.ops.ceremony.commit_pipeline.
run_commit_pipeline` directly, in-process, with an explicit `stage_paths`
pathspec -- the SAME seam `ceremony.scoped_git_commit` and `wsc-tail`'s own
commit leg already use, chosen over the former `coordinator/bin/
coordinator-safe-commit` shell-out (Defect 3, 2026-07-27) because that
binary's default mode refuses outright under ordinary multi-session
concurrency ("multiple live sessions detected; default-mode commit is
unsafe") and this caller never passed it a scope to avoid that refusal --
see `close_out_and_stamp()`'s own docstring for the explicit-path derivation
this fix introduces. Neither the stamp nor the commit pipeline itself is
reimplemented here.

Negative-spec -- no plan-body-hash write here, deliberately: this op writes
ONLY the plan's `status:` field (via `plan_status_transition`), never an
`execution_authorized_sha`-shaped field. That field belongs to a DIFFERENT
stamp -- the review-time execution-authorization stamp written by
`coordinator_core.review_assemble.exec_auth_stamp` at the `/review` Exit
gate, whose hash recipe (the plan BODY hashed via the shared
`coordinator_core.frontmatter.primitives.canonical_body_sha` blob-hash
recipe, byte-identical to `git hash-object --stdin` over the body) is a
distinct, already-shipped concern this module does not touch, mutate, or
re-derive. This is the deliberate fix for the campaign's live bookkeeping
defect (three plans stamped a commit sha where a plan-body blob hash
belonged) -- the fix here is to not manufacture a second hash-shaped field
at all, not to re-derive the existing one with a different recipe.

Absent-spine posture: a plan with no `## Tasks` spine (pre-spine-format, or
still mid-authoring) has no per-chunk oracle to check completeness against
-- mirrors `coordinator-harvest-deferrals`' own "absent spine -> skip
silently, proceed" posture for the identical absent-spine case. A MALFORMED
spine (>1 fenced block, or a fence not directly under the heading) is NOT
guessed past -- it fails loud, since chunk-completeness cannot be safely
determined against a spine that cannot be located.

Dispatch Ledger fallback (Defect fix, 2026-08-06 -- 23 pre-spine plans found
permanently unstampable, or worse, silently mis-stampable): the "no spine ->
full-shipped" posture above is only SAFE when the plan genuinely has no
recorded per-chunk delivery state at all. 23 of the 51 real plans surveyed
this fix predate the `## Tasks` spine entirely (`locate_fenced_block`
returns `ABSENT`) but DO carry a hand-authored `## Dispatch Ledger` markdown
table whose `status` column records `committed <sha>` per row once that row
lands -- verified live pre-fix: `close_out_and_stamp('docs/plans/
2026-07-02-ccos-6-rehome-attribution-python.md', dry_run=True)` returned
`shipped: true` purely because the spine was absent, without the ledger's
own 7 `committed <sha>` rows ever being read at all. `_dispatch_ledger_
delivered` (see its own docstring, and the block comment above `_all_spine_
ids`) closes this: an ABSENT spine now reroutes to that ledger oracle FIRST,
falling back to the unconditional full-shipped verdict ONLY when no
Dispatch Ledger can be read either (the genuinely-nothing-to-check case D7
was written for). A LOCATED spine, even a trivially empty one, is untouched
by this fallback and never reaches it -- spine-present always wins.

Resolution-model widening (C7, plan-line-item-resolution-model, 2026-07-27):
the completeness oracle no longer keys on `deferred` alone. A row is
commit-required only when its `disposition` (D1: open|coded|spun_off|
backlogged|wont_do, default open) is `open` or `coded` --
`spun_off`/`backlogged`/`wont_do` are excluded exactly the way legacy
`deferred: true` always has been, and `deferred: true` STAYS excluded
independently of any disposition it may also carry (D8's legacy-
equivalence). Once every commit-required id has a matching commit
("shipped" below), the plan's `status:` target depends on whether every
row has *resolved*, not merely whether the code landed: any row still
`open` stamps `landed` (D9 -- code is in, resolution isn't); zero rows
`open` stamps `implemented`. Before that decision is made, a committed
chunk-id whose row is still `open` is auto-resolved in place to
`disposition: coded` with `disposition_ref` set to the covering commit's
sha AND `disposition_detail` set to that commit's own subject line (AC8;
DR-103 requires non-empty `disposition_detail` on every non-`open` row --
see `_commit_subject`/`_auto_resolve_committed_open_rows`) -- reusing the
SAME `<chunk-id>: ...` / sub-chunk-suffix match
`_committed_chunk_ids`/`_committed_id_covers_spine_id` already compute for
the completeness check, not new matching machinery. `resolve --coded`
(`plan_tasks_mutate`'s PM-gated sibling verb, C4) remains the manual
override for a commit whose subject does not follow that convention at
all. The auto-resolve write and the `landed` stamp are both performed
directly in this module (a bounded, unlocked splice/replace-field write,
mirroring `plan_status_transition.py`'s own "single-writer, once-per-
plan-completion, no locking" posture for its `stamp-implemented` verb)
rather than through `plan_tasks_mutate`'s `stamp` verb or a new
`plan_status_transition` verb -- both those modules gate their writes to
paths contained under `<worktree>/docs/plans/` (`plan_tasks_mutate`) or
support only the single hardcoded `implemented` target
(`plan_status_transition`), neither of which this op's write-scope
extends to; see `_auto_resolve_committed_open_rows`/`_stamp_plan_landed`'s
own docstrings for the exact reasoning.

Cross-repo scope scanning (Defect fix, 2026-07-27 -- the last false-negative
in this oracle): every commit-detection function above was scoped to a
SINGLE `repo_root`, but a plan routinely spans more than one repo -- a
`scope:` frontmatter list entry naming a sibling via the documented
`<repo-id>:<path>` prefix grammar (`_SCOPE_SIBLING_PREFIX_RE` below --
this module's own copy only, NOT the SSOT at `coordinator_core/
pickup_assemble/__init__.py`'s own `_SCOPE_SIBLING_PREFIX_RE`; see the
2026-07-27 regex-grammar fix note on `_SCOPE_SIBLING_PREFIX_RE` itself
for why the two copies have diverged and why the other copy -- and
`coordinator_core/reconcile/commit_reality.py`'s -- were deliberately
left untouched).
A chunk that legitimately shipped as a commit in that sibling repo could
never be seen by a completeness scan that only ever ran `git log` against
`repo_root` -- observed live, `docs/plans/
2026-07-27-plan-line-item-resolution-model.md`'s chunk C5b shipped as
commit `649797c9` in claude-klabauter, and the oracle (run from coordinator-claude)
reported it uncommitted because it never looked there. A SECOND
false-negative in this same grammar (the mandatory-whitespace-after-colon
pattern, when every real plan author writes the zero-space
`<repo-id>:<path>` form) was found and fixed the same day -- see the
negative-spec block on `_SCOPE_SIBLING_PREFIX_RE` itself.

The fix (`_plan_sibling_repo_ids`, `_resolve_sibling_repo_root`,
`_sibling_committed_chunk_ids`) derives the sibling set FROM THE PLAN
ITSELF -- the `<repo-id>:` prefixes already present in its own `scope:`
list, via `_extract_scope_paths` (the SAME `scope:` list-block scanner
`pickup_assemble`'s own preflight legs already consume) -- never a
hardcoded repo-id. Each named repo-id is resolved to an on-disk clone root
through `coordinator_core.machine_resolver.registry_get("repos.<repo-id-
with-underscores>")`, the identical registry seam `compute_tree_quiescence`
already uses for sibling resolution, never a literal path. Every sibling
repo is scanned with the EXACT SAME gates the home repo already applies --
`_sibling_committed_chunk_ids` is a thin per-sibling loop OVER the existing,
already-deliverable-scoped, already-multi-chunk-aware `_committed_chunk_ids`
function, not a second implementation -- and each sibling gets its OWN
merge-base range computed against its OWN `origin/main` (`_committed_chunk_
ids`/`_committed_chunk_shas` already derive the range from whichever
`repo_root` they are called against, so no same-branch/same-merge-base
assumption is ever made about a sibling).

Degrade-safely, and say so: a sibling repo may be unresolvable -- not
cloned on this machine, unregistered, or a git-log query failure once
resolved (not a git repo, detached HEAD with no resolvable history, etc.).
None of those crash `close_out_and_stamp`, and none are silently swallowed
either -- `_sibling_committed_chunk_ids` returns `(committed_ids,
skipped_repos)`, and `close_out_and_stamp`'s own result dict surfaces
`skipped_sibling_repos` (a list of `"<repo-id>: <reason>"` strings) on
every call, empty when there is nothing to report. A skipped sibling NEVER
counts as evidence for any chunk-id it might have covered -- this is the
same false-negative-over-false-positive posture Deliverable-Id scoping
above already commits to: a chunk whose only covering commit lives in an
unscannable sibling reads as still-missing, not silently assumed shipped.
That is also why "a plan whose sibling could not be scanned should not
claim full-shipped on that basis alone" falls out structurally rather than
needing a special case -- the chunk-ids that sibling would have covered
simply never enter `committed`, so they surface as `missing_chunk_ids`
exactly like any other genuinely-uncommitted chunk.

Sibling SHAs are NEVER fed into AC8's auto-resolve (`_auto_resolve_
committed_open_rows`'s `committed_shas` map stays HOME-REPO-ONLY, sourced
from `close_out_and_stamp`'s own `_committed_chunk_shas(root, ...)` call,
unchanged by this fix): a bare sha is ambiguous without knowing which repo
it belongs to, and `disposition_ref` has no established qualified-ref
shape to disambiguate one. The safe direction is the one this fix takes --
a row committed only in a sibling repo is still counted as SHIPPED for the
`missing_chunk_ids`/`shipped` verdict (the sibling's Deliverable-Id-scoped
commit is real evidence), but its spine row is left `open` rather than
auto-stamped `disposition: coded` with an ambiguous sha; `resolve --coded`
(`plan_tasks_mutate`'s PM-gated manual verb) remains the correct way to
resolve it. A plan whose every commit-required chunk shipped, but whose
sibling-shipped chunks were only auto-detected (not auto-resolved), targets
`status: landed` rather than `implemented` (D9's own existing "some row
still open" rule) until those rows are manually resolved -- this is a
DELIBERATE, not incidental, consequence of declining to qualify a
cross-repo sha.

Deliverable-Id equivalence join (2026-08-04, `state/deliverable-equivalence.
yaml` wiring -- `docs/plans/2026-08-03-scope-guard-peer-claim-release.md`
C7's fork closure): a genuine fork -- the SAME plan minting two
`deliverable_id` values (e.g. a spinoff handoff and its plan re-minting
instead of carrying, `_mint_deliverable_id_from_slug`'s randomness making
the collision certain, never a hash artifact) -- was previously invisible
to `_committed_chunk_shas`'s exact-equality join: a chunk's commit could
carry the "other" leg's id forever and never join. `_committed_chunk_shas`
and `_deliverable_id_near_miss_diagnostics` now canonicalize BOTH sides of
their `Deliverable-Id:` comparison through `coordinator_core.ops.
deliverable_equivalence.canonicalize()` against the declared map at
`state/deliverable-equivalence.yaml` -- the SAME mechanism already wired
into eight other readers (`ops/commit_anchors.py` et al; this module was a
genuine coverage gap, its earlier audit scoped its grep to `coordinator_
core/ops/` only and this module lives outside that directory). This is a
JOIN-KEY transform ONLY, applied at the two exact-equality comparison
points named above -- it does NOT widen chunk-id SUBJECT matching (the
`Deliverable-Id`-scoped subject match itself, and the false-positive
incident this module's docstring already records for why unscoped subject
matching must never return, are both untouched -- canonicalizing a
known-equivalent deliverable KEY is a different thing from unscoping the
subject match), and it does NOT write a canonicalized value back to any
artifact (the plan's own `deliverable_id:` frontmatter is read and compared
as-is; only the in-memory comparison value is canonicalized). A pair absent
from the map behaves exactly as before this fix -- the map is `{loser:
winner}`, and `canonicalize()` returns any unrecognised id unchanged.

Plan-side disposition_ref evidence -- a SECOND, independently-verified
evidence path (2026-08-04, `docs/plans/2026-08-03-klabauter-rows-relocate-
into-claude-klabauter.md` C5/C6): the commit-subject join above has a blind spot with
no recovery path -- a chunk whose work genuinely landed, in a commit whose
subject named the acceptance criterion or the artifact rather than the
chunk-id (`_extract_chunk_ids`'s own docstring already names this failure
mode: "a plan whose commits never named chunk-ids ... has a DIFFERENT cause
[than a Deliverable-Id mismatch]"), is invisible to `_committed_chunk_shas`
forever, and a commit subject on a shared branch cannot be rewritten to fix
it after the fact. The plan-tasks YAML already carries a per-row
`disposition_ref` (a commit sha) that `_auto_resolve_committed_open_rows`
itself writes and `plan_tasks_render.py` already reads for the `open_chunk_
ids` verdict -- it was simply never consulted for `shipped`/`missing_chunk_
ids`, which is the gap this fix closes.

`_verify_disposition_ref`/`_disposition_ref_evidence` add this as a SECOND
evidence source, unioned into `_determine_shipped`'s own `committed` set
alongside the home-repo and sibling-repo commit-subject evidence -- it does
NOT touch, widen, or relax `_committed_chunk_shas`'s own exact-equality
`Deliverable-Id:` join (see § Deliverable scoping above, and its own
explicit "not to be relaxed" negative-spec) -- that join is untouched, and
remains the ONLY evidence source for a row whose `disposition_ref` is
missing or fails verification.

The anti-self-attestation gate (the design constraint this fix must not
regress -- "this must not become 'write a field, get a stamp'"):
`disposition_ref` counts as evidence ONLY IF it resolves to a REAL commit
object in this repo's own history AND `git merge-base --is-ancestor` proves
that commit is an ancestor of `HEAD` (`_verify_disposition_ref`). An absent,
malformed (not a hex-shaped git ref), unresolvable (does not name an actual
commit object), or non-ancestor (resolves, but to a commit HEAD never
reached -- a rebased-away, cherry-picked-elsewhere, or simply fabricated
sha) ref is REJECTED -- the row's chunk-id is NOT added to `committed`, it
stays in `missing_chunk_ids`, and the specific rejection reason (one of
those four words) is available via `_disposition_ref_evidence`'s own
returned rejection map for a caller to report. A verified ancestor sha is
MORE evidence than a subject prefix anyone can type into a commit message,
so this makes the oracle strictly stronger, not looser, than the subject-
only join it sits beside.

Decided, and pinned by tests: a `disposition_ref` commit does NOT also need
to carry a matching `Deliverable-Id:` trailer. The trailer join's exact-
equality requirement exists to scope an ambiguous, corpus-wide subject
SEARCH (any commit whose subject happens to start with `C5:` could belong to
any plan reusing that id) down to the one plan being closed out --
disambiguation this path never needs, because a `disposition_ref` is not a
search result at all: it is a specific sha an executor/PM recorded, by hand,
inside THIS plan's own spine row, under THIS plan's own frontmatter. The
scoping is structural (the ref lives inside the row it is evidence for), not
inferred from a trailer value that two independent producers are already
on record disagreeing about by value (`_deliverable_id_near_miss_
diagnostics`'s own docstring documents that exact two-producer desync).
Requiring the trailer here would buy no additional anti-self-attestation
protection -- ancestry-of-HEAD already proves the commit is real and landed
-- while reintroducing the identical false-negative the near-miss
diagnostic exists to explain, for the one evidence path meant to survive it.

Spec backlink: coordinator-claude coordinator/skills/execute-plan/SKILL.md § Phase 4,
docs/plans/2026-07-27-plan-line-item-resolution-model.md § C7 (AC7/AC8/AC9),
docs/plans/2026-08-03-klabauter-rows-relocate-into-claude-klabauter.md § C5/C6
(disposition_ref evidence)
"""

from __future__ import annotations

import dataclasses
import datetime
import difflib
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import yaml

import coordinator_core.archive_stamp as archive_stamp
from coordinator_core.execute_plan_assemble.row_spans import (  # noqa: F401 -- re-exported
    _find_row_spans,
    _find_row_spans_in_plan,
    _parse_spine_rows,
    _unquote_row_id,
)
from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    remove_fm_field,
    replace_fm_field,
    serialize_yaml_scalar,
    split_frontmatter,
    unquote_yaml_scalar,
)
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.machine_resolver import registry_get
from coordinator_core.ops.ceremony import git_native, post_commit_tail
from coordinator_core.ops.ceremony.commit_pipeline import (
    PUSH_STATUS_NOT_ATTEMPTED,
    run_commit_pipeline,
)
from coordinator_core.ops.deliverable_equivalence import canonicalize, load_equivalence_map
from coordinator_core.ops.extract_scope_paths import _extract_scope_paths
from coordinator_core.ops.fleet._common import plan_claim_dir
from coordinator_core.ops.handoff_close_origin_stub import _handler as _close_origin_stub_handler
from coordinator_core.ops.plan_status_transition import (
    _FLIPPABLE_STATUSES,
    _FROZEN_STATUSES,
    _strip_unquoted_trailing_comment,
)
from coordinator_core.session import core as session_core
from coordinator_core.wire_paths import rel_id

EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2

#: Chunk-id token character class (Defect fix, 2026-08-06 -- apostrophe
#: chunk-ids invisible to the DEC-2 subject matcher): a review-time split of
#: a spine row can mint an id like `C9'` (`_committed_chunk_ids` was never
#: told, and `_extract_chunk_ids` never split, ANY dynamic per-id pattern
#: here -- there is no `re.compile(f"...{chunk_id}...")`-shaped
#: interpolation anywhere in this module for `re.escape` to guard; this is
#: a single STATIC pattern applied to every commit subject). The bug was
#: purely that `[A-Za-z0-9._-]` never admitted `'` as a legal id character
#: at all, so a subject beginning `C9': ...` could not satisfy the id group
#: BEFORE the `:` -- the match failed outright and `_extract_chunk_ids`
#: returned `[]`, exactly as it would for any other punctuation this class
#: excludes (`(`, `)`, `[`, `]`, a bare space). `.` was already admitted
#: literally (inside a character class `.` has no metacharacter meaning),
#: so no `C9.`-matches-`C9x` false positive was ever possible here -- that
#: risk applies only to a DYNAMIC pattern built from unescaped id text,
#: which this module never constructs. `'` is added to the class rather
#: than widened further (parens/brackets have no observed real chunk-id
#: use) -- see `test_apostrophe_chunk_id_registers_and_covers_its_own_
#: spine_id` for the confirmed live corpus shape this fixes.
_CHUNK_SUBJECT_RE = re.compile(
    r"^([A-Za-z0-9._'-]+(?:(?:,\s*|\s*[+/]\s*)[A-Za-z0-9._'-]+)*):\s"
)

_CHUNK_ID_SHAPE_RE = re.compile(r"^C\d")
"""FALLBACK-ONLY shape gate, used by `_extract_chunk_ids`'s multi-id split
ONLY when no `spine_ids` context is supplied at all (see that function's
`spine_ids` parameter). Originally the ONLY gate this module had -- it
assumed every real spine id in the corpus starts with an uppercase `C`
followed immediately by a digit (`C1`, `C5b`, `C8a`, `C8a-doe`,
`C3-classification`, ...), which was true of every plan on the branch until
it wasn't (Defect fix, 2026-08-01): plan
`docs/plans/2026-08-01-baton-spine-information-integrity.md` uses spine ids
`A1`-`A6`/`B1`-`B4`/`V1`, and its own compound-subject chunk commits
(`7614fb7ad`: `A1+A2+A3+A5+B1+B2+B3: ...`; `3dc5b71cd`:
`A4+A6+B4: ...`) registered ZERO ids under this gate -- every split token
was silently dropped for not starting with `C`, so the plan's detector
reported all 11 chunks open on a fully-shipped plan.

The real fix is NOT to widen this regex to admit more letters (a bare
letter-then-digit shape is exactly what let the milestone/wave-tagged
`g4-M1/M3a/M3b/M4/M4b: ...` false-positive in
`test_path_shaped_subject_before_colon_is_not_mis_split_into_chunk_ids`
happen in the first place -- widening the SHAPE only trades one set of
false positives for another). Instead, every production call site now
threads the plan's own `## Tasks` spine ids through as `spine_ids`, and
`_extract_chunk_ids` bounds the multi-id split to tokens that actually
COVER one of those real ids (`_committed_id_covers_spine_id`) -- the
requirement that candidate chunk-ids come ONLY from the plan's own spine,
never a generic shape heuristic. This static regex survives only as the
degrade-safe fallback for a caller with no spine context at all (direct
unit-test calls to `_extract_chunk_ids(subject)`, and
`_deliverable_id_near_miss_diagnostics`'s pre-2026-08-01 callers before
that function started passing `missing_chunk_ids` as `spine_ids` too)."""


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


_OPEN = "open"
_CODED = "coded"
_LANDED_STATUS = "landed"
_COMMIT_REQUIRED_DISPOSITIONS = frozenset({_OPEN, _CODED})


def _row_disposition(row: dict) -> str:
    """Row's disposition, defaulting to 'open' per the schema default (D1).
    A missing/blank/non-string value degrades to 'open' -- the same
    tolerant-read posture `plan_tasks_render.py`'s own `_disposition`
    helper uses for the identical rule (restated here, not imported --
    that helper is private to its own module and the rule is one line)."""
    value = row.get("disposition")
    return value if isinstance(value, str) and value else _OPEN


def _commit_required_chunk_ids(spine_rows: list[Any]) -> list[str]:
    """Chunk-ids requiring a matching commit under the widened
    completeness oracle (AC9, D8): a row's disposition must be `open` or
    `coded` -- `spun_off`/`backlogged`/`wont_do` are excluded exactly the
    way legacy `deferred: true` always has been. `deferred: true` STAYS
    excluded independently of any disposition it may also carry (D8's
    legacy-equivalence -- a deferred row is backlogged-equivalent, never
    commit-required)."""
    ids: list[str] = []
    for row in spine_rows:
        if not isinstance(row, dict):
            continue
        if row.get("deferred", False):
            continue
        if _row_disposition(row) not in _COMMIT_REQUIRED_DISPOSITIONS:
            continue
        chunk_id = row.get("id")
        if chunk_id:
            ids.append(str(chunk_id))
    return ids


def _open_blocking_chunk_ids(spine_rows: list[Any]) -> list[str]:
    """Chunk-ids whose row is still `open` and therefore blocks an
    `implemented` stamp (AC7) -- everything else (`coded`/`spun_off`/
    `backlogged`/`wont_do`, and legacy `deferred: true` rows, which D8
    treats as backlogged-equivalent) is resolved and does not block."""
    ids: list[str] = []
    for row in spine_rows:
        if not isinstance(row, dict):
            continue
        if row.get("deferred", False):
            continue
        if _row_disposition(row) == _OPEN:
            chunk_id = row.get("id")
            if chunk_id:
                ids.append(str(chunk_id))
    return ids


def _extract_chunk_ids(
    subject: str, spine_ids: Optional[Iterable[str]] = None
) -> list[str]:
    """Extracts every chunk-id a single commit subject registers.

    Handles the single-id form (`C1: land chunk`) AND the multi-chunk form a
    single commit legitimately uses when it lands more than one spine row at
    once -- an id-list joined by `,` (optionally `, ` with a trailing space,
    the one spacing variant seen live -- `C2, C7b: ...`), `+`
    (`C3+C2b: ...`, or space-padded `C4 + C3b + C5a: ...`), or `/`
    (`C8a-doe/C8p: ...`, or space-padded `C4b / C5b: ...`) -- registering
    EVERY id the list names, e.g. "C8a-doe/C8p: add `landed` to the plan
    status enum" registers both `C8a-doe` and `C8p` (Defect fix, 2026-07-27:
    the prior parser understood only `,` as a separator, so a `+`- or
    `/`-joined subject failed the match ENTIRELY and contributed zero ids --
    observed live, `C8p` shipped inside `C8a-doe/C8p: ...` and this oracle
    reported it uncommitted). Separator set is corpus-derived (`git log
    --format='%s'` over both coordinator-claude and claude-klabauter, 2026-07-27) --
    do not widen it past `,`/`+`/`/` without fresh corpus evidence.

    Whitespace around `+`/`/` (Defect fix, 2026-08-06 cross-repo memo
    `close-out-and-stamp-compound-subject-space-separator`): `_CHUNK_
    SUBJECT_RE`'s `+`/`/` branches now tolerate optional surrounding
    whitespace, matching the `,` branch's pre-existing comma-then-space
    tolerance --
    prior to this fix `C4 + C3b + C5a: ...` and `C4b + C5b: ...` failed the
    WHOLE leading-token match (zero ids registered, not a partial miss),
    since the separator regex required no whitespace at all around `+`/`/`.

    Bounding (so ordinary prose subjects don't start registering as chunk
    ids): the id-list must be the LEADING token(s) before the first `: `,
    with no whitespace inside any single id -- `_CHUNK_SUBJECT_RE`'s
    `[A-Za-z0-9._-]+` character class already excludes whitespace, so a
    prose subject with an embedded colon (`fix: whatever`) still only ever
    contributes its single leading token, exactly as before this fix.

    Conservatism on the MULTI-id path only (Defect fix's own failure-
    direction requirement -- over-eager splitting is the dangerous
    direction, since it can credit a chunk that never shipped): when the
    id-list contains more than one token, each split component is dropped
    unless it survives a bounding check. Two bounding strategies exist,
    selected by whether `spine_ids` is supplied:

    - `spine_ids` given (every production call site, as of the 2026-08-01
      fix below): a token survives only when it COVERS one of the given ids
      (`_committed_id_covers_spine_id(token, spine_id)` -- an exact match,
      or `token` is `spine_id` plus a recognized sub-chunk/dash-tag suffix).
      This is the "candidate chunk-ids come ONLY from the plan's own `##
      Tasks` spine" bound: no static shape assumption about what a chunk-id
      LOOKS like, since the corpus proved that assumption wrong (see
      `_CHUNK_ID_SHAPE_RE`'s own docstring) -- only whether the token
      actually names one of THIS plan's real ids.
    - `spine_ids` omitted (`None`): falls back to the static
      `_CHUNK_ID_SHAPE_RE` shape gate (starts with `C` then a digit) for a
      caller with no spine context to bound against at all.

    Either way this is what stops a path-shaped subject like
    `coordinator/bin/stitch-observer-sidecar.py: add --scan standalone leak
    sweep` (a REAL subject on this branch) from being mis-split into bogus
    ids `coordinator`/`bin`/`stitch-observer-sidecar.py`, and what stops a
    milestone/wave-tagged subject like `g4-M1/M3a/M3b/M4/M4b: ...` from
    contributing ids that were never real spine chunk-ids at all -- both
    patterns are present, unguarded, in the live corpus this fix was
    derived from -- while ALSO admitting a real non-`C`-prefixed spine id
    (`A1`, `B3`, `V1`, ...) that the old static gate silently dropped
    (Defect fix, 2026-08-01: plan `docs/plans/2026-08-01-baton-spine-
    information-integrity.md`'s own compound-subject chunk commits,
    `A1+A2+A3+A5+B1+B2+B3: ...` and `A4+A6+B4: ...`, registered zero ids
    under the old `^C\\d`-only gate and the plan's detector reported all 11
    of its chunks open on a fully-shipped plan).

    The single-id path (no separator present at all) is DELIBERATELY exempt
    from EITHER bounding strategy, preserving prior behavior verbatim (a
    bare `fix: whatever was broken` subject still contributes the single
    token `fix` -- see `test_prose_subject_with_colon_does_not_register_as_a_chunk_id`,
    which this fix must not regress): a lone extracted token can never
    spuriously satisfy an unrelated real spine id (`_committed_id_covers_
    spine_id` requires an exact-prefix match), so gating it changes nothing
    about correctness and would only needlessly diverge from tested
    behavior that predates this fix.

    KNOWN, DELIBERATE false negative -- non-leading chunk-id mentions
    (Defect fix, 2026-08-04): the bounding rule above means a subject whose
    id-list is NOT the leading token contributes NOTHING, even when a real
    chunk id appears later in the subject text. A live example: `mise:
    wave 5 -- xwin-03+04 C12 ... + xwin-05 C3` registers only the leading
    token `mise` -- both `C12` and `C3` are invisible to every caller of
    this function. This is a REAL, RECURRING corpus shape, not a one-off --
    confirmed by `git log --format='%s'` over both claude-klabauter (8372
    subjects) and coordinator-claude (9940 subjects), 2026-08-04: e.g. `mise: wave
    1 -- DOCTRINE-C7a admission gate ...; RESIDUE-C9 named-dispatch strip
    guard ...; RESIDUE-C10 read-only tier offer ...` and `mise: wave 2 --
    RESIDUE-C1..C7 relocate the auto-memory corpus ... so C8 can verify
    each relocation ...` both legitimately land multiple real chunk-ids
    with none in leading position.

    VERDICT: do not widen this function to scan the full subject for
    chunk-id-shaped tokens. The SAME corpus search that confirms the miss
    also shows why a full-subject scan is the dangerous direction this
    function's own docstring already warns against: prose subjects that
    MENTION a chunk id without landing it are common and would be silently
    OVER-credited -- e.g. `close: mark C8 shipped, and record why this plan
    cannot stamp implemented` (a status note, not C8's landing commit),
    `cross-repo: deliver ... C7 sweep deny was inverted memo from ...` (a
    memo about C7, not C7 itself), `doctrine: stage the resolves-trailer
    zero-join amendment ahead of claude-klabauter C4` (references C4, does not land
    it). A bare `\\bC\\d+\\b`-shaped scan cannot tell these apart from a
    genuine wave/batch landing commit, and the wave-shaped examples above
    use compound, non-`C\\d`-shaped tags (`DOCTRINE-C7a`, `RESIDUE-C9`,
    `RESIDUE-C1..C7` -- a RANGE, not even a discrete token list) that would
    need their own bespoke grammar to parse correctly, not a widened
    character class.

    This is therefore left as a KNOWN, DOCUMENTED false negative rather
    than guessed at: a subject that does not lead with its id-list is
    invisible to `_extract_chunk_ids` and to every caller built on it
    (`_determine_shipped`'s `missing_chunk_ids`, `_deliverable_id_near_
    miss_diagnostics`) -- such a chunk can read as still-missing even after
    it has genuinely shipped, and a reader/caller relying on this function
    for completeness must not assume otherwise for a non-leading-id
    subject. See `test_leading_token_only_bound_documents_non_leading_
    chunk_id_miss` for the pinned corpus example and the pinned counter-
    examples that justify not widening."""
    match = _CHUNK_SUBJECT_RE.match(subject)
    if not match:
        return []
    raw = match.group(1)
    if not any(sep in raw for sep in (",", "+", "/")):
        # Review: code-reviewer -- Finding 3: bound the single-id path
        # against `spine_ids` too, when supplied, for the same reason the
        # multi-id path is bounded -- an unrelated prose subject can
        # otherwise accidentally cover a short spine id via the sub-chunk/
        # dash-tag suffix shapes `_committed_id_covers_spine_id` accepts.
        # The `spine_ids is None` path is untouched -- context-free callers
        # keep exact prior behavior.
        if spine_ids is not None:
            if any(_committed_id_covers_spine_id(raw, spine_id) for spine_id in spine_ids):
                return [raw]
            return []
        return [raw]
    tokens = [token for token in (t.strip() for t in re.split(r"[,+/]", raw)) if token]
    if spine_ids is not None:
        spine_id_list = list(spine_ids)
        return [
            token
            for token in tokens
            if any(_committed_id_covers_spine_id(token, spine_id) for spine_id in spine_id_list)
        ]
    return [token for token in tokens if _CHUNK_ID_SHAPE_RE.match(token)]


_SINGLE_LETTER_SUFFIX_RE = re.compile(r"^[a-z]$")
_DASH_TAG_SUFFIX_RE = re.compile(r"^-[a-z][a-z0-9]*$")
_TRAILING_DIGITS_SUFFIX_RE = re.compile(r"^\d+$")


def _committed_id_covers_spine_id(committed_id: str, spine_id: str) -> bool:
    """True iff `committed_id` satisfies `spine_id`'s chunk-completion
    check -- an exact match, or `committed_id` is `spine_id` plus one of
    three recognized suffix shapes:

    1. A SINGLE trailing lowercase letter (Defect 2(c)): `/execute-plan`'s
       own disjoint-write-target expansion rule legitimately expands one
       spine row `C1` into several dispatches subjected `C1a:`/`C1b:`,
       which never satisfy an exact match against the spine's own `C1`.
    2. A trailing DASH-TAG -- `-` followed by one or more lowercase
       alphanumerics (`-doe`, `-mak`, `-fix2`) -- the repo-side/variant tag
       a chunk's commit subject carries when the same spine row lands via
       more than one commit (e.g. `C8a-mak: ...` in claude-klabauter,
       `C8a-doe/C8p: ...` in coordinator-claude, both covering spine `C8a`; real
       corpus examples also include `C1-fix2`, `C3-classification`). This
       is a LOCAL-repo concern, not cross-repo lookup: this function only
       ever sees commit subjects already present in THIS repo's own git
       log -- it never reaches into a sibling repo's history to resolve
       the OTHER side of a cross-repo-split chunk (that remains explicitly
       out of scope for this oracle).
    3. One or more trailing DIGITS (Defect fix, 2026-08-07 -- see bug-
       backlog entry `2026-08-07-close-out-and-stamp-reports-key-mismatch-
       dc4072b44474.yaml`), but ONLY when `spine_id` itself does NOT end in
       a digit: a wave-map fanout of a single spine row into several
       disjoint dispatches sometimes numbers them (`C6a` -> `C6a1`..`C6a7`,
       `C6b` -> `C6b1`/`C6b2` -- observed live, `docs/plans/2026-08-07-
       claim-state-ledger-first-authoritative-read.md`), the same
       disjoint-write-target expansion the single-letter suffix already
       covers, just numbered instead of lettered. Gating on `spine_id` not
       ending in a digit preserves the EXISTING `C11` must-not-cover-`C1`
       guarantee verbatim: `C1` ends in the digit `1`, so this new rule
       never fires for it and `C11` is still read as its own, unrelated,
       spine row -- the ambiguity this suffix shape would otherwise create
       is exactly the one the digit-suffix exclusion below (rule 3's own
       gate) was written to avoid, and it still does, because the gate is
       on the BASE id's own last character, not on the suffix alone.

    Conservative on purpose -- `C1a` covers `C1`, but `C11` must NOT: a
    single-lowercase-letter suffix is the sub-chunk-expansion shape
    (`C1a`, `C3r`); a trailing DIGIT on a digit-ending base id is a
    different, unrelated chunk id (`C11` is its own spine row, not a
    sub-chunk of `C1`) -- and a dash-tag suffix must itself start with a
    lowercase letter right after the dash (`-doe`, not `-3x` or a bare
    `-`), so none of these suffix shapes can be satisfied by an extension
    that would collide with a genuinely distinct spine id."""
    if committed_id == spine_id:
        return True
    if not committed_id.startswith(spine_id):
        return False
    suffix = committed_id[len(spine_id):]
    if _SINGLE_LETTER_SUFFIX_RE.match(suffix):
        return True
    if _DASH_TAG_SUFFIX_RE.match(suffix):
        return True
    if spine_id and not spine_id[-1].isdigit() and _TRAILING_DIGITS_SUFFIX_RE.match(suffix):
        return True
    return False


def _plan_deliverable_id(plan_text: str) -> Optional[str]:
    """Reads the plan's own `deliverable_id:` frontmatter field, unquoted
    and comment-stripped (`read_fm_field_unquoted` -- the comparison-safe
    reader, since this value is compared against a git trailer value
    below, not echoed or rewritten verbatim). Returns `None` when the
    plan has no parseable frontmatter, or no `deliverable_id:` field at
    all -- callers treat `None` as "cannot scope the commit search to
    this plan" (see this module's docstring § Deliverable scoping), never
    as "scope to nothing" or "fall back to unscoped"."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    return read_fm_field_unquoted(split.fm_text, "deliverable_id")


#: Grammar for the documented cross-repo `scope:` form `<repo-id>:<path>`
#: (e.g. `claude-klabauter:coordinator_core/dag.py`) -- reused VERBATIM
#: (Defect fix, 2026-07-27) from `coordinator_core/pickup_assemble/
#: __init__.py`'s own `_SCOPE_SIBLING_PREFIX_RE`, which that module's own
#: docstring names the grammar's SSOT, and which
#: `coordinator_core/reconcile/commit_reality.py` already reuses the
#: identical way. As of this fix all THREE copies carry the byte-identical
#: pattern below -- discovered live during this fix that the other two
#: call sites had ALREADY been independently repaired to this exact
#: shape the same day this module's own copy was found still broken (see
#: the negative-spec block below for what was broken and why). A
#: repo-id-shaped token (letters, digits, hyphens, underscores, starting
#: with a letter, MINIMUM TWO CHARACTERS since the `[A-Za-z]` first-char
#: class is followed by `[A-Za-z0-9_-]+`, one-or-more) followed by a
#: colon, a negative lookahead rejecting `//` immediately after the colon
#: (before any whitespace is consumed), then OPTIONAL whitespace.
#:
#: Drive-letter safety: a Windows drive letter (`C:`, `D:`) is exactly ONE
#: character before the colon, and the mandatory two-character minimum on
#: the repo-id group means `C:\Users\...` / `D:/foo/bar` can never satisfy
#: group 1 at all -- the regex engine cannot even start a match at the
#: drive letter, regardless of what follows the colon.
#:
#: URL safety: a URL scheme (`https:`, `http:`, `ftp:`) is ALSO
#: two-or-more characters before the colon, so the two-char minimum alone
#: does NOT exclude `https://example.com/x` the way it excludes a drive
#: letter. The `(?!//)` negative lookahead placed immediately after the
#: colon (BEFORE `\s*` consumes anything) is what excludes it instead: a
#: URL's `://` means the two characters right after the colon are always
#: `//`, so the lookahead fails and the match cannot anchor at this
#: position at all. A genuine sibling-repo path never starts with `//`
#: (scope paths are repo-relative), so this exclusion costs nothing on
#: the legitimate grammar.
#:
#: NEGATIVE SPEC -- what was broken here before this fix, and why the
#: prior `\s+`-mandatory pattern looked reasonable but silently disabled
#: the entire feature: the prior pattern made whitespace after the colon
#: MANDATORY, a form no real plan/handoff scope entry ever satisfies --
#: YAML parses `- repo: path` (with a space) as a MAPPING, not the plain
#: string a `scope:` list wants, so every real author writes `- repo:path`
#: (no space) instead. Confirmed against real plans: `grep -rhoE '^\s+-
#: [a-z0-9-]+:[^ ]+' docs/plans/*.md` in coordinator-claude returns only no-space
#: entries, e.g. `claude-klabauter:coordinator_core/ops/plan_tasks_mutate.py`.
#: Do not reintroduce a mandatory `\s+` here even if it looks like it
#: "keeps the grammar strict" -- it excludes the only form real authors
#: can structurally write.
_SCOPE_SIBLING_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]+):(?!//)\s*(.+)$")

#: SECOND, narrower grammar for a real live shape the colon grammar above
#: never covered (Defect fix, 2026-08-01): `<repo-id>/<path>`, e.g.
#: `claude-klabauter/coordinator_core/ops/rollup_derive.py` -- observed live
#: on `docs/plans/2026-08-01-baton-spine-information-integrity.md`'s own
#: `scope:` list, which used this shape exclusively and so registered ZERO
#: sibling repos at all under the colon-only grammar: the plan's own
#: cross-repo chunks (landed in claude-klabauter) read as permanently open
#: with `skipped_sibling_repos: []` -- not even reported as unscanned,
#: since the scanner never knew a sibling was named in the first place.
#:
#: Deliberately NOT folded into `_SCOPE_SIBLING_PREFIX_RE` itself, and
#: deliberately NOT treated the same way by `_plan_sibling_repo_ids` below:
#: a slash immediately after the leading token is fundamentally AMBIGUOUS
#: with an ordinary LOCAL repo-relative path whose first segment merely
#: happens to look repo-id-shaped -- `coordinator/bin/widget.py` is a REAL
#: local scope entry on this very branch, and this grammar's own character
#: class cannot tell "coordinator" (a local top-level directory) from
#: "claude-klabauter" (a sibling repo-id) by shape alone. See
#: `_plan_sibling_repo_ids`'s own docstring for how that ambiguity is
#: resolved (registry membership, not shape).
_SCOPE_SIBLING_SLASH_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]+)/(.+)$")


def _plan_sibling_repo_ids(
    plan_text: str, repo_root: Optional[Path] = None
) -> list[str]:
    """Distinct sibling repo-ids the plan's own `scope:` frontmatter list
    declares, in first-seen order -- the ONLY signal this scanner uses to
    decide which repos besides `repo_root` to also scan for chunk commits
    (see this module's docstring § Cross-repo scope scanning). Never
    hardcodes a repo-id; every sibling scanned is one THIS plan's own
    `scope:` named.

    `repo_root` (Review: code-reviewer -- Finding 1, 2026-08-02) is the
    HOME repo this plan is being closed out against, and gates the
    slash-form path below: a registry match alone is not sufficient to
    accept `candidate_id` as a sibling reference, because an ordinary
    LOCAL scope entry (`some-dir/file.py`) can coincidentally alias a
    real `repos.<id>` registry key on the machine running the scan --
    over-detection here unions an unrelated repo's committed chunk-ids
    into this plan's evidence and can stamp an unfinished plan
    `implemented`. `candidate_id` is only accepted once BOTH the registry
    resolves it AND `(repo_root / candidate_id)` does NOT exist as a real
    local path/directory -- a containment check, not merely a registry
    check. `repo_root` omitted (`None`, e.g. a caller with no repo context
    at all) skips the containment leg entirely -- the same degrade-safe
    posture as an unspecified check, not a silent narrowing of production
    behavior since every production call site always supplies its real
    `repo_root`.

    Reuses `_extract_scope_paths` (the SAME `scope:` list-block scanner
    `coordinator_core.pickup_assemble`'s own `preflight.dirty_paths`/
    `preflight.tree_quiescence` legs already consume) rather than a second
    hand-rolled YAML-list reader, and `_SCOPE_SIBLING_PREFIX_RE` (the
    documented `<repo-id>: <path>` grammar's SSOT) to recognize which
    entries name a sibling repo at all. A bare local-repo path (no prefix)
    is skipped, as is a prefix-shaped entry whose `rest` is empty or itself
    contains whitespace -- prose mistakenly shaped like a repo-id prefix,
    the SAME rejection `compute_tree_quiescence` applies to its own
    identical parse of this grammar.

    Second grammar, registry-gated (Defect fix, 2026-08-01): an entry that
    does not match the colon grammar is ALSO tried against
    `_SCOPE_SIBLING_SLASH_RE` (`<repo-id>/<path>`) -- but, UNLIKE the colon
    form, a slash-form match is only accepted as a sibling reference when
    `_resolve_sibling_repo_root` actually resolves its leading token to a
    real, registered clone root on THIS machine. This is the disambiguator:
    an ordinary local scope entry (`coordinator/bin/widget.py`) has a
    leading segment that is never a registered `repos.<key>` entry, so it
    is correctly never mistaken for a sibling; a genuine sibling reference
    (`claude-klabauter/coordinator_core/...`) IS registered, so it resolves
    and is recognized.

    KNOWN LIMITATION, stated rather than papered over: a sibling named
    ONLY via this slash form, on a machine where that sibling is NOT
    registered (or not cloned), is INDISTINGUISHABLE from an ordinary local
    path and is silently not recognized as sibling-shaped at all -- its
    chunk-ids will read as open, the same as today, rather than being
    surfaced in `skipped_sibling_repos`. This differs from the colon form's
    own unresolvable-sibling handling (which IS surfaced there), and is a
    deliberate asymmetry: gating slash-form recognition on shape alone
    (skipping registry resolution) would instead misfire on every ordinary
    plan with an ANY slash-shaped local scope entry, reporting bogus
    `skipped_sibling_repos` noise on the common case to cover a rarer one.
    False-negative-on-an-edge-case over false-positive-on-the-common-case,
    the same asymmetry this module's docstring already commits to
    elsewhere."""
    split = split_frontmatter(plan_text)
    if split is None:
        return []
    repo_ids: list[str] = []
    for raw_entry in _extract_scope_paths(split.fm_text):
        entry = raw_entry.strip()
        match = _SCOPE_SIBLING_PREFIX_RE.match(entry)
        if match is not None:
            rest = match.group(2).strip()
            if not rest or " " in rest:
                continue
            repo_id = match.group(1)
        else:
            slash_match = _SCOPE_SIBLING_SLASH_RE.match(entry)
            if slash_match is None:
                continue
            candidate_id = slash_match.group(1)
            resolved_root, _resolve_error = _resolve_sibling_repo_root(candidate_id)
            if resolved_root is None:
                continue
            if repo_root is not None and (repo_root / candidate_id).exists():
                # Review: code-reviewer -- Finding 1: a registered
                # repo-id alone is not sufficient; if the same token is
                # ALSO a real local path/directory in this plan's home
                # repo, treat it as the local entry it demonstrably is,
                # not a sibling reference.
                continue
            repo_id = candidate_id
        if repo_id not in repo_ids:
            repo_ids.append(repo_id)
    return repo_ids


def _resolve_sibling_repo_root(repo_id: str) -> tuple[Optional[Path], Optional[str]]:
    """Resolves a `scope:`-declared sibling `repo_id` to an on-disk clone
    root via the SAME `coordinator_core.machine_resolver.registry_get`
    seam `coordinator_core.pickup_assemble.compute_tree_quiescence` already
    uses for identical sibling-repo resolution (`repos.<repo-id-with-
    underscores>`) -- never a hardcoded path. Returns `(root, None)` on
    success, or `(None, reason)` when the repo-id is unregistered on this
    machine, OR the registered path is not an existing directory here (a
    stale registry entry, or a clone that was simply never made on this
    machine) -- both are ordinary, expected "not available on this
    machine" outcomes, never a crash."""
    registry_key = f"repos.{repo_id.replace('-', '_')}"
    raw = registry_get(registry_key)
    if not raw:
        return None, f"unregistered (no value from registry_get({registry_key!r}))"
    root = Path(raw)
    if not root.is_dir():
        return None, f"registered root {raw!r} is not a directory on this machine"
    return root, None


def _sibling_committed_chunk_ids(
    plan_text: str,
    deliverable_id: Optional[str],
    spine_ids: Optional[Iterable[str]] = None,
    repo_root: Optional[Path] = None,
) -> tuple[set[str], list[str]]:
    """Unions committed chunk-ids across every sibling repo this plan's own
    `scope:` names (`_plan_sibling_repo_ids`), applying the EXACT SAME
    gates `_committed_chunk_ids` already applies to the home repo --
    `Deliverable-Id:` trailer scoping and multi-chunk subject parsing both
    compose unchanged, since this is a thin per-sibling loop OVER that
    existing function, not a second implementation. Each sibling repo gets
    its OWN merge-base range computed against its OWN `origin/main` --
    `_committed_chunk_ids` already derives the range from whichever
    `repo_root` it is called against, so passing a sibling's root here is
    sufficient; no same-branch/same-merge-base assumption about a sibling
    is ever made. `spine_ids` (Defect fix, 2026-08-01) is forwarded verbatim
    to every per-sibling `_committed_chunk_ids` call -- the plan's own spine
    ids are the same bounding set regardless of which repo a covering commit
    happens to live in (see `_extract_chunk_ids`'s own docstring).

    Returns `(committed_ids, skipped_repos)`. `skipped_repos` is a list of
    human-readable `"<repo-id>: <reason>"` strings for every sibling that
    could NOT be scanned -- not cloned on this machine, unregistered, or a
    git-log query failure once resolved (not a git repo, detached HEAD with
    no resolvable history, etc.) -- so the caller can surface exactly which
    repos it could not read, rather than silently treating an unscannable
    sibling as contributing zero evidence with no trace (see this module's
    docstring § Cross-repo scope scanning, "Degrade-safely, and say so").
    A skipped sibling NEVER counts as evidence for any chunk-id -- a false
    negative (chunk still reads as missing) is the deliberate, safe failure
    direction here, identical to the reasoning behind Deliverable-Id
    scoping itself. Never raises -- every failure mode this function can
    hit degrades to a skip, not an exception.

    `repo_root` (Review: code-reviewer -- Finding 1, 2026-08-02) is
    forwarded to `_plan_sibling_repo_ids` verbatim as its own containment
    check's home-repo anchor -- see that function's docstring."""
    committed: set[str] = set()
    skipped: list[str] = []
    for repo_id in _plan_sibling_repo_ids(plan_text, repo_root):
        sibling_root, resolve_error = _resolve_sibling_repo_root(repo_id)
        if sibling_root is None:
            skipped.append(f"{repo_id}: {resolve_error}")
            continue
        query_ok, sibling_committed = _committed_chunk_ids(
            sibling_root, deliverable_id, spine_ids, plan_text=plan_text
        )
        if not query_ok:
            skipped.append(f"{repo_id}: git-log query failed against {sibling_root}")
            continue
        committed |= sibling_committed
    return committed, skipped


def _committed_chunk_ids(
    repo_root: Path,
    deliverable_id: Optional[str],
    spine_ids: Optional[Iterable[str]] = None,
    plan_text: Optional[str] = None,
) -> tuple[bool, set[str]]:
    """Chunk-ids with a landed commit BELONGING TO THIS PLAN, per the
    DEC-2 recovery-triple convention (`<chunk-id>: ...` or a `,`/`+`/`/`-
    joined `<id-1>[sep]<id-2>[sep]...: ...` commit subject -- see
    `_extract_chunk_ids`),
    searched over the branch-divergence-from-`origin/main` range (or the
    whole `HEAD` history when that range can't be resolved -- see this
    module's docstring § Range choice). NOT path-scoped to the plan doc
    (Defect 2(a) -- see the same docstring section for why); IS
    deliverable-scoped via `Deliverable-Id:` (see § Deliverable scoping)
    -- `deliverable_id` is the plan's own frontmatter value
    (`_plan_deliverable_id`), and a commit only counts as evidence when
    its `Deliverable-Id` trailer matches it exactly. `spine_ids` (Defect
    fix, 2026-08-01) is forwarded verbatim to `_extract_chunk_ids` as the
    multi-id split's bounding set -- see that function's docstring; `None`
    falls back to its static shape-gate default.

    Returns `(query_ok, committed_ids)`. `query_ok` is `False` ONLY when
    the git query itself failed (git not on PATH, a `git log` non-zero
    exit) -- Defect 2(d): this is the caller's signal to distinguish a
    BROKEN query from a repo that genuinely has zero chunk commits, so a
    query failure is never silently read as "every chunk missing".

    Thin wrapper over `_committed_chunk_shas` (C7, AC8) that drops the
    sha half -- kept as a separate, narrower entry point because it is
    the one every pre-existing caller (this module's own
    `_determine_shipped`, and this file's test suite) already depends on
    by this exact 2-tuple shape; `_committed_chunk_shas` is the widened
    3-tuple form AC8's auto-resolution needs the commit sha from.

    `plan_text` (range-fix, 2026-08-07 -- see `_chunk_evidence_log_range`'s
    own docstring) is forwarded verbatim to `_committed_chunk_shas` as the
    range-widening anchor; `None` (a caller with no plan text at hand)
    degrades to the pre-fix range exactly, never a hard failure."""
    query_ok, committed, _shas, _join_stats = _committed_chunk_shas(
        repo_root, deliverable_id, spine_ids, plan_text=plan_text
    )
    return query_ok, committed


def _plan_execution_authorized_sha(plan_text: str) -> Optional[str]:
    """Reads the plan's own `execution_authorized_sha:` frontmatter field,
    unquoted -- the four-field execution-authorization stamp's content-
    binding witness (`review_assemble.exec_auth_stamp`). `None` when the
    plan has no parseable frontmatter or no such field.

    NEGATIVE SPEC, load-bearing for `_chunk_evidence_log_range` below: this
    value is `canonical_body_sha` -- `git_blob_sha1` of the plan BODY text
    ALONE (frontmatter excluded), a synthetic content hash never written
    into this (or any) repo's git object store, since no real git blob ever
    holds body-without-frontmatter content. `git cat-file -e <this-sha>`
    therefore fails on essentially every plan, confirmed empirically against
    a live fixture on this branch (2026-08-07). `_chunk_evidence_log_range`
    still attempts to resolve it as a commit FIRST (cheap, and the one case
    it succeeds -- a plan re-stamped by the historical bug this module's own
    docstring already records, "three plans stamped a commit sha where a
    plan-body blob hash belonged" -- makes it a genuinely useful anchor for
    those specific plans) before falling through to the range below that
    does not depend on it resolving at all."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    return read_fm_field_unquoted(split.fm_text, "execution_authorized_sha")


#: Record and field separators for the ONE `git log` shape every
#: `Deliverable-Id` reader in this module runs. Chosen over the newline the
#: format used before the message-line fallback landed (2026-08-10) because
#: that fallback needs `%B` -- a multi-line atom -- on the same record as the
#: sha, so a newline can no longer delimit records. ASCII RS/US are the
#: control characters reserved for exactly this and cannot occur in a sha, a
#: subject, or a trailer value; a commit BODY carrying one literally would
#: garble its own record and drop it from the join, which is a strictly
#: better failure than the silent mis-attribution a printable sentinel could
#: produce.
_LOG_RECORD_SEP = "\x1e"
_LOG_FIELD_SEP = "\x1f"

#: A `Deliverable-Id:` line anywhere in a commit BODY, anchored to line start
#: so a value merely quoted mid-sentence cannot match. See
#: `_resolve_deliverable_id` for when this is consulted at all.
#:
#: NEGATIVE SPEC -- CRLF (Review: code-reviewer -- Finding 1, refuted): a
#: `re.MULTILINE` `$` does not match immediately before `\r\n` (only before a
#: bare `\n` or end-of-string), so a naive reading suggests a CRLF-terminated
#: body line would silently fail this pattern despite reading
#: `Deliverable-Id: <id>` at column 0. That never reaches this regex: `body`
#: comes from `_run_git`'s `subprocess.run(..., text=True)` call, which
#: applies Python's universal-newline translation to stdout BEFORE this code
#: ever sees it -- both `\r\n` and a lone `\r` are converted to `\n` at the
#: subprocess boundary, so a `\r` cannot survive into `body`. This pattern is
#: therefore safe against a CRLF-authored commit body only because of that
#: decoding step; a future change to `_run_git` (e.g. adding `newline=''`, or
#: swapping to a byte-mode/`Popen` call that skips universal-newline
#: translation) is what would reopen this, not a change here.
_DELIVERABLE_ID_BODY_LINE_RE = re.compile(
    r"^Deliverable-Id:[ \t]*(\S[^\r\n]*?)[ \t]*$", re.MULTILINE
)


def _resolve_deliverable_id(trailer_block: str, body: str) -> str:
    """This commit's `Deliverable-Id` join key: git's own parsed trailer when
    it produced one, else the message-line fallback.

    The fallback exists because git recognises ONLY the message's LAST
    paragraph as trailers, and a defect in `commit()`'s trailer-join branch
    (fixed at `5fcbb42696e5`, 2026-08-10 -- it tested `endswith("\\n")`, so a
    message already ending in a blank line kept both newlines and the branch
    whose entire purpose is to avoid a paragraph break produced one) emitted
    a blank line between the caller's `Deliverable-Id:` and the pipeline's
    own `Commit-Token:`/`Session-Id:` block. Git then reads only that last
    paragraph as trailers and DEMOTES the caller's line to prose:
    `%(trailers:key=Deliverable-Id,valueonly)` returns empty for a commit
    that visibly carries the line. Every `-F`-file caller was exposed, which
    is the commit practice `/execute-plan` doctrine tells EMs to use, and the
    demotion was live for as long as that path has been -- so the affected
    set is "every plan whose chunks landed before the fix", not the two
    commits that surfaced it (`b1e0881d39a7`, `3301a8d1f68c` -- bug-backlog
    `2026-08-10-two-commits-on-work-machine-a-080826-carry-af536f05255e.yaml`).
    Recovering them by rewriting shared-branch history is explicitly not the
    remedy (`/execute-plan` SKILL Phase 4), so the reader adapts instead.

    Precedence is trailer-first and never the reverse: where git parsed a
    trailer, that value is authoritative, so a correctly-formed commit's join
    key is byte-identical to what it was before this fallback existed and no
    already-passing verdict can change. Only the empty-atom case -- which
    previously joined against nothing at all -- consults the body.

    Multi-value handling is FIRST-NON-EMPTY-value-wins, not byte-identical to
    the pre-fallback reader for one narrow edge case (Review: code-reviewer --
    Finding 2): for the typical case -- a single trailer value, or a genuinely
    populated first value among several -- this matches the old reader
    exactly, since the old newline-delimited format emitted one output line
    per value and any second line, carrying no tab, was dropped by every
    caller's `len(parts) < 2` filter. But an EMPTY first `Deliverable-Id`
    trailer value followed by a populated second one for the same key differs:
    the old reader saw a blank first line, found no tab, and `continue`d --
    treating the WHOLE commit as trailer-less (never joined via the trailer
    path at all). This reader instead skips the blank line and returns the
    second, populated value, joining on it. Judged the better behavior, not
    weakened to match: a populated `Deliverable-Id` value that exists anywhere
    in the trailer block is real evidence and discarding it to preserve exact
    parity with a reader whose blank-first-line handling was itself
    incidental (a side effect of the old single-line `len(parts) < 2` filter,
    not a deliberate join-key policy) would throw away a genuine join for no
    benefit. Requires an explicit empty-then-populated same-key trailer pair,
    so it is narrow in practice.

    The body fallback takes the LAST matching line, mirroring git's own
    preference for the last trailer block when a message carries several.

    False-positive posture: a body line reading `Deliverable-Id: <id>` at
    column 0 that is NOT an attribution -- prose quoting the convention, or a
    pasted commit message inside another commit message -- is
    indistinguishable from the real thing here and would join. That is
    accepted for the same reason the trailer join itself is scoped by exact
    post-canonicalization equality against ONE plan's own id: a false match
    requires quoting that specific deliverable's id at line start, and the
    cost of a miss (a shipped plan permanently unstampable) exceeds the cost
    of that hit. This does NOT widen subject matching, which is where the
    2026-07-27 false-positive incident lived -- see this module's docstring
    § Deliverable scoping."""
    for candidate in trailer_block.splitlines():
        value = candidate.strip()
        if value:
            return value
    matches = _DELIVERABLE_ID_BODY_LINE_RE.findall(body)
    if matches:
        return matches[-1].strip()
    return ""


def _deliverable_log_records(
    repo_root: Path, log_args: Sequence[str], full_sha: bool = False
) -> tuple[bool, list[tuple[str, str, str]]]:
    """Runs the module's single `Deliverable-Id`-bearing `git log` shape over
    `log_args` and returns `(query_ok, [(sha, subject, deliverable_id)])`.

    Every reader of a commit's deliverable identity routes through here so
    the format string, the record parse, and the message-line fallback
    (`_resolve_deliverable_id`) can never drift between them -- the same
    single-producer property `_chunk_evidence_log_range` already enforces for
    the RANGE, extended to the PARSE now that a demoted trailer means the raw
    atom is no longer the whole answer.

    `full_sha` (Review: code-reviewer -- Finding 2) selects `%H` (full sha)
    over the default `%h` (abbreviated) for the sha atom only -- every other
    field is identical either way. `_first_deliverable_commit_range_base`
    passes `True`: it feeds the sha straight into `git rev-parse --verify
    --quiet <sha>^`, and an abbreviated sha is only guaranteed unambiguous
    against the object database at the moment THIS `git log` ran -- on this
    machine's concurrency a commit can land between this query and that
    later `rev-parse` call and widen the disambiguation boundary, in
    principle producing an ambiguous-short-sha failure a full sha never
    could. `_chunk_evidence_log_lines` (every other caller) keeps the
    default `%h`: its consumers (`committed_shas`, diagnostics) surface the
    sha to callers that have always seen the abbreviated form, and widening
    that surface is a change of its own, not a side effect of this fix.

    `query_ok` is `False` ONLY when git itself failed (not on PATH, non-zero
    `git log` exit), never for an empty result -- callers must check it
    before trusting the records, exactly as they did before this extraction.
    A record whose field count is short (a body containing a literal RECORD
    separator, `\\x1e`, which fragments the record itself) is skipped rather
    than guessed at; the field split uses `maxsplit=3` so a body containing a
    literal FIELD separator (`\\x1f`) cannot silently truncate `%B` into a
    fourth field and inflate the split past 4 -- the trailing field always
    absorbs the rest of the record verbatim, matching the "dropped, not
    guessed at" contract this docstring makes for the record-separator case."""
    sha_atom = "%H" if full_sha else "%h"
    result = _run_git(
        [
            "log",
            "--format="
            + _LOG_RECORD_SEP
            + sha_atom
            + _LOG_FIELD_SEP
            + "%s"
            + _LOG_FIELD_SEP
            + "%(trailers:key=Deliverable-Id,valueonly)"
            + _LOG_FIELD_SEP
            + "%B",
            *log_args,
        ],
        repo_root,
    )
    if result.returncode != 0:
        return False, []
    records: list[tuple[str, str, str]] = []
    for raw_record in (result.stdout or "").split(_LOG_RECORD_SEP):
        if not raw_record.strip():
            continue
        fields = raw_record.split(_LOG_FIELD_SEP, 3)
        if len(fields) < 4:
            continue
        sha = fields[0].strip()
        if not sha:
            continue
        records.append((sha, fields[1], _resolve_deliverable_id(fields[2], fields[3])))
    return True, records


def _first_deliverable_commit_range_base(
    repo_root: Path, deliverable_id: Optional[str]
) -> Optional[str]:
    """The exclusive lower bound of "every commit this repo's `HEAD` history
    carries for THIS plan's own deliverable" -- the earliest (oldest, via
    `--reverse`) commit anywhere in `HEAD`'s ancestry whose `Deliverable-Id`
    trailer canonicalizes (via `deliverable_equivalence.canonicalize()`, the
    same join-key transform every other chunk-evidence reader already uses)
    equal to `deliverable_id`. Returns that commit's OWN PARENT sha (so the
    earliest chunk commit itself stays INSIDE the `<base>..HEAD` range this
    function's caller builds), `""` when that earliest commit is a root
    commit with no parent (the whole history is this deliverable's own, so
    the caller should use bare `HEAD`), or `None` when `deliverable_id` is
    falsy, the git query fails, or no commit anywhere in `HEAD`'s history
    carries a matching trailer at all.

    Range-fix (2026-08-07, close-out-and-stamp's chunk-evidence range
    collapsing to empty on `main`): NOT bounded by `origin/main` at all,
    unlike the merge-base rung below it in `_chunk_evidence_log_range` --
    on a shared-main workflow `origin/main` advances past a session's own
    already-landed chunk commits within minutes as peers push, so a
    merge-base-bounded search excludes exactly the commits this oracle
    needs to find (the confirmed root cause; see this module's own bug-
    backlog entry `2026-08-07-close-out-and-stamp-s-chunk-evidence-joi-
    8b6a7a32d833.yaml`). Safe to widen this far specifically BECAUSE the
    `Deliverable-Id:` exact-equality (post-canonicalization) join is the
    thing doing the scoping here, not the range -- see this module's
    docstring § Deliverable scoping and its own 2026-07-27 false-positive
    incident for why an UNSCOPED subject-match widening would be dangerous;
    this widening is scoped by deliverable identity, never by subject shape
    alone, so it does not reintroduce that incident.

    Queries `_deliverable_log_records` with `full_sha=True` (Review:
    code-reviewer -- Finding 2): the matched commit's sha feeds straight into
    `git rev-parse --verify --quiet <sha>^` below, and only a full sha is
    guaranteed unambiguous against the object database at the time that
    later call runs -- see `_deliverable_log_records`'s own docstring."""
    if not deliverable_id:
        return None
    query_ok, records = _deliverable_log_records(repo_root, ["--reverse", "HEAD"], full_sha=True)
    if not query_ok:
        return None
    equivalence_map = load_equivalence_map(repo_root)
    canonical_deliverable_id = canonicalize(deliverable_id, equivalence_map)
    for commit_sha, _subject, trailer_value in records:
        if not trailer_value:
            continue
        if canonicalize(trailer_value, equivalence_map) != canonical_deliverable_id:
            continue
        parent_result = _run_git(["rev-parse", "--verify", "--quiet", f"{commit_sha}^"], repo_root)
        parent_sha = (parent_result.stdout or "").strip()
        if parent_result.returncode == 0 and parent_sha:
            return parent_sha
        return ""
    return None


def _chunk_evidence_log_range(
    repo_root: Path, plan_text: Optional[str] = None
) -> list[str]:
    """The `git log` range every chunk-evidence query runs over.

    Range-fix ladder (2026-08-07 -- see this module's own bug-backlog entry
    `2026-08-07-close-out-and-stamp-s-chunk-evidence-joi-8b6a7a32d833.yaml`,
    which supersedes the single `merge-base origin/main HEAD`..`HEAD` range
    this function used before this fix): `merge-base origin/main HEAD` is
    correct on a feature branch but DEGENERATE when a plan executes directly
    on `main` (this fleet's normal case -- the branch gate refuses to cut a
    feature branch when live peers share the worktree) -- `origin/main`
    advances as peers push, so a session's own already-landed chunk commits
    fall BEHIND that merge-base within minutes, and the range collapses to
    almost nothing. Rungs, tried in order, first one that yields a usable
    base wins:

      1. The plan's own `execution_authorized_sha:` -- attempted as a
         literal commit-ish (`<sha>^{commit}`). Resolves ONLY for a plan hit
         by the historical mis-stamping bug this module's docstring already
         records (a commit sha landed where a plan-body blob hash belonged)
         -- see `_plan_execution_authorized_sha`'s own docstring for why
         this rung is a no-op for an ordinary, correctly-stamped plan.
      2. The earliest commit anywhere in `HEAD`'s history carrying a
         `Deliverable-Id` trailer that canonicalizes equal to this plan's
         own `deliverable_id` (`_first_deliverable_commit_range_base`) --
         NOT `origin/main`-bounded, which is the load-bearing widening this
         fix exists for. Safe because the Deliverable-Id join, not the
         range, is what scopes evidence to THIS plan (see that function's
         own docstring).
      3. `merge-base origin/main HEAD`..`HEAD` -- the pre-fix range,
         preserved as a rung rather than removed: still correct and
         cheapest on an ordinary feature branch, and a safety net when
         `plan_text` is `None` (a caller with no plan text at hand, e.g. a
         direct unit-test call) or carries no `deliverable_id:` at all.
      4. Bare `HEAD` -- the pre-fix fallback for when no merge-base
         resolves at all (a repo with no `origin/main`, or a fresh history).

    Negative-spec: this exists so the evidence query
    (`_committed_chunk_shas`) and the diagnostic that EXPLAINS that
    query's verdict (`_deliverable_id_near_miss_diagnostics`) can never
    drift onto different ranges. A diagnostic that counted commits over a
    wider or narrower range than the oracle it explains would report
    trailer counts the verdict never saw -- a wrong explanation is worse
    than none here, since the whole point of the diagnostic is to be
    trusted at face value. Both callers MUST route through this."""
    if plan_text is not None:
        sha = _plan_execution_authorized_sha(plan_text)
        if sha:
            resolved = _run_git(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], repo_root)
            resolved_sha = (resolved.stdout or "").strip()
            if resolved.returncode == 0 and resolved_sha:
                return [f"{resolved_sha}..HEAD"]

        deliverable_id = _plan_deliverable_id(plan_text)
        base = _first_deliverable_commit_range_base(repo_root, deliverable_id)
        if base is not None:
            if base == "":
                return ["HEAD"]
            return [f"{base}..HEAD"]

    merge_base_result = _run_git(["merge-base", "origin/main", "HEAD"], repo_root)
    base_sha = (merge_base_result.stdout or "").strip()
    if merge_base_result.returncode == 0 and base_sha:
        return [f"{base_sha}..HEAD"]
    return ["HEAD"]


def _chunk_evidence_log_lines(
    repo_root: Path, plan_text: Optional[str] = None
) -> tuple[bool, list[str], list[str]]:
    """Runs the ONE `git log` query every chunk-evidence caller
    (`_committed_chunk_shas`, `_deliverable_id_near_miss_diagnostics`,
    `_hyphen_range_subject_diagnostics`) needs -- same range
    (`_chunk_evidence_log_range`), same
    `%h%x09%s%x09%(trailers:key=Deliverable-Id,valueonly)` format string --
    so the format string itself can never drift between the three call
    sites the way `_chunk_evidence_log_range` already prevents range drift
    (Review: code-reviewer -- Finding 3, 2026-08-05).

    `plan_text` (range-fix, 2026-08-07) is forwarded verbatim to
    `_chunk_evidence_log_range` as its own widening anchor; `None` (a
    caller with no plan text at hand) degrades to the pre-fix range
    exactly -- see that function's own docstring for the full ladder.

    Returns `(query_ok, lines)`. `query_ok` carries the identical BROKEN-
    query distinction every caller already documents (Defect 2(d)):
    `False` ONLY when the git query itself failed (git not on PATH, a
    `git log` non-zero exit) -- callers must check it before trusting
    `lines`, exactly as before this extraction.

    `lines` is one `<short-sha>\\t<subject>\\t<deliverable-id>` line per
    commit (`[]` on a failed query) -- the SAME tab-separated shape the
    three callers' own `split("\\t", 2)` already consumes, so their parsing
    is untouched. Calls `_deliverable_log_records` with its default
    `full_sha=False` (`%h`) deliberately (Review: code-reviewer -- Finding
    2): `committed_shas` and the diagnostics built on this surface have
    always carried the abbreviated sha, and widening that width is a
    surface change of its own, not a side effect of the full-sha fix
    `_first_deliverable_commit_range_base` needed for its own, unrelated,
    `rev-parse ...^` ambiguity concern -- see that function's docstring. It is no longer raw `git log` stdout: since 2026-08-10 the
    underlying query is record-separated (`_deliverable_log_records`) and the
    third field is the RESOLVED join key, which falls back to a message-line
    read when git demoted the trailer to prose -- see `_resolve_deliverable_id`
    for the defect that makes that necessary. Normalizing here rather than at
    each call site is deliberate: all three callers must agree on the join
    key for the same reason `_chunk_evidence_log_range` forces them to agree
    on the range -- a diagnostic that explained a verdict computed from a
    different key would be worse than no diagnostic at all.

    Returns `(query_ok, lines, log_range)` (Review: code-reviewer, 2026-08-10,
    slice D finding P1): `log_range` is the SAME `_chunk_evidence_log_range`
    result this call resolved and queried against, threaded back out so a
    caller needing a second, separately-formatted `git log` over the
    IDENTICAL range (`_session_id_fallback_evidence`, via
    `_committed_chunk_shas`) can pass it in verbatim instead of re-deriving
    it -- `_chunk_evidence_log_range` is a live git query (merge-base /
    earliest-deliverable-commit lookup) whose result depends on repo HEAD
    state at call time, so a second independent call is not provably the
    same range on a machine where a commit can land between the two calls."""
    log_range = _chunk_evidence_log_range(repo_root, plan_text)
    query_ok, records = _deliverable_log_records(repo_root, log_range)
    if not query_ok:
        return False, [], log_range
    return True, [f"{sha}\t{subject}\t{deliverable_id}" for sha, subject, deliverable_id in records], log_range


def _chunk_evidence_range_summary(
    repo_root: Path, plan_text: Optional[str] = None
) -> dict[str, Any]:
    """Human-and-machine-readable facts about the range
    `_chunk_evidence_log_range` actually searched -- the misdirection fix
    named in this module's own bug-backlog entry
    `2026-08-07-close-out-and-stamp-s-chunk-evidence-joi-8b6a7a32d833.yaml`:
    a caller told "commits in range carry a Deliverable-Id trailer, but
    never one equal to this plan's own frontmatter value" (the
    `key_mismatch` reason string) has no way to tell "the range was
    genuinely wide and this really is a key mismatch" apart from "the range
    was a narrow, buggy sliver that happened to contain exactly one
    unrelated commit" without this. Never influences `shipped`/`missing`/
    `join_provenance` -- reporting only, computed from the SAME range
    (`_chunk_evidence_log_range`) and format `_chunk_evidence_log_lines`
    already queries, no second range convention.

    Returns `{"base": str, "commit_count": int}`. `base` is the literal
    lower-bound token `_chunk_evidence_log_range` resolved to (a sha, or
    the string `"HEAD"` when the range is bare `HEAD` -- i.e. the whole
    history). `commit_count` is `0` when the git query itself failed (never
    conflated with "zero commits found" -- a caller already has `query_ok`
    from the evidence query itself to distinguish those; this summary is
    diagnostic-only and degrades to `0` rather than raising)."""
    log_range = _chunk_evidence_log_range(repo_root, plan_text)
    range_token = log_range[0] if log_range else "HEAD"
    base = range_token.split("..", 1)[0] if ".." in range_token else "HEAD"
    query_ok, log_lines, _log_range = _chunk_evidence_log_lines(repo_root, plan_text)
    commit_count = len([line for line in log_lines if line]) if query_ok else 0
    return {"base": base, "commit_count": commit_count}


@dataclasses.dataclass(frozen=True)
class DeliverableJoinStats:
    """Facts about the `Deliverable-Id:` trailer join `_committed_chunk_shas`
    performs, needed by `_determine_shipped` to distinguish an UNATTRIBUTABLE
    git-log result (the join key was never available, or nothing in range
    could be compared against it) from a genuine "nothing shipped" verdict --
    see this module's docstring § Deliverable scoping and the cross-repo
    memo this dataclass exists to close (a no-join-key/no-join-candidates
    result was being reported as an ordinary "N chunk(s) still uncommitted"
    verdict, which reads as a substantive delivery finding even when no join
    was ever possible at all).

    `attempted` -- `True` iff the caller's own `deliverable_id` was truthy,
    i.e. the `if deliverable_id:` guard below was entered at all. `False`
    means the plan carried no `deliverable_id:` frontmatter field, so the
    join was never even attempted -- this is `_determine_shipped`'s
    `"no_join_key"` state.

    `trailered_commit_count` -- how many commits in the SAME `git log` range/
    query this function already runs carried ANY non-empty `Deliverable-Id`
    trailer, regardless of whether that value matched `deliverable_id`. Zero
    here (with `attempted` `True`) is `_determine_shipped`'s `"no_join_
    candidates"` state -- the key was present, but nothing in range could
    ever have been compared against it.

    `matched_commit_count` -- how many of those trailered commits carried a
    value EQUAL to `deliverable_id` (the join's own success condition) AND
    registered at least one usable chunk-id from that commit's subject via
    `_extract_chunk_ids` (cross-repo memo fix, 2026-08-08,
    `trailer-confirmed-and-reporting-shape-accepted` §2: a plan is authored
    through the ceremony surface before its chunks are ever dispatched, so a
    trailered plan-authoring/ceremony commit -- subject never chunk-shaped
    -- is in range on every plan by construction; counting it here reported
    `"joined"` off commits that could never cover a single spine chunk-id,
    masking the true "no chunk-shaped evidence exists" state behind an
    ordinary-looking "N chunk(s) still uncommitted" verdict). Greater than
    zero is `_determine_shipped`'s `"joined"` state; zero (with
    `trailered_commit_count` greater than zero) is its `"key_mismatch"`
    state -- trailered candidates exist, just none of them both matched the
    deliverable id AND named a real chunk-id.

    Computed from the SAME single `git log` call `_committed_chunk_shas`
    already makes for `committed_ids`/`committed_shas` -- no second query."""

    attempted: bool
    trailered_commit_count: int
    matched_commit_count: int


def _plan_claim_holder_session_id(root: Path, plan_path_rel: Optional[str]) -> Optional[str]:
    """The `session_id` file content inside THIS plan's own claim dir --
    `coordinator_core.ops.fleet._common.plan_claim_dir`'s canonical
    `<common_dir>/coordinator-sessions/plan-claims/<plan-path-stem>` path,
    the SAME convention `session.claims.claim_plan`/`claim_artifact("plan",
    ...)` already write and that module's own negative-spec forbids
    hand-rolling a second time -- reused verbatim, not a new plan-identity
    join (Session-Id fallback leg, 2026-08-10, `docs/plans/2026-08-10-a-
    commit-trailer-that-names-the-session.md` C6, finding 0).

    `None` (never a crash, never a guess) when `plan_path_rel` is not
    supplied (a caller with no plan-path context at all -- e.g. a direct
    unit-test call to `_committed_chunk_ids`/`_committed_chunk_shas`, or a
    sibling-repo scan, which never threads this through -- see
    `_committed_chunk_shas`'s own docstring), `git_common_dir` cannot
    resolve (not a git repo), or the claim dir/`session_id` file does not
    exist or cannot be read (no session has ever claimed this plan, or the
    claim was released/reaped). This mirrors the same false-negative-over-
    false-positive posture the rest of this module already commits to: an
    unresolvable claim degrades the Session-Id fallback to "no evidence",
    never a guessed session identity.

    NO LIVENESS CHECK (Review: code-reviewer, Finding P3, 2026-08-10, slice
    D): whatever `session_id` was last written to the claim dir counts as
    "the session holding a claim on this plan" -- no pid check, no
    staleness/reap check. A crashed session's stale, not-yet-reaped claim
    dir can therefore authorize fallback evidence for its own old commits.
    Left unexploited by the fail-closed design elsewhere (matching stays
    scoped to `plan_path_rel` and further gated on `spine_ids` coverage --
    a wrong/stale `session_id` just means the fallback finds no match,
    never a false positive across plans), so this is a caveat, not a fix."""
    if not plan_path_rel:
        return None
    try:
        common_dir = git_common_dir(root)
    except RuntimeError:
        return None
    claim_dir = plan_claim_dir(common_dir, Path(plan_path_rel))
    try:
        value = (claim_dir / "session_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _session_id_fallback_evidence(
    repo_root: Path,
    log_range: list[str],
    claim_holder_sid: str,
    spine_ids: Optional[Iterable[str]],
) -> tuple[set[str], dict[str, str]]:
    """Session-Id-scoped chunk-subject matching -- the fallback leg
    `_committed_chunk_shas` runs ONLY when its own `Deliverable-Id:` join
    found zero evidence for this plan (2026-08-10, `docs/plans/2026-08-10-
    a-commit-trailer-that-names-the-session.md` C6, finding 0; see that
    function's own docstring for the zero-evidence gate this is called
    behind -- this function itself performs NO gating of its own).

    A separate `git log` from `_chunk_evidence_log_lines`'s shared query,
    on purpose: that query's 3-field line shape (`sha\\tsubject\\t
    deliverable-id`) is consumed by THREE other callers via
    `line.split("\\t", 2)` (maxsplit 2) -- widening its format string to
    also carry `Session-Id` would silently fold a fourth field into their
    third, corrupting every one of them. Querying separately, over the
    IDENTICAL `log_range` the Deliverable-Id leg already resolved (passed
    in verbatim, never re-derived -- see this module's docstring § Range
    choice for why two independently-derived ranges must never drift; this
    verbatim-passing is enforced by `_committed_chunk_shas`, which threads
    the range `_chunk_evidence_log_lines` itself resolved straight into this
    call rather than re-invoking `_chunk_evidence_log_range` a second time --
    Review: code-reviewer, Finding P1, 2026-08-10), costs one extra
    `git log` call ONLY on the already-gated zero-evidence path.

    Uses git's own trailer-parsing format directive
    (`%(trailers:key=Session-Id,valueonly)`), no message-line fallback the
    way `_resolve_deliverable_id` needs for `Deliverable-Id`: the trailer-
    demotion defect that fallback exists for only ever pushed the CALLER-
    supplied `Deliverable-Id:` line out of git's last-paragraph trailer scan
    by inserting a blank line ahead of the pipeline's own trailing
    `Commit-Token:`/`Session-Id:` block -- it never demoted `Session-Id`
    itself, which stays inside that last paragraph (see
    `_resolve_deliverable_id`'s own docstring for the defect this
    distinguishes from).

    A commit counts as fallback evidence only when its resolved
    `Session-Id` trailer value equals `claim_holder_sid` (the session
    `_plan_claim_holder_session_id` found currently holding a claim on this
    plan) AND `_extract_chunk_ids(subject, spine_ids)` -- reused VERBATIM,
    already bounded to cover one of `spine_ids` via
    `_committed_id_covers_spine_id` internally, never re-cut here --
    registers at least one chunk-id from the subject. Returns
    `(committed_ids, committed_shas)`, the same shape
    `_committed_chunk_shas`'s own Deliverable-Id leg produces, ready to
    union straight in. A broken `git log` (git not on PATH, non-zero exit)
    degrades to `(set(), {})` -- no fallback evidence, never a crash."""
    result = _run_git(
        [
            "log",
            "--format="
            + _LOG_RECORD_SEP
            + "%h"
            + _LOG_FIELD_SEP
            + "%s"
            + _LOG_FIELD_SEP
            + "%(trailers:key=Session-Id,valueonly)",
            *log_range,
        ],
        repo_root,
    )
    if result.returncode != 0:
        return set(), {}

    committed: set[str] = set()
    committed_shas: dict[str, str] = {}
    for raw_record in (result.stdout or "").split(_LOG_RECORD_SEP):
        if not raw_record.strip():
            continue
        fields = raw_record.split(_LOG_FIELD_SEP, 2)
        if len(fields) < 3:
            continue
        sha = fields[0].strip()
        if not sha:
            continue
        subject = fields[1]
        session_id_value = fields[2].strip()
        if not session_id_value or session_id_value != claim_holder_sid:
            continue
        for chunk_id in _extract_chunk_ids(subject, spine_ids):
            committed.add(chunk_id)
            committed_shas.setdefault(chunk_id, sha)
    return committed, committed_shas


def _committed_chunk_shas(
    repo_root: Path,
    deliverable_id: Optional[str],
    spine_ids: Optional[Iterable[str]] = None,
    plan_text: Optional[str] = None,
    plan_path_rel: Optional[str] = None,
) -> tuple[bool, set[str], dict[str, str], DeliverableJoinStats]:
    """Chunk-ids with a landed commit BELONGING TO THIS PLAN, PLUS the
    covering commit's own (abbreviated) sha per id -- a one-capture-group
    extension of `_committed_chunk_ids` (AC8's own framing: "one
    capture-group extension of existing code, not new machinery"). Same
    query, same range, same `_extract_chunk_ids` convention; the sha is
    simply the leading `%h` token on each matching line, kept alongside
    the id it was extracted from. `spine_ids` (Defect fix, 2026-08-01,
    optional) is the plan's own `## Tasks` spine ids, forwarded to every
    `_extract_chunk_ids` call below as its multi-id-split bounding set --
    see that function's docstring for why this replaced the prior static
    `^C\\d`-only shape gate.

    Deliverable-scoped (Defect fix, 2026-07-27 -- see this module's
    docstring § Deliverable scoping): `deliverable_id` is the CLOSING
    plan's own frontmatter `deliverable_id:` value
    (`_plan_deliverable_id`). A commit's subject-derived chunk-id(s) are
    only added to `committed_ids`/`committed_shas` when that commit's own
    `Deliverable-Id:` git trailer equals `deliverable_id` exactly --
    read via git's own trailer-parsing format directive
    (`%(trailers:key=Deliverable-Id,valueonly)`), never a hand-rolled
    regex over the commit body. When `deliverable_id` is `None` (the
    plan carries no `deliverable_id:` frontmatter field at all), NOTHING
    is ever added -- the conservative choice documented in this module's
    docstring, not a silent fallback to unscoped subject-matching.

    Join-key canonicalization (2026-08-04, `state/deliverable-equivalence.
    yaml` wiring): both sides of the equality check -- the plan's own
    `deliverable_id` and each commit's `Deliverable-Id` trailer value --
    are passed through `coordinator_core.ops.deliverable_equivalence.
    canonicalize()` before comparison, so a declared fork pair (one
    `deliverable_id` re-minted for the same underlying work, per DR-207
    D1) still joins. This is a JOIN-KEY transform only, applied at the
    existing exact-equality comparison point -- it does NOT widen chunk-id
    subject matching (see this module's docstring § "Deliverable scoping"
    and the 2026-07-27 false-positive incident that section records; that
    scoping is untouched) and it does NOT write a canonicalized value back
    to any artifact (`deliverable_equivalence.py`'s own negative-spec). A
    pair absent from the map canonicalizes to itself, i.e. today's raw
    comparison, unchanged.

    `plan_text` (range-fix, 2026-08-07) is forwarded verbatim to
    `_chunk_evidence_log_lines`/`_chunk_evidence_log_range` as the range-
    widening anchor -- `None` degrades to the pre-fix range exactly.

    Returns `(query_ok, committed_ids, committed_shas, join_stats)`.
    `committed_shas` maps each committed id to the MOST RECENT covering
    commit's sha (`git log` lists newest-first, and `dict.setdefault` keeps
    the first -- i.e. newest -- sha seen per id); every key in
    `committed_shas` is also a member of `committed_ids`, and vice versa.
    `query_ok` carries the identical BROKEN-query distinction
    `_committed_chunk_ids` documents (Defect 2(d)) -- unaffected by
    deliverable scoping: a `None`/absent `deliverable_id` still runs the
    query (so a genuinely broken git-log is still distinguishable from
    "nothing to attribute"), it just never populates the result from it.
    `join_stats` is a `DeliverableJoinStats` (see that dataclass's own
    docstring) carrying the join-provenance facts `_determine_shipped`
    needs to distinguish an UNATTRIBUTABLE result from a genuine
    "nothing shipped" one -- computed from this SAME query, never a second
    `git log` call. On a broken query (`query_ok` `False`), `join_stats` is
    a zeroed placeholder (`attempted` still reflects whether `deliverable_id`
    was truthy; the two counts are `0` since no commits were ever read) --
    callers must check `query_ok` first, exactly as they already do.

    Session-Id-scoped fallback (2026-08-10, `docs/plans/2026-08-10-a-
    commit-trailer-that-names-the-session.md` C6, finding 0): when the
    Deliverable-Id join above finds ZERO evidence for this plan
    (`join_stats.matched_commit_count == 0` -- determined from the counts
    this function already computed above, no re-query for the GATE itself),
    `committed`/`committed_shas` degrade to a second join --
    `_session_id_fallback_evidence` -- bounded to `spine_ids` and gated on a
    commit's `Session-Id:` trailer naming the session
    `_plan_claim_holder_session_id` finds currently holding a claim on THIS
    plan (`plan_path_rel`, optional -- `None` skips the fallback entirely,
    degrading to the pre-fix zero-evidence result exactly). ZERO-EVIDENCE-
    GATED, NOT A GENERAL WIDENING: when `matched_commit_count > 0` this
    fallback never runs at all, and the exact-equality Deliverable-Id path
    above stands completely untouched -- this is the whole safety argument
    (see this module's docstring § Deliverable scoping and its 2026-07-27
    `C8b` false-positive incident: an unconditional Session-Id join would
    re-admit the identical cross-plan chunk-id bleed that incident exists to
    keep out). The gate is PER-PLAN (zero evidence for THIS plan's
    `deliverable_id`), never per-commit or per-chunk-id. Same false-
    negative-over-false-positive posture as everywhere else in this module:
    a fallback that cannot resolve a claim, or whose own `git log` query
    fails, degrades to no fallback evidence -- never a crash, never a
    guess."""
    query_ok, log_lines, log_range = _chunk_evidence_log_lines(repo_root, plan_text)
    if not query_ok:
        return (
            False,
            set(),
            {},
            DeliverableJoinStats(
                attempted=bool(deliverable_id),
                trailered_commit_count=0,
                matched_commit_count=0,
            ),
        )

    equivalence_map = load_equivalence_map(repo_root)
    canonical_deliverable_id = canonicalize(deliverable_id, equivalence_map)

    committed: set[str] = set()
    committed_shas: dict[str, str] = {}
    trailered_commit_count = 0
    matched_commit_count = 0
    for line in log_lines:
        parts = line.split("\t", 2)
        if len(parts) < 2 or not parts[0]:
            continue
        sha = parts[0]
        subject = parts[1]
        trailer_value = parts[2].strip() if len(parts) > 2 else ""
        if trailer_value:
            trailered_commit_count += 1
        canonical_trailer_value = canonicalize(trailer_value, equivalence_map) if trailer_value else trailer_value
        if not deliverable_id or canonical_trailer_value != canonical_deliverable_id:
            continue
        subject_chunk_ids = _extract_chunk_ids(subject, spine_ids)
        if not subject_chunk_ids:
            continue
        matched_commit_count += 1
        for chunk_id in subject_chunk_ids:
            committed.add(chunk_id)
            committed_shas.setdefault(chunk_id, sha)

    join_stats = DeliverableJoinStats(
        attempted=bool(deliverable_id),
        trailered_commit_count=trailered_commit_count,
        matched_commit_count=matched_commit_count,
    )

    # Session-Id-scoped fallback (2026-08-10, plan C6, finding 0) -- see
    # this function's own docstring. ZERO-EVIDENCE-GATED: `committed` is
    # guaranteed empty here whenever `matched_commit_count == 0` (both are
    # only ever incremented together in the loop above), so this can only
    # ADD evidence a genuine zero-evidence query never found, never touch or
    # override an existing exact-equality match.
    if matched_commit_count == 0 and plan_path_rel:
        claim_holder_sid = _plan_claim_holder_session_id(repo_root, plan_path_rel)
        if claim_holder_sid:
            # Review: code-reviewer -- Finding P1, 2026-08-10, slice D. `log_range`
            # is the SAME `_chunk_evidence_log_range` result the Deliverable-Id
            # leg above queried against (threaded out of `_chunk_evidence_log_lines`
            # itself), passed in verbatim rather than re-derived: a second,
            # independent `_chunk_evidence_log_range` call is a live git query
            # whose result can shift if a commit lands between the two calls.
            fallback_committed, fallback_shas = _session_id_fallback_evidence(
                repo_root, log_range, claim_holder_sid, spine_ids
            )
            committed |= fallback_committed
            for chunk_id, sha in fallback_shas.items():
                committed_shas.setdefault(chunk_id, sha)

    return True, committed, committed_shas, join_stats


def _deliverable_id_near_miss_diagnostics(
    repo_root: Path,
    deliverable_id: Optional[str],
    missing_chunk_ids: list[str],
    plan_text: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Diagnostic-only, unhappy-path helper: names the CAUSE when
    `_committed_chunk_shas`'s exact-equality `Deliverable-Id:` join reports
    zero/under-counted evidence for a plan whose chunks actually shipped.

    Defect this closes (2026-08-01 -- see `coordinator_core/contract/
    commit-trailer-producer-contract.md` § 1.2, and `docs/decisions/
    DR-207-deliverable-spine-initiative-entity.md` D1): claude-klabauter has TWO
    independent producers of the `Deliverable-Id:` FK --
    `coordinator/bin/coordinator-prepare-commit-msg` derives it from the
    handoff/baton chain's `pickup.deliverable_id`, while `commit.anchors`
    derives it from staged plan frontmatter -- and nothing reconciles them,
    so a plan's own frontmatter `deliverable_id:` can disagree BY VALUE
    with the trailer its own commits actually carry. Under that mismatch,
    `_committed_chunk_shas`'s deliberate exact-equality join (see this
    module's docstring § Deliverable scoping -- NOT relaxed by this fix)
    correctly reports zero/under-counted evidence, but the caller-facing
    `missing_chunk_ids` output NAMES THE MISSING CHUNKS -- a symptom that
    points the reader at the wrong layer entirely (two live instances,
    `docs/plans/2026-08-01-percolate-root-rung-ordering.md` and
    `docs/plans/2026-07-31-exec-cli-posix-leg-convergence.md`, both
    fully-shipped plans whose oracle read as zero-shipped for this exact
    reason). This function re-scans the SAME git-log range/format
    `_committed_chunk_shas` already queries and reports which OTHER
    `Deliverable-Id` trailer value(s) this branch's chunk-shaped commits
    actually carry, so the caller can name both values in one sentence
    instead of a reader re-deriving the cause from a `missing` list by
    hand.

    Diagnostic-only -- never called on the happy path (see
    `close_out_and_stamp`'s own call site, gated on `missing` being
    non-empty), and never changes `_committed_chunk_shas`'s join semantics
    or verdict; this only explains an ALREADY-DECIDED zero/under-count.

    A candidate trailer value only counts here when it appears on a commit
    whose subject-derived chunk-id actually COVERS one of the ids still
    reported missing (`_committed_id_covers_spine_id`, the same matcher
    `_determine_shipped` uses to decide coverage). Intersecting against
    `missing_chunk_ids` rather than merely requiring a chunk-shaped subject
    is load-bearing for precision: `_extract_chunk_ids` deliberately
    registers the single leading token of ANY `<token>: <prose>` subject
    (its own docstring says so -- `fix: whatever` contributes `fix`), so a
    subject-shape-only gate would count ordinary `fix:`/`ceremony:`/
    `memo:` commits as near-misses and could name a wholly unrelated
    deliverable as the cause. A diagnostic that confidently names the
    wrong id is worse than none, since its entire purpose is to be trusted
    at face value. `deliverable_id` itself, and any empty/absent trailer
    value, are excluded -- neither is a "near miss", the first because it
    already counts as real evidence and the second because it means "no
    trailer at all" (the untrailered/pre-convention case this module's
    docstring already treats as a deliberate false negative, not a
    mismatch).

    Corollary worth stating, since it bounds what this diagnostic can and
    cannot explain: a plan whose commits never named chunk-ids in their
    subjects at all has no near-miss to report here, and correctly gets
    `[]`. Its close-out miss has a DIFFERENT cause (subject convention not
    followed), and silently attributing that to an id mismatch is exactly
    the misdirection this function exists to end.

    Passes `missing_chunk_ids` itself as `_extract_chunk_ids`'s `spine_ids`
    bounding set (Defect fix, 2026-08-01) -- the exact ids this diagnostic
    already intersects the extracted subject-ids against below, so binding
    the multi-id split to that same set costs nothing and, unlike the old
    static `^C\\d`-only shape gate, also surfaces a near-miss for a plan
    whose real spine ids are not `C`-prefixed.

    Join-key canonicalization (2026-08-04, `state/deliverable-equivalence.
    yaml` wiring): the exclusion at the `trailer_value == deliverable_id`
    check below is canonicalized through the same `deliverable_equivalence.
    canonicalize()` map `_committed_chunk_shas` now joins on, so a trailer
    value that is a DECLARED fork of `deliverable_id` (i.e. already joins
    as real evidence over there) is correctly excluded here too, rather
    than still being reported as a near-miss candidate after the join
    that resolves it. Join-key transform only -- see `_committed_chunk_
    shas`'s own docstring for the full negative-spec this mirrors.

    Returns a list of `{"deliverable_id": str, "commit_count": int}` dicts,
    one per DISTINCT near-miss trailer value, sorted by `commit_count`
    descending (ties broken by value) so the caller can name the most
    plausible candidate first. `[]` when `deliverable_id` is falsy (nothing
    to compare against -- mirrors `_committed_chunk_shas`'s own posture for
    an absent `deliverable_id`), when the git query itself fails, or when
    no near-miss candidate exists at all.

    `plan_text` (range-fix, 2026-08-07) is forwarded verbatim to
    `_chunk_evidence_log_lines` so this diagnostic always scans the
    IDENTICAL range `_committed_chunk_shas` scanned for the verdict it
    explains -- see `_chunk_evidence_log_range`'s own negative-spec."""
    if not deliverable_id:
        return []

    query_ok, log_lines, _log_range = _chunk_evidence_log_lines(repo_root, plan_text)
    if not query_ok:
        return []

    equivalence_map = load_equivalence_map(repo_root)
    canonical_deliverable_id = canonicalize(deliverable_id, equivalence_map)

    counts: dict[str, int] = {}
    for line in log_lines:
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        subject = parts[1]
        trailer_value = parts[2].strip()
        if not trailer_value or canonicalize(trailer_value, equivalence_map) == canonical_deliverable_id:
            continue
        subject_ids = _extract_chunk_ids(subject, missing_chunk_ids)
        if not any(
            _committed_id_covers_spine_id(subject_id, missing_id)
            for subject_id in subject_ids
            for missing_id in missing_chunk_ids
        ):
            continue
        counts[trailer_value] = counts.get(trailer_value, 0) + 1

    return [
        {"deliverable_id": value, "commit_count": count}
        for value, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _hyphen_range_subject_diagnostics(
    repo_root: Path,
    deliverable_id: Optional[str],
    missing_chunk_ids: list[str],
    plan_text: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Diagnostic-only, unhappy-path helper: names a THIRD cause (distinct
    from the two `_deliverable_id_near_miss_diagnostics` already covers) a
    reader must be told about when `missing_chunk_ids` is non-empty -- a
    commit subject that used `-` as its id-LIST separator.

    Defect this closes (2026-08-05, live incident): an EM committed four
    genuinely-shipped chunks under one commit subject shaped `C1-C4: ...`.
    `_extract_chunk_ids` recognizes only `,`/`+`/`/` as multi-id separators
    (see that function's own docstring for the corpus evidence behind that
    exact set, and why `-` is deliberately EXCLUDED from it -- real,
    recurring corpus subjects use `-` INSIDE compound chunk tags,
    `DOCTRINE-C7a`/`RESIDUE-C9`/`RESIDUE-C1..C7`, and admitting `-` as a
    separator would shatter every one of those into bogus fragments). So
    `C1-C4:` goes down the SINGLE-id path with `raw = "C1-C4"`, which then
    fails `_committed_id_covers_spine_id` against every real spine id
    (`_DASH_TAG_SUFFIX_RE` requires a LOWERCASE letter immediately after
    the dash -- `-C4` does not match it). Net effect: four shipped chunks
    read as missing, and NOTHING in the returned JSON said why -- this
    module's other diagnostic, `_deliverable_id_near_miss_diagnostics`,
    correctly declines to explain this (its own docstring's "Corollary
    worth stating" scopes it to trailer-VALUE mismatches, a different
    cause entirely).

    THIS FUNCTION DOES NOT, AND MUST NEVER, CHANGE `_extract_chunk_ids`'S
    SEPARATOR SET, THE COVERAGE JOIN, OR THE SHIPPED/HALTED VERDICT. Do NOT
    "fix" this by adding `-` to `_extract_chunk_ids`'s split characters --
    that is the actively wrong move this function exists to make
    unnecessary; see the paragraph above and `_extract_chunk_ids`'s own
    docstring for why the conservative separator set must survive. `C1-C4:`
    genuinely registers nothing under this oracle, and it SHOULD keep
    registering nothing -- a range is not the same thing as an explicit
    id-list, and crediting `C2`/`C3` (which the subject never names as
    discrete tokens) would be exactly the over-crediting `_extract_chunk_
    ids`'s own docstring refuses. This is diagnostic-only, exactly like its
    sibling: it explains an already-decided `missing` count, it never
    alters it.

    Detection rule -- deliberately narrow, same "confidently naming a wrong
    cause is worse than naming none" posture `_deliverable_id_near_miss_
    diagnostics` already commits to:

      1. Re-run the SAME git-log query (`_chunk_evidence_log_range`, the
         same range/format string `_committed_chunk_shas` and the near-miss
         diagnostic both already use -- never a second range convention).
      2. For each commit subject, take its leading `<token>:` via the SAME
         `_CHUNK_SUBJECT_RE` `_extract_chunk_ids` itself uses. A subject
         whose leading token already contains a RECOGNIZED separator
         (`,`/`+`/`/`) is skipped outright -- that subject already takes
         the existing multi-id path (correctly, or via the near-miss
         diagnostic if it's a Deliverable-Id issue); this diagnostic exists
         ONLY for the pure single-id path a bare `-` silently starves.
      3. Split that leading token on `-`. Fewer than two components means
         there was no range shape to begin with -- skip.
      4. Fire ONLY when EVERY component covers a DISTINCT spine id still
         present in `missing_chunk_ids` (`_committed_id_covers_spine_id`,
         the identical matcher `_determine_shipped` uses for coverage) --
         `DOCTRINE-C7a` -> `["DOCTRINE", "C7a"]`, `DOCTRINE` covers nothing,
         does NOT fire; `RESIDUE-C9` -> `["RESIDUE", "C9"]`, `RESIDUE`
         covers nothing, does NOT fire; `C1-C4` -> `["C1", "C4"]`, both
         cover distinct still-missing spine ids, FIRES. This is load-
         bearing precision, not a shape-only gate: a compound dash-tag
         whose first component happens to be prose-shaped (`DOCTRINE`,
         `RESIDUE`) must never be confidently reported as "a hyphen-range
         subject" just because a `-` is present at all.
      5. Deliverable-Id-scoped exactly like the real join (see this
         module's docstring § Deliverable scoping): a candidate commit only
         counts here when its own `Deliverable-Id` trailer canonicalizes
         (via `deliverable_equivalence.canonicalize()`, the SAME map/seam
         `_committed_chunk_shas` already joins on) EQUAL to this plan's own
         `deliverable_id` -- never a mismatch, unlike the sibling near-miss
         diagnostic, which looks for the OPPOSITE (a candidate value that
         does NOT match). A hyphen-range subject that genuinely belongs to
         a DIFFERENT plan reusing the same short spine-id shape (`C1`..`C4`
         are reused by convention across every plan on the branch) must
         never be reported against THIS plan just because its subject
         shape happens to match -- exactly the cross-plan collision this
         module's docstring already records once (§ Deliverable scoping).
         `deliverable_id` falsy (`None`/empty) returns `[]` immediately --
         nothing to scope the search against, and reporting unscoped here
         would reintroduce that exact collision.

    Returns a list of `{"sha": str, "subject": str, "spanned_chunk_ids":
    [str, ...]}` dicts, one per offending commit, in `git log` order
    (newest first) -- `spanned_chunk_ids` is the plan's own spine ids the
    range appears to span, in split order (each id used at most once across
    the subject's own components, per the distinctness bar above). `[]`
    when `deliverable_id` is falsy, the git query fails, or no offending
    commit is found -- mirrors `_deliverable_id_near_miss_diagnostics`'s
    own empty-result posture.

    `plan_text` (range-fix, 2026-08-07) is forwarded verbatim to
    `_chunk_evidence_log_lines` so this diagnostic scans the IDENTICAL
    range the oracle it explains scanned -- see `_chunk_evidence_log_range`'s
    own negative-spec."""
    if not deliverable_id:
        return []

    query_ok, log_lines, _log_range = _chunk_evidence_log_lines(repo_root, plan_text)
    if not query_ok:
        return []

    equivalence_map = load_equivalence_map(repo_root)
    canonical_deliverable_id = canonicalize(deliverable_id, equivalence_map)

    offenders: list[dict[str, Any]] = []
    for line in log_lines:
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        sha, subject = parts[0], parts[1]
        trailer_value = parts[2].strip()
        if not trailer_value:
            continue
        if canonicalize(trailer_value, equivalence_map) != canonical_deliverable_id:
            continue

        match = _CHUNK_SUBJECT_RE.match(subject)
        if not match:
            continue
        raw = match.group(1)
        if any(sep in raw for sep in (",", "+", "/")):
            continue
        components = raw.split("-")
        if len(components) < 2:
            continue

        # Review: code-reviewer -- Finding 2: two-pass assignment, exact
        # matches first. A single greedy pass picked whichever candidate
        # appeared first in `missing_chunk_ids` regardless of whether it
        # was an exact match or a suffix-derived one, so a component that
        # exactly matches one missing id could be assigned to a DIFFERENT
        # missing id it also covers via the sub-chunk/dash-tag suffix
        # rule, starving a later component that needed the exact match
        # and had no substitute -- aborting the whole detection
        # (`spanned = []`) even though a valid one-to-one assignment
        # existed. Pass 1 reserves every exact match first; pass 2 then
        # greedily assigns the remaining components from what's left.
        component_assignment: dict[int, str] = {}
        used_missing_ids: set[str] = set()
        for idx, component in enumerate(components):
            if component in missing_chunk_ids and component not in used_missing_ids:
                component_assignment[idx] = component
                used_missing_ids.add(component)

        spanned_ok = True
        for idx, component in enumerate(components):
            if idx in component_assignment:
                continue
            covered = [
                missing_id
                for missing_id in missing_chunk_ids
                if missing_id not in used_missing_ids
                and _committed_id_covers_spine_id(component, missing_id)
            ]
            if not covered:
                spanned_ok = False
                break
            component_assignment[idx] = covered[0]
            used_missing_ids.add(covered[0])

        spanned = (
            [component_assignment[idx] for idx in range(len(components))]
            if spanned_ok
            else []
        )

        if spanned:
            offenders.append(
                {"sha": sha, "subject": subject, "spanned_chunk_ids": spanned}
            )

    return offenders


#: The pre-spine legacy delivery record, still live on ~23 real plans on this
#: branch (2026-08-06 census -- see this module's docstring §
#: `Dispatch Ledger` fallback for the exact count and how it was taken):
#: a hand-authored `## Dispatch Ledger` markdown table whose `status` column
#: carries a literal `committed <sha>` cell per row once that row's work
#: lands, predating the `## Tasks` machine-parseable spine convention
#: entirely. `_parse_spine_rows`'s own ABSENT branch (D7) reads a missing
#: spine as "no per-chunk oracle to check completeness against" and treats
#: it as full-shipped UNCONDITIONALLY -- correct for a plan genuinely too
#: early/small to ever carry a spine, but WRONG for one of these 23: it
#: means `close_out_and_stamp` would stamp `implemented` on a plan whose own
#: Dispatch Ledger might show unfinished, parked, or never-dispatched rows,
#: without ever reading that ledger at all (verified live, this fix:
#: `close_out_and_stamp('docs/plans/2026-07-02-ccos-6-rehome-attribution-
#: python.md', dry_run=True)` returned `shipped: true` under the OLD
#: unconditional-bypass behavior purely because the spine was absent -- the
#: Dispatch Ledger's own 7 `committed <sha>` rows were never consulted).
#:
#: `_dispatch_ledger_delivered` below closes that hole: an ABSENT spine no
#: longer bypasses the oracle -- it reroutes to a SECOND, narrower oracle
#: that reads the plan's own Dispatch Ledger table instead. Reuses
#: `locate_fenced_block` (the SAME locate seam `_parse_spine_rows` already
#: calls) to detect the ABSENT case in the first place -- this is a
#: FALLBACK path only, reached exclusively when a `## Tasks` spine could not
#: be located at all; a plan with a real (even if MALFORMED) spine never
#: reaches this code, spine-present always wins.
#:
#: Conservative by construction, same failure direction as everywhere else
#: in this module (false-negative over false-positive): a plan with no
#: `## Dispatch Ledger` heading at all, a heading with no parseable table,
#: a table missing a recognizable `chunk-id`/`status` column pair, or ANY
#: row whose `status` cell is not exactly `committed <sha>` (a bare `sha`
#: that does not resolve via `git cat-file -e` in THIS repo counts as NOT
#: committed, same as a missing cell) is reported NOT-SHIPPED. There is no
#: "mostly parseable, assume the rest" path -- an ambiguous ledger returns
#: not-shipped, never a guess.
#: Widened (Defect fix, false-positive-stamp incident) to tolerate a
#: trailing suffix after the heading text itself -- a real corpus heading
#: (`## Dispatch Ledger — claude-klabauter [M] slice`) never matched the old
#: exact-line anchor, so that plan's ABSENT-spine, present-ledger case fell
#: all the way through to `_determine_shipped`'s no-evidence branch instead
#: of being read by `_dispatch_ledger_delivered`. Matches only the heading
#: TEXT, deliberately not `_parse_dispatch_ledger_table`'s own row/column
#: matching -- widening those is explicitly out of scope for this fix.
#: Review: coordinator:code-reviewer -- requires a separator before the
#: suffix so `## Dispatch LedgerFooBar` (no space/dash) doesn't also match;
#: `.*` alone was looser than the stated "tolerate a trailing suffix" intent.
_DISPATCH_LEDGER_HEADING_RE = re.compile(r"^## Dispatch Ledger(\s.*)?$", re.MULTILINE)
_DISPATCH_LEDGER_NEXT_HEADING_RE = re.compile(r"^## ", re.MULTILINE)
_DISPATCH_LEDGER_COMMITTED_RE = re.compile(r"^committed\s+([0-9a-fA-F]{6,40})\b")


def _dispatch_ledger_section(plan_text: str) -> Optional[str]:
    """Slices the plan text from a `## Dispatch Ledger` heading (if any) up
    to (excluding) the next `## ` heading, or end-of-document -- `None` when
    no such heading exists at all. Pure text slicing, no table parsing."""
    heading_match = _DISPATCH_LEDGER_HEADING_RE.search(plan_text)
    if heading_match is None:
        return None
    section_start = heading_match.end()
    next_match = _DISPATCH_LEDGER_NEXT_HEADING_RE.search(plan_text, section_start)
    section_end = next_match.start() if next_match is not None else len(plan_text)
    return plan_text[section_start:section_end]


def _parse_dispatch_ledger_table(
    section_text: str,
) -> tuple[Optional[list[dict[str, str]]], Optional[str]]:
    """Parses the FIRST markdown pipe-table found in `section_text` into
    `[{"chunk_id": str, "status_cell": str}, ...]`, keyed off the table's
    OWN header row (`chunk-id`/`status` columns, matched case-insensitively)
    rather than a fixed column index -- the real corpus's Dispatch Ledger
    tables do not all order columns identically. Returns `(None, reason)`
    on anything this parser cannot confidently read: no table found under
    the heading, a header missing either required column, a second
    (separator) row that is not a `-`/`:`-only markdown separator shape, or
    a data row with fewer cells than the header promises. Never guesses a
    partial parse into a verdict -- see this module's own conservatism note
    above."""
    lines = [line.strip() for line in section_text.splitlines()]
    table_lines: list[str] = []
    started = False
    for line in lines:
        if line.startswith("|") and line.endswith("|") and len(line) >= 2:
            table_lines.append(line)
            started = True
        elif started:
            break
    if len(table_lines) < 3:
        return None, "no Dispatch Ledger table (header + separator + >=1 row) found"

    header_cells = [c.strip().lower() for c in table_lines[0].strip("|").split("|")]
    if "chunk-id" not in header_cells or "status" not in header_cells:
        return None, "Dispatch Ledger table header has no chunk-id/status columns"
    chunk_idx = header_cells.index("chunk-id")
    status_idx = header_cells.index("status")

    # Review: coordinator:code-reviewer -- `table_lines[1]` is assumed to be
    # the markdown header/data separator row purely by position. Validate
    # its shape before skipping it; a real data row landing there (a
    # separator-less or differently-shaped table) must fail loud rather
    # than be silently dropped, per this parser's own "never guesses a
    # partial parse into a verdict" posture.
    separator_cells = [c.strip() for c in table_lines[1].strip("|").split("|")]
    if not separator_cells or not all(
        cell and set(cell) <= {"-", ":"} for cell in separator_cells
    ):
        return None, f"Dispatch Ledger table has no header/data separator row: {table_lines[1]!r}"

    rows: list[dict[str, str]] = []
    for line in table_lines[2:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) <= max(chunk_idx, status_idx):
            return None, f"malformed Dispatch Ledger row (too few columns): {line!r}"
        chunk_id = cells[chunk_idx]
        if not chunk_id:
            return None, f"Dispatch Ledger row has an empty chunk-id: {line!r}"
        rows.append({"chunk_id": chunk_id, "status_cell": cells[status_idx]})
    if not rows:
        return None, "Dispatch Ledger table has no data rows"
    return rows, None


def _dispatch_ledger_delivered(
    plan_text: str, repo_root: Path
) -> tuple[bool, list[str], Optional[str]]:
    """The legacy-format oracle (see this module's own § Dispatch Ledger
    fallback comment block above `_dispatch_ledger_heading_re`): returns
    `(is_shipped, missing_chunk_ids, error)`. `error` set (other fields
    meaningless) means the ledger could not be read at all -- no heading, no
    table, or an unrecognizable header -- which callers treat as NOT-SHIPPED
    (conservative), distinct from a genuine per-row gap only for diagnostic
    messaging.

    A row counts as delivered ONLY when its `status` cell matches
    `committed <sha>` (optionally followed by trailing prose, e.g.
    `committed ed6c513d7 (EM-inline)` -- a real corpus shape) AND that `sha`
    resolves to a real object in `repo_root`'s history via
    `git cat-file -e` AND `git merge-base --is-ancestor <sha> HEAD` proves
    that object is reachable from `HEAD` -- a ledger citing a SHA that does
    not exist in this repo (a typo, a sha from a different repo/clone, a
    fabricated value) OR that exists but was never landed on this branch
    (a dangling, rebased-away, or fetched-but-unmerged commit) does NOT
    count, exactly the same anti-self-attestation posture
    `_verify_disposition_ref` already applies to the spine-based oracle's
    own second evidence path. Any other status text (`ready — not yet
    dispatched`, `identified — not yet dispatched`, `PARKED`, blank, ...)
    is NOT delivered -- this function does not attempt to special-case or
    exclude those rows the way the spine oracle excludes
    `spun_off`/`backlogged`/`wont_do` rows via `disposition`; the Dispatch
    Ledger format has no equivalent field, so every ledger row is
    commit-required by construction."""
    section = _dispatch_ledger_section(plan_text)
    if section is None:
        return False, [], "no '## Dispatch Ledger' heading found in plan"
    rows, error = _parse_dispatch_ledger_table(section)
    if error is not None:
        return False, [], error

    missing: list[str] = []
    for row in rows:
        match = _DISPATCH_LEDGER_COMMITTED_RE.match(row["status_cell"])
        if match is None:
            missing.append(row["chunk_id"])
            continue
        sha = match.group(1)
        # Review: coordinator:code-reviewer -- cat-file -e alone only proves
        # the object exists in the object store, not that HEAD ever reached
        # it (dangling/abandoned-branch/never-merged commits pass). Mirror
        # `_verify_disposition_ref`'s two-stage check: existence, THEN
        # `merge-base --is-ancestor` reachability from HEAD.
        verify = _run_git(["cat-file", "-e", sha], repo_root)
        if verify.returncode != 0:
            missing.append(row["chunk_id"])
            continue
        ancestor = _run_git(["merge-base", "--is-ancestor", sha, "HEAD"], repo_root)
        if ancestor.returncode != 0:
            missing.append(row["chunk_id"])
    return (len(missing) == 0), missing, None


#: Fourth join-provenance value, ledger-fallback-specific -- see
#: `_determine_shipped`'s own routing to `_dispatch_ledger_delivered` when
#: the spine is ABSENT. Not one of the Deliverable-Id join's own four
#: states (`DeliverableJoinStats` is never computed on this path at all --
#: the ledger fallback has no `Deliverable-Id:` trailer join to report on),
#: so it is deliberately a FIFTH distinct string, not a reuse of
#: `JOIN_PROVENANCE_KEY_MISMATCH` or any existing value.
JOIN_PROVENANCE_LEDGER_FALLBACK = "ledger_fallback"


def _all_spine_ids(spine_rows: list[Any]) -> list[str]:
    """Every id the plan's own spine names, regardless of `disposition`/
    `deferred` -- the full candidate set `_extract_chunk_ids` bounds its
    multi-id subject split against (Defect fix, 2026-08-01: see that
    function's and `_CHUNK_ID_SHAPE_RE`'s own docstrings). Deliberately
    WIDER than `_commit_required_chunk_ids`'s own filtered subset: a
    `spun_off`/`backlogged`/legacy-`deferred` row is still a REAL spine id
    that a commit subject may legitimately reference (e.g. alongside
    commit-required ids in the same compound subject), and excluding it
    from the bounding set would only reintroduce a narrower version of the
    same false-negative this fix closes, for no false-positive benefit --
    a row not in `_commit_required_chunk_ids` is already never consulted
    for the missing/shipped verdict regardless of whether it appears here."""
    ids: list[str] = []
    for row in spine_rows:
        if not isinstance(row, dict):
            continue
        chunk_id = row.get("id")
        if chunk_id:
            ids.append(str(chunk_id))
    return ids


#: The four join-provenance states `_determine_shipped` can report alongside
#: `missing_chunk_ids` -- see `DeliverableJoinStats`'s own docstring for the
#: facts each is derived from. Named here as a single source of truth for
#: the literal strings, since `close_out_and_stamp` and
#: `coordinator_core.workstream_complete` both branch on them by value.
JOIN_PROVENANCE_JOINED = "joined"
JOIN_PROVENANCE_NO_JOIN_KEY = "no_join_key"
JOIN_PROVENANCE_NO_JOIN_CANDIDATES = "no_join_candidates"
JOIN_PROVENANCE_KEY_MISMATCH = "key_mismatch"

#: Sixth join-provenance value (false-positive-stamp incident, 2026-08-06):
#: the D7 no-spine/no-ledger branch (see `_determine_shipped`'s own "genuinely
#: pre-spine, pre-ledger case" comment) performs ZERO evidence lookups -- no
#: spine parse, no git-log query, no Deliverable-Id join, no Dispatch Ledger
#: read -- because there is nothing on the plan to consult in the first
#: place. Reporting `JOIN_PROVENANCE_JOINED` there was byte-identical to a
#: genuine evidence-backed join, so a caller had no way to tell "nothing
#: existed to check" apart from "I checked and it's clean". This value names
#: that distinction honestly: no evidence source existed to consult, at all
#: -- distinct from every other provenance value above, each of which
#: describes a join that was AT LEAST attempted. `shipped` stays `True` on
#: that branch (the D7 posture itself is unchanged -- a plan with genuinely
#: nothing to check is not "unshipped"); this value exists so a stamping
#: caller can choose not to treat that as attributed evidence of delivery.
JOIN_PROVENANCE_NO_EVIDENCE_SOURCE = "no_evidence_source"

#: Seventh join-provenance value (Review: code-reviewer, Finding P2,
#: 2026-08-10, slice D): the Deliverable-Id leg found ZERO evidence
#: (`matched_commit_count == 0`), but the Session-Id-scoped fallback
#: `_committed_chunk_shas` runs behind that zero-evidence gate (see this
#: module's docstring § Deliverable scoping) resolved evidence for at least
#: one `missing_chunk_ids` row anyway. Both `JOIN_PROVENANCE_NO_JOIN_CANDIDATES`
#: and `JOIN_PROVENANCE_KEY_MISMATCH` describe "nothing existed to compare
#: against"/"never one equal to this plan's own value" -- true of the
#: Deliverable-Id leg alone, but misleading once the fallback DID find
#: comparable evidence for some chunks in the same range. This value never
#: widens or replaces the exact-equality Deliverable-Id join itself (still
#: computed identically above); it only reports honestly that the fallback,
#: not that join, is why some (possibly not all) of `missing_chunk_ids`
#: aren't uncommitted-by-mistake.
JOIN_PROVENANCE_SESSION_FALLBACK_PARTIAL = "session_fallback_partial"

#: Plain-language reason strings for every NON-`"joined"` provenance value --
#: `close_out_and_stamp`'s own halted-branch `message` uses these so a reader
#: sees WHY attribution failed, not just that it did. Deliberately excludes
#: `JOIN_PROVENANCE_JOINED` itself -- that state keeps the pre-existing
#: "still uncommitted" wording verbatim, never this mapping.
_JOIN_PROVENANCE_REASON = {
    JOIN_PROVENANCE_NO_JOIN_KEY: (
        "the plan's own frontmatter carries no deliverable_id: field, so the "
        "commit-coverage join was never attempted"
    ),
    JOIN_PROVENANCE_NO_JOIN_CANDIDATES: (
        "no commit in the search range carries a Deliverable-Id trailer at "
        "all, so there was nothing to join against"
    ),
    JOIN_PROVENANCE_KEY_MISMATCH: (
        "commits in range carry a Deliverable-Id trailer, but never one "
        "equal to this plan's own frontmatter value, so the join could not "
        "match them"
    ),
    JOIN_PROVENANCE_SESSION_FALLBACK_PARTIAL: (
        "the Deliverable-Id join found zero evidence, but a Session-Id-"
        "scoped fallback resolved evidence for at least one chunk-id from "
        "the session currently holding a claim on this plan -- some of the "
        "listed chunk-ids may still be genuinely uncommitted, not merely "
        "unattributable"
    ),
    JOIN_PROVENANCE_LEDGER_FALLBACK: (
        "this plan predates the ## Tasks spine -- completeness was decided "
        "from its own ## Dispatch Ledger table's 'committed <sha>' cells "
        "instead of a Deliverable-Id trailer join"
    ),
    JOIN_PROVENANCE_NO_EVIDENCE_SOURCE: (
        "this plan has neither a ## Tasks spine nor a ## Dispatch Ledger "
        "heading, so no evidence source existed to consult at all -- "
        "'shipped' reflects nothing to check, not a verified delivery"
    ),
}


def _determine_shipped(
    plan_text: str, plan_path_rel: str, repo_root: Path
) -> tuple[bool, list[str], str, Optional[str]]:
    """Returns `(is_shipped, missing_chunk_ids, join_provenance, error)`.
    `error` is set (and every other field is meaningless) in two cases: the
    spine is MALFORMED, or the `_committed_chunk_shas` git-log query itself
    failed (Defect 2(d) -- distinguishes a BROKEN query from a repo that
    genuinely has zero chunk commits). Every other outcome is a definite
    shipped/halted verdict.

    `join_provenance` (widened, cross-repo memo fix -- see
    `DeliverableJoinStats`'s own docstring) separates "the Deliverable-Id
    join genuinely found this chunk-id uncommitted" from "the join itself
    could never attribute anything, so 'missing' is not a substantive
    delivery finding" -- one of:

      - `JOIN_PROVENANCE_JOINED` ("joined") -- the plan's own
        `deliverable_id:` was present AND at least one commit in range
        carried a matching `Deliverable-Id` trailer. `missing_chunk_ids`
        here is a genuine, join-backed "these rows have no covering commit"
        result -- the ordinary case this oracle has always reported.
      - `JOIN_PROVENANCE_NO_JOIN_KEY` ("no_join_key") -- the plan carries no
        `deliverable_id:` frontmatter field at all, so the join was NEVER
        attempted. Every commit-required chunk-id reads as missing (see
        this module's docstring § "No `deliverable_id` in the plan's own
        frontmatter"), but that is an attribution gap, not evidence the
        work is unshipped.
      - `JOIN_PROVENANCE_NO_JOIN_CANDIDATES` ("no_join_candidates") -- the
        key was present, but ZERO commits in the search range carried any
        `Deliverable-Id` trailer at all -- nothing existed to compare
        against it (a pre-trailer-convention range, or a range with no
        chunk commits of any kind).
      - `JOIN_PROVENANCE_KEY_MISMATCH` ("key_mismatch") -- the key was
        present and commits in range DO carry a `Deliverable-Id` trailer,
        just never one equal to this plan's own value (the two-producer
        desync `_deliverable_id_near_miss_diagnostics` already diagnoses in
        more detail).
      - `JOIN_PROVENANCE_NO_EVIDENCE_SOURCE` ("no_evidence_source") -- the
        plan has neither a `## Tasks` spine nor a `## Dispatch Ledger`
        heading, so no evidence source existed to consult at all.
        `shipped` is still `True` (D7's own "nothing to check" posture,
        unchanged), but this value tells a stamping caller that verdict was
        never backed by a lookup of any kind -- see this value's own
        docstring.

    Only meaningful when `missing_chunk_ids` is non-empty -- a fully-shipped
    verdict (`missing_chunk_ids == []`) still returns a real value (never
    `None`) for API simplicity, but callers only ever branch on it once
    `missing_chunk_ids` is non-empty, per this oracle's own existing
    "unhappy-path-only" posture for the sibling near-miss diagnostic."""
    # Dispatch Ledger fallback (see `_dispatch_ledger_delivered`'s own
    # docstring block, above `_all_spine_ids`): an ABSENT `## Tasks` spine no
    # longer bypasses this oracle as an automatic full-shipped verdict (the
    # OLD D7 posture, still correct for a plan that genuinely never had
    # per-chunk work to check) -- it reroutes to the plan's own legacy
    # Dispatch Ledger table instead, checked FIRST (before ever calling
    # `_parse_spine_rows`) so a LOCATED (even if empty-bodied) spine always
    # wins and never reaches this fallback at all.
    if locate_fenced_block(plan_text).status == LocateStatus.ABSENT:
        if _dispatch_ledger_section(plan_text) is None:
            # No spine AND no `## Dispatch Ledger` heading at all -- this is
            # the genuinely pre-spine, pre-ledger case D7 was written for
            # (nothing at all to check completeness against). Preserve the
            # `shipped=True` posture verbatim, but report HONESTLY that no
            # evidence source was ever consulted (false-positive-stamp
            # incident fix -- see `JOIN_PROVENANCE_NO_EVIDENCE_SOURCE`'s own
            # docstring): this is no longer byte-identical to a genuine
            # evidence-backed join, so a stamping caller can tell them apart.
            return True, [], JOIN_PROVENANCE_NO_EVIDENCE_SOURCE, None
        ledger_shipped, ledger_missing, ledger_error = _dispatch_ledger_delivered(
            plan_text, repo_root
        )
        if ledger_error is not None:
            # A heading exists, but the table under it could not be
            # confidently read (missing/unrecognized columns, malformed
            # rows) -- mirrors the spine's own MALFORMED posture: fail loud
            # rather than guess, since a guess here could silently stamp
            # undelivered work terminal (this module's own conservatism
            # rule -- see `_dispatch_ledger_delivered`'s docstring).
            return (
                False,
                [],
                JOIN_PROVENANCE_LEDGER_FALLBACK,
                f"{plan_path_rel}: {ledger_error}",
            )
        return ledger_shipped, ledger_missing, JOIN_PROVENANCE_LEDGER_FALLBACK, None

    rows, error = _parse_spine_rows(plan_text, plan_path_rel)
    if error is not None:
        return False, [], JOIN_PROVENANCE_JOINED, error

    chunk_ids = _commit_required_chunk_ids(rows)
    if not chunk_ids:
        return True, [], JOIN_PROVENANCE_JOINED, None

    spine_ids = _all_spine_ids(rows)
    deliverable_id = _plan_deliverable_id(plan_text)
    query_ok, committed, _committed_shas, join_stats = _committed_chunk_shas(
        repo_root, deliverable_id, spine_ids, plan_text=plan_text, plan_path_rel=plan_path_rel
    )
    if not query_ok:
        return (
            False,
            [],
            JOIN_PROVENANCE_JOINED,
            f"{plan_path_rel}: git-log query for landed chunk commits failed -- "
            "cannot determine chunk-completion mechanically (this is a BROKEN "
            "query, distinct from a repo that genuinely has zero chunk commits "
            "-- see close_out_and_stamp.py's Defect 2(d) fix)",
        )

    if not join_stats.attempted:
        join_provenance = JOIN_PROVENANCE_NO_JOIN_KEY
    elif join_stats.trailered_commit_count == 0:
        join_provenance = JOIN_PROVENANCE_NO_JOIN_CANDIDATES
    elif join_stats.matched_commit_count > 0:
        join_provenance = JOIN_PROVENANCE_JOINED
    else:
        join_provenance = JOIN_PROVENANCE_KEY_MISMATCH

    # Review: code-reviewer -- Finding P2, 2026-08-10, slice D. `committed`
    # here is `_committed_chunk_shas`'s own return, BEFORE the sibling-repo
    # and disposition_ref unions below -- with `matched_commit_count == 0`
    # (both `NO_JOIN_CANDIDATES` and `KEY_MISMATCH` branches above), the
    # Deliverable-Id leg contributes nothing, so any non-empty `committed`
    # here can only be the Session-Id-scoped fallback (`_committed_chunk_
    # shas`'s own zero-evidence-gated leg). Scoped to the genuinely PARTIAL
    # case -- fallback resolved evidence for at least one chunk-id but not
    # all of them -- and not the fully-resolved case: when the fallback
    # already covers every `chunk_ids` row, "no_join_candidates"/
    # "key_mismatch" already read correctly as "the Deliverable-Id join
    # found nothing, distinct from 'joined'", and every row is present in
    # `committed` either way, so relabeling there would only manufacture a
    # distinction with no user-facing difference (and dodges the
    # cross-repo-scan/disposition_ref unions further below intentionally
    # NOT being folded into this signal -- this checks against `chunk_ids`
    # directly instead of relying on `missing`, which isn't computed until
    # after those unions run). This does NOT touch the exact-equality join
    # above, only the provenance label used for messaging.
    if join_stats.matched_commit_count == 0 and committed:
        fallback_leaves_some_uncovered = any(
            not any(_committed_id_covers_spine_id(cid, chunk_id) for cid in committed)
            for chunk_id in chunk_ids
        )
        if fallback_leaves_some_uncovered:
            join_provenance = JOIN_PROVENANCE_SESSION_FALLBACK_PARTIAL

    # Cross-repo scope scanning (Defect fix, 2026-07-27 -- see this
    # module's docstring): union in every chunk-id found committed in a
    # sibling repo this plan's own `scope:` names, under the identical
    # Deliverable-Id/multi-chunk gates. `_plan_sibling_repo_ids` returns
    # `[]` (no sibling scan performed) when `scope:` names no sibling repo
    # at all -- the common case -- so this is a no-op there, byte-identical
    # to behavior before this fix. Sibling join outcomes are deliberately
    # NOT folded into `join_provenance` above -- that value describes THIS
    # repo's own join against `repo_root`'s git history, the same scope
    # `DeliverableJoinStats` was computed over; a sibling's own join success/
    # failure is a separate concern already surfaced via
    # `skipped_sibling_repos` elsewhere.
    sibling_committed, _skipped_sibling_repos = _sibling_committed_chunk_ids(
        plan_text, deliverable_id, spine_ids, repo_root
    )
    committed = committed | sibling_committed

    # Plan-side disposition_ref evidence (see this module's docstring §
    # Plan-side disposition_ref evidence): a SECOND, independently-verified
    # evidence path alongside the commit-subject join above -- never a
    # replacement for it, and never a relaxation of the Deliverable-Id
    # exact-equality join itself (untouched above). `join_provenance`
    # deliberately does NOT reflect this path -- it describes the
    # Deliverable-Id join's own outcome, unchanged by whether a
    # disposition_ref independently covered some other row.
    disposition_ref_committed, _disposition_ref_rejections = _disposition_ref_evidence(
        rows, repo_root
    )
    committed = committed | disposition_ref_committed

    missing = [
        cid
        for cid in chunk_ids
        if not any(_committed_id_covers_spine_id(committed_id, cid) for committed_id in committed)
    ]
    return (len(missing) == 0), missing, join_provenance, None


# ---------------------------------------------------------------------------
# docs/project-tracker.md `N of M` reconciliation (AC7)
#
# Tracker rows are hand-authored prose carrying PM-ratified boundary and gate
# narrative wrapped around a bare `N of M` chunk-progress claim -- the sending
# memo's explicit hard constraint (see this module's own C8 backlink) is that
# a fix here may edit ONLY that digit claim; the surrounding narrative must
# come out byte-identical. This is deliberately NOT a full-row rewrite (a
# render() in render_project_tracker.py's sense) -- that machinery folds a
# queue-backed store's OWN authored fields, never PM-ratified prose a human
# wrote around a number. A bounded edit or a no-op (when no plan can be
# joined, or the claim already agrees) is the correct, safe answer; guessing
# a full section back together from parts is not attempted here.
#
# Spec backlink: pln-terminal-state-propagation-giv-c85539
# § C8 / AC7.
# ---------------------------------------------------------------------------

_TRACKER_WORKSTREAM_HEADER_RE = re.compile(r"^### \d+\. .*$", re.MULTILINE)
"""Matches the `### {number}. {title}` header render_project_tracker.py's
own `_render_workstream_section` emits for a queue-backed tracker, and which
this repo's hand-curated `docs/project-tracker.md` also uses -- the section
boundary this reconciler splits on."""

_TRACKER_SPECS_LINE_RE = re.compile(r"^\*\*Specs:\*\*\s*(.+)$", re.MULTILINE)
"""One workstream section's `**Specs:**` line, per the tracker format
contract (coordinator/pipelines/update-docs/tracker-maintenance.md § Project
Tracker Format Reference) -- the join anchor to a `docs/plans/*.md` spec."""

_TRACKER_SPEC_PLAN_PATH_RE = re.compile(r"`(docs/plans/[^`]+\.md)`")
"""Backtick-quoted `docs/plans/*.md` paths inside a `**Specs:**` line --
join CANDIDATES only; join on `deliverable_id`, never on the path/`plan:`
field itself (see this module's C8 backlink and the plan's own R1/R1a/R2
rulings on why `plan:` is not a join key)."""

_TRACKER_N_OF_M_RE = re.compile(r"\b(\d+) of (\d+) chunks?\b")
"""The bounded edit target itself -- a literal `N of M chunk(s)` digit
claim. Deliberately narrow (no word-number form like "all four chunks",
no bare `N/M`) so this reconciler can never mistake an unrelated number
pair for the claim it is licensed to touch."""


def _tracker_section_spec_plan_paths(section_text: str) -> list[str]:
    """Every `docs/plans/*.md` path a workstream section's own `**Specs:**`
    line names, in order -- `[]` when the section carries no `**Specs:**`
    line, or none of its entries are a backtick-quoted plan path."""
    match = _TRACKER_SPECS_LINE_RE.search(section_text)
    if not match:
        return []
    return _TRACKER_SPEC_PLAN_PATH_RE.findall(match.group(1))


def _tracker_row_shipped_of_total(
    plan_path_rel: str, repo_root: Path
) -> Optional[tuple[int, int]]:
    """Reduces `_determine_shipped`'s own commit-evidence verdict for ONE
    Specs:-referenced plan to a bare `(shipped, total)` pair for a tracker
    row's `N of M` claim -- reusing `_parse_spine_rows` /
    `_commit_required_chunk_ids` / `_determine_shipped` verbatim (never a
    second implementation) and the same forward-slash `rel_id` normalisation
    `close_out_and_stamp`'s own entrypoint uses (Windows is first-class
    here -- see that call site's own A5-fix comment).

    Returns `None` -- "leave the tracker claim untouched" -- when the plan
    is unreadable, carries no parseable frontmatter, its `## Tasks` spine
    cannot be located/parsed, the git-log query itself failed, or the spine
    names zero commit-required chunks (a 0-of-0 plan has no `N of M` claim
    to reconcile against). A tracker claim is never overwritten with a
    guess in any of those cases -- silence, not a wrong number, is the safe
    failure direction."""
    live_path = Path(plan_path_rel)
    if not live_path.is_absolute():
        live_path = repo_root / live_path
    if not live_path.is_file():
        return None
    try:
        text = live_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if split_frontmatter(text) is None:
        return None

    try:
        norm_rel = rel_id(live_path, repo_root)
    except ValueError:
        norm_rel = plan_path_rel

    rows, rows_error = _parse_spine_rows(text, norm_rel)
    if rows_error is not None or rows is None:
        return None
    total = len(_commit_required_chunk_ids(rows))
    if total == 0:
        return None

    _shipped, missing, _join_provenance, spine_error = _determine_shipped(
        text, norm_rel, repo_root
    )
    if spine_error is not None:
        return None
    return total - len(missing), total


def _reconcile_tracker_section(
    section_text: str, repo_root: Path
) -> tuple[str, Optional[dict[str, Any]]]:
    """Reconciles ONE workstream section's `N of M` claim (if any) against
    commit evidence, returning `(possibly-rewritten section_text, edit-or-
    None)`. The rewrite -- when one fires -- replaces ONLY the matched
    claim's own digit run; every other character of `section_text`,
    including everything before and after that span, is returned
    unmodified (HARD CONSTRAINT -- see this module's own test for the
    byte-identical assertion this exists to satisfy)."""
    header_match = re.match(r"^### \d+\. (.*)$", section_text, re.MULTILINE)
    title = header_match.group(1).strip() if header_match else "?"

    plan_paths = _tracker_section_spec_plan_paths(section_text)
    if not plan_paths:
        return section_text, None

    claim_match = _TRACKER_N_OF_M_RE.search(section_text)
    if claim_match is None:
        return section_text, None

    derived: Optional[tuple[int, int]] = None
    derived_plan_path: Optional[str] = None
    for plan_path in plan_paths:
        result = _tracker_row_shipped_of_total(plan_path, repo_root)
        if result is not None:
            derived = result
            derived_plan_path = plan_path
            break
    if derived is None:
        return section_text, None

    shipped, total = derived
    claimed_shipped = int(claim_match.group(1))
    claimed_total = int(claim_match.group(2))
    if (shipped, total) == (claimed_shipped, claimed_total):
        return section_text, None

    old_digits = claim_match.group(0)
    new_digits = f"{shipped} of {total}" + old_digits[len(f"{claimed_shipped} of {claimed_total}"):]
    new_section_text = (
        section_text[: claim_match.start()] + new_digits + section_text[claim_match.end() :]
    )
    edit = {
        "section": title,
        "plan_path": derived_plan_path,
        "old": f"{claimed_shipped} of {claimed_total}",
        "new": f"{shipped} of {total}",
    }
    return new_section_text, edit


def reconcile_tracker_shipped_counts(
    tracker_text: str, repo_root: Path
) -> tuple[str, list[dict[str, Any]]]:
    """Bounded-edit reconciliation pass over `docs/project-tracker.md`'s own
    text (AC7) -- pure, no I/O of its own (the caller reads/writes the
    file; see `apply_tracker_reconciliation` for the single write
    entrypoint). Splits `tracker_text` on `_TRACKER_WORKSTREAM_HEADER_RE`
    and reconciles each workstream section independently via
    `_reconcile_tracker_section`.

    Returns `(new_text, edits)`. `edits` names every section this pass
    actually rewrote; `new_text == tracker_text` (byte-identical) whenever
    `edits == []` -- content the reconciler could not join, or already
    agrees with commit evidence, passes through completely untouched, not
    merely "unchanged in effect"."""
    headers = list(_TRACKER_WORKSTREAM_HEADER_RE.finditer(tracker_text))
    if not headers:
        return tracker_text, []

    edits: list[dict[str, Any]] = []
    pieces = [tracker_text[: headers[0].start()]]
    for index, header in enumerate(headers):
        section_end = headers[index + 1].start() if index + 1 < len(headers) else len(tracker_text)
        section_text = tracker_text[header.start() : section_end]
        new_section_text, edit = _reconcile_tracker_section(section_text, repo_root)
        if edit is not None:
            edits.append(edit)
        pieces.append(new_section_text)
    return "".join(pieces), edits


def apply_tracker_reconciliation(
    tracker_path: Path, repo_root: Path
) -> list[dict[str, Any]]:
    """The single write entrypoint for AC7's tracker reconciliation (this
    repo's own north star: read-only compute + ONE apply entrypoint, never
    a second ad hoc writer -- `reconcile_tracker_shipped_counts` above is
    that read-only compute half). Reads `tracker_path`, reconciles it, and
    writes back ONLY when at least one edit fired -- a no-op run never
    dirties the tree or perturbs the file's mtime. Returns the same `edits`
    list `reconcile_tracker_shipped_counts` returns (`[]` on a no-op run),
    for a caller (`ceremony.update_docs_scan`'s manifest today; the mise
    tracker-sync step once it shares this same compute path) to report."""
    text = tracker_path.read_text(encoding="utf-8")
    new_text, edits = reconcile_tracker_shipped_counts(text, repo_root)
    if edits:
        tracker_path.write_text(new_text, encoding="utf-8")
    return edits


#: Rejection reason strings `_verify_disposition_ref` returns -- named here as
#: a single source of truth since `_disposition_ref_evidence` and any caller
#: reporting them (`close_out_and_stamp`'s own result dict) must use the
#: identical four values (see this module's docstring § Plan-side
#: disposition_ref evidence).
DISPOSITION_REF_ABSENT = "absent"
DISPOSITION_REF_MALFORMED = "malformed"
DISPOSITION_REF_UNRESOLVABLE = "unresolvable"
DISPOSITION_REF_NOT_ANCESTOR = "non-ancestor"

#: A `disposition_ref` is always written by this module (or a human
#: following the same convention) as a bare hex commit sha -- never a
#: symbolic ref, branch name, or tag. Bounding the shape BEFORE ever handing
#: the value to `git rev-parse` is deliberate defense-in-depth: it means an
#: arbitrary string (blank, whitespace, a `-`-leading token that could be
#: mistaken for a flag, a symbolic ref like `HEAD~3` that resolves to
#: something OTHER than what the author actually pinned) is rejected as
#: `DISPOSITION_REF_MALFORMED` before any subprocess call, rather than
#: silently resolving to an unintended commit.
_DISPOSITION_REF_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _verify_disposition_ref(
    repo_root: Path, ref: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Verifies a single row's `disposition_ref` value as commit-required
    evidence (see this module's docstring § Plan-side disposition_ref
    evidence for the design this implements and why it is safe against
    self-attestation).

    Returns `(sha, reason)`: `sha` is the ref's own full, resolved commit sha
    when -- and ONLY when -- it names a real commit object in `repo_root`'s
    history that `git merge-base --is-ancestor` proves is reachable from
    `HEAD`. Otherwise `sha` is `None` and `reason` is exactly one of
    `DISPOSITION_REF_ABSENT` (not a non-blank string at all -- the row has no
    `disposition_ref`, or it is blank/whitespace-only), `DISPOSITION_REF_
    MALFORMED` (present, but not a bare hex sha shape -- see `_DISPOSITION_
    REF_SHA_RE`'s own docstring for why this is checked before ever reaching
    git), `DISPOSITION_REF_UNRESOLVABLE` (hex-shaped, but `git rev-parse
    --verify` cannot resolve it to a commit object in this repo -- a typo, a
    sha from a repo this isn't, or an object this shallow/partial clone does
    not have), or `DISPOSITION_REF_NOT_ANCESTOR` (resolves to a real commit,
    but `HEAD` never reached it -- a rebased-away, cherry-picked-into-a-
    different-branch, or fabricated sha). Never raises."""
    if not isinstance(ref, str) or not ref.strip():
        return None, DISPOSITION_REF_ABSENT
    ref = ref.strip()
    if not _DISPOSITION_REF_SHA_RE.match(ref):
        return None, DISPOSITION_REF_MALFORMED

    resolve_result = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo_root)
    sha = (resolve_result.stdout or "").strip()
    if resolve_result.returncode != 0 or not sha:
        return None, DISPOSITION_REF_UNRESOLVABLE

    ancestor_result = _run_git(["merge-base", "--is-ancestor", sha, "HEAD"], repo_root)
    if ancestor_result.returncode != 0:
        return None, DISPOSITION_REF_NOT_ANCESTOR

    return sha, None


def _disposition_ref_evidence(
    spine_rows: list[Any], repo_root: Path
) -> tuple[set[str], dict[str, str]]:
    """Chunk-ids for which a `disposition: coded` row's own `disposition_ref`
    verifies as real evidence (`_verify_disposition_ref`), PLUS a rejection-
    reason map for every `coded` row whose `disposition_ref` did NOT verify
    -- see this module's docstring § Plan-side disposition_ref evidence.

    Scoped to `disposition: coded` rows ONLY -- narrower than `_commit_
    required_chunk_ids`'s own `open`/`coded` set, and deliberately so: an
    `open` row has not yet been resolved by anything (its `disposition_ref`,
    if present at all, is not this evidence path's concern -- it either
    ships via the ordinary commit-subject join, or AC8's own auto-resolve
    step picks it up later), so treating a plain legacy `open` row's absent
    `disposition_ref` as a "rejection" would manufacture rejection-reason
    noise on every ordinary legacy spine that has never used `disposition:`
    at all (every row defaults to `open` per D1). `coded` is the disposition
    a row is EXPLICITLY moved to once something -- an executor, a prior
    auto-resolve pass, or a manual `resolve --coded` -- attests it landed;
    that is the one state where a missing/failed `disposition_ref` is a
    genuine, reportable gap rather than "this plan predates the field
    entirely".

    Returns `(verified_ids, rejections)`. `verified_ids` is unioned directly
    into `_determine_shipped`'s own `committed` set by its caller -- no
    `_committed_id_covers_spine_id` matching is needed here (unlike the
    subject-join path), since a `disposition_ref` is evidence for the exact
    row it lives on, never a prefix that might cover a sub-chunk or dash-tag
    variant. `rejections` maps every `coded` chunk-id whose ref did NOT
    verify to its own `_verify_disposition_ref` reason string -- callers
    report this ONLY for ids that remain in `missing_chunk_ids` after every
    evidence path has been unioned in, per this module's docstring's
    "unhappy-path-only" posture for its other diagnostics."""
    verified: set[str] = set()
    rejections: dict[str, str] = {}
    for row in spine_rows:
        if not isinstance(row, dict):
            continue
        if row.get("deferred", False):
            continue
        if _row_disposition(row) != _CODED:
            continue
        chunk_id = row.get("id")
        if not chunk_id:
            continue
        chunk_id = str(chunk_id)
        sha, reason = _verify_disposition_ref(repo_root, row.get("disposition_ref"))
        if sha is not None:
            verified.add(chunk_id)
        else:
            rejections[chunk_id] = reason
    return verified, rejections


# Admits `disposition_detail:` to the fidelity gate's allowed-change set
# DELIBERATELY, alongside `disposition:`/`disposition_ref:` -- this stamper
# now writes all three fields (DR-103), so `_assert_stamp_fidelity` below
# must recognize a `disposition_detail:` line as an expected touch, not an
# unrelated-line corruption.
_STAMP_LINE_RE = re.compile(r"^[ \t]*disposition(?:_ref|_detail)?:[ \t]")


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return "\n"


def _row_key_line_indices(
    lines: list[str], start: int, end: int, content_indent: int
) -> dict[str, int]:
    """Within row-span `[start, end)`, finds the line index of each of
    this row's own `disposition:` / `disposition_ref:` / `disposition_detail:`
    / `deferred:` keys -- matched ONLY at exactly `content_indent` (the
    row's own top-level key indent, never a deeper nested line) so a
    `body: |` block scalar's continuation text that happens to contain
    one of these words can never be mistaken for the key itself.

    `disposition_detail` is listed BEFORE the bare `disposition` alternative
    for readability only -- this is defensive, not correctness-load-bearing.
    Keeps only the FIRST occurrence of each key (a
    well-formed row never repeats a key; a duplicate is not this
    function's problem to police)."""
    key_re = re.compile(
        r"^"
        + re.escape(" " * content_indent)
        + r"(disposition_detail|disposition_ref|disposition|deferred):[ \t]"
    )
    found: dict[str, int] = {}
    for idx in range(start, end):
        match = key_re.match(lines[idx])
        if match:
            found.setdefault(match.group(1), idx)
    return found


_ROW_KEY_LINE_RE = re.compile(r"^([ \t]*)[A-Za-z_][A-Za-z0-9_]*:([ \t]|$)")


def _measure_row_content_indent(
    lines: list[str], start: int, end: int, dash_indent: int
) -> int:
    """Measures a row's actual child-key indent from its own body, rather
    than assuming `yaml.safe_dump`'s `dash_indent + 2` default -- the
    formatting this fix exists to STOP imposing, since the file is no
    longer re-dumped and a row's real indent may be whatever a human (or a
    different emitter) left there.

    Review: code-reviewer -- F4: `content_indent = dash_indent + 2` was
    assumed, not measured, so a non-default child-key indent made every
    key read as absent and both stamp lines landed at the wrong indent.

    Scans the row's span (excluding the dash line itself, since `id:`
    shares that line and does not establish a sibling-key indent) for any
    YAML mapping-key line strictly deeper than `dash_indent`, and returns
    the SHALLOWEST such indent -- the row's own top-level sibling keys sit
    at the shallowest indent among the row's lines; anything deeper is
    nested content (a `body: |` block scalar's continuation, etc). Falls
    back to `dash_indent + 2` only when the row has no other key line at
    all to measure against."""
    indents = [
        len(match.group(1))
        for idx in range(start + 1, end)
        if (match := _ROW_KEY_LINE_RE.match(lines[idx])) and len(match.group(1)) > dash_indent
    ]
    return min(indents) if indents else dash_indent + 2


def _stamp_rows_in_body(
    body: str,
    updates: dict[str, str],
    details: Optional[dict[str, str]] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Line-level (never round-tripping) stamp of `disposition: coded` /
    `disposition_ref: <sha>` / `disposition_detail: <prose>` onto every row
    named in `updates` (chunk-id -> covering commit sha), leaving every
    other line of `body` byte-identical -- comments, blank lines, quoting,
    key order, and block scalars all survive untouched, unlike the prior
    `yaml.safe_dump` round-trip this replaces (see this module's docstring
    § the original defect this fixes: a fence-body comment or `|` block
    scalar silently lost or reformatted by a full re-dump of a lossy
    `yaml.safe_load`).

    `details` (chunk-id -> `disposition_detail` prose, e.g. the covering
    commit's own subject line via `_commit_subject`) is OPTIONAL and
    independent of `updates`' own key set: an id present in `updates` but
    absent from (or not passed at all in) `details` gets no
    `disposition_detail` line written for it at all -- this function's own
    direct unit tests rely on that to exercise the disposition/
    disposition_ref-only shape without also needing a detail fixture.
    Every REAL caller inside this module (`_auto_resolve_committed_open_rows`)
    always pairs every `updates` id with a `details` entry (DR-103: "
    disposition_detail holds prose and is required on every non-open row"),
    since a `coded` row this stamps is never `open`. The value is written
    through `serialize_yaml_scalar` -- a commit subject routinely carries
    `:`, `#`, or quote characters that are YAML-structural if emitted bare.

    Per row: if `disposition:` / `disposition_ref:` / `disposition_detail:`
    already exist at the row's own key indent, their lines are REPLACED in
    place (never duplicated). Any key still missing is INSERTED as a new
    line, positioned immediately after the row's `deferred:` key if
    present, otherwise at the very end of the row's own span (i.e. after
    any trailing `body: |` block-scalar continuation, never spliced into
    the middle of one).

    Trailing-newline preservation (defect fix, 2026-08-01 -- the
    false-positive fidelity refusal reported by example-cockpit-repo-em):
    `locate_fenced_block(...).span` hands this function a body whose FINAL
    LINE TERMINATOR LIVES OUTSIDE THE SPAN -- for a real plan, `body` ends
    `'  deferred: false'` with no `\\n`, and the `'\\n'` that logically
    terminated it is the first character of `plan_text[end:]`. When the
    last row's stamp lines are inserted at the very end of the body, the
    insertion fixup below has to newline-terminate that previously-final
    line; without the compensating strip at the return, the caller's
    `plan_text[:start] + new_body + plan_text[end:]` reassembly then
    emits BOTH that added newline and the span-external one, planting a
    bare blank line between the last `disposition_detail:` line and the
    closing fence -- which `_assert_stamp_fidelity` correctly refuses
    (deterministically, on every retry). So: whether `body` ended with a
    line terminator is captured up front and restored at the return. A
    body that already ended with one is unaffected -- the strip is a
    no-op there, so no pre-existing behavior changes.

    Returns `(new_body, error)`. `error` is set only when `updates` names
    a chunk-id this scan cannot locate a row for -- a caller/oracle
    mismatch that must fail loud rather than silently stamp nothing."""
    details = details or {}
    body_ended_with_newline = body.endswith(("\n", "\r"))
    lines = body.splitlines(keepends=True)
    spans = _find_row_spans(lines)
    span_by_id = {chunk_id: (start, end) for start, end, chunk_id in spans}

    missing = sorted(set(updates) - set(span_by_id))
    if missing:
        return None, f"could not locate a row for chunk-id(s) {missing!r} to stamp"

    # Process rows in REVERSE row-order so an earlier row's insertion never
    # shifts a later row's already-computed line indices out from under it.
    for start, end, chunk_id in sorted(spans, key=lambda s: s[0], reverse=True):
        if chunk_id not in updates:
            continue
        sha = updates[chunk_id]
        detail = details.get(chunk_id)

        dash_line = lines[start]
        dash_indent = len(dash_line) - len(dash_line.lstrip(" \t"))
        # Review: code-reviewer -- F4: measure the row's actual sibling-key
        # indent instead of assuming yaml.safe_dump's `dash_indent + 2`
        # default, which this fix exists to stop imposing on the file.
        content_indent = _measure_row_content_indent(lines, start, end, dash_indent)
        newline = _line_ending(dash_line)

        keys = _row_key_line_indices(lines, start + 1, end, content_indent)
        pad = " " * content_indent
        disposition_line = f"{pad}disposition: {_CODED}{newline}"
        # `numeric_quoting=True` is load-bearing, not defensive. An abbreviated
        # commit sha is hex, so ~2.3% of them ((10/16)**8) are all-digit --
        # roughly one commit in 43. Emitted bare, YAML parses such a sha as an
        # INT, and the row then fails plan-tasks.schema.json's `type: string`
        # on disposition_ref. That is not hypothetical: a real auto-resolve run
        # (1576648b) wrote `disposition_ref: 17519732` into
        # docs/plans/2026-07-28-sat-01b-observed-set-fold-actuator.md, and the
        # write-time spine guard flags it to this day. Same reasoning, same
        # flag, as `execution_authorized_sha` in review_assemble/exec_auth_stamp.py.
        disposition_ref_line = (
            f"{pad}disposition_ref: {serialize_yaml_scalar(sha, numeric_quoting=True)}{newline}"
        )
        disposition_detail_line = (
            f"{pad}disposition_detail: {serialize_yaml_scalar(detail)}{newline}"
            if detail is not None
            else None
        )

        if "disposition" in keys:
            lines[keys["disposition"]] = disposition_line
        if "disposition_ref" in keys:
            lines[keys["disposition_ref"]] = disposition_ref_line
        if disposition_detail_line is not None and "disposition_detail" in keys:
            lines[keys["disposition_detail"]] = disposition_detail_line

        to_insert = []
        if "disposition" not in keys:
            to_insert.append(disposition_line)
        if "disposition_ref" not in keys:
            to_insert.append(disposition_ref_line)
        if disposition_detail_line is not None and "disposition_detail" not in keys:
            to_insert.append(disposition_detail_line)

        if to_insert:
            insert_at = keys["deferred"] + 1 if "deferred" in keys else end
            if insert_at > 0 and not lines[insert_at - 1].endswith(("\n", "\r\n")):
                # The line we are about to insert after has no trailing
                # newline (only possible when it is the body's last line
                # with no final newline) -- add one so the new line does
                # not get glued onto the end of it.
                lines[insert_at - 1] += newline
            lines[insert_at:insert_at] = to_insert

    new_body = "".join(lines)
    if not body_ended_with_newline and new_body.endswith(("\n", "\r")):
        # Restore the body's own trailing-newline property (see this
        # function's docstring § Trailing-newline preservation): strip
        # exactly ONE line terminator, `\r\n` before `\n` so a CRLF body
        # loses the pair rather than being left with a dangling `\r`.
        if new_body.endswith("\r\n"):
            new_body = new_body[:-2]
        else:
            new_body = new_body[:-1]
    return new_body, None


def _row_span_containing(
    spans: list[tuple[int, int, str]], idx: int
) -> Optional[tuple[int, int]]:
    """Finds the `(start, end)` row-span (as returned by `_find_row_spans`)
    that contains line-index `idx`, or `None` if `idx` falls outside every
    row (e.g. the body has no rows at all)."""
    for start, end, _chunk_id in spans:
        if start <= idx < end:
            return start, end
    return None


_FIDELITY_NEXT_MOVE = (
    "Fix the row-span / stamp-assembly logic in _stamp_rows_in_body -- that "
    "is where this divergence is produced. Your plan file was NOT modified: "
    "this gate runs before any write, so there is nothing to restore from "
    "git, and the failure is deterministic, so re-running close-out-and-stamp "
    "reproduces it rather than clearing it."
)
"""Shared tail for every `_assert_stamp_fidelity` refusal message.

Design-as-offers (memo, example-cockpit-repo-em 2026-08-01): leads with the ONE
useful next move -- where the defect lives -- instead of the two misleading
instructions this text used to carry. "Restore the plan from git" implied a
damaged file when the gate refuses BEFORE any write happens, and "re-run
close-out-and-stamp" invited a retry loop against a deterministic stamper
defect. Both facts are now stated explicitly, after the next move, so a
reader who was about to do either stops."""


def _assert_stamp_fidelity(
    old_text: str, new_text: str, plan_path_rel: str
) -> Optional[str]:
    """Step 2 fidelity gate: every line of `old_text` must still be
    present verbatim in `new_text`, except that a row's `disposition:` /
    `disposition_ref:` / `disposition_detail:` lines may have been changed
    or newly inserted. Returns `None` when the write is safe to land; otherwise a fail-loud,
    design-as-offers-worded error naming `plan_path_rel` and the first
    diverging line -- the caller must refuse to write, commit, or push on
    any non-`None` return (this is the correctness backstop for the exact
    failure mode this fix addresses: a lossy re-dump silently destroying
    unrelated content).

    Deliberately independent of `_stamp_rows_in_body`'s own bookkeeping --
    this diffs the ACTUAL before/after text via `difflib.SequenceMatcher`
    (the same line-oriented algorithm `git diff` and `difflib.unified_diff`
    build on) rather than trusting the stamper's insert/replace indices,
    so a bug in the stamper's own row-span math is still caught here
    rather than silently landing. Every non-`equal` diff opcode's touched
    lines (both sides) must match `_STAMP_LINE_RE`; a `delete` opcode (an
    original line vanishing outright) is refused unconditionally, since
    stamping never removes a line.

    Review: code-reviewer -- F3: matching `_STAMP_LINE_RE` against an
    already-`.strip()`-ed line made its own `^[ \\t]*` prefix vacuous, and
    even matched against the raw line the pattern's `[ \\t]*` is a
    wildcard -- neither form alone can tell a correctly-indented stamp
    line from a wrongly-indented one. This gate independently re-derives
    each touched line's OWN row from `old_text` (never trusting
    `_stamp_rows_in_body`'s bookkeeping) and asserts the touched line's
    actual leading-whitespace width equals that row's own measured
    content indent (`_measure_row_content_indent`, the same
    independently-correct measurement `_stamp_rows_in_body` itself now
    uses per F4) -- so a stamp landing at the wrong indent is refused here
    even if the stamper's own row-span math got it wrong."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    spans = _find_row_spans_in_plan(old_lines, old_text)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            first = old_lines[i1] if i1 < len(old_lines) else ""
            return (
                f"{plan_path_rel}: refusing to write -- stamp fidelity check "
                f"found an original line removed where only a disposition/"
                f"disposition_ref change was expected (first diverging line: "
                f"{first!r}). {_FIDELITY_NEXT_MOVE}"
            )

        # Independently re-derive which row this change belongs to, and
        # that row's own expected content indent -- anchored on the line
        # immediately BEFORE the change (an `insert` opcode has i1 == i2,
        # so there is no touched old-side line to anchor on directly; the
        # line right before the insertion point is always the true owning
        # row, including the edge case of an insertion at a row's very
        # end, which lands exactly on the next row's start index).
        anchor = i1 - 1 if i1 > 0 else i1
        span = _row_span_containing(spans, anchor)
        if span is None:
            span = _row_span_containing(spans, i1)
        expected_indent = None
        if span is not None:
            row_start, row_end = span
            dash_line = old_lines[row_start]
            dash_indent = len(dash_line) - len(dash_line.lstrip(" \t"))
            expected_indent = _measure_row_content_indent(
                old_lines, row_start, row_end, dash_indent
            )

        touched = old_lines[i1:i2] + new_lines[j1:j2]
        for line in touched:
            if not _STAMP_LINE_RE.match(line):
                return (
                    f"{plan_path_rel}: refusing to write -- stamp fidelity "
                    "check found a change outside the disposition/"
                    f"disposition_ref/disposition_detail fields (first diverging line: {line!r}). "
                    f"{_FIDELITY_NEXT_MOVE}"
                )
            if expected_indent is not None:
                actual_indent = len(line) - len(line.lstrip(" \t"))
                if actual_indent != expected_indent:
                    return (
                        f"{plan_path_rel}: refusing to write -- stamp fidelity "
                        "check found a disposition/disposition_ref/disposition_detail "
                        f"line at "
                        f"indent {actual_indent} but this row's own content "
                        f"indent is {expected_indent} (first diverging line: "
                        f"{line!r}). {_FIDELITY_NEXT_MOVE}"
                    )
    return None


def _commit_subject(repo_root: Path, sha: str) -> str:
    """Resolves `sha`'s own commit subject line, for AC8's `disposition_detail`
    write (DR-103: "`disposition_detail` holds prose and is required on
    every non-`open` row" -- a `coded` row this stamper writes is never
    `open`). Reuses this module's own `_run_git` helper (same
    `CREATE_NO_WINDOW` convention silencing the Windows console flash).

    Never raises and never wedges the stamp on a git-read failure -- a
    shallow clone missing the commit object, a detached/corrupt ref, or any
    other `git log` failure falls back to a plain, honest placeholder
    string naming the sha itself, since `disposition_ref` already carries
    the authoritative sha regardless of whether its prose companion could
    be enriched."""
    result = _run_git(["log", "-1", "--format=%s", sha], repo_root)
    subject = (result.stdout or "").strip()
    if result.returncode == 0 and subject:
        return subject
    return f"commit {sha} (subject unavailable)"


def _auto_resolve_committed_open_rows(
    live_path: Path,
    plan_text: str,
    spine_rows: list[Any],
    committed_shas: dict[str, str],
    repo_root: Path,
    *,
    dry_run: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """AC8: auto-resolves every committed-but-`open` spine row to
    `disposition: coded`, `disposition_ref: <covering commit sha>`,
    `disposition_detail: <covering commit's own subject line>` (DR-103).

    A row qualifies when it is not legacy `deferred: true`, its
    disposition (D1 default) is `open`, and SOME committed id
    (`committed_shas`, from `_committed_chunk_shas`) covers its spine id
    per `_committed_id_covers_spine_id` -- the SAME sub-chunk-suffix match
    this module's own completeness oracle already computes; this is a
    consumer of that match, not new matching machinery. `resolve --coded`
    (`plan_tasks_mutate`'s PM-gated sibling verb, C4) remains the manual
    override for a commit whose subject does not follow the `<chunk-id>:`
    convention at all -- this function only ever auto-resolves a row this
    module's OWN oracle can already see is committed.

    Writer choice (not `plan_tasks_mutate.stamp`): that op's `_resolve_path`
    hard-requires the target be contained under `<worktree>/docs/plans/`
    and takes a cross-process `locked_rmw` lock -- appropriate for its own
    concurrent-mutation surface, but this op already performs its OTHER
    write (the `status:` stamp, via `plan_status_transition`/
    `_stamp_plan_landed`) unlocked and without that containment check, so
    routing ONE of this op's two writes through a differently-gated
    module would make the pair inconsistent for no safety gain -- both
    writes happen once, at plan close-out, never on a concurrent-mutation
    hot path (mirrors `plan_status_transition.py`'s own "single-writer,
    once-per-plan-completion, no locking" precedent for its
    `stamp-implemented` verb).

    Line-level splice, NOT a `yaml.safe_dump` round-trip (defect fix,
    2026-07-27): the prior implementation re-parsed the whole fence body
    with `yaml.safe_load` and re-emitted it with `yaml.safe_dump`, which
    silently discards every YAML-level comment in the body (`<!-- Review:
    ... -->` lines, `#` comments) and never re-selects `|` literal block
    scalars (PyYAML re-serializes them as single-quoted scalars, doubling
    embedded apostrophes) -- a real close-out run destroyed 485 lines of
    a live plan doc this way, committed and pushed before anyone noticed.
    `_stamp_rows_in_body` instead edits `located.body`'s raw text
    line-by-line, touching ONLY the `disposition:`/`disposition_ref:`/
    `disposition_detail:` lines of the rows actually being stamped;
    everything else in the body -- comments, blank lines, quoting, key
    order, block scalars -- is reused verbatim. `_assert_stamp_fidelity`
    then independently verifies that promise against the actual
    before/after text before any write happens, and refuses (no write, no
    commit, no push) on any other divergence.

    `disposition_detail` (DR-103, `docs/decisions/DR-103-plan-line-item-
    resolution-model-and-landed-status.md`: "`disposition_detail` holds
    prose and is required on every non-`open` row"): every row this
    function auto-resolves to `coded` gets a `disposition_detail` set to
    the covering commit's own subject line (`_commit_subject(repo_root,
    sha)`) -- never the sha again (that already lives in
    `disposition_ref`), and never left absent, since an auto-resolved row
    is by construction no longer `open` and the newly-wired
    `_cf_plan_tasks_disposition_shape` cross-field validator
    (`coordinator_core.frontmatter.schema_validate`) hard-rejects a
    non-`open` row with no detail. Prior to this fix, this function
    produced exactly that invalid shape on every real auto-resolve run --
    a defect that went unnoticed only because nothing on the auto-resolve
    write path actually invoked the cross-field validator (see
    `coordinator_core/ops/plan_tasks_mutate.py`'s `_validate_row` fix,
    landed alongside this one, for the other half of that gap).

    `dry_run` (2026-08-04, `--dry-run` mode -- see `close_out_and_stamp`'s
    own docstring): runs every computation and validation below UNCHANGED
    -- row matching, `_stamp_rows_in_body`, and the `_assert_stamp_fidelity`
    refusal check all still execute, so a dry run reports the identical
    `(new_text, error)` a live run would (including a fidelity-refusal
    `error`, if one would occur) -- only the final `live_path.write_text`
    call is skipped. This is the ONLY behavioral difference; no second
    computation is introduced.

    Returns `(new_text, error)`. `new_text` is the plan's full text AFTER
    the write (or, under `dry_run`, the text that WOULD have been written;
    `None` when there was nothing to resolve, or on failure) -- callers use
    this to avoid re-reading a body-block region they just wrote
    themselves (or, under `dry_run`, to keep computing against the same
    would-be content in memory). `error` is set only on a genuine failure
    (a LOCATED spine unexpectedly not found, a row-span the scan could
    not locate, a fidelity-check refusal, or the write itself failing)."""
    updates: dict[str, str] = {}
    details: dict[str, str] = {}
    for row in spine_rows:
        if not isinstance(row, dict):
            continue
        if row.get("deferred", False):
            continue
        chunk_id = row.get("id")
        if not chunk_id:
            continue
        chunk_id = str(chunk_id)
        if _row_disposition(row) != _OPEN:
            continue
        sha = next(
            (
                committed_shas[committed_id]
                for committed_id in committed_shas
                if _committed_id_covers_spine_id(committed_id, chunk_id)
            ),
            None,
        )
        if sha is not None:
            updates[chunk_id] = sha
            details[chunk_id] = _commit_subject(repo_root, sha)

    if not updates:
        return None, None

    located = locate_fenced_block(plan_text)
    if located.status != LocateStatus.LOCATED:
        # spine_rows was derived from a LOCATED parse (_parse_spine_rows) by
        # every caller of this function, so this branch should be
        # unreachable in practice -- fail loud rather than silently skip a
        # real write if that invariant is ever violated.
        return None, "auto-resolve: expected a LOCATED ## Tasks spine but none was found"

    start, end = located.span
    new_body, stamp_error = _stamp_rows_in_body(plan_text[start:end], updates, details)
    if stamp_error is not None:
        return None, f"auto-resolve: {stamp_error}"

    new_text = plan_text[:start] + new_body + plan_text[end:]

    fidelity_error = _assert_stamp_fidelity(plan_text, new_text, str(live_path))
    if fidelity_error is not None:
        return None, fidelity_error

    if not dry_run:
        try:
            live_path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return None, f"auto-resolve: could not write {live_path}: {exc}"

    return new_text, None


def _peek_plan_status(plan_text: str) -> Optional[str]:
    """Best-effort read of the plan's current, normalized `status:`
    frontmatter value -- mirrors the identical parse/strip/unquote steps
    `_stamp_plan_landed` (below) and `plan_status_transition._stamp_implemented`
    each perform internally, used here ONLY so `close_out_and_stamp` can
    tell, from the OUTSIDE and BEFORE calling either stamp helper, whether
    that call is about to hit one of their own documented no-op branches
    (already-terminal / already-at-target). Review finding, 2026-07-27:
    neither helper's return code alone distinguishes "wrote a real change"
    from "no-op, rc=0" -- both return 0 on their no-op branches too -- so a
    caller that sets `stamped = True` on `rc == 0` unconditionally is wrong
    on an idempotent re-run against an already-terminal/already-landed
    plan: `stamped` reads `True` against a byte-clean `plan.md`, and the
    commit leg then attempts (and fails loud on) a zero-diff commit. See
    `close_out_and_stamp()`'s own docstring, "Commit leg gated on
    `wrote_anything`" section.

    Returns `None` on any parse failure (no frontmatter, no status field,
    an unsupported quoted-scalar-plus-trailing-comment shape). Both call
    sites below already know the stamp call itself will fail loud
    (`stamp_rc != 0`) on the identical parse failure and return before
    `stamped` is ever set from this value -- so a `None` here is never
    read as a specific status, only as "the stamp call is about to error
    out anyway."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    status = read_fm_field(split.fm_text, "status")
    if status is None or status.startswith("#"):
        return None
    status = _strip_unquoted_trailing_comment(status)
    if status and status[0] in ("'", '"') and not status.endswith(status[0]):
        return None
    return unquote_yaml_scalar(status)


def _stamp_plan_landed(
    plan_path: str, *, dry_run: bool = False, plan_text: Optional[str] = None
) -> int:
    """Flips a plan's `status:` frontmatter field to `landed` (D9) -- the
    intermediate status meaning "every chunk's code is on the branch, but
    not every spine row has reached a disposition".

    Mirrors `coordinator_core.ops.plan_status_transition._stamp_implemented`'s
    parse/gate/write shape -- there is no `stamp-landed` verb there to
    compose over (`landed` is new territory this plan introduces, and
    `plan_status_transition.py` is outside this module's write-scope) --
    but imports that module's already-correct `_FROZEN_STATUSES` /
    `_FLIPPABLE_STATUSES` / `_strip_unquoted_trailing_comment` rather than
    re-deriving the comment-stripping/quote-handling edge cases it already
    solved: composition of the reusable PARTS, not a second independent
    parser. `landed` itself is a member of `_FLIPPABLE_STATUSES` (that
    module bucket it there, C8a, D9 -- flippable onward to `implemented`,
    not terminal), so gating this write against those same two sets keeps
    the two stamps' safety posture identical: neither ever resurrects an
    abandoned/deferred/superseded/implemented plan, and both are
    idempotent no-ops when already at their own target.

    Returns 0 on transition-applied or no-op, 1 on error -- the same
    exit-code contract `_stamp_implemented` uses.

    `dry_run` (2026-08-04, `--dry-run` mode -- see `close_out_and_stamp`'s
    own docstring): the transition decision below (frozen/no-op/landed/
    unexpected-status) runs completely unchanged; only the final disk
    write is skipped -- the write is a purely mechanical last step
    (persisting an already-computed `rebuilt` string) that plays no part
    in the decision itself, so suppressing it changes nothing this
    function reports. `plan_text` (dry-run callers only) supplies the
    CURRENT in-memory plan text to operate on instead of re-reading
    `plan_path` from disk -- required because a preceding dry-run AC8
    auto-resolve step (if any) never persisted its own change to disk, so
    re-reading the live file here would see STALE, pre-auto-resolve
    content; `plan_text` lets this call see the same effective input a
    live run's own sequential writes would have produced. Live callers
    never pass `plan_text` -- `None` preserves the original
    read-from-disk behavior verbatim."""
    if plan_text is not None:
        original = plan_text.replace("\r\n", "\n")
    else:
        if not os.path.exists(plan_path):
            print(f"close-out-and-stamp: plan not found: {plan_path}", file=sys.stderr)
            return 1

        with open(plan_path, "r", encoding="utf-8", newline="") as f:
            original = f.read()
        original = original.replace("\r\n", "\n")

    split = split_frontmatter(original)
    if split is None:
        print(
            f"close-out-and-stamp: no parseable YAML frontmatter in {plan_path}",
            file=sys.stderr,
        )
        return 1

    status = read_fm_field(split.fm_text, "status")
    if status is None or status.startswith("#"):
        print(
            f"close-out-and-stamp: no \"status\" field found in frontmatter of {plan_path}",
            file=sys.stderr,
        )
        return 1
    status = _strip_unquoted_trailing_comment(status)
    if status and status[0] in ("'", '"') and not status.endswith(status[0]):
        print(
            f"close-out-and-stamp: status value appears to carry a "
            "quoted-scalar-plus-trailing-comment, which stamp-landed does not "
            f"support -- remove the inline comment or the quotes ({plan_path})",
            file=sys.stderr,
        )
        return 1
    status = unquote_yaml_scalar(status)

    if status in _FROZEN_STATUSES:
        print(f"close-out-and-stamp: {plan_path} status \"{status}\" is terminal/deferred — no-op")
        return 0
    if status == _LANDED_STATUS:
        print(f"close-out-and-stamp: {plan_path} status already \"landed\" — no-op")
        return 0
    if status not in _FLIPPABLE_STATUSES:
        print(
            f"close-out-and-stamp: unexpected current status \"{status}\" for stamp-landed",
            file=sys.stderr,
        )
        return 1

    fm_text = replace_fm_field(split.fm_text, "status", _LANDED_STATUS)
    rebuilt = rebuild(split, fm_text)

    if not dry_run:
        with open(plan_path, "w", encoding="utf-8", newline="") as f:
            f.write(rebuilt)

    suffix = " (dry-run, not written)" if dry_run else ""
    print(f"close-out-and-stamp: {plan_path} status \"{status}\" → landed{suffix}")
    return 0


_CLOSE_OUT_PARTIAL_FIELD = "close_out_last_partial"


def _close_out_partial_stamp_value(
    missing_chunk_ids: list[str], join_provenance: str
) -> str:
    """The single-line value `_stamp_close_out_partial_evaluation` writes --
    a UTC timestamp plus the exact verdict this run reached, so a later
    reader sees not just THAT a partial evaluation happened but WHAT it
    found and WHEN (see that function's own docstring for the defect this
    closes)."""
    timestamp = (
        datetime.datetime.now(datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    ids = ",".join(missing_chunk_ids)
    return f"{timestamp} -- {len(missing_chunk_ids)} missing ({join_provenance}): {ids}"


def _stamp_close_out_partial_evaluation(
    plan_text: str,
    missing_chunk_ids: list[str],
    join_provenance: str,
) -> Optional[str]:
    """Defect 2 fix (2026-08-06): "a skipped stamp is indistinguishable from
    an unrun one." Before this fix, the halted/partial branch of
    `close_out_and_stamp` wrote NOTHING to the plan at all unless AC8's
    auto-resolve happened to fire on some OTHER row -- so a plan this
    ceremony genuinely evaluated and correctly declined to stamp (missing
    chunks, correctly reported) was, on disk, byte-identical to a plan
    nobody had ever run this ceremony against at all. A later reader (human
    or another op) had no way to tell "ran, found partial, correctly
    declined" from "never evaluated" short of re-running the whole ceremony
    and hoping to catch a stale git-log range.

    The fix reuses this module's OWN existing write mechanism -- the
    `replace_fm_field`/`insert_fm_field`/`rebuild` frontmatter-stamp
    primitives `_stamp_plan_landed` (above) and `plan_status_transition`
    are both already built on, already imported into this module -- rather
    than a second writer, a new state file, or a new artifact type: it
    writes ONE additional scalar frontmatter field,
    `close_out_last_partial:`, recording the UTC timestamp this run
    evaluated the plan plus its own verdict (missing-chunk-id list and
    `join_provenance`, `_close_out_partial_stamp_value`'s own format).
    This is the SAME kind of write this op already makes elsewhere (a
    single frontmatter scalar), just a second field instead of `status:` --
    not a new mechanism, and not a plan-body/Dispatch-Ledger/Tasks-spine
    write, which stay exactly as untouched as they always have been (this
    module's own docstring negative-spec on `status:` being the ONLY write
    describes the SHIPPED path's stamp; this field is the halted path's own
    analogous, narrower stamp).

    Fires ONLY on the halted path (`missing_chunk_ids` non-empty -- callers
    gate this), and is skipped entirely when the plan's frontmatter cannot
    be split at all (mirrors every other write helper's degrade-safe
    posture: never crash on an unparseable document; `close_out_and_stamp`
    itself has already refused earlier for that case in practice, but this
    helper does not assume that invariant holds forever).

    Idempotent by PRESENCE, not by value, ON THE HALTED PATH ONLY
    (deliberate choice, NOT the obvious "always refresh the timestamp"
    design): while this plan keeps evaluating as halted, once
    `close_out_last_partial:` exists at all, a later halted-path call
    NEVER rewrites it, even when this run's own verdict (missing-id list,
    provenance) differs from what is already stamped. The alternative --
    rewriting on every call -- was tried and reverted during this fix's own
    test pass: it silently broke the pre-existing "a halted plan with
    nothing new to report is a genuine no-op" guarantee
    (`TestCloseOutAndStampContinued
    ::test_partially_shipped_with_nothing_committed_at_all_is_a_genuine_
    noop`, `TestIdempotentRerunDoesNotAttemptAZeroDiffCommit`'s sibling
    invariant for the halted path) -- every repeat close-out call against
    an unchanged, still-halted plan would mint a fresh commit purely from
    the timestamp ticking forward, which is not what "evaluated and found
    partial" needs to mean. One evaluation record per plan is sufficient
    to answer the question this fix exists for ("has ANYONE ever run
    this ceremony against this plan") -- a STALE recorded verdict is a
    strictly better failure mode than a perpetually dirtying halted plan,
    and `missing_chunk_ids` in this call's own live return value is
    already the CURRENT verdict regardless of what the frontmatter
    field's own last-recorded snapshot says. This never-rewrite-on-repeat
    posture is UNCHANGED and still load-bearing -- nothing about it moved.

    Note (Review: coordinator:code-reviewer -- SUPERSEDED, C1,
    2026-08-08): this note originally claimed the field is "NOT cleared or
    refreshed" once a plan ships, and that a stale field is "harmless to
    any programmatic reader (`status:` always dominates)". Both claims are
    now FALSE, not merely stale. `close_out_and_stamp`'s certified-ship
    path (`status_target == "implemented"`) now clears
    `close_out_last_partial:` from the plan's frontmatter BEFORE calling
    `archive_stamp.cs_stamp_plan_implemented` -- see that call site's own
    comment for why the clear must precede the stamp, not follow it. So a
    plan that ships via this op's own certified path, WHEN THE STAMP ITSELF
    SUCCEEDS, never carries a stale marker at all. A stamp failure after the
    clear (`stamp_rc not in (0, 2)`) is a narrower, different case -- the
    call site now restores the marker on disk before returning, so this
    still does not leave a stray false-clean marker behind; see that call
    site's own comment for the reachable scenario this covers (a real,
    non-no-op status flip whose own commit attempt fails). The "harmless to
    any programmatic reader" half is doubly
    stale: `coordinator_core.workstream_complete` now reads this field as a
    programmatic signal in its own right (leg A) -- the exact reader this
    claim did not anticipate -- so a stray, uncleared marker is no longer
    harmless-by-construction; it is load-bearing input to a different op.

    Returns the rewritten plan text, or `None` when nothing was written
    (frontmatter unparseable, OR the field is already present -- both
    read as "no write needed" to the caller identically). Pure -- like
    `_stamp_plan_landed`'s own transition-decision half, this performs NO
    disk I/O itself; the caller (`close_out_and_stamp`) owns the single
    live-path write, gated on `dry_run` exactly the way its other two
    stamp branches already are."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    if read_fm_field(split.fm_text, _CLOSE_OUT_PARTIAL_FIELD) is not None:
        return None
    value = _close_out_partial_stamp_value(missing_chunk_ids, join_provenance)
    new_fm = insert_fm_field(
        split.fm_text, _CLOSE_OUT_PARTIAL_FIELD, value, after_key="status"
    )
    return rebuild(split, new_fm)


def _clear_close_out_partial_marker(plan_text: str) -> Optional[str]:
    """Certified-ship counterpart to `_stamp_close_out_partial_evaluation`
    (C1, 2026-08-08 -- `docs/plans/2026-08-08-a-status-field-cannot-vouch-
    for-itself.md`): removes `close_out_last_partial:` from `plan_text`'s
    own frontmatter, via the same `remove_fm_field`/`rebuild` primitives
    every other write in this module already composes over -- not a second
    writer.

    Returns the rewritten text, or `None` when there is nothing to clear
    (frontmatter unparseable, OR the field is already absent) -- identical
    "no write needed" contract to `_stamp_close_out_partial_evaluation`'s
    own return convention, so callers can gate a write on `is not None`
    the same way in both directions.

    Placement is load-bearing and MUST NOT be re-derived by a future
    reader: the only caller (`close_out_and_stamp`'s `status_target ==
    "implemented"` branch) calls this, and writes the result to
    `live_path`, BEFORE calling `archive_stamp.cs_stamp_plan_implemented`
    -- never after. `cs_stamp_plan_implemented` forwards to
    `plan_status_transition._stamp_implemented`, which reads the LIVE FILE
    off disk itself (via its own locked read-modify-write) and commits its
    write itself; this module's own in-memory `text` is never re-read
    after that call returns. A write-back mirroring the halted-path
    marker's OWN placement (mutate `text`, write it out AFTER the stamp
    call) would silently REVERT `status: implemented` back to whatever
    status `text` still held at that point, since `text` predates the
    stamp's own disk write. Clearing first means `plan_status_transition`'s
    `locked_rmw` reads the already-cleaned file and flips `status:` in the
    SAME read-modify-write, landing both changes in one commit."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    if read_fm_field(split.fm_text, _CLOSE_OUT_PARTIAL_FIELD) is None:
        return None
    new_fm = remove_fm_field(split.fm_text, _CLOSE_OUT_PARTIAL_FIELD)
    return rebuild(split, new_fm)


def _dry_run_scratch_plan(text: str, suffix: str) -> Path:
    """`--dry-run` support helper: materializes `text` (this ceremony's own
    in-memory plan content -- reflecting any dry-run-computed AC8
    auto-resolve change, which a dry run never persists to disk) into a
    throwaway file in the system temp directory.

    Why this exists (composition, not duplication -- see this module's own
    docstring § "Composition, not duplication"): `archive_stamp.
    cs_stamp_plan_implemented` has no in-memory-text entry point of its own
    -- it forwards straight to `plan_status_transition.main(["stamp-
    implemented", "--plan", ...])`, a byte-parity port of the node
    stamp-implemented oracle that reads its `--plan` argument from disk
    internally. Re-deriving that port's own frozen/flippable/unexpected-
    status decision matrix locally (the way `_stamp_plan_landed` above
    does for the DIFFERENT `landed` transition) would be exactly the kind
    of second, drifting implementation this module's docstring warns
    against for the `implemented` transition specifically -- there IS a
    canonical single writer for it, `cs_stamp_plan_implemented`, and this
    op composes over it rather than parallel-implementing it (see
    `close_out_and_stamp`'s own docstring, "Composition, not
    duplication").

    So a `--dry-run` call to the `implemented` transition instead invokes
    the REAL, canonical function -- unmodified -- against a scratch COPY of
    the plan's current in-memory content, never the live file. The
    resulting exit code is therefore byte-identical to what a live call
    would report (same function, same input bytes), while the live file on
    disk is never touched. Caller deletes the returned path once done (see
    `close_out_and_stamp`'s own `--dry-run` call site, which does so in a
    `finally:` block) -- this helper does not clean up after itself, since
    the caller needs the file to exist for the duration of the call it
    wraps."""
    fd, tmp_name = tempfile.mkstemp(prefix="close-out-and-stamp-dry-run-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return Path(tmp_name)


def _reach_post_commit_tail_stub_close(
    root: Path,
    plan_path_rel: str,
    committed_sha: str,
    delivery_proof: Optional[dict] = None,
) -> dict:
    """Give this ceremony's own successful commit reach to
    `post_commit_tail`'s origin-stub-close leg (step 5d) -- the SAME
    composition `ceremony.wsc_tail` already invokes in-process (see
    `post_commit_tail.run()`'s own docstring), never a second copy of its
    join/scan/guard logic.

    `delivery_proof` (optional; PM ruling -- let a positive, complete
    delivery proof close the origin stub directly, with the live-children
    guard retained as the fallback for callers holding no such proof) is
    forwarded VERBATIM into `post_commit_tail.run()`, which forwards it
    verbatim into `handoff.close_origin_stub`'s own `delivery_proof` param --
    this function neither builds nor validates it; the caller (this
    function's own two call sites, both inside the `status_target ==
    "implemented"` branch, the only place this ceremony has a proof to give)
    constructs it from THIS run's own already-computed `shipped`/
    `join_provenance`/`missing`/`plan_deliverable_id`/`status_target`
    values -- see `handoff_close_origin_stub._is_complete_delivery_proof`
    for the exact completeness conditions it is checked against downstream.
    `None` (the default) preserves today's guard-only behaviour exactly.

    Both `/execute-plan`'s close-out and `/mise-en-place`'s per-baton tail
    land here through the SAME `coordinator/bin/close-out-and-stamp` ->
    `close_out_and_stamp()` call path, so wiring this one call site gives
    both ceremonies reach in one place (spec: docs/plans/2026-08-04-
    terminal-state-propagation-join-keys.md § C5).

    `chain_terminal=False` on the composed call is deliberate: it makes
    `post_commit_tail.run()`'s OTHER composed step
    (`consumed_handoff_stamp.post_commit_stamp_and_ship`) a documented,
    side-effect-free no-op here -- this ceremony has no WSC session id and
    owns no consumed-handoff set of its own, and stamping consumed handoffs
    is `ceremony.wsc_tail`'s job, not this one's. Only the origin-stub-close
    leg is a genuine reach target for this ceremony.

    `initial_consumed=[]` -- this ceremony resolves no consumed handoffs of
    its own; the plan path alone (via `governing_plan_slug`) is the join
    key `_run_origin_stub_close` needs (C1's `deliverable_id` fallback join
    is what makes that plan-path-only join actually resolve something).

    Never raises -- `post_commit_tail.run()`'s origin-stub-close leg is
    already soft-fail-and-record internally; a failure here surfaces only
    inside the returned `{acted, skipped, failed}` dict.
    """
    governing_plan_slug = Path(plan_path_rel).stem
    common_dir = git_common_dir(root)

    async def _run() -> post_commit_tail.PostCommitTailOutcome:
        return await post_commit_tail.run(
            root,
            common_dir,
            "",
            committed_sha,
            chain_terminal=False,
            governing_plan_slug=governing_plan_slug,
            initial_consumed=[],
            close_origin_stub_handler=_close_origin_stub_handler,
            delivery_proof=delivery_proof,
        )

    import asyncio

    outcome = asyncio.run(_run())
    return outcome.origin_stub_result


def _stage_paths_committed_already(root: Path, stage_paths: Sequence[str]) -> bool:
    """True iff none of `stage_paths` carries any uncommitted change
    (staged or unstaged) per `git status --porcelain` -- i.e. this ceremony's
    own writes to those paths already landed in a commit made by SOMEONE
    ELSE before this op's own commit leg got a chance to run.

    Exists for the DR-272 interaction (`plan_status_transition._commit_plan_
    flip`, 2026-08-05/06): `cs_stamp_plan_implemented` -> `_stamp_implemented`
    now commits its own real (non-no-op) status flip immediately, under its
    own name, via `git_native.commit_authored_content` -- so by the time
    control returns to THIS function, the plan doc this op just stamped (and,
    when AC8 also fired, auto-resolved) is very often ALREADY sitting at
    HEAD, byte-identical to the worktree. `run_commit_pipeline`'s own
    `explicit_stage` does not distinguish that from a genuinely dirty path --
    it includes any EXISTING path in `stage_paths` in its computed
    `commit_paths` regardless of whether that path actually differs from
    HEAD -- so without this check `close_out_and_stamp` would always reach a
    real `git commit` with nothing to commit, which git (correctly) refuses
    with a bare `exit_code=1`. See this function's only call site for how
    the two are told apart.

    What's now true (W3, docs/plans/2026-08-08-a-landed-commit-reported-as-
    failed.md): this specific `exit_code=1` -- the genuine "nothing to
    commit" no-op -- is NOT the defect that plan fixes. `commit_pipeline.
    commit()` (W1) still sets `landed=False` on exactly this path by design
    (see `CommitOutcome.landed`'s own docstring: "the ordinary 'nothing to
    commit' empty-commit-set exit 1... must keep `landed=False`"), so
    `run_commit_pipeline` still reports it as an ordinary `commit_failed=
    True`, same as before W1/W2 -- this function's own check below remains
    the correct, load-bearing way to tell that apart from a genuine refusal.
    The DEFECT W1/W2/W3 fix is a DIFFERENT `exit_code=1` shape entirely: a
    commit that DID land (history changed) but whose sha could not be
    resolved (`PipelineResult.sha_unverified`) -- see this module's own
    `elif pipeline_result.sha_unverified:` branch at this function's call
    site for how that state (never reaching this function at all, since it
    is not "nothing landed") is now rendered honestly instead.

    Deliberately narrower than a repo-wide dirty check: scoped to exactly
    `stage_paths` (this op's own pathspec), so a live peer session's
    unrelated dirty file elsewhere in the shared worktree never influences
    this decision -- mirrors `run_commit_pipeline`'s own gate_paths scoping.
    """
    result = git_native.status_porcelain(root)
    if not result.ok:
        # A porcelain-query failure is not evidence of "already committed" --
        # fall through to the ordinary commit attempt, which will surface
        # its own diagnostic if something is genuinely wrong.
        return False
    dirty_paths = {line[3:] for line in result.stdout.splitlines() if line}
    return not (dirty_paths & set(stage_paths))


_AC_HEADING_RE = re.compile(r"^## Acceptance Criteria\s*$", re.MULTILINE | re.IGNORECASE)
"""Anchors the plan's `## Acceptance Criteria` section -- the PM-authored
surface the reviewer's finding names as never consulted by this module's
own spine-only completeness oracle (see this module's docstring, and
`_ac_table_desync_finding` below).

Case-insensitive (Review: code-reviewer -- Finding [P3], 2026-08-06): real
plans vary the heading's casing (`## Acceptance criteria`, `## acceptance
CRITERIA`); a case-sensitive match silently reads those as "no AC heading
at all" and the desync check goes quiet on exactly the plans it exists to
watch. Widening costs nothing on the false-positive axis -- this regex
only decides WHERE the AC section starts, never whether a row reads
resolved -- and the failure direction if some other `## ...` heading ever
coincidentally matched case-insensitively would still only ever be "no
finding" or a mis-scoped section that itself degrades to `_parse_ac_table_
rows` returning `None`, never a spurious desync report (C3)."""

_AC_NEXT_HEADING_RE = re.compile(r"^## ", re.MULTILINE)

_AC_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_AC_TABLE_SEPARATOR_RE = re.compile(r"^[\s|:-]+$")

_AC_UNRESOLVED_STATUS_RE = re.compile(r"^(pending|todo|tbd|open)?$", re.IGNORECASE)
_AC_STRIKETHROUGH_CELL_RE = re.compile(r"^~~(.+)~~$", re.DOTALL)
"""Matches a `Status` cell whose ENTIRE (whitespace-trimmed) content is
struck through, e.g. `~~pending~~` -- see `_ac_status_is_unresolved` for
why this must be checked BEFORE `strip("*_~")` runs (Review: code-reviewer
-- Finding [P2], 2026-08-06): stripping the delimiters first collapses
`~~pending~~` down to the bare vocabulary word `pending`, silently
defeating the deliberate "struck-through text reads as author-resolved"
rule this constant's own sibling `_AC_UNRESOLVED_CHECKBOX_GLYPHS` already
documents. Only a WHOLE-cell strikethrough counts -- a cell that merely
contains a struck-through fragment alongside other text (`~~pending~~,
now green`) does not match this anchor and falls through to the ordinary
vocabulary check on its own (post-strip) merits."""
_AC_UNRESOLVED_CHECKBOX_GLYPHS = frozenset({"☐"})
"""The narrow, corpus-derived set of AC `Status` cell values this check
treats as UNRESOLVED (D1/C1) -- deliberately conservative (a check with a
high false-positive rate is worse than none, per this stub's own Report
instruction): a blank cell, `pending`, `todo`, `tbd`, `open`, or an empty
checkbox glyph `☐`. Everything else -- `✅`, `☑`, `green (<sha>)`,
`done (<chunk>)`, `superseded`, struck-through `~~...~~` text, or any other
free-form terminal-looking value -- is treated as RESOLVED, on purpose:
this check exists to catch the specific desync the reviewer named (spine
fully resolved, AC table still reads exactly like nobody ever touched it),
not to adjudicate every possible AC status vocabulary a plan author might
invent. See `docs/plans/*.md` § Acceptance Criteria for the corpus this
was derived from, 2026-08-06."""


def _ac_section_text(plan_text: str) -> Optional[str]:
    """The plan body slice between its own `## Acceptance Criteria` heading
    and the next `## ` heading (or end of document) -- `None` when the plan
    carries no such heading at all. Mirrors `_reconcile_tracker_section`'s
    own heading-to-next-heading slicing convention above, applied to the
    plan doc instead of the tracker."""
    match = _AC_HEADING_RE.search(plan_text)
    if match is None:
        return None
    start = match.end()
    next_heading = _AC_NEXT_HEADING_RE.search(plan_text, start)
    end = next_heading.start() if next_heading else len(plan_text)
    return plan_text[start:end]


def _parse_ac_table_rows(section_text: str) -> Optional[list[tuple[str, str]]]:
    """Parses a markdown pipe-table's rows out of an `## Acceptance
    Criteria` section slice, returning `[(ac_id, status_cell), ...]` in
    document order, or `None` when no recognizable table is present (no
    pipe-rows at all, fewer than a header+separator+one data row, or a
    header row with no cells to key off of) -- callers treat `None` as "no
    finding", never as an error (C3: degrade quietly, never raise).

    Corpus shapes observed (`docs/plans/*.md`, 2026-08-06 read-through):
    `| # | Criterion | Status |`, `| ID | Criterion | Status |`, with
    either `|---|---|---|` or `| --- | --- | --- |` separator spacing --
    the `Status` column is located BY HEADER NAME (case-insensitive
    substring match), never a fixed column index, since column order and
    count both vary across plans (some carry only `ID`/`Criterion`/
    `Status`, at least one observed 2-column variant with no `#`/`ID`
    column at all). Falls back to the LAST column when no header cell
    mentions "status" -- still the common convention even when unnamed.

    A row whose cell count doesn't match the header's is skipped rather
    than guessed at (malformed row -- C3's degrade-quietly posture applies
    per-row too, not just to the table as a whole)."""
    raw_rows: list[list[str]] = []
    for line in section_text.splitlines():
        match = _AC_TABLE_ROW_RE.match(line)
        if match is None:
            continue
        raw_rows.append([cell.strip() for cell in match.group(1).split("|")])
    if len(raw_rows) < 2:
        return None

    header = raw_rows[0]
    if not header or not any(header):
        return None

    status_idx: Optional[int] = None
    for idx, cell in enumerate(header):
        if "status" in cell.lower():
            status_idx = idx
            break
    if status_idx is None:
        status_idx = len(header) - 1

    body_rows = [
        row for row in raw_rows[1:] if not _AC_TABLE_SEPARATOR_RE.match("|".join(row))
    ]
    if not body_rows:
        return None

    parsed: list[tuple[str, str]] = []
    for row in body_rows:
        if len(row) != len(header) or status_idx >= len(row):
            continue
        ac_id = row[0] if row[0] else "?"
        parsed.append((ac_id, row[status_idx]))
    if not parsed:
        return None
    return parsed


def _ac_status_is_unresolved(status_cell: str) -> bool:
    """True iff an AC table `Status` cell value reads as still-unresolved
    per `_AC_UNRESOLVED_CHECKBOX_GLYPHS`'s own narrow, corpus-derived
    vocabulary -- see that constant's docstring for what is and is not
    included, and why.

    Whole-cell strikethrough short-circuits to RESOLVED (Review:
    code-reviewer -- Finding [P2], 2026-08-06) BEFORE the `*_~` delimiter
    strip below runs: stripping first would collapse `~~pending~~` down to
    the bare word `pending`, which the vocabulary check would then flag
    unresolved -- exactly backwards from `_AC_UNRESOLVED_CHECKBOX_GLYPHS`'s
    own documented intent that struck-through prose reads as
    author-resolved. See `_AC_STRIKETHROUGH_CELL_RE`'s own docstring."""
    trimmed = status_cell.strip()
    if _AC_STRIKETHROUGH_CELL_RE.match(trimmed):
        return False
    cleaned = trimmed.strip("*_~").strip()
    if cleaned in _AC_UNRESOLVED_CHECKBOX_GLYPHS:
        return True
    return bool(_AC_UNRESOLVED_STATUS_RE.match(cleaned.lower()))


def _ac_table_desync_finding(
    plan_text: str, spine_fully_resolved: bool
) -> Optional[dict[str, Any]]:
    """Advisory-only desync detector (C1/C2 -- eng-director review finding,
    2026-08-06): fires when every commit-required `## Tasks` spine row has
    already reached a terminal, verified disposition (`spine_fully_
    resolved`, computed by the caller from the SAME `shipped`/`fully_
    resolved` verdict this module already derives for its own stamp
    decision -- no second completeness oracle here) while the plan's own
    `## Acceptance Criteria` table still carries at least one row this
    check reads as unresolved.

    Returns `None` -- "no finding" -- whenever `spine_fully_resolved` is
    `False` (the spine itself isn't done; an AC table lagging a plan that
    hasn't shipped is not this check's concern), the plan has no `##
    Acceptance Criteria` heading at all, its table is unparseable/
    malformed/absent (`_parse_ac_table_rows` returning `None`), or every
    row it DID parse reads as resolved. Never raises -- every parse step
    above is a pure string/regex operation over already-in-memory text, but
    the whole computation is wrapped in a broad `except Exception` guard
    anyway (mirrors `compute_open_spine_row_gate`'s own degrade-never-raise
    posture for this exact shape of advisory check -- see
    `coordinator_core/workstream_complete/directives_spine_worklist.py`)
    so a corpus shape this parser did not anticipate can never turn an
    advisory check into a new stamp-path failure mode.

    C2's own load-bearing constraint lives entirely in the CALLER, not
    here: this function only ever produces a finding dict for the result
    payload and message text -- it has no access to, and never touches,
    the stamp decision itself."""
    if not spine_fully_resolved:
        return None
    try:
        section = _ac_section_text(plan_text)
        if section is None:
            return None
        rows = _parse_ac_table_rows(section)
        if rows is None:
            return None
        unresolved_ac_ids = [
            ac_id for ac_id, status_cell in rows if _ac_status_is_unresolved(status_cell)
        ]
        if not unresolved_ac_ids:
            return None
        return {
            "unresolved_ac_ids": unresolved_ac_ids,
            "total_ac_rows": len(rows),
        }
    except Exception:
        return None


def close_out_and_stamp(
    plan_path: str, *, repo_root: Optional[Path] = None, dry_run: bool = False
) -> tuple[int, dict[str, Any]]:
    """Decide full-shipped vs. halted, stamp `status: implemented` on the
    full-shipped path only, then land one scoped commit covering every path
    this ceremony itself changed.

    `dry_run` (2026-08-04 -- see this module's own docstring and
    `coordinator/bin/close-out-and-stamp`'s `--dry-run` usage text for the
    incident this closes: a caller with no way to observe this ceremony's
    verdict short of MUTATING had no choice but to run the mutating path
    purely to read it, and did): one computation, two dispositions of the
    SAME result -- every read, decision, and diagnostic below (the shipped/
    halted oracle, the deliverable-id/disposition_ref evidence unions, the
    implemented-vs-landed-vs-halted status target, the AC8 auto-resolve
    row-matching, the fidelity check) runs IDENTICALLY regardless of
    `dry_run`. The three write sites this ceremony owns are individually
    gated on it instead:

      1. AC8's auto-resolve backfill (`_auto_resolve_committed_open_rows`)
         -- computes and fidelity-checks the same `new_text` either way;
         its own final `live_path.write_text` is skipped under `dry_run`.
      2. The `status:` stamp -- `_stamp_plan_landed` skips its own final
         disk write under `dry_run` (same transition decision either way);
         the `implemented` transition instead invokes the REAL, unmodified
         `archive_stamp.cs_stamp_plan_implemented` against a throwaway
         scratch COPY of this run's current in-memory plan text
         (`_dry_run_scratch_plan`) rather than re-deriving that function's
         own decision matrix a second time -- see that helper's own
         docstring for why composing over the real writer (pointed at a
         copy) is the shared-computation-preserving choice here, not a
         parallel implementation.
      3. The commit leg (`run_commit_pipeline`) is not invoked at all under
         `dry_run` -- there is no scratch-copy equivalent for "stage and
         commit into THIS repo's real history", so this leg is skipped
         outright rather than simulated; `commit_result` reports what WOULD
         have been staged instead.

    The returned result dict always carries `"dry_run": bool` (present on
    every return, including `EXIT_BUSINESS_FAIL`) so a caller can never
    mistake a preview for a completed close-out.

    Commit-leg path set (Defect 3 fix, 2026-07-27): this op's ONLY write is
    the plan's own `status:` frontmatter field (via `cs_stamp_plan_implemented`
    on the shipped path; nothing at all on the halted path). The prior
    implementation shelled out to `coordinator-safe-commit` in its
    liveness-auto-detecting default mode with no scope at all, which that
    binary correctly refuses under ordinary multi-session concurrency ("this
    repo's NORMAL state" -- Defect 3 report). The fix runs the commit
    in-process through `run_commit_pipeline` with an EXPLICIT `stage_paths`
    of exactly `[plan_path_rel]` -- the one path this function is capable of
    having changed -- never a broad/auto-detected scope, so a peer session's
    concurrently-dirty files on the same branch are never swept in. This is
    intentionally NOT "the plan document itself, plus other changed paths"
    (the docstring language a prior draft of this fix used) -- there ARE no
    other paths this op ever touches, so the plan path alone IS the
    complete, defensible non-empty set.

    Commit leg gated on `wrote_anything` (`stamped or auto_resolved`), not
    unconditional (correction during this fix's own test pass; corrected
    AGAIN, review finding, 2026-07-27 -- see below): `run_commit_pipeline`'s
    empty-`commit_paths` short-circuit fires ONLY when `stage_paths` resolves
    to nothing stageable at all (a missing/swept path) -- it does NOT detect
    "staged content is byte-identical to HEAD". If this op wrote nothing, but
    the commit leg still ran anyway, `plan_path_rel` would still exist and
    still stage cleanly with zero actual diff, and the underlying
    `git commit -- plan_path_rel` would fail loud ("nothing to commit",
    exit 1) rather than no-op. `wrote_anything` is this op's own single
    source of truth for "did I change anything"; the commit leg runs ONLY
    when it is `True`, and is skipped entirely (`committed_sha=None`,
    `commit_failed=False`) rather than attempted and caught otherwise.

    `stamped` alone is NOT that source of truth, and must not be read as
    one (review finding, 2026-07-27): `_stamp_plan_landed` and
    `plan_status_transition._stamp_implemented` (via
    `cs_stamp_plan_implemented`) BOTH return `rc == 0` on a documented
    no-op branch (status already terminal; landed's stamp additionally
    no-ops on an already-"landed" status) with NO on-disk write. `stamped`
    is set from `_peek_plan_status`'s PRE-CALL read of the plan's current
    status, cross-referenced against the exact no-op conditions each
    helper documents -- not from the stamp call's bare return code -- so
    it is `True` only when that branch's own call genuinely wrote. This
    keeps a repeated `close_out_and_stamp` call against an
    already-shipped, fully-resolved plan a genuine no-op end to end: no
    stamp write, `wrote_anything` stays `False` (assuming AC8 also finds
    nothing new to auto-resolve), and the commit leg is skipped rather
    than attempted against zero diff.

    Repo-identity gate (C4a, 2026-08-11 -- see
    `docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md`):
    `coordinator_core.pickup_assemble.compute_repo_identity_gate(root, sid)`
    is called once `root` is resolved. cwd-derived-only: this call refuses
    (existing `EXIT_BUSINESS_FAIL`/`{"error": ...}` vocabulary, carrying the
    gate's own `message`) ONLY on a `MISMATCH` verdict AND only when the
    caller did NOT pass an explicit `repo_root` -- an explicitly-supplied
    root is the caller's own choice and never second-guessed here.
    `UNRESOLVED` never refuses (DR-277: hardening it into a refusal turns a
    fail-open guard into a fleet-wide ceremony outage). Both `MATCH` and
    `UNRESOLVED` (and `MISMATCH` on an explicit-root call, which is not
    refused) are carried informationally on the `EXIT_OK` return as
    `"gates": {"repo_identity": <gate's own returned dict>}`.

    Returns `(exit_code, result_dict)`:
      - `EXIT_OK` with `{"shipped": bool, "stamped": bool,
        "missing_chunk_ids": [str, ...], "deliverable_id_mismatch": [...],
        "commit": {...}, "message": str, "skipped_sibling_repos": [str, ...],
        "gates": {"repo_identity": {...}}}`
        on success. `skipped_sibling_repos` (Defect fix, 2026-07-27 -- see
        this module's docstring § Cross-repo scope scanning) is `[]` when
        the plan's `scope:` names no sibling repo, or every named sibling
        was scanned successfully; otherwise each entry is a human-readable
        `"<repo-id>: <reason>"` string naming a sibling this run could NOT
        scan (never cloned here, unregistered, or a git-log query failure)
        -- present on this key even on a commit-failure `EXIT_BUSINESS_FAIL`
        return, since that failure is about the commit leg, not the
        completeness scan that already ran. `deliverable_id_mismatch`
        (2026-08-01 -- see `_deliverable_id_near_miss_diagnostics`) is `[]`
        whenever `missing_chunk_ids` is empty (only computed on the
        unhappy path) OR no near-miss `Deliverable-Id` trailer candidate
        was found; otherwise a list of `{"deliverable_id": str,
        "commit_count": int}` dicts, one per distinct near-miss value on
        this branch's own chunk-shaped commits, sorted by `commit_count`
        descending -- the diagnostic CAUSE behind a zero/under-counted
        `missing_chunk_ids` verdict, present alongside it even on a
        commit-failure `EXIT_BUSINESS_FAIL` return for the same reason
        `skipped_sibling_repos` is. `hyphen_range_subjects` (2026-08-05 --
        see `_hyphen_range_subject_diagnostics`) is `[]` whenever
        `missing_chunk_ids` is empty, this plan carries no
        `deliverable_id:`, or no commit subject used `-` to join what looks
        like a chunk-id list over still-missing spine ids; otherwise a list
        of `{"sha": str, "subject": str, "spanned_chunk_ids": [str, ...]}`
        dicts, one per offending commit -- a THIRD, distinct diagnostic
        cause from `deliverable_id_mismatch` above (a subject convention
        not followed, never a Deliverable-Id value disagreement), present
        alongside `missing_chunk_ids` for the identical reason. Diagnostic
        only -- never changes `missing_chunk_ids`, `shipped`, or the
        `_extract_chunk_ids` separator set (`,`/`+`/`/`) it explains a gap
        in. `disposition_ref_rejections` (2026-08-04
        -- see this module's docstring § Plan-side disposition_ref evidence)
        is `{}` whenever `missing_chunk_ids` is empty, or every still-missing
        row's `disposition_ref` verified as real evidence; otherwise a
        `{chunk_id: reason}` map, one entry per still-missing chunk-id whose
        row carried a `disposition_ref` that did NOT verify, `reason` being
        one of `DISPOSITION_REF_ABSENT`/`DISPOSITION_REF_MALFORMED`/
        `DISPOSITION_REF_UNRESOLVABLE`/`DISPOSITION_REF_NOT_ANCESTOR` -- the
        specific cause a rejected disposition_ref did not count, present
        alongside `missing_chunk_ids` for the same reason the other
        diagnostics are. `join_provenance` (2026-08-06) is `_determine_
        shipped`'s own returned provenance string verbatim -- always
        present, including `JOIN_PROVENANCE_LEDGER_FALLBACK` when this run's
        verdict came from the legacy Dispatch Ledger fallback (Defect 1 fix)
        rather than the spine's own Deliverable-Id join.
        `partial_evaluation_stamped` (2026-08-06, Defect 2 fix -- see
        `_stamp_close_out_partial_evaluation`'s own docstring) is `True`
        whenever this run wrote the plan's `close_out_last_partial:`
        frontmatter field this call (halted verdict, non-empty `missing`);
        `False` on a shipped/landed verdict, or a halted verdict whose
        frontmatter could not be split at all. This is the durable trace
        that lets a LATER reader of the plan tell "this ceremony ran,
        evaluated, and correctly declined to stamp" apart from "nobody has
        ever run this ceremony against this plan" -- previously
        indistinguishable on a halted run with no accompanying AC8
        auto-resolve write.
      - `EXIT_BUSINESS_FAIL` with `{"error": str, ...}` on a resolution
        failure, a malformed spine, a failed stamp, or a failed commit --
        every `EXIT_BUSINESS_FAIL` dict also carries `"dry_run": bool`
        (see the `dry_run` parameter's own docstring section above), even
        the very earliest parse-failure returns, before any of this op's
        own writes were ever attempted.

    Both branches ALWAYS carry `"dry_run": bool` (present on every return),
    per the `dry_run` parameter's own docstring section above.
    """
    # Deferred: `coordinator_core.pickup_assemble` imports `coordinator_core.ops`,
    # whose eager registration walk reaches this module. A module-level import here
    # closes that cycle and drops the cascade ops from the registry.
    from coordinator_core.pickup_assemble import (
        _REPO_IDENTITY_MISMATCH,
        compute_repo_identity_gate,
        resolve_repo_root,
    )

    explicit_repo_root = repo_root is not None
    root = repo_root or resolve_repo_root()
    if root is None:
        return EXIT_BUSINESS_FAIL, {
            "error": "could not resolve a git worktree root",
            "dry_run": dry_run,
        }

    # Repo-identity gate (C4a) -- cwd-derived-only: an explicitly-supplied
    # `repo_root` still gets the informational `gates.repo_identity` entry
    # below, but never a refusal from it (the caller named the root, so a
    # cwd/registry-anchor divergence isn't this call's business). Only the
    # cwd-derived (`repo_root` omitted) path can refuse on MISMATCH.
    # UNRESOLVED never refuses either way (DR-277).
    sid = session_core.resolve_session_id(str(root)) or None
    repo_identity_gate = compute_repo_identity_gate(root, sid)
    if (
        repo_identity_gate["verdict"] == _REPO_IDENTITY_MISMATCH
        and not explicit_repo_root
    ):
        return EXIT_BUSINESS_FAIL, {
            "error": repo_identity_gate["message"],
            "dry_run": dry_run,
        }

    live_path = Path(plan_path) if Path(plan_path).is_absolute() else root / plan_path
    if not live_path.is_file():
        return EXIT_BUSINESS_FAIL, {"error": f"{plan_path}: not found", "dry_run": dry_run}

    try:
        text = live_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return EXIT_BUSINESS_FAIL, {
            "error": f"{plan_path}: could not read ({exc})",
            "dry_run": dry_run,
        }

    if split_frontmatter(text) is None:
        return EXIT_BUSINESS_FAIL, {
            "error": f"{plan_path}: no parseable frontmatter",
            "dry_run": dry_run,
        }

    # A5 fix: `rel_id` (not `str(...relative_to(...))`) -- `plan_path_rel`
    # is matched below against git-derived paths (`_determine_shipped`),
    # which are ALWAYS forward-slash; `str()` renders `os.sep`, so a
    # Windows session misclassifies shipped/missing chunks at plan
    # close-out.
    try:
        plan_path_rel = rel_id(live_path, root)
    except ValueError:
        plan_path_rel = plan_path

    shipped, missing, join_provenance, spine_error = _determine_shipped(
        text, plan_path_rel, root
    )
    if spine_error is not None:
        return EXIT_BUSINESS_FAIL, {"error": spine_error, "dry_run": dry_run}

    # Deliverable-Id near-miss diagnostic (2026-08-01) -- unhappy path ONLY
    # (`missing` non-empty; the happy path never pays for this extra
    # git-log call, see `_deliverable_id_near_miss_diagnostics`'s own
    # docstring). Never feeds back into `shipped`/`missing` -- diagnostic
    # only, explaining an already-decided verdict, not changing it.
    plan_deliverable_id = _plan_deliverable_id(text)
    deliverable_id_mismatch: list[dict[str, Any]] = (
        _deliverable_id_near_miss_diagnostics(root, plan_deliverable_id, missing, plan_text=text)
        if missing
        else []
    )

    # Hyphen-range-subject diagnostic (2026-08-05) -- unhappy path ONLY,
    # same posture as the near-miss diagnostic above: explains a THIRD,
    # distinct cause a still-non-empty `missing` can have (a `C1-C4: ...`-
    # style subject silently registering zero ids, since `-` is deliberately
    # NOT a recognized multi-id separator -- see `_hyphen_range_subject_
    # diagnostics`'s own docstring for the live incident and why the
    # separator set must not be widened to fix it). Never feeds back into
    # `shipped`/`missing` -- diagnostic only.
    hyphen_range_subjects: list[dict[str, Any]] = (
        _hyphen_range_subject_diagnostics(root, plan_deliverable_id, missing, plan_text=text)
        if missing
        else []
    )

    # Range-searched summary (2026-08-07 misdirection fix -- see this
    # module's own bug-backlog entry `2026-08-07-close-out-and-stamp-s-
    # chunk-evidence-joi-8b6a7a32d833.yaml`): unhappy path ONLY, same
    # posture as the two diagnostics above. Never a substitute for
    # `deliverable_id_mismatch`/`join_provenance` -- see
    # `_chunk_evidence_range_summary`'s own docstring.
    chunk_evidence_range: dict[str, Any] = (
        _chunk_evidence_range_summary(root, text) if missing else {}
    )

    # AC8: auto-resolve any committed-but-open row BEFORE deciding the
    # implemented/landed/halted target below -- a row this step resolves
    # must not still read as "open" when the target is chosen immediately
    # after (see _auto_resolve_committed_open_rows's own docstring for the
    # writer-choice rationale). Runs independently of `shipped` -- a
    # still-halted plan (some OTHER row's chunk-id missing) can still have
    # individual committed rows auto-resolved. Parsed here (rather than
    # after the sibling-skip-listing call below) so `spine_ids` -- the
    # `_extract_chunk_ids` multi-id-split bounding set, Defect fix
    # 2026-08-01 -- is available to BOTH that call and the auto-resolve
    # evidence query below, instead of each re-deriving it separately.
    rows, rows_error = _parse_spine_rows(text, plan_path_rel)
    if rows_error is not None:
        return EXIT_BUSINESS_FAIL, {"error": rows_error, "dry_run": dry_run}
    spine_ids = _all_spine_ids(rows) if rows else []

    # Plan-side disposition_ref rejection diagnostic (2026-08-04) -- unhappy
    # path ONLY, mirroring `deliverable_id_mismatch` above: names WHY a
    # still-missing row's own `disposition_ref` did not count as evidence
    # (see this module's docstring § Plan-side disposition_ref evidence).
    # `_determine_shipped` already unioned any VERIFIED disposition_ref
    # evidence into its own `committed` set before computing `missing`
    # above, so re-deriving rejections here for the same `rows` is a
    # read-only diagnostic re-scan, never a second verdict.
    disposition_ref_rejections: dict[str, str] = {}
    if missing and rows:
        _verified_ids, all_rejections = _disposition_ref_evidence(rows, root)
        disposition_ref_rejections = {
            chunk_id: reason
            for chunk_id, reason in all_rejections.items()
            if chunk_id in missing
        }

    # Cross-repo scope scanning (Defect fix, 2026-07-27): surface which
    # sibling repos (if any) this plan's own `scope:` named but this run
    # could not scan -- `_determine_shipped` above already folded any
    # SUCCESSFULLY-scanned sibling's evidence into `missing`; this second,
    # cheap call (same `_plan_sibling_repo_ids`/registry-resolve path,
    # `[]` immediately when `scope:` names no sibling) exists ONLY to
    # surface the skip list itself in this op's own result dict, per this
    # module's docstring "Degrade-safely, and say so".
    _unused_sibling_ids, skipped_sibling_repos = _sibling_committed_chunk_ids(
        text, _plan_deliverable_id(text), spine_ids
    )

    auto_resolved = False
    if rows:
        deliverable_id = _plan_deliverable_id(text)
        query_ok, _committed_ids, committed_shas, _join_stats = _committed_chunk_shas(
            root, deliverable_id, spine_ids, plan_text=text, plan_path_rel=plan_path_rel
        )
        if query_ok and committed_shas:
            new_text, resolve_error = _auto_resolve_committed_open_rows(
                live_path, text, rows, committed_shas, root, dry_run=dry_run
            )
            if resolve_error is not None:
                return EXIT_BUSINESS_FAIL, {
                    "error": (
                        f"{plan_path_rel}: auto-resolve of committed-but-open "
                        f"rows failed: {resolve_error}"
                    ),
                    "dry_run": dry_run,
                }
            if new_text is not None:
                auto_resolved = True
                text = new_text
                rows, rows_error = _parse_spine_rows(text, plan_path_rel)
                if rows_error is not None:
                    return EXIT_BUSINESS_FAIL, {"error": rows_error, "dry_run": dry_run}

    # AC7: `implemented` requires BOTH the code oracle (`shipped`) and the
    # resolution oracle (no row still `open`) -- `landed` is the
    # intermediate state where code is in but resolution isn't (D9).
    open_blocking = _open_blocking_chunk_ids(rows) if rows else []
    fully_resolved = not open_blocking

    # False-positive-stamp incident fix: `shipped=True` alone is no longer
    # sufficient to stamp -- `JOIN_PROVENANCE_NO_EVIDENCE_SOURCE` means
    # `shipped` reflects "nothing existed to check", not an evidence-backed
    # verdict (see that constant's own docstring). A plan carrying it is
    # refused a stamp here, same failure direction (false-negative over
    # false-positive) as every other conservative choice in this module.
    evidence_backed = join_provenance != JOIN_PROVENANCE_NO_EVIDENCE_SOURCE

    # AC-table/spine desync check (C1/C2 -- eng-director review finding,
    # 2026-08-06, standalone from the ratified succession-edge problem-set):
    # advisory ONLY, computed from the SAME shipped/fully_resolved verdict
    # already derived above -- never a second completeness oracle, and
    # never a new blocking exit (see `_ac_table_desync_finding`'s own
    # docstring for the full C1-C3 rationale).
    commit_required_ids = _commit_required_chunk_ids(rows) if rows else []
    spine_fully_resolved = bool(commit_required_ids) and shipped and fully_resolved
    ac_table_desync = _ac_table_desync_finding(text, spine_fully_resolved)

    if shipped and fully_resolved and evidence_backed:
        status_target: Optional[str] = "implemented"
    elif shipped and evidence_backed:
        status_target = _LANDED_STATUS
    else:
        status_target = None

    # Delivery proof for `_reach_post_commit_tail_stub_close` (PM ruling --
    # let a positive, complete delivery proof close the origin stub
    # directly). Built ONLY on the full-shipped path (`status_target ==
    # "implemented"`) -- the only branch this function's own reach call
    # sites run on -- from facts this run already computed (never re-derived
    # or re-joined): `plan_deliverable_id` (this plan's own frontmatter),
    # `join_provenance` (the SAME `_determine_shipped` verdict that gated
    # this stamp), `missing` (empty here by construction, since
    # `status_target == "implemented"` requires `fully_resolved`, which
    # requires `not open_blocking` -- `missing` is intersected into
    # `open_blocking` upstream), and the literal `status_target` value
    # itself. `handoff_close_origin_stub._is_complete_delivery_proof`
    # re-checks every one of these conditions independently -- this dict is
    # the claim, not a bypass of that check.
    delivery_proof: Optional[dict] = (
        {
            "deliverable_id": plan_deliverable_id,
            "join_provenance": join_provenance,
            "missing_chunk_ids": list(missing),
            "status": status_target,
            # Review: staff-eng Finding 0 -- `join_provenance == "joined"`
            # alone does not prove the join ran: `_determine_shipped`'s
            # `if not chunk_ids:` early branch also emits
            # JOIN_PROVENANCE_JOINED with an empty `missing` on ZERO
            # commit-required spine rows, without ever calling
            # `_committed_chunk_shas`. Carrying the already-computed
            # `len(commit_required_ids)` here lets
            # `_is_complete_delivery_proof` require positive evidence
            # existed to check in the first place.
            "commit_required_chunk_count": len(commit_required_ids),
        }
        if status_target == "implemented"
        else None
    )

    stamped = False
    if status_target == "implemented":
        # Pass the already-resolved ABSOLUTE live_path, not plan_path_rel --
        # cs_stamp_plan_implemented forwards straight to
        # plan_status_transition.main(["stamp-implemented", "--plan", ...]),
        # which resolves --plan against the process cwd (it has no repo-root
        # anchoring of its own -- see that module's os.path.exists/open calls).
        # This op is engine-dispatched with no cwd guarantee, so a repo-relative
        # path silently stamps the wrong file (or fails) whenever cwd != root.
        # live_path is absolute and already verified to exist/be readable
        # above, so this is strictly more correct than the relative string.
        # plan_path_rel is KEPT for the commit-subject strings and error
        # messages below -- those are human-facing display, repo-relative is
        # right there; only the stamp call needed the absolute path.
        pre_stamp_status = _peek_plan_status(text)
        # C1 (2026-08-08): clear `close_out_last_partial:` BEFORE the stamp
        # call, not after -- see `_clear_close_out_partial_marker`'s own
        # docstring for why a post-stamp write-back would revert the
        # `implemented` flip. The clear itself is NOT dry-run-gated -- it
        # folds into `text` unconditionally, identically on both legs; only
        # the LIVE-FILE WRITE below it is gated on `dry_run`. Under
        # `dry_run` the live file is never touched, so the cleared `text` is
        # instead materialized into the (already-existing) throwaway scratch
        # copy below, which picks it up the same way.
        #
        # Review: code-reviewer -- P2 finding, 2026-08-08: this pre-stamp
        # live-file write is not transactional with the stamp call
        # succeeding. `pre_clear_marker_value` captures the marker's raw
        # value (before the clear) so a failed stamp can restore it -- see
        # the `stamp_rc not in (0, 2)` handling below for why this matters:
        # `plan_status_transition._stamp_implemented` can flip `status:` to
        # `implemented` on disk via its own locked_rmw and STILL return a
        # failure rc (a real flip whose own commit attempt fails, or whose
        # subsequent commit-plan-flip resume also fails) -- that makes
        # `status:` terminal on disk with the marker already cleared, both
        # uncommitted. `coordinator_core.workstream_complete` leg A reads a
        # terminal `status:` next to an absent marker as "not-applicable"
        # (verified clean) -- exactly the false-clean read this fix must not
        # produce. Restoring only the marker field (never touching `status:`
        # or anything else `_stamp_implemented` may have written) undoes the
        # part of this hazard this module owns, without reverting a
        # genuinely-landed-but-uncommitted status flip that plan_status_
        # transition's own next-run resume logic (`_stamp_implemented`'s
        # "stranded uncommitted status flip" branch) already knows how to
        # recover.
        pre_clear_split = split_frontmatter(text)
        pre_clear_marker_value = (
            read_fm_field_unquoted(pre_clear_split.fm_text, _CLOSE_OUT_PARTIAL_FIELD)
            if pre_clear_split is not None
            else None
        )
        cleared_text = _clear_close_out_partial_marker(text)
        cleared_live_marker = False
        if cleared_text is not None:
            text = cleared_text
            if not dry_run:
                live_path.write_text(text, encoding="utf-8")
                cleared_live_marker = True
        if dry_run:
            # See `_dry_run_scratch_plan`'s own docstring: composes over the
            # REAL, unmodified `cs_stamp_plan_implemented` (never a second,
            # locally re-derived decision matrix) by pointing it at a
            # throwaway copy of this run's current in-memory `text` -- the
            # live plan file is never opened by this branch at all.
            scratch_path = _dry_run_scratch_plan(text, live_path.suffix)
            try:
                stamp_rc = archive_stamp.cs_stamp_plan_implemented(str(scratch_path))
            finally:
                scratch_path.unlink(missing_ok=True)
        else:
            stamp_rc = archive_stamp.cs_stamp_plan_implemented(str(live_path))
        # rc=2 (C6, docs/plans/2026-08-04-terminal-state-propagation-join-keys.md § C6
        # Addendum Q4): plan_status_transition.main() now fires the terminal-state
        # cascade after a non-no-op flip and returns 2 when the cascade resolved no
        # downstream artifact -- the plan's own status DID flip to implemented; only
        # the cascade found nothing to advance (e.g. a docs-only plan with no live
        # handoff carrying its deliverable_id). That is not a stamp failure and must
        # not be reported as one -- only rc=1 (a genuine stamp error) is fatal here.
        if stamp_rc not in (0, 2):
            # Review: code-reviewer -- P2 finding, 2026-08-08: restore the
            # marker this branch cleared before the stamp call, since a
            # failed stamp does not undo it. Re-reads the LIVE file (never
            # `text`) because `_stamp_implemented` may itself have written
            # to `live_path` (a real, non-no-op status flip whose own
            # commit attempt then failed) -- restoring `text` verbatim would
            # revert that flip too, which is plan_status_transition's own
            # concern to resume on a later run, not this op's to undo. Only
            # the marker field is touched, and only when it is still absent
            # (a defensive re-check -- see this call's own docstring for why
            # this can legitimately no longer be true, e.g. a later run
            # already resumed and re-cleared it).
            if cleared_live_marker and pre_clear_marker_value is not None:
                try:
                    current_text = live_path.read_text(encoding="utf-8", errors="replace")
                    current_split = split_frontmatter(current_text)
                    if current_split is not None and read_fm_field(
                        current_split.fm_text, _CLOSE_OUT_PARTIAL_FIELD
                    ) is None:
                        restored_fm = insert_fm_field(
                            current_split.fm_text,
                            _CLOSE_OUT_PARTIAL_FIELD,
                            pre_clear_marker_value,
                            after_key="status",
                        )
                        live_path.write_text(
                            rebuild(current_split, restored_fm), encoding="utf-8"
                        )
                except OSError:
                    # Restoring the marker is best-effort recovery on top of
                    # an already-failing stamp -- a second disk error here
                    # must not mask the real failure being reported below.
                    pass
            return EXIT_BUSINESS_FAIL, {
                "error": f"{plan_path_rel}: stamp-plan-implemented failed (rc={stamp_rc})",
                "dry_run": dry_run,
            }
        # cs_stamp_plan_implemented (via plan_status_transition._stamp_implemented)
        # no-ops with rc=0, writing NOTHING, when the plan's status was
        # ALREADY terminal (`_FROZEN_STATUSES`, which includes
        # "implemented" itself) -- `stamped` must reflect the PRE-CALL
        # status, not the bare rc, or an idempotent re-run against an
        # already-implemented plan reads as `stamped=True` with a
        # byte-clean plan.md (review finding, 2026-07-27).
        stamped = pre_stamp_status is not None and pre_stamp_status not in _FROZEN_STATUSES
    elif status_target == _LANDED_STATUS:
        pre_stamp_status = _peek_plan_status(text)
        stamp_rc = _stamp_plan_landed(str(live_path), dry_run=dry_run, plan_text=text)
        if stamp_rc != 0:
            return EXIT_BUSINESS_FAIL, {
                "error": f"{plan_path_rel}: stamp-plan-landed failed (rc={stamp_rc})",
                "dry_run": dry_run,
            }
        # _stamp_plan_landed no-ops with rc=0, writing NOTHING, on BOTH an
        # already-terminal status AND an already-"landed" status -- mirror
        # both no-op conditions here (same review finding as above).
        stamped = (
            pre_stamp_status is not None
            and pre_stamp_status not in _FROZEN_STATUSES
            and pre_stamp_status != _LANDED_STATUS
        )

    # Defect 2 fix (C2, 2026-08-06): the halted path gets its own durable
    # "evaluated and found partial" trace -- see
    # `_stamp_close_out_partial_evaluation`'s own docstring for the defect
    # this closes. Runs AFTER the implemented/landed stamp branches above
    # (status_target is None here whenever it fires, so it never competes
    # with either of them) and BEFORE `wrote_anything`/the commit leg below,
    # so this write is folded into the SAME commit as any AC8 auto-resolve
    # that also fired this run, rather than needing a second commit.
    partial_evaluation_stamped = False
    if status_target is None and missing:
        new_text = _stamp_close_out_partial_evaluation(text, missing, join_provenance)
        if new_text is not None and new_text != text:
            text = new_text
            partial_evaluation_stamped = True
            if not dry_run:
                live_path.write_text(text, encoding="utf-8")

    if status_target == "implemented":
        subject = f"close-out: {plan_path_rel} shipped end-to-end, stamped implemented"
    elif status_target == _LANDED_STATUS:
        subject = (
            f"close-out: {plan_path_rel} code landed, {len(open_blocking)} "
            f"row(s) still open ({', '.join(open_blocking)}) -- stamped landed"
        )
    elif join_provenance == JOIN_PROVENANCE_NO_EVIDENCE_SOURCE:
        # `shipped` was True but `evidence_backed` was False (see the
        # stamp-decision gate above) -- distinct wording from the generic
        # unattributable-join branch below: there is no `missing` list to
        # report at all here, just an unconsulted plan.
        subject = (
            f"close-out: {plan_path_rel} not stamped -- no ## Tasks spine or "
            "## Dispatch Ledger to consult, so no evidence source was ever "
            "read"
        )
    elif join_provenance != JOIN_PROVENANCE_JOINED:
        # Reporting separation only (join semantics unchanged -- see
        # `_determine_shipped`'s own docstring): a non-"joined" provenance
        # means `missing` was never a substantive delivery finding at all --
        # the commit-coverage join could not be attributed, for one of three
        # named reasons. Say so instead of asserting the chunks are
        # uncommitted, which conflates "unattributable" with "unshipped".
        subject = (
            f"close-out: {plan_path_rel} partial -- "
            f"{len(missing)} chunk(s) could not be attributed "
            f"({join_provenance}: {', '.join(missing)})"
        )
        if auto_resolved:
            subject += " -- auto-resolved committed-but-open row(s)"
    else:
        subject = (
            f"close-out: {plan_path_rel} partial -- "
            f"{len(missing)} chunk(s) still uncommitted ({', '.join(missing)})"
        )
        if auto_resolved:
            subject += " -- auto-resolved committed-but-open row(s)"

    # A commit is owed whenever this op wrote ANYTHING to the plan doc -- a
    # status stamp (implemented/landed) OR an AC8 auto-resolve write with
    # no accompanying status flip (a still-halted plan where one committed
    # row nonetheless got auto-resolved to coded). `stamped` alone
    # under-counts the second case; `wrote_anything` is this function's
    # actual single source of truth for "did I change anything" that
    # Defect 3's commit-gating rationale refers to.
    wrote_anything = stamped or auto_resolved or partial_evaluation_stamped
    stage_paths = [plan_path_rel]
    origin_stub_result: dict = {"acted": [], "skipped": [], "failed": []}
    if wrote_anything and dry_run:
        # `--dry-run`: a commit/push is owed on the live path, but there is
        # no scratch-copy equivalent for "stage and commit into THIS repo's
        # real history" the way the two stamp writes above have -- so this
        # leg is skipped OUTRIGHT rather than simulated. `commit_result`
        # names what WOULD have been staged, so a caller can tell "nothing
        # to commit" apart from "a commit was owed but suppressed".
        commit_result = {
            "committed_sha": None,
            "pushed": None,
            "push_status": PUSH_STATUS_NOT_ATTEMPTED,
            "pushed_range": None,
            "pushed_count": None,
            "commit_failed": False,
            "diagnostics": [
                f"dry_run: commit suppressed (would stage/commit {stage_paths!r})",
            ],
        }
    elif wrote_anything and _stage_paths_committed_already(root, stage_paths):
        # DR-272 interaction (see `_stage_paths_committed_already`'s own
        # docstring): the stamp write (and, when it fired, AC8's
        # auto-resolve write, bundled into the SAME on-disk state the stamp
        # step re-reads before committing) already landed in a commit made
        # by `plan_status_transition._commit_plan_flip` under its own name --
        # there is nothing left on `stage_paths` for this op's own commit
        # leg to stage. Report the ALREADY-LANDED HEAD sha as this op's own
        # `committed_sha` (a real, resolvable commit this run caused,
        # whichever op's name is on it) rather than attempting a redundant
        # `git commit` that git would correctly refuse with a bare
        # "nothing to commit".
        head_result = git_native.rev_parse_head(root)
        commit_result = {
            "committed_sha": head_result.stdout.strip() if head_result.ok else None,
            "pushed": None,
            "push_status": PUSH_STATUS_NOT_ATTEMPTED,
            "pushed_range": None,
            "pushed_count": None,
            "commit_failed": False,
            "diagnostics": [
                "already committed by the stamp/auto-resolve write's own "
                "committing op (plan-status-transition, DR-272) -- no "
                "separate commit needed",
            ],
        }
        if commit_result["committed_sha"]:
            # `delivery_proof` (PM ruling) lets a complete, stub-specific
            # proof close the origin stub WITHOUT consulting the
            # live-children guard: this close is IN PLACE (deployment_state
            # -> shipped, no `git mv`), so it cannot strand a dependent the
            # way an archival move could -- archival remains separately
            # gated on liveness in `archive_handoffs.py`, untouched here.
            origin_stub_result = _reach_post_commit_tail_stub_close(
                root, plan_path_rel, commit_result["committed_sha"], delivery_proof
            )
    elif wrote_anything:
        # Explicit, non-empty stage_paths -- see this function's docstring
        # "Commit-leg path set" section: the plan doc is the ONLY path this
        # op ever changes, so it is also the complete, defensible pathspec.
        # Never a broad/auto-detected scope -- that is precisely the
        # blanket-add hazard the (now-bypassed) coordinator-safe-commit
        # liveness gate existed to prevent, and reintroducing it here would
        # defeat this fix.
        session_id = f"close-out-and-stamp-{uuid.uuid4().hex}"

        pipeline_result = run_commit_pipeline(
            root,
            session_id=session_id,
            subject=subject,
            stage_paths=stage_paths,
            caller_paths=set(stage_paths),
        )
        commit_result = {
            "committed_sha": pipeline_result.committed_sha,
            "pushed": pipeline_result.pushed,
            # C6a (docs/plans/2026-08-08-the-push-leg-that-never-asked-
            # which-branch.md): surfaces the fully-disambiguated
            # `push_status` alongside the legacy tristate `pushed` field --
            # `pushed` is kept verbatim for compatibility (never removed or
            # renamed), `push_status` is the richer companion a caller
            # should read to tell "declined" apart from "no-remote" apart
            # from "not-attempted" (all three read `pushed=None`). Every
            # `PipelineResult` construction site sets `push_status` (C2), so
            # a plain attribute read is correct -- a `getattr` fallback here
            # would silently degrade a future missing-field bug into
            # "not-attempted" instead of raising loud (C7b, docs/plans/
            # 2026-08-08-the-push-leg-that-never-asked-which-branch.md).
            "push_status": pipeline_result.push_status,
            # AC7 (C3b): the pushed-extent fields belong on THIS payload,
            # not buried in `diagnostics` prose -- this is the exact site
            # the original memo pinned as reporting `"pushed": true` while
            # coordinator-claude's stamp had actually advanced `origin/main` by
            # three commits that were not its own; an operator reading
            # `commit_result` needs the range/count alongside the bare
            # boolean to see the extent of what landed. `None` unless a
            # push actually landed (`push_status == PUSH_STATUS_PUSHED`) --
            # mirrors `PipelineResult.pushed_range`/`pushed_count`'s own
            # explicit-unknown-vs-not-applicable contract. Plain attribute
            # reads, same reasoning as `push_status` above: C3b set these on
            # every construction site, so a `getattr` fallback would only
            # mask a future missing-field bug.
            "pushed_range": pipeline_result.pushed_range,
            "pushed_count": pipeline_result.pushed_count,
            "commit_failed": pipeline_result.commit_failed,
            # W3 (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md):
            # surfaced unconditionally, not just on the failure branch below --
            # a caller inspecting `commit_result` after a `sha_unverified`
            # landing needs to see WHY `committed_sha` stayed `None` despite
            # `commit_failed` also being `False`.
            "sha_unverified": pipeline_result.sha_unverified,
            "diagnostics": pipeline_result.diagnostics,
        }
        if pipeline_result.commit_failed:
            return EXIT_BUSINESS_FAIL, {
                "error": (
                    "close-out commit failed: "
                    f"{'; '.join(pipeline_result.diagnostics) or 'commit pipeline reported commit_failed with no diagnostics'}"
                ),
                "shipped": shipped,
                "stamped": stamped,
                "join_provenance": join_provenance,
                "partial_evaluation_stamped": partial_evaluation_stamped,
                "missing_chunk_ids": missing,
                "deliverable_id_mismatch": deliverable_id_mismatch,
                "hyphen_range_subjects": hyphen_range_subjects,
                "chunk_evidence_range": chunk_evidence_range,
                "disposition_ref_rejections": disposition_ref_rejections,
                "open_chunk_ids": open_blocking,
                "ac_table_desync": ac_table_desync,
                "commit": commit_result,
                "skipped_sibling_repos": skipped_sibling_repos,
                "dry_run": dry_run,
            }
        if pipeline_result.committed_sha:
            # Reach `post_commit_tail`'s stub-close leg (AC4) -- see
            # `_reach_post_commit_tail_stub_close`'s own docstring.
            # `delivery_proof` (PM ruling) lets a complete, stub-specific
            # proof close the origin stub WITHOUT consulting the
            # live-children guard: this close is IN PLACE (deployment_state
            # -> shipped, no `git mv`), so it cannot strand a dependent the
            # way an archival move could -- archival remains separately
            # gated on liveness in `archive_handoffs.py`, untouched here.
            origin_stub_result = _reach_post_commit_tail_stub_close(
                root, plan_path_rel, pipeline_result.committed_sha, delivery_proof
            )
        elif pipeline_result.sha_unverified:
            # W3: the commit landed (`commit_failed=False` above, so this is
            # NOT the raise branch) but has no resolvable sha --
            # `_reach_post_commit_tail_stub_close` needs a real sha to join
            # the origin stub on (see that function's own docstring), so the
            # reach is skipped rather than attempted-and-crashed. Recorded
            # here, not silently dropped -- same labelled-skip posture as
            # `wsc_tail`'s own W3 fix for the identical gap.
            origin_stub_result = {
                "acted": [],
                "skipped": [
                    "post_commit_tail:landed-sha-unverified -- commit "
                    "landed but its sha could not be resolved, so the "
                    "origin-stub-close reach (needs a real committed sha) "
                    "was skipped"
                ],
                "failed": [],
            }
    else:
        # Nothing of this op's own to commit -- skipped entirely rather
        # than attempted-and-caught (see "wrote_anything" above; this is
        # the "no code landed AND nothing auto-resolved" case).
        commit_result = {
            "committed_sha": None,
            "pushed": None,
            "push_status": PUSH_STATUS_NOT_ATTEMPTED,
            "pushed_range": None,
            "pushed_count": None,
            "commit_failed": False,
            "diagnostics": [],
        }

    if status_target == "implemented":
        message = f"{plan_path_rel}: full plan shipped, stamped implemented, committed"
    elif status_target == _LANDED_STATUS:
        message = (
            f"{plan_path_rel}: code landed, {len(open_blocking)} row(s) still open, "
            "stamped landed, committed"
        )
    else:
        if join_provenance != JOIN_PROVENANCE_JOINED:
            # Same reporting separation as the subject above -- see
            # `_determine_shipped`'s own docstring for what each provenance
            # value means. `_JOIN_PROVENANCE_REASON` supplies the plain-
            # language reason so the message names WHY attribution failed,
            # not just that it did.
            message = (
                f"{plan_path_rel}: {len(missing)} chunk(s) could not be "
                f"attributed ({join_provenance}) -- "
                f"{_JOIN_PROVENANCE_REASON[join_provenance]}; "
                "committed partial state"
            )
            if chunk_evidence_range:
                # Range-searched summary (2026-08-07 misdirection fix): a
                # `key_mismatch` reader must be able to tell a genuinely
                # wide search apart from a narrow, buggy one WITHOUT
                # re-deriving the range by hand -- see
                # `_chunk_evidence_range_summary`'s own docstring.
                message += (
                    f" (searched {chunk_evidence_range['commit_count']} "
                    f"commit(s) from {chunk_evidence_range['base']} to HEAD)"
                )
        else:
            message = (
                f"{plan_path_rel}: {len(missing)} chunk(s) still uncommitted, "
                "committed partial state"
            )
        if deliverable_id_mismatch:
            # Points at the CAUSE (a Deliverable-Id VALUE mismatch between
            # this plan's own frontmatter and its commits' own trailers),
            # not the symptom (`missing_chunk_ids` naming chunk-ids that
            # never had a chance to match) -- see
            # `_deliverable_id_near_miss_diagnostics`'s own docstring.
            # Names the top (highest-commit-count) candidate only; the
            # full candidate set is always available via the structured
            # `deliverable_id_mismatch` result key. Appended regardless of
            # `join_provenance` above -- it is additive context (a SPECIFIC
            # near-miss candidate, when one exists), never a replacement for
            # the provenance-aware base message.
            top = deliverable_id_mismatch[0]
            message += (
                f" -- NOTE: {top['commit_count']} commit(s) carry Deliverable-Id "
                f"'{top['deliverable_id']}' but this plan's frontmatter "
                f"deliverable_id is '{plan_deliverable_id}'; chunk evidence is "
                "joined on canonicalized equality (state/deliverable-"
                "equivalence.yaml), and this pair is not (yet) a declared "
                "equivalence, so those commits do not count. If these are the "
                "SAME deliverable forked into two ids, declare it in "
                "state/deliverable-equivalence.yaml: earliest artifact wins "
                "(DR-207 DD#1) -- the direction must be established from "
                "creation-order evidence (commit/handoff timestamps), never "
                "from commit_count above."
            )
        if disposition_ref_rejections:
            # Points at a SPECIFIC still-missing row's own disposition_ref
            # and why it did not count (see this module's docstring §
            # Plan-side disposition_ref evidence) -- additive context,
            # never a replacement for the provenance-aware base message
            # above, same posture as the deliverable_id_mismatch NOTE.
            rejection_notes = ", ".join(
                f"{chunk_id} ({reason})"
                for chunk_id, reason in sorted(disposition_ref_rejections.items())
            )
            message += (
                f" -- NOTE: disposition_ref did not count as evidence for: "
                f"{rejection_notes}."
            )
        if hyphen_range_subjects:
            # Points at the CAUSE (a `-`-joined id-list subject, which is
            # NOT a recognized multi-id separator), not the symptom
            # (`missing_chunk_ids` naming chunk-ids a range subject never
            # registered) -- see `_hyphen_range_subject_diagnostics`'s own
            # docstring. Names every offending commit's spanned ids so the
            # next reader gets the fix, not just the fault. Additive
            # context, same posture as the two NOTE blocks above.
            hyphen_notes = "; ".join(
                f"{offender['sha']} \"{offender['subject']}\" -> "
                f"{', '.join(offender['spanned_chunk_ids'])}"
                for offender in hyphen_range_subjects
            )
            # Review: code-reviewer -- Finding 4: the illustrative example
            # is derived from the first real offender rather than a fixed
            # literal, so it always matches an actual subject named in
            # `hyphen_notes` above instead of potentially matching neither
            # offender when more than one fires.
            example_offender = hyphen_range_subjects[0]
            example_match = _CHUNK_SUBJECT_RE.match(example_offender["subject"])
            example_raw = (
                example_match.group(1)
                if example_match
                else "-".join(example_offender["spanned_chunk_ids"])
            )
            example_fixed = ",".join(example_offender["spanned_chunk_ids"])
            message += (
                " -- NOTE: commit subject(s) used '-' to join a chunk-id "
                f"list, which is NOT a recognized separator ({hyphen_notes}); "
                "recognized separators are ',', '+', '/' -- re-commit (or "
                "amend, if unshared) using one of those, e.g. "
                f"'{example_fixed}: ...' instead of '{example_raw}: ...'."
            )

    if ac_table_desync:
        # Advisory NOTE only (C2 -- this must never gate the stamp decision
        # above, which has already run by this point) -- same additive-
        # suffix posture as the deliverable_id_mismatch/disposition_ref_
        # rejections/hyphen_range_subjects NOTE blocks above, appended
        # regardless of which status_target branch produced `message`.
        message += (
            " -- ADVISORY: the '## Tasks' spine is fully resolved but the "
            f"plan's own '## Acceptance Criteria' table still has "
            f"{len(ac_table_desync['unresolved_ac_ids'])} of "
            f"{ac_table_desync['total_ac_rows']} row(s) reading unresolved "
            f"({', '.join(ac_table_desync['unresolved_ac_ids'])}) -- the AC "
            "table may simply be stale; not blocking the stamp."
        )

    if dry_run:
        # Additive suffix only -- every branch above still describes what
        # WOULD happen (subject/message wording talks about "stamped"/
        # "committed" unconditionally, since that prose is shared with the
        # live path -- see this function's own docstring "one computation,
        # two dispositions"); this is the one place a reader is told, in
        # the human-facing `message` itself, that nothing was actually
        # written.
        message += " [dry-run: no write/commit performed]"

    return EXIT_OK, {
        "shipped": shipped,
        "stamped": stamped,
        "status_target": status_target,
        "join_provenance": join_provenance,
        "partial_evaluation_stamped": partial_evaluation_stamped,
        "missing_chunk_ids": missing,
        "deliverable_id_mismatch": deliverable_id_mismatch,
        "hyphen_range_subjects": hyphen_range_subjects,
        "chunk_evidence_range": chunk_evidence_range,
        "disposition_ref_rejections": disposition_ref_rejections,
        "open_chunk_ids": open_blocking,
        "ac_table_desync": ac_table_desync,
        "commit": commit_result,
        "message": message,
        "skipped_sibling_repos": skipped_sibling_repos,
        "dry_run": dry_run,
        "origin_stub_close": origin_stub_result,
        "gates": {"repo_identity": repo_identity_gate},
    }


def main(argv: list[str]) -> int:
    """`close-out-and-stamp <plan-path> [--dry-run]`

    `--dry-run` computes and returns the full close-out verdict while
    writing NOTHING (no frontmatter stamp, no plan-body disposition
    backfill, no commit, no push) -- see `close_out_and_stamp`'s own
    docstring for the shared-computation design this implements, and this
    module's header-comment docstring in `coordinator/bin/
    close-out-and-stamp` for the incident that motivated it. The result
    dict always carries `"dry_run": bool` so a caller cannot mistake a
    preview for a completed close-out.

    Still strictly positional otherwise, and still errors on any argument
    beyond the one required `<plan-path>` plus the one optional
    `--dry-run` flag -- extending, not loosening, the pre-existing
    "extra arguments are a usage error" contract."""
    import json

    if not argv or argv[0] in ("--help", "-h"):
        print(
            "usage: close-out-and-stamp <plan-path> [--dry-run]",
            file=sys.stderr if argv else sys.stdout,
        )
        return EXIT_OK if argv and argv[0] in ("--help", "-h") else EXIT_USAGE

    plan_path: Optional[str] = None
    dry_run = False
    extra: list[str] = []
    for arg in argv:
        if arg == "--dry-run":
            dry_run = True
        elif plan_path is None and not arg.startswith("--"):
            plan_path = arg
        else:
            extra.append(arg)

    if extra:
        print(f"close-out-and-stamp: unrecognized argument(s): {extra!r}", file=sys.stderr)
        return EXIT_USAGE
    if plan_path is None:
        print("close-out-and-stamp: missing required <plan-path>", file=sys.stderr)
        return EXIT_USAGE

    exit_code, result = close_out_and_stamp(plan_path, dry_run=dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
