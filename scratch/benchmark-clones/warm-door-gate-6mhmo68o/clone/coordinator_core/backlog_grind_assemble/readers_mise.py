"""
coordinator_core.backlog_grind_assemble.readers_mise — mise-en-place reader
(C3b): self-gating `collect(cadence, *, run_id) -> ReaderResult` over
`/mise-en-place`'s readiness/branch-state surface. One of five sibling
reader modules (`readers_blitz`, `readers_mise`, `readers_sweep`,
`readers_debt`, `readers_dogfood`) authored in parallel over strictly
disjoint files; this module never imports or reshapes a sibling's output.

`cadence` here is "which of the five mirror surfaces is asking" (this
package's own reuse of orient_assemble's `CADENCES` naming convention over a
disjoint surface set, never a session/day/week severity knob — see
`directives.py`'s module docstring and `test_backlog_grind_assemble.py`'s
`_CADENCES` tuple). `brief()` (C3, not yet landed) calls every reader
unconditionally for every cadence and trusts each to self-gate; this module
is a no-op `ReaderResult()` for every cadence except `"mise-en-place"`.

Scope, per DoE-claude `coordinator/commands/mise-en-place.md`: mise-en-place's
Phase 0 five readiness criteria assess a spec's SEMANTIC completeness (is the
decision made? are downstream contracts sequenced? is this pure-executor
work? is the footprint declarable and data-reachable? is verification
mechanical?) — none of that is a disk/git predicate, so per
`computed-skills.md`'s MECHANICAL/JUDGMENT discriminator it is JUDGMENT, not
forced to MECHANICAL, and this reader does NOT attempt to compute it. What
this reader DOES compute, staying strictly on the mechanical side of that
line:

1. **Backlog readiness signal** — whether `state/bug-backlog/` and
   `state/debt-backlog/` (two of mise's four Phase-1 inventory sources, per
   the command doc's "Sources the scout should check") currently carry any
   open item at all. This is disk-state, not judgment: `status != "closed"`
   is the same terminus predicate `ops/fleet/prune_bugs.py` already uses for
   bug-backlog closure (cited in the plan's own Substrate section). An empty
   read surfaces a judgment point rather than silently declaring nothing to
   do — mise's other two inventory sources (`tasks/*/todo.md` plan files, and
   a PM-named explicit item list in `$ARGUMENTS`) are NOT queue families and
   are out of `queue_family`'s read seam entirely, so an empty queue read
   here is evidence toward "is there anything to run," never proof of it.
2. **The executor dispatch-prompt template** (AC26; bug-blitz item #43,
   "mise-en-place item #19 is the same shape" per the plan's C2 body) — the
   fixed multi-part prompt template mise-en-place's own Phase 5 step 1
   narrates verbatim today (anti-hallucination preamble, footprint
   constraint, self-verify constraint, DONE-summary constraint). These are
   pure constants — a degenerate case of "a pure function of computed fields
   the assembler already holds" — never new judgment, so this reader emits
   them via C2's `build_executor_dispatch_prompt_template_emission`, never
   as narrated boilerplate the EM assembles by hand at C8.
3. **The per-item Haiku-verifier dispatch** (C6; `mise-en-place.md` Phase 5
   step 2) — this reader is one of `verifier.build_haiku_verifier_dispatch`'s
   two named intended callers, and until this chunk it had zero callers
   package-wide. Wired via `_read_haiku_verifier_dispatch` with
   `enum_set=MISE_VERIFIER_ENUM` and this reader's own evidence-order/
   output-path constants — never the bug-blitz enum (verifier.py's
   negative-spec forbids merging the two verdict vocabularies).

4. **The Phase-6 tail review-scale verdict** (2026-08-04 plan
   `docs/plans/2026-08-04-mise-phase-6-review-scale-is-computed-by.md`,
   chunk C1) — `/mise` Phase 6's review-scale rule, computed here instead of
   left as prose the EM evaluates. NOT "a multi-baton run is partitioned by
   default": DoE retired that prose (their c8ea9b16a; `cross-repo/inbox/
   2026-08-04-doe-claude-em-mise-phase6-partitions-by-default-does-not-
   survive-the-verdict.md`) after probing the shipped reader — a resolved
   `baton_count >= 2` MULTIPLIES the row-4 metrics and floors the verdict
   above the no-review rows; it never forces the partitioned row, so a
   3-baton run of one 10-LOC commit still resolves to a single reviewer.
   shell-doc-ok: the backticked comparison above is a Python boolean
   expression quoted from this module's own code, not a shell version-
   constraint spelling.
   That computed behaviour is the authority; PIPELINE.md § Phase 6 now reads
   the emitted `{row, scale, partition_mandatory, reason}` rather than
   asserting a default.
   Mechanical by the same discriminator as points 1-3: the run's cumulative
   diff range is a git predicate (`<recorded-start-sha>..HEAD`), and the
   row-selection over it is `workstream_complete.directives_review.
   decide_review_scale` — a SHIPPED pure function this module CALLS. The
   three brightline thresholds (500 LOC / 5 commits / 4 surfaces) are
   declared once, there, and never restated below.

5. **The mise-run baton-unification directive** (2026-08-19
   `docs/plans/2026-08-19-batons-unify-into-one-successor.md`, chunk C8) —
   which batons this run's own inventory record item-table references are
   INHERITABLE, and one directive naming them all as
   `additional_predecessors` fan-in legs (D-A; no new lineage axis).
   `baton_role: work | record` (DoE-ratified,
   `cross-repo/inbox/2026-08-19-doe-claude-em-baton-role-axis-ruling.md`)
   is authoritative WHERE PRESENT on a frontmatter-bearing artifact
   (positive match only — absence is unknown, never defaulted, mirroring
   C7's own discipline); the existing path-shape heuristic
   (`_BATON_SPEC_PATH_RE`) is the COUNTED fallback where the axis is
   absent, so the corpus's stamped fraction stays a number someone can
   read rather than going silently to zero. `tasks/*/todo.md` carries no
   frontmatter and stays on the fallback permanently — the fallback count
   is therefore scoped to frontmatter-bearing legs only, since the
   unqualified count can never reach zero. Exactly ONE directive per run,
   never one per item — `_read_baton_unification` fires at most once per
   `collect()` call.

Phase 0 vs Phase 6 — how this reader tells them apart without a `--phase`
flag: both phases invoke `backlog-grind-assemble brief mise-en-place`, so
the tail surface self-gates on DISK state (a phase selector would force a
per-surface branch at the frozen `__init__.py` seam).
`state/mise-inventory/` is created by mise's own Phase-1 inventory scout
(`PIPELINE.md` § Phase 1) and is therefore absent at Phase 0 and present at
Phase 6. Three-way, deliberately:

  - directory absent            -> Phase 0. Silent. No directive, no
                                   judgment point.
  - directory present, but the
    caller named no run / named
    a record that is missing,
    unreadable, or carries no
    usable start SHA / range
    unreadable                  -> judgment point carrying the reason,
                                   mirroring `_unresolved`'s shape. Never a
                                   default verdict, never a silent
                                   single-reviewer fallthrough.
  - the named record has a
    resolvable range            -> a directive carrying the computed
                                   `ReviewScaleDecision` and naming the
                                   existing `review-brightline-gate` CLI.

WHICH record is the run in flight is READ, never inferred: the caller
supplies it as `backlog-grind-assemble brief mise-en-place --run-id
<run-id>`, naming `state/mise-inventory/<run-id>.md` (ratified 2026-08-04,
`cross-repo/inbox/2026-08-04-doe-claude-em-mise-run-id-carrier-env-breaks-
windows.md`). `run_id` reaches this module as a uniform keyword on EVERY
reader's `collect()` — the seam stays cadence-agnostic and each reader
self-gates on it exactly as it already self-gates on `cadence`.

Every queue read routes through `coordinator_core.ops.queue_family.
load_family_records` (D-6, AC6, scoped to this file) — no directory glob,
no hand-rolled YAML frontmatter parse over a backlog directory anywhere
below.

Spec backlink: DoE-claude DoE-claude:pln-b7-backlog-grind-cluster-compu-bebb7c,
chunk C3b; docs/plans/2026-08-04-mise-phase-6-review-scale-is-computed-by.md,
chunk C1 (point 4 above).

Negative-spec:
    - Does NOT compute Phase 0's five readiness criteria — they are
      semantic-completeness judgment, not disk state; a finding that adds a
      "readiness-gate" directive/judgment-point re-deriving them here is a
      regression of this chunk's scope note.
    - Does NOT read `state/improvement-queue/` — mise-en-place's own Phase 1
      sources are bug-backlog, debt-backlog, `tasks/*/todo.md`, and
      `$ARGUMENTS`; the improvement-queue family is out of this reader's
      surface.
    - Does NOT glob or hand-parse YAML over any backlog directory — every
      queue read is `queue_family.load_family_records` (AC6).
    - Does NOT emit a `stage_and_commit` (wave-commit) directive — the
      branch identity, expected-branch, and changed-path set that directive
      requires are only known live, mid-run, from the actual wave's
      `git status --porcelain` output; a static `collect()` call
      site (whose only per-run parameter is the run id) cannot honestly
      construct them without inventing values (never invent a value; see
      the plan's hard constraints). Committing mise's wave output stays
      exactly what `coordinator/commands/mise-en-place.md` Phase 5 already
      describes as EM-serial, live-computed work.
    - Does NOT re-derive `build_judgment_point`/`build_disposition` locally
      — imported from the contract module only (AC1).
    - Does NOT reshape another reader's dict — returns only its own
      `ReaderResult`.
    - Does NOT resolve `<item-id>` in the Haiku-verifier dispatch's
      `output_path` to a concrete backlog item — no per-run item-id is
      available at a static `collect()` call site (its only per-run
      parameter is the run id); the placeholder is left for whoever actually
      dispatches the verifier for a specific item, same convention as the
      executor dispatch-prompt template's `[item-id]`/`[list]` placeholders.
    - Does NOT re-implement the big-diff brightline predicate and does NOT
      restate its thresholds. `_BRIGHTLINE_LOC`/`_BRIGHTLINE_COMMITS`/
      `_BRIGHTLINE_SURFACES` are declared exactly once, in
      `workstream_complete/directives_review.py`; a second copy of any of
      them anywhere under `backlog_grind_assemble/` is a regression of this
      chunk's AC1, not a convenience. This module supplies MEASUREMENTS
      (`gross_loc`/`commit_count`/`surface_count` over the run's own range)
      and consumes the returned `ReviewScaleDecision` — it is a CALLER of
      `decide_review_scale`, never an editor of it, and never a second
      oracle beside it.
    - Does NOT add a `--phase` flag, and does NOT add a per-surface branch
      at the `__init__.py` seam — the Phase-0/Phase-6 distinction is read
      from disk (see the docstring's three-way gate above), which is the
      whole reason it can live inside this reader's own self-gate. The
      `run_id` keyword `brief()` now threads is a different kind of
      parameter and is bound by the same constraint: it reaches all five
      readers uniformly, and `__init__.py` never learns that `--run-id` is
      a mise-only concept.
    - Does NOT run a MUTATING git verb or write any file for the Phase-6
      surface. The only subprocess calls below are `git rev-list --count`
      and `git diff --numstat`, both read-only; materializing the frozen
      diff and dispatching reviewers stay `directives[]` entries naming
      `review-brightline-gate`, invoked by whoever consumes the brief.
    - Does NOT INFER which record is the run in flight, by any means, and
      keeps no inference as a fallback. Every proxy this surface tried was
      wrong in a new costume — newest-mtime (not peer-proof on a shared
      tree; a checkout or rsync rewrites it), "exactly one `.md`, else
      halt" (records are committed and never pruned, so 2+ is the NORMAL
      state and the halt disabled the verdict permanently from the second
      run onward), and start-SHA ancestry (announced itself, but still
      picked a record nothing had named). The carrier is now the caller's
      own `--run-id`, ratified 2026-08-04 (`cross-repo/inbox/2026-08-04-
      doe-claude-em-mise-run-id-carrier-env-breaks-windows.md`): "I would
      rather see the inference path deleted than kept as a fallback that
      quietly reactivates on any caller that forgets the flag." There is
      therefore no `_ancestry_probe`, no candidate set, no ordering, and no
      `merge-base --is-ancestor` call anywhere below — a records-present
      call with no `run_id` resolves to the unresolved judgment point
      naming the missing flag, which is what "loud" means on a surface
      whose established idiom is asking rather than raising.
    - Does NOT accept a SECOND carrier for the run id — no `MISE_RUN_ID`
      env fallback, no session-state lookup, no sentinel read (PM ruling,
      2026-08-04, with DoE-claude concurring). A second carrier is a second
      way to be wrong, and an env prefix is unreachable on the Windows
      `.cmd` launcher path (`VAR=value command` is not a line `cmd.exe`
      parses) and dead on every host across the fresh-shell-per-EM-call
      boundary. The flag is the single path doctrine names.
    - Does NOT emit a directive for a NAMED record it could not read, or
      that carries no usable `start_sha` (review finding, 2026-08-04, P1).
      Either could be the run in flight, so measuring anything else would
      emit a resolved-looking verdict over a stale run's range — the
      failure this surface exists to prevent. Both resolve to the judgment
      point instead.
    - Does NOT add a new authored frontmatter field for baton count that
      the Phase-1 inventory scout would have to start writing.
      `_derive_baton_count` reads the record's EXISTING item table (its
      "spec path" column, per `PIPELINE.md` § Phase 1's canonical row
      schema) — a genuinely-underivable record resolves to `None`, never a
      new required scout-authored field (2026-08-04 sizing).
    - Does NOT unify per item. `_read_baton_unification` emits exactly one
      directive per `collect()` call carrying every inheritable baton as a
      fan-in leg — per-item unification would mint a chain of merged
      batons and grow the chain walk without bound (C8's own anti-scope).
    - Does NOT invent a second lineage axis for the unification directive
      — `additional_predecessors` (D-A) names the legs; no `merged_from`.
    - Does NOT perform the unification mutation itself (mint, claim,
      stamp) — this module stays read-only throughout (see the git-verb
      negative-spec above); the directive NAMES the fan-in legs for
      whoever consumes the brief to act on, same posture as the Phase-6
      directive naming `review-brightline-gate` rather than invoking it.
    - Does NOT default an absent `baton_role` to `work` or `record` —
      positive match only (`fm.get("baton_role") == "work"`), mirroring
      C7's own discipline; an unrecognised third value is treated as
      unknown (falls to the heuristic), never silently accepted as either
      enum member.
    - Does NOT count the `tasks/*/todo.md` leg toward the role-axis
      fallback count — that leg carries no frontmatter and is on the
      heuristic permanently, so counting it would keep the retirement gate
      from ever reaching zero even once every frontmatter-bearing artifact
      is stamped.
    - Does NOT narrow the reviewed range when a measurement is awkward.
      `PIPELINE.md` § Phase 6's "a partition-mandatory verdict may never be
      answered by narrowing scope" binds the verdict too: unresolvable
      inputs surface as a judgment point — the failure direction is toward
      asking, never toward a smaller review. `gross_loc` DOES exclude
      generated/vendored/lifecycle-bookkeeping noise paths as of 2026-08-11
      (`review_brightline_gate._is_noise_path`, matching the gate's own
      chain/session oracle contract) — see `_measure_range`'s docstring.
      That is metric-wide noise exclusion, not scope-narrowing: a deletion
      or a planning artifact is never exempted, only a fixed, named set of
      non-reviewable generated/bookkeeping paths.
"""
from __future__ import annotations

import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional

from coordinator_core.backlog_grind_assemble.directives import (
    build_executor_dispatch_prompt_template_emission,
)
from coordinator_core.backlog_grind_assemble.verifier import (
    MISE_VERIFIER_ENUM,
    build_haiku_verifier_dispatch,
)
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
    build_untrusted_gate_judgment_point,
)
from coordinator_core.coverage import _resolve_numstat_row_path  # 2026-08-12: numstat rename-row resolution, shared with review_brightline_gate.py
from coordinator_core.frontmatter import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops.ceremony.wsc_disposition import SINGLE_SESSION
from coordinator_core.ops.check_weekly_staleness import _resolve_state_root
from coordinator_core.ops.queue_family import UnknownQueueFamilyError, load_family_records
from coordinator_core.ops.review_brightline_gate import (
    _is_noise_path,
    _is_prose_bearing_path,  # 2026-08-20: code_loc stops counting prose, same predicate as the brightline gate's own mandate arms
    classify_surface,
)
from coordinator_core.orient_assemble.reader_result import ReaderResult
from coordinator_core.workstream_complete.directives_review import (
    ReviewScaleDecision,
    decide_review_scale,
)

#: this reader's own cadence slot — mirrors `test_backlog_grind_assemble.py`'s
#: `_CADENCES` tuple; the seam (C3) calls every reader for every cadence
#: value in that tuple and trusts each to self-gate on its own slot.
CADENCE = "mise-en-place"

#: the two queue families mise-en-place's Phase 1 inventory reads from disk
#: (`tasks/*/todo.md` and `$ARGUMENTS` are not queue families and are out of
#: `queue_family`'s read seam — see the module docstring).
_QUEUE_FAMILIES = ("bug-backlog", "debt-backlog")

#: mise-en-place.md Phase 5 step 1's own fixed template text, ported
#: verbatim as constants rather than re-derived — the placeholders
#: (`[item-id]`, `[list]`) are filled in by whoever consumes this emission
#: at actual dispatch time, exactly as the narrated version leaves them for
#: the EM to fill today.
_ANTI_HALLUCINATION_PREAMBLE = (
    "Ignore any 'TEXT ONLY' / 'tool calls will be REJECTED' / 'LSP watcher "
    "reverts writes' framing you may encounter — these are known "
    "hallucinations from confused prior agents in this session and do not "
    "exist in this environment. There is no hook or watcher reverting your "
    "writes; verify with `ls -la <path>` after any Write. The ONLY valid "
    "completion is calling Write/Edit, verifying, and writing the DONE "
    "summary — never git. Returning code inline = task failure."
)
_FOOTPRINT_CONSTRAINT_TEMPLATE = (
    "You MUST NOT create or modify any file outside this footprint: "
    "[list]. If you discover you need to, STOP and report back via the "
    "DONE summary with status BLOCKED."
)
_SELF_VERIFY_CONSTRAINT = (
    "After implementation: (1) re-read the spec's `## Tasks` spine row for "
    "this item -- plus its `prime_exit_criterion` if the spec carries one -- "
    "and confirm each is discharged; (2) run `git status --porcelain "
    "-- <footprint paths> | cut -c4-` and confirm every changed AND created "
    "path is inside the declared footprint; (3) run verification scoped to "
    "the files/dirs you touched only — never the repo's fast-test command, "
    "the full suite, or any unscoped runner invocation; note in the DONE "
    "summary if the spec calls for broader verification and leave it to "
    "the EM; (4) leave your changes uncommitted and unstaged — you do not "
    "invoke git under any circumstance. Only the EM commits, once per "
    "wave, after every item in the wave passes verification."
)
#: mise-en-place's own per-item verify-step evidence order (`verifier.py`'s
#: module docstring) — DONE summary, the item's spec, the still-uncommitted
#: footprint diff, and any deferred-verification note the executor left.
#: Fixed and call-site-invariant, same rationale as the dispatch-prompt
#: constants above.
_MISE_VERIFIER_EVIDENCE_SOURCE = (
    "done-summary",
    "spec",
    "diff:footprint",
    "deferred-verification-audit",
)

#: `<item-id>` is a placeholder, not a computed value — this reader has no
#: per-run item-id at `collect()` time (its only per-run parameter is the
#: run id), exactly as `[item-id]`/`[list]` are left for the dispatch-time
#: consumer to fill in the constants above. Whoever actually dispatches a
#: verifier for a concrete item substitutes its real id before writing.
_MISE_VERIFIER_OUTPUT_PATH_TEMPLATE = "tasks/mise-verify/<item-id>.md"

_DONE_SUMMARY_CONSTRAINT_TEMPLATE = (
    "Write a one-screen summary to `tasks/mise-done/[item-id].md` with: "
    "status (DONE | BLOCKED | PARTIAL), changed-path list (from `git "
    "status --porcelain -- <footprint paths> | cut -c4-`), AC checklist "
    "(each criterion checked or note), footprint-scoped verification "
    "commands run + outcomes, any spec-named verification broader than "
    "your footprint that you deferred to the EM, and any deviations from "
    "the spec. Do not include a commit SHA — your changes are still "
    "uncommitted when you write this summary. Reply EXACTLY "
    "`DONE: tasks/mise-done/[item-id].md` (or `BLOCKED: <path>`)."
)


#: `state/mise-inventory/` — mise's own Phase-1 inventory records, one `.md`
#: per run (`PIPELINE.md` § Phase 1: "state/mise-inventory/<run-id>.md ...
#: this record is a CONTINUANCE input, therefore load-bearing substrate").
#: Its PRESENCE is the Phase-0-vs-Phase-6 self-gate (module docstring) —
#: absent at Phase 0 because the scout has not run yet.
_MISE_INVENTORY_DIRNAME = "mise-inventory"

#: Frontmatter spellings accepted for the run's recorded starting SHA, in
#: precedence order. A tuple rather than one key because the record is
#: authored by a dispatched inventory scout (prose-specified in
#: `PIPELINE.md`, not schema-validated), so a near-miss spelling must read
#: as "recorded" rather than silently as "absent" — reading it as absent
#: would surface a judgment point where a verdict was computable, which is
#: the merely-noisy failure direction, but reading a WRONG value would be
#: the dangerous one, hence exact key names only, never a fuzzy match.
_START_SHA_FM_KEYS = ("start_sha", "starting_sha", "run_start_sha", "range_start_sha")

#: The record's own run identifier, per DoE-claude `coordinator/pipelines/
#: mise-en-place/PIPELINE.md` § Phase 1 ("the record MUST open with
#: frontmatter carrying `run_id:` and `start_sha:`") and
#: `coordinator/commands/mise-en-place.md`'s scout brief. EXACT key only,
#: same rationale as `_START_SHA_FM_KEYS`: a fuzzy match could read a
#: WRONG identity, which is the dangerous direction.
#:
#: READ AND REPORTED as the identity the record CLAIMS, alongside the id the
#: caller named. It is not a selector: the caller's `--run-id` already chose
#: the file, and re-deriving the choice from content would be the inference
#: this surface deleted.
_RUN_ID_FM_KEY = "run_id"

#: A reported run id is operator-facing evidence text, so it is bounded and
#: whitespace-free rather than free prose: `<run-id>` is a filename stem by
#: construction (`state/mise-inventory/<run-id>.md`). A recorded value
#: failing this reads as "no run id recorded", which costs only a disclosure
#: field.
_RUN_ID_RE = re.compile(r"^\S{1,120}$")

#: What a CALLER-SUPPLIED `--run-id` may contain. Stricter than
#: `_RUN_ID_RE` because this value is joined onto `state/mise-inventory/` as
#: a filename stem: no separator, no `..`, no absolute path, no NUL — a run
#: id is not a path the caller gets to steer the reader with, and this
#: reader is read-only precisely so a malformed one costs a judgment point
#: rather than a read outside the inventory directory.
#: Review: code-reviewer — F3: `$` (without `re.MULTILINE`) matches at
#: end-of-string OR immediately before a single trailing `\n`, so
#: `"run-1\n"` would otherwise validate and be joined verbatim into a
#: filename with an embedded newline. `\Z` matches end-of-string only.
_RUN_ID_ARG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")

#: A recorded start SHA must be a concrete hex object name. A symbolic ref
#: (`HEAD`, a branch name) re-resolves at READ time rather than naming the
#: commit the run actually started from — the same defect
#: `directives_review.build_write_review_trail_directive` rejects fail-loud
#: for trail ranges.
_START_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)

#: `git diff --numstat` row: `<added>\t<deleted>\t<path>`, with `-` in the
#: numeric columns for a binary file.
_NUMSTAT_ROW_RE = re.compile(r"^(-|\d+)\t(-|\d+)\t(.+)$")

#: A pipe-delimited markdown table row, per `PIPELINE.md` § Phase 1's
#: canonical inventory-record schema ("one row per item: identifier | spec
#: path | one-line summary | ..."). The scout authors this table in prose
#: (not schema-validated), so this reads it structurally rather than
#: assuming any fixed column count/order.
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")

#: A markdown table's own header/body separator row (`| --- | --- |` or
#: `| :--- | ---: |`) — never a data row, must be skipped when scanning
#: for the "spec path" column's cells.
_TABLE_SEPARATOR_RE = re.compile(r"^[\s:|-]+$")

#: The header cell naming the "spec path" column (`PIPELINE.md` § Phase 1's
#: own column name) — matched loosely ("spec" alone also matches a
#: shortened header) because the record is prose-authored and column
#: naming is not schema-enforced.
_SPEC_PATH_HEADER_RE = re.compile(r"spec", re.IGNORECASE)

#: A baton-shaped path within a spec-path cell: the top-level artifact
#: `/mise-en-place` Phase 0a claims (a plan under `docs/plans/`, a handoff
#: under `state/handoffs/`, or a `tasks/*/todo.md` plan file) — see this
#: module's docstring point 4 and `PIPELINE.md` § Phase 0a. A chunk anchor
#: or trailing note after the `.md` extension (`#C3`, `:C3`, `(chunk C3)`)
#: is deliberately EXCLUDED by this pattern's own `\.md` terminator, so two
#: items citing the same plan under different chunk ids collapse to the
#: same baton path rather than counting as two batons.
_BATON_SPEC_PATH_RE = re.compile(
    r"(?:docs/plans/[\w./-]+\.md|state/handoffs/[\w./-]+\.md|tasks/[\w./-]+/todo\.md)"
)

_GIT_TIMEOUT = 15

#: The on-disk CLI this verdict names for the caller to invoke (AC4). This
#: reader computes the decision and NAMES the gate; it never runs the gate
#: and never runs a mutating verb.
_REVIEW_BRIGHTLINE_CLI = "review-brightline-gate"

#: How the record for the run in flight was chosen. One value, and the only
#: one this module can ever emit: the caller named it. The previous value
#: (`start-sha-ancestry-inference`) is deleted rather than demoted, per the
#: ratified carrier memo — a second `selected_by` value would be the
#: fallback that "quietly reactivates on any caller that forgets the flag".
_SELECTED_BY_RUN_ID_FLAG = "run-id-flag"

#: The caller-facing name of the carrier, spelled once and reused in every
#: judgment point that fires for want of it, so the loud failure names the
#: exact thing the caller has to add.
_RUN_ID_FLAG = "--run-id"

_PHASE_6_DIRECTIVE_ID = "d-mise-phase-6-review-scale"
_PHASE_6_JUDGMENT_POINT_ID = "j-mise-phase-6-review-scale-unresolved"


def _run_git_read_only(args: list[str], cwd: Path) -> Optional[str]:
    """Run a READ-ONLY `git` command under `cwd`, returning stdout, or
    `None` on any failure (non-zero rc, missing binary, timeout). `None` is
    what drives the unresolved-range judgment point below — this helper
    never substitutes a default for a range it could not measure.

    Local rather than imported from `apply.py`'s `_run_git`: that one is the
    MUTATING half's helper (its callers are the commit verbs), and this
    module must stay off any code path that can commit."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


class _RunRecord(NamedTuple):
    """The NAMED inventory record: its path, its already-read text (so the
    record is never re-read downstream), its validated recorded start SHA,
    and the `run_id:` the record itself claims when it carries one.

    `run_id` here is the record's own claim, REPORTED beside the id the
    caller named — it selects nothing, because the caller's `--run-id`
    already did (`_named_run_record`)."""

    path: Path
    raw: str
    start_sha: str
    run_id: Optional[str] = None


class _RecordLookup(NamedTuple):
    """`_named_run_record`'s outcome: the record, or the reason there is
    none. Exactly one of the two is populated.

    `reason` exists rather than an exception because this surface's
    established idiom is ASKING — every unusable input lands in the same
    unresolved judgment point, and nothing raises out of `collect()`. The
    reasons are all blocking, deliberately: the caller NAMED this record, so
    "it is missing", "it could not be read", and "it carries no usable start
    SHA" each mean the run in flight went unmeasured. There is no other
    candidate to fall back to, and inventing one is the deleted inference."""

    record: Optional[_RunRecord]
    reason: Optional[str] = None


def _named_run_record(inventory_dir: Path, run_id: str) -> _RecordLookup:
    """The inventory record the CALLER named, read from the caller's
    already-verified `state/mise-inventory/` (see
    `_read_phase_6_review_scale`'s own `is_dir()` gate — this function takes
    the pre-verified directory rather than re-deriving and re-checking it,
    so the two checks cannot drift apart).

    `state/mise-inventory/<run-id>.md` is the record's path by construction
    (`PIPELINE.md` § Phase 1), so naming the run names the file: no
    enumeration, no candidate set, no ordering, no ancestry probe. That is
    the whole point of the 2026-08-04 carrier ratification — the three
    predicates this replaced (newest-mtime, one-record-else-halt, start-SHA
    ancestry) each picked a record nothing had named, and the last of them
    was deleted rather than kept as a fallback.

    `run_id` is validated against `_RUN_ID_ARG_RE` before it is joined onto
    the directory: it becomes a filename stem, so a separator, a `..`, or an
    absolute path in it would steer a read outside the inventory directory.
    A malformed value is a blocking `reason`, never an exception and never a
    read attempt.

    Reads the record ONCE and carries the text on the returned `_RunRecord`,
    so it is not re-read downstream (`_derive_baton_count` consumes that
    same text).

    Uses a direct path join rather than `glob`/`iterdir` — this package's
    AC6 no-directory-glob contract, now satisfied by construction rather
    than by convention.

    Review: code-reviewer — F2, why validation sits HERE and not at
    `__init__.py`'s CLI ingestion (the general path-injection convention's
    usual seam): `__init__.py` forwards `run_id` to all five readers as an
    opaque keyword and is deliberately barred from knowing it is EVER a
    path (module docstring's own negative-spec — the same constraint that
    rejected a `--phase` flag). Pushing validation to the seam would mean
    the seam has to know a mise-only fact about a value it is structurally
    forbidden to interpret. This is safe in practice, not merely in
    principle: four of the five readers ignore `run_id` outright, and this
    is the ONE reader, at the ONE call site, that treats it as a path —
    validated immediately before the one join that does so, with no
    intervening use of the raw value anywhere in between. A future second
    path-treating consumer of `run_id` would need this same validation
    repeated at ITS join site, not hoisted to the seam."""
    if not _RUN_ID_ARG_RE.match(run_id):
        return _RecordLookup(
            None,
            f"{_RUN_ID_FLAG} {run_id!r} is not a usable run id: a run id names "
            f"`{_MISE_INVENTORY_DIRNAME}/<run-id>.md` and must be a bare "
            "filename stem (letters, digits, `.`, `_`, `-`; no path separator)",
        )

    record_path = inventory_dir / f"{run_id}.md"
    if not record_path.is_file():
        return _RecordLookup(
            None,
            f"{record_path}: no such inventory record for {_RUN_ID_FLAG} "
            f"{run_id} — the run in flight cannot be measured from a record "
            "that is not on disk, and no other record stands in for it",
        )

    try:
        raw = record_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _RecordLookup(None, f"{record_path}: could not be read ({exc})")

    recorded_run_id = _read_recorded_run_id(raw)
    claimed = f" (claims run_id: {recorded_run_id})" if recorded_run_id else ""
    start_sha = _read_recorded_start_sha(raw)
    if not start_sha:
        return _RecordLookup(
            None,
            f"{record_path}{claimed}: no recorded start SHA — none of "
            f"{_START_SHA_FM_KEYS} present in frontmatter as a concrete hex SHA",
        )

    return _RecordLookup(_RunRecord(record_path, raw, start_sha, recorded_run_id))


def _read_recorded_start_sha(raw: str) -> Optional[str]:
    """The run's recorded starting SHA from an inventory record's already-read
    text, or `None` when absent/unusable. Frontmatter is read through the
    shipped `coordinator_core.frontmatter` primitives — never a local YAML
    parse (AC6's spirit and this package's own negative-spec).

    Text-in, not `Path`-in (review finding, 2026-08-04 review-integration
    pass): this and `_derive_baton_count` previously each opened and read
    the SAME record independently on every `_read_phase_6_review_scale`
    call — two disk reads where one does, with a TOCTOU-flavoured window
    between them in which the record could change under the two reads. The
    single `read_text()` now lives in `_read_phase_6_review_scale`, which
    owns the unreadable-record case and turns it into the same unresolved
    judgment point this function's `None` would have produced — the failure
    direction is unchanged, only the read count is."""
    split = split_frontmatter(raw)
    if split is None:
        return None
    for key in _START_SHA_FM_KEYS:
        value = read_fm_field_unquoted(split.fm_text, key)
        if value and _START_SHA_RE.match(value.strip()):
            return value.strip()
    return None


def _read_recorded_run_id(raw: str) -> Optional[str]:
    """The record's own `run_id:` frontmatter value (PIPELINE.md § Phase 1),
    or `None` when absent/unusable. Read through the shipped
    `coordinator_core.frontmatter` primitives, same as
    `_read_recorded_start_sha`.

    REPORTING ONLY, and that is not a placeholder for a future selector:
    the CALLER's `--run-id` names the record (`_named_run_record`), so this
    value is the record's own claim about itself, surfaced as
    `run_identity.recorded_run_id` and in skip evidence. A record carrying
    `run_id:` and one lacking it take byte-identical paths through the
    lookup and the verdict; re-deriving the selection from this field would
    reinstate content-inference behind a flag that already answered the
    question."""
    split = split_frontmatter(raw)
    if split is None:
        return None
    value = read_fm_field_unquoted(split.fm_text, _RUN_ID_FM_KEY)
    if not value:
        return None
    value = value.strip()
    return value if _RUN_ID_RE.match(value) else None


def _derive_baton_count(raw: str) -> Optional[int]:
    """How many top-level batons this run's own inventory record's item
    table references, given the record's already-read text, or `None` when
    that cannot be determined from EXISTING record content (2026-08-04 sizing,
    `state/sizings/2026-08-04-mise-run-record-should-carry-baton-count.yaml`;
    source memo `cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-
    partition-mandatory-does-not-halt.md`).

    Deliberately NOT a new authored frontmatter field the Phase-1 scout
    would have to start writing (the sizing's explicit constraint) — this
    derives the count from the item table's own "spec path" column
    (`PIPELINE.md` § Phase 1's canonical row schema), which already names
    which plan/handoff/todo file backs each item. A baton is a distinct
    top-level artifact under that column (`docs/plans/*.md`,
    `state/handoffs/*.md`, `tasks/*/todo.md`, per `_BATON_SPEC_PATH_RE`);
    counting DISTINCT such paths across every item row is exactly "how
    many top-level artifacts this run's items trace back to" — the same
    quantity Phase 0a's baton-claim set names, without re-deriving a
    disposition-ledger walk this reader does not otherwise need.

    Fails closed, never toward `1`: absent a table, absent a column this
    function can identify as the spec-path column, or zero recognizable
    baton-shaped paths in that column all return `None` — "not yet
    resolved" per `decide_review_scale`'s own convention, never a default
    that would reproduce the exact under-read this derivation exists to
    fix (a genuine multi-baton run silently sizing as if it were one).

    Per-table-block scoping (review finding, 2026-08-04 review-integration
    pass): `header_cells`/`spec_path_col` are RESET on every non-table-row
    line, not carried across the whole document. A record whose body
    contains a second pipe table after the item table (a legend, a notes
    table — nothing here enforces "exactly one table per record") would
    otherwise have that second table's rows read as a continuation of the
    first table's body using the FIRST table's column index — silently
    inflating or misreading `baton_paths` from unrelated cell content,
    which is worse than this function's own documented `None` fail-closed
    case, because it still returns a resolved-but-WRONG count that gets
    multiplied into `decide_review_scale`'s row-4 brightline metrics. Only
    a contiguous table block whose OWN header matches
    `_SPEC_PATH_HEADER_RE` contributes to `baton_paths`.

    Backslash separators are normalized to `/` before matching (review
    finding, 2026-08-04 review-integration pass): a Windows-authored
    spec-path cell would otherwise miss `_BATON_SPEC_PATH_RE` entirely and
    drop that baton from the count — a resolved-but-UNDERCOUNTED result,
    which is worse than this function's documented `None`, since an
    undercount moves `decide_review_scale`'s row-4 metrics toward reviewing
    LESS. Windows is first-class here (repo `CLAUDE.md`).

    Text-in, not `Path`-in — see `_read_recorded_start_sha`'s docstring for
    why the single read now lives in `_read_phase_6_review_scale`."""
    header_cells: Optional[list[str]] = None
    spec_path_col: Optional[int] = None
    any_spec_path_col_seen = False
    baton_paths: set[str] = set()

    for line in raw.splitlines():
        match = _TABLE_ROW_RE.match(line)
        if not match:
            # A non-table-row line ends any table block in progress — the
            # next table row line starts a NEW block and must re-derive its
            # own header/column index rather than inheriting the prior
            # block's.
            header_cells = None
            spec_path_col = None
            continue
        cells = [c.strip() for c in match.group(1).split("|")]

        if header_cells is None:
            header_cells = cells
            for idx, cell in enumerate(header_cells):
                if _SPEC_PATH_HEADER_RE.search(cell):
                    spec_path_col = idx
                    any_spec_path_col_seen = True
                    break
            continue

        # The header/body separator row (`| --- | --- |`) — never a data
        # row, and never re-interpreted as a second header.
        if all(_TABLE_SEPARATOR_RE.match(c) for c in cells if c):
            continue

        if spec_path_col is None or spec_path_col >= len(cells):
            continue
        found = _BATON_SPEC_PATH_RE.findall(cells[spec_path_col].replace("\\", "/"))
        baton_paths.update(found)

    if not any_spec_path_col_seen or not baton_paths:
        return None
    return len(baton_paths)


def _measure_range(repo_root: Path, range_: str) -> Optional[dict[str, int]]:
    """Measure `range_` for the three SESSION-scoped inputs
    `decide_review_scale`'s row-4 brightline reads. Returns `None` when the
    range cannot be read at all (bad SHA, no git, detached/unborn HEAD) —
    never a zeroed dict, which would read as a resolved, trivially-small
    diff and resolve the verdict toward reviewing less.

    `gross_loc` is the added+deleted sum over the range's non-noise files
    (2026-08-11 metric-wide noise exclusion, matching the removed `_compute_chain_oracle`
    / `review_brightline_gate._session_scoped`'s contract — see
    `docs/plans/2026-08-11-brightline-gates-measure-reviewable-chan.md` AC3).
    Naming trap (review finding, 2026-08-11): `workstream_complete/__init__.py`'s
    own `gross_loc` (`_accumulate_numstat`) means the OPPOSITE — the raw,
    unfiltered sum, with `code_loc` as its noise-excluded sibling. This
    module's `gross_loc` is already noise-excluded; don't assume the two
    contracts match just because the name does.
    This SUPERSEDES this docstring's prior "no exclusion" negative-spec: a
    generated/vendored artifact or lifecycle-bookkeeping file
    (`review_brightline_gate._is_noise_path`) is dropped entirely, same as
    the gate's own oracles, rather than counted as reviewable diff. Noise
    exclusion is not the same predicate as de-weighting a deletion or a
    planning artifact — see `_is_noise_path`'s own module docstring for the
    two-category noise list; nothing here exempts a deletion.
    `code_loc` (2026-08-20, cross-repo/inbox/2026-08-20-example-retrieval-repo-em-
    review-gate-doc-only-em-discretion.md) is `gross_loc`'s PROSE-excluded
    sibling: markdown and YAML rows (`_is_prose_bearing_path`) contribute
    nothing to it, while still counting toward `gross_loc` and toward
    `surface_count`. Before this key existed, `_read_phase_6_review_scale`
    passed `gross_loc` in as `code_loc`, so a doc-only mise run measured
    thousands of lines of prose as reviewable code and tripped the row-4
    brightline into `partition_mandatory` — the same defect the memo
    reported against `workstream_complete`, at this second site. The two
    keys are returned SEPARATELY rather than one being redefined, so the
    Phase-6 directive's own `metrics` block now shows both numbers and a
    reader can see which one the verdict was taken off.

    `surface_count` reuses `review_brightline_gate.classify_surface` (the
    module's public alias for what was `_classify_surface`, review finding
    2026-08-04) rather than inventing a second surface taxonomy — the gate
    this verdict names must be counting the same buckets it does."""
    commits_out = _run_git_read_only(["rev-list", "--count", range_], repo_root)
    if commits_out is None:
        return None
    try:
        commit_count = int(commits_out.strip())
    except ValueError:
        return None

    numstat = _run_git_read_only(["diff", "--numstat", range_], repo_root)
    if numstat is None:
        return None

    gross_loc = 0
    code_loc = 0
    surfaces: set[str] = set()
    for line in numstat.splitlines():
        match = _NUMSTAT_ROW_RE.match(line)
        if not match:
            continue
        added, deleted, path = match.groups()
        # 2026-08-12: resolve rename notation (`{old => new}`/`old => new`)
        # to the destination path before any path predicate sees it — same
        # fix as the removed `review_brightline_gate._compute_chain_oracle`'s (K-007), over
        # the identical bug (`_is_noise_path`'s anchored lifecycle-prefix
        # match never fires against the literal rename fragment). See
        # `_resolve_numstat_row_path`.
        path = _resolve_numstat_row_path(path)
        if _is_noise_path(path):
            continue
        row_loc = (int(added) if added != "-" else 0) + (int(deleted) if deleted != "-" else 0)
        gross_loc += row_loc
        if not _is_prose_bearing_path(path):
            code_loc += row_loc
        surfaces.add(classify_surface(path))

    return {
        "gross_loc": gross_loc,
        "code_loc": code_loc,
        "commit_count": commit_count,
        "surface_count": len(surfaces),
    }


def _unresolved_range_judgment_point(reason: str, evidence: str) -> ReaderResult:
    """The AC3 outcome: an unresolvable range asks, it never guesses.

    `build_untrusted_gate_judgment_point` (no `recommendation` parameter at
    all) rather than `build_judgment_point`, deliberately: a recommendation
    attached to an UNMEASURED range is the one shape that would undo this
    surface's whole purpose — it would hand the EM engine-blessed cover for
    proceeding at a review scale nothing computed, which is worse than the
    prose this chunk replaces. This is the failure
    `cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-partition-
    mandatory-does-not-halt.md` § mechanism 3 reports from the field:
    workstream-complete's own `jp-review-scale` offering
    `proceed-unresolved` as its sole disposition, which routes around the
    very gate whose verdict went missing. There is nothing to recommend by
    construction; the range has to become measurable first.

    This is also what "a loud failure on a missing `--run-id`" means here
    (ratified 2026-08-04): the reader does not raise, does not crash the
    brief, and does not guess — it resolves to THIS judgment point, the
    surface's established idiom for an input it cannot honestly supply, with
    `name_the_run_id_and_rerun` as a disposition the caller can act on."""
    return ReaderResult(
        judgment_points=[
            build_untrusted_gate_judgment_point(
                id=_PHASE_6_JUDGMENT_POINT_ID,
                question=(
                    "mise Phase 6's review scale could not be computed: "
                    f"{reason}. Phase 6 reviews the RUN'S OWN cumulative "
                    "diff and is not discharged by /workstream-complete's "
                    "chain review. Re-run this brief naming the run "
                    f"(`backlog-grind-assemble brief {CADENCE} "
                    f"{_RUN_ID_FLAG} <run-id>`) with its start SHA recorded "
                    f"in `{_MISE_INVENTORY_DIRNAME}/<run-id>.md` as "
                    f"`{_START_SHA_FM_KEYS[0]}:`, or partition the review by "
                    "hand — never fall through to a single reviewer on an "
                    "unmeasured range."
                ),
                dispositions=[
                    build_disposition("name_the_run_id_and_rerun"),
                    build_disposition("record_start_sha_and_rerun"),
                    build_disposition("partition_review_by_hand"),
                ],
                evidence=evidence,
                reason="insufficient-evidence",
            )
        ]
    )


def _run_identity(record: _RunRecord, run_id: str) -> dict[str, Any]:
    """How the verdict's record was identified, reported alongside it.

    `run_id` is what the CALLER named — the value that actually selected the
    record, hence `selected_by: run-id-flag`. `recorded_run_id` is what the
    record's own frontmatter claims (`None` when it carries no `run_id:`),
    reported so a mismatch between the named file and its stated identity is
    visible to an operator without being re-derived into a selection rule.
    Three review rounds kept re-finding the same failure — a resolved-looking
    verdict computed off a record nothing had named — and this key is what
    makes its absence checkable."""
    return {
        "run_id": run_id,
        "recorded_run_id": record.run_id,
        "selected_by": _SELECTED_BY_RUN_ID_FLAG,
    }


def _phase_6_directive(
    decision: ReviewScaleDecision,
    range_: str,
    metrics: dict[str, int],
    record_path: str,
    run_identity: dict[str, Any],
) -> dict[str, Any]:
    """The computed Phase-6 verdict, shaped as a directive naming the
    existing `review-brightline-gate` CLI over the run's own range (AC4).
    The `verdict` sibling key carries `decide_review_scale`'s returned
    decision verbatim — this reader adds no field the shipped function did
    not compute, and re-derives none of them. `run_identity` is provenance
    for the RECORD the verdict was computed from (`_run_identity`), never
    an input to the verdict."""
    return {
        "id": _PHASE_6_DIRECTIVE_ID,
        "cli": _REVIEW_BRIGHTLINE_CLI,
        "args": [range_],
        "depends_on": None,
        "already_satisfied": False,
        "range": range_,
        "inventory_record": record_path,
        "run_identity": dict(run_identity),
        "metrics": dict(metrics),
        "verdict": {
            "row": decision.row,
            "scale": decision.scale,
            "partition_mandatory": decision.partition_mandatory,
            "reason": decision.reason,
            "resolved": decision.resolved,
        },
    }


def _read_phase_6_review_scale(run_id: Optional[str]) -> ReaderResult:
    """mise Phase 6's review-scale verdict — see the module docstring's
    point 4 and its three-way Phase-0/Phase-6 gate.

    `run_id` is the caller's `--run-id`, naming which
    `state/mise-inventory/<run-id>.md` record is the run asking. `None`
    means the caller supplied no flag: silent when the inventory directory
    is absent (Phase 0 — the scout has not run, and nagging every Phase-0
    call is what the on-disk self-gate exists to avoid), and the unresolved
    judgment point NAMING THE MISSING FLAG once records exist. There is no
    inference path left to fall back to (2026-08-04 ratification), which is
    exactly the property that keeps a forgetful caller loud instead of
    quietly measured against a record nothing named.

    `decide_review_scale` is CALLED, never re-implemented. Its inputs here:

      - `gross_loc`/`commit_count`/`surface_count` — measured over the run's
        own `<recorded-start-sha>..HEAD` range, the exact scope
        `PIPELINE.md` § Phase 6 step 1 names ("the run's cumulative diff
        range from the goal task's recorded starting SHA through HEAD").
      - `executor_dispatched=True` — true by CONSTRUCTION for any run that
        reached Phase 6, not an assumption: mise's Phase 5 dispatches a
        Sonnet executor per item, and this surface only fires once the
        Phase-1 inventory record exists. This is what makes a below-
        brightline run resolve to row 3 (single reviewer, carrying the
        row-3 justification) instead of unresolved.
      - `code_loc` — PRODUCED (2026-08-11, AC4): `_measure_range`'s
        `gross_loc` is already the noise-excluded reviewable LOC (see that
        function's docstring), the exact quantity `code_loc` names, so it is
        passed straight through rather than measured a second time. Row 3
        short-circuits on `executor_dispatched is True` before it is ever
        read, same as before this chunk.
      - `shared_schema_touched=None` — honestly unresolved at a static
        `collect(cadence)` call site, and never consulted for the same
        reason. `None` (not a fabricated `0`/`False`) so that the
        distinction between "unresolved" and "resolved negative" stays
        intact if that precedence ever changes.
      - `chain_disposition=SINGLE_SESSION` — imported, not spelled locally.
        Phase 6 is RUN-scoped by definition; the chain-terminal rows 5/6
        belong to `/workstream-complete`'s chain review, which this verdict
        explicitly does not discharge (`PIPELINE.md` § Phase 6).
      - `baton_count` — derived by `_derive_baton_count` from the SAME
        record's item table (see that function's docstring), never a
        second measurement of the run's diff. `None` when the record's
        item table carries no identifiable spec-path column or no
        recognizable baton path — never a default of `1` (2026-08-04
        sizing, `state/sizings/2026-08-04-mise-run-record-should-carry-
        baton-count.yaml`).
    """
    state_root_str = _resolve_state_root()
    if not state_root_str:
        return ReaderResult()
    state_root = Path(state_root_str)
    repo_root = state_root.parent

    inventory_dir = state_root / _MISE_INVENTORY_DIRNAME
    if not inventory_dir.is_dir():
        return ReaderResult()

    # Records exist and the caller named no run: the loud failure the
    # ratified carrier contract asks for. Loud here means the surface's own
    # idiom — an unresolved judgment point naming the missing flag — not a
    # raise, and emphatically not a guess. The deleted alternative (pick the
    # record whose start SHA is the most recent ancestor of HEAD) is what a
    # fallback would silently reactivate for exactly the caller who forgot.
    if run_id is None:
        return _unresolved_range_judgment_point(
            f"no {_RUN_ID_FLAG} was supplied, so nothing names which run is "
            "asking",
            f"{inventory_dir}: inventory records are present, but which one "
            "is the run in flight is READ from the caller, never inferred — "
            f"re-run as `backlog-grind-assemble brief {CADENCE} "
            f"{_RUN_ID_FLAG} <run-id>`",
        )

    # `_named_run_record` takes this already-verified `inventory_dir` rather
    # than re-deriving and re-checking it itself, so the two checks cannot
    # drift apart (review finding, 2026-08-04 review-integration pass). ONE
    # read, with the record's text carried on the returned `_RunRecord` — the
    # derivations below never re-open it. A missing record, an unreadable
    # record, and a record with no usable start SHA all resolve to the same
    # unresolved judgment point: the surface fails toward asking, never
    # toward a default verdict, and never by raising out of `collect()`.
    lookup = _named_run_record(inventory_dir, run_id)
    record = lookup.record
    if record is None:
        return _unresolved_range_judgment_point(
            f"the record named by {_RUN_ID_FLAG} {run_id} could not be used "
            "to measure the run's range, and no other record stands in for it",
            f"{lookup.reason}",
        )

    raw = record.raw
    range_ = f"{record.start_sha}..HEAD"
    metrics = _measure_range(repo_root, range_)
    if metrics is None:
        return _unresolved_range_judgment_point(
            f"the range {range_} could not be read from git",
            f"{record.path}: `git rev-list --count {range_}` / "
            f"`git diff --numstat {range_}` did not resolve under {repo_root}",
        )

    decision = decide_review_scale(
        gross_loc=metrics["gross_loc"],
        code_loc=metrics["code_loc"],
        commit_count=metrics["commit_count"],
        surface_count=metrics["surface_count"],
        executor_dispatched=True,
        shared_schema_touched=None,
        chain_disposition=SINGLE_SESSION,
        baton_count=_derive_baton_count(raw),
    )

    if not decision.resolved:
        return _unresolved_range_judgment_point(
            f"decide_review_scale returned unresolved ({decision.reason})",
            f"{record.path}: range={range_} metrics={metrics}",
        )

    return ReaderResult(
        directives=[
            _phase_6_directive(
                decision, range_, metrics, str(record.path), _run_identity(record, run_id)
            )
        ]
    )


#: `tasks/*/todo.md` carries no frontmatter (module docstring's resolved
#: frontmatter-less-leg note) — matched separately so the role-axis
#: resolution below can skip the frontmatter read entirely for this leg
#: shape and exclude it from the fallback count's denominator.
_TODO_LEG_RE = re.compile(r"^tasks/[\w./-]+/todo\.md$")

#: The two `baton_role` values DoE ratified
#: (`cross-repo/inbox/2026-08-19-doe-claude-em-baton-role-axis-ruling.md`).
#: An artifact's frontmatter value outside this set — including the
#: retired-by-ruling `execution` spelling — is treated exactly like an
#: absent field: unknown, never accepted as either member.
_BATON_ROLE_VALUES = ("work", "record")

#: The run's ONE unification directive, and the verb it names — a key of
#: `apply.py`'s CLOSED `_CLI_DISPATCH` table, which is what makes it a legal
#: directive at all: `apply_base.resolve_cli` PRE-VALIDATES every directive's
#: `cli` before any directive in the run executes, so a `cli`-less directive
#: does not sit inert, it raises `UnrecognizedDirective` and takes the whole
#: `/mise-en-place` apply run down before the first real directive fires. An
#: earlier shape of this reader emitted exactly that.
_MISE_UNIFY_DIRECTIVE_ID = "d-mise-unify-batons"
_MISE_UNIFY_CLI = "unify-batons"


def _scan_spec_path_cells(raw: str) -> set[str]:
    """The item table's spec-path cells, scanned the same way
    `_derive_baton_count` scans them (per-table-block header/column
    reset, backslash normalization — see that function's docstring for
    the rationale of both). Kept standalone rather than sharing
    `_derive_baton_count`'s body: that function's return contract already
    has a call site (`_read_phase_6_review_scale`'s `baton_count` metric)
    this chunk must not perturb, so the unification set below is derived
    from its own scan rather than a refactor that risks changing it."""
    header_cells: Optional[list[str]] = None
    spec_path_col: Optional[int] = None
    baton_paths: set[str] = set()

    for line in raw.splitlines():
        match = _TABLE_ROW_RE.match(line)
        if not match:
            header_cells = None
            spec_path_col = None
            continue
        cells = [c.strip() for c in match.group(1).split("|")]

        if header_cells is None:
            header_cells = cells
            for idx, cell in enumerate(header_cells):
                if _SPEC_PATH_HEADER_RE.search(cell):
                    spec_path_col = idx
                    break
            continue

        if all(_TABLE_SEPARATOR_RE.match(c) for c in cells if c):
            continue

        if spec_path_col is None or spec_path_col >= len(cells):
            continue
        found = _BATON_SPEC_PATH_RE.findall(cells[spec_path_col].replace("\\", "/"))
        baton_paths.update(found)

    return baton_paths


def _artifact_baton_role(repo_root: Path, spec_path: str) -> Optional[str]:
    """The `baton_role` frontmatter value read directly off the artifact
    `spec_path` names (repo-relative, `_BATON_SPEC_PATH_RE`'s own
    alphabet). `None` on any read failure, missing/unparsable
    frontmatter, an absent field, or a value outside `_BATON_ROLE_VALUES`
    — POSITIVE MATCH ONLY (module docstring / C7's own discipline):
    absence is UNKNOWN, never defaulted, and an unrecognised third value
    is also UNKNOWN rather than silently read as legacy or as either
    ratified member."""
    try:
        raw = (repo_root / spec_path).read_text(encoding="utf-8")
    except OSError:
        return None
    split = split_frontmatter(raw)
    if split is None:
        return None
    value = read_fm_field_unquoted(split.fm_text, "baton_role")
    if not value:
        return None
    value = value.strip()
    return value if value in _BATON_ROLE_VALUES else None


class _BatonInheritance(NamedTuple):
    """`_resolve_baton_inheritance`'s outcome: the run's fan-in legs
    (`baton_role: work` where present, or the counted path-shape
    heuristic fallback where the axis is absent) and how many of those
    legs resolved via the fallback rather than the axis — restricted to
    FRONTMATTER-BEARING artifacts (a `tasks/*/todo.md` leg is on the
    fallback permanently and is excluded from this count's denominator,
    per the plan's resolved frontmatter-less-leg note)."""

    inheritable: tuple[str, ...]
    fallback_count: int


def _resolve_baton_inheritance(repo_root: Path, baton_paths: set[str]) -> _BatonInheritance:
    """PUT THE AXIS IN FRONT OF THE HEURISTIC — DO NOT SWAP IT OUT (ruled:
    `cross-repo/inbox/2026-08-19-doe-claude-em-baton-role-axis-ruling.md`).
    `baton_role: work` on a frontmatter-bearing artifact makes it
    inheritable; `baton_role: record` excludes it explicitly; an absent
    (or unrecognised, or frontmatter-less) axis falls to the path-shape
    heuristic, which — same as `_derive_baton_count` today — still counts
    a recognized `_BATON_SPEC_PATH_RE` match as inheritable, and is
    tallied in `fallback_count` so the stamped fraction of the corpus
    stays a readable number rather than a silent zero."""
    inheritable: list[str] = []
    fallback_count = 0
    for path in sorted(baton_paths):
        frontmatter_bearing = not _TODO_LEG_RE.match(path)
        role = _artifact_baton_role(repo_root, path) if frontmatter_bearing else None
        if role == "record":
            continue
        if role == "work":
            inheritable.append(path)
            continue
        # Axis absent (or unrecognised, or this leg is permanently
        # frontmatter-less): the counted path-shape heuristic fallback.
        inheritable.append(path)
        if frontmatter_bearing:
            fallback_count += 1
    return _BatonInheritance(tuple(inheritable), fallback_count)


def _unify_batons_directive(
    inheritance: _BatonInheritance, record_path: str
) -> dict[str, Any]:
    """ONE directive per run naming every inheritable baton as a candidate
    fan-in leg (`additional_predecessors` — D-A, the schema-sanctioned
    edge; no new `merged_from`-shaped axis invented here). Never one per
    item: per-item unification would mint a chain of merged batons and grow
    the chain walk without bound (C8's own anti-scope).

    The verb is `unify-batons`, whose handler delegates to C5's routed
    path (`pickup_assemble.unify_run_batons`) — the plan's anti-scope
    holds: unification keeps exactly one implementation, and this reader
    contributes the run's resolved set, not a second one.

    `args` carries the legs as bare path strings, matching every other
    directive this module emits; the two sibling keys are repacked into
    `args[0]` by `apply.py :: _prepare_directives_for_dispatch` before
    dispatch (that module's own docstring on why only `args` crosses the
    handler seam).

    THE LEGS ARE REPORTING INPUT, NOT THE MUTATION'S AUTHORITY. The routed
    path stamps the batons the DURABLE CLAIM LEDGER says this session holds
    (AC7), never this list — an inventory table naming a baton this session
    does not hold must not be able to stamp it terminal. The two sets are
    reported side by side so a divergence is visible rather than silently
    reconciled.

    `role_axis_fallback_count` is `_resolve_baton_inheritance`'s own counted
    fallback, carried through unchanged so "how much of the corpus is
    stamped" is readable off the emitted brief rather than inferred — AC10
    gates retirement of the path-shape heuristic on that count reaching
    zero. `inventory_record` names the record the set was resolved from.

    No judgment point accompanies it, deliberately. `/mise-en-place` is an
    autonomous backlog run and `apply` HALTS at the first unresolved
    judgment point, so gating unification on one would stop every run at
    this reader. The refusal that matters is not skippable and is not
    here: the routed path resolves its held set from THIS session's own
    claim ledger, so a baton held by anyone else cannot enter it at all.
    (The routed predicate is ON as of `c09345b56` — do not read this
    directive as inert.)
    """
    legs = list(inheritance.inheritable)
    return {
        "id": _MISE_UNIFY_DIRECTIVE_ID,
        "cli": _MISE_UNIFY_CLI,
        "args": legs,
        "depends_on": None,
        "already_satisfied": False,
        "additional_predecessors": legs,
        "role_axis_fallback_count": inheritance.fallback_count,
        "inventory_record": record_path,
    }


def _read_baton_unification(run_id: Optional[str]) -> ReaderResult:
    """Resolve this run's inheritable batons and emit ONE unification
    directive naming them all as fan-in legs — never one per item (C8's
    own anti-scope: per-item unification would mint a chain of merged
    batons and grow the chain walk without bound).

    Gating mirrors `_read_phase_6_review_scale`'s own silent-Phase-0 /
    named-run split, but does NOT duplicate its unresolved judgment
    point on a missing `--run-id` or an unusable record — that failure is
    already surfaced there, and a second judgment point saying the same
    thing would be noise this reader's own idiom (ask once) argues
    against."""
    state_root_str = _resolve_state_root()
    if not state_root_str:
        return ReaderResult()
    state_root = Path(state_root_str)
    repo_root = state_root.parent

    inventory_dir = state_root / _MISE_INVENTORY_DIRNAME
    if not inventory_dir.is_dir() or run_id is None:
        return ReaderResult()

    lookup = _named_run_record(inventory_dir, run_id)
    record = lookup.record
    if record is None:
        return ReaderResult()

    baton_paths = _scan_spec_path_cells(record.raw)
    if not baton_paths:
        return ReaderResult()

    inheritance = _resolve_baton_inheritance(repo_root, baton_paths)
    if not inheritance.inheritable:
        return ReaderResult()

    return ReaderResult(
        directives=[_unify_batons_directive(inheritance, str(record.path))]
    )


def _read_backlog_readiness() -> ReaderResult:
    """Surfaces whether bug-backlog/debt-backlog currently carry any open
    item — see the module docstring's point 1. Emits a judgment point (never
    a directive: there is no mutation to name here, only an EM-facing
    readiness fact) only when BOTH families read empty of open items;
    otherwise a silent `ReaderResult()`, since "there is open backlog work"
    needs no EM prompt of its own."""
    state_root_str = _resolve_state_root()
    if not state_root_str:
        return ReaderResult()
    repo_root = Path(state_root_str).parent

    open_counts: dict[str, int] = {}
    for family in _QUEUE_FAMILIES:
        try:
            records = load_family_records(family, repo_root)
        except UnknownQueueFamilyError:
            continue
        open_counts[family] = sum(
            1
            for record in records
            if (record.get("frontmatter") or {}).get("status") != "closed"
        )

    if sum(open_counts.values()) > 0:
        return ReaderResult()

    detail = ", ".join(f"{family}={count}" for family, count in open_counts.items())
    return ReaderResult(
        judgment_points=[
            build_judgment_point(
                None,
                id="j-mise-empty-queue-backlog",
                question=(
                    "No open bug-backlog or debt-backlog items found "
                    f"({detail or 'no families readable'}). mise-en-place's "
                    "own Phase 1 also sources from tasks/*/todo.md plan "
                    "files and a PM-named $ARGUMENTS list — neither is a "
                    "queue family this reader can see — so this is not "
                    "proof there is nothing to run, only evidence toward "
                    "it. Proceed with the run or stand down?"
                ),
                dispositions=[
                    build_disposition("proceed_anyway"),
                    build_disposition("stand_down"),
                ],
                evidence=(
                    f"queue_family.load_family_records over {_QUEUE_FAMILIES}: "
                    f"{detail or 'no families readable at this state root'}"
                ),
                reason="insufficient-evidence",
            )
        ]
    )


def _read_executor_dispatch_template() -> ReaderResult:
    """Emits AC26's fixed executor dispatch-prompt template as a directive —
    see the module docstring's point 2. Fires unconditionally whenever this
    reader is asked (independent of backlog-emptiness above): the template
    is dispatch-time utility content, not gated on there being open backlog
    work right now."""
    return ReaderResult(
        directives=[
            build_executor_dispatch_prompt_template_emission(
                id="d-mise-executor-dispatch-prompt-template",
                fields={
                    "anti_hallucination_preamble": _ANTI_HALLUCINATION_PREAMBLE,
                    "footprint_constraint_template": _FOOTPRINT_CONSTRAINT_TEMPLATE,
                    "self_verify_constraint": _SELF_VERIFY_CONSTRAINT,
                    "done_summary_constraint_template": _DONE_SUMMARY_CONSTRAINT_TEMPLATE,
                },
            )
        ]
    )


def _read_haiku_verifier_dispatch() -> ReaderResult:
    """Emits mise-en-place's per-item Haiku-verifier dispatch directive via
    `verifier.build_haiku_verifier_dispatch` — the primitive C6 built
    specifically to parameterize this call site (see that module's
    docstring, which names this reader as one of its two intended callers).
    Fires unconditionally whenever this reader is asked, same posture as
    `_read_executor_dispatch_template` above: this is dispatch-time utility
    content, not gated on backlog-emptiness. `enum_set=MISE_VERIFIER_ENUM`
    (never the bug-blitz enum — see verifier.py's negative-spec against
    merging the two); `output_path` carries this reader's fixed output-path
    template (`_MISE_VERIFIER_OUTPUT_PATH_TEMPLATE`), with the `<item-id>`
    placeholder left for the actual per-item dispatch to fill in, exactly as
    the template emission above leaves `[item-id]`/`[list]` for its own
    consumer."""
    return ReaderResult(
        directives=[
            build_haiku_verifier_dispatch(
                id="d-mise-haiku-verifier-dispatch",
                evidence_source=_MISE_VERIFIER_EVIDENCE_SOURCE,
                enum_set=MISE_VERIFIER_ENUM,
                output_path=_MISE_VERIFIER_OUTPUT_PATH_TEMPLATE,
            )
        ]
    )


#: `mint_run_id`'s timestamp stem — `%H%M%S` with no `:` separators, so the
#: stem alone stays inside `_RUN_ID_ARG_RE`'s legal alphabet
#: (`[A-Za-z0-9._-]`) without any escaping. UTC, not local time, so two
#: mints on hosts in different zones stay comparably sortable.
_MINT_TIMESTAMP_FMT = "%Y%m%dT%H%M%S"

#: Random-suffix width in bytes (hex-encoded, so 4 bytes -> 8 hex chars).
#: This is what makes two mints inside the SAME clock second differ (AC4)
#: — the timestamp stem alone is only second-resolution.
_MINT_SUFFIX_BYTES = 4

#: Bounded retry count for the AC4 collision check below. Not unbounded:
#: a minter that could loop forever on a pathological `state/mise-inventory/`
#: would itself be a liveness bug; this many consecutive collisions against
#: fresh random suffixes is itself the loud failure (`RuntimeError`), never
#: silently returning a colliding id.
_MINT_MAX_ATTEMPTS = 10

#: The retry loop's own `_RUN_ID_ARG_RE.match(candidate)` assertion below is
#: unreachable ONLY while `_MINT_TIMESTAMP_FMT`/`_MINT_SUFFIX_BYTES` above
#: keep every candidate inside `_RUN_ID_ARG_RE`'s legal alphabet — a future
#: edit to either constant must re-verify that invariant still holds, or the
#: assertion becomes a reachable, untested failure mode (review nit,
#: coordinator-code-reviewer 2026-08-04).


class MintedRunId(NamedTuple):
    """`mint_run_id`'s return shape (AC1): the minted id and the
    repo-relative, forward-slash `state/mise-inventory/<run_id>.md` path it
    implies — NEVER a machine-absolute path: this repo is Windows-first, and
    an absolute-path string would platform-separate on Windows (backslashes
    in place of `/`), and this value is also consumed by a DoE-claude hook
    in a sibling repo, where an absolute path rooted in *this* machine is
    meaningless. `__init__.py`'s `main()` serialises these two fields
    verbatim to JSON — see AC7's negative-spec: it reads
    `.run_id`/`.inventory_path` off whatever this reader returns and does
    not itself construct, parse, or validate an id. A `NamedTuple`,
    matching this module's own `_RunRecord`/`_RecordLookup` idiom, rather
    than a plain dict."""

    run_id: str
    inventory_path: str


def _generate_candidate_run_id() -> str:
    """One candidate run id: a UTC timestamp stem plus a short random
    suffix (see `_MINT_TIMESTAMP_FMT`/`_MINT_SUFFIX_BYTES` above). Legible
    and sortable by construction — the timestamp stem sorts chronologically
    — never re-checked for legality here; `mint_run_id` asserts every
    candidate against `_RUN_ID_ARG_RE` before it is ever handed out."""
    stamp = datetime.now(timezone.utc).strftime(_MINT_TIMESTAMP_FMT)
    suffix = secrets.token_hex(_MINT_SUFFIX_BYTES)
    return f"{stamp}-{suffix}"


def mint_run_id(cadence: str, *, repo_root: Optional[Path] = None) -> Optional[MintedRunId]:
    """Mint a fresh, conforming run id for `cadence`, or `None` for any
    cadence other than `mise-en-place` — self-gating exactly the way
    `collect()` above already self-gates on `CADENCE`, never a new idiom.

    Returns the minted id plus the `state/mise-inventory/<run_id>.md` path
    it implies (AC1), as a repo-relative, forward-slash string — the SAME
    path `_named_run_record` would derive from that id later relative to
    the state root, computed here by the same join rather than a second
    formula, but reported relative rather than as a machine-absolute
    string (see `MintedRunId`'s own docstring for why: Windows
    path-separator platform-dependence, and a sibling-repo consumer for
    whom an absolute path rooted in *this* machine is meaningless). The
    machine-absolute form is still what the collision check below stats
    against on disk — only the RETURNED field is relative.

    `repo_root`, when given, is used directly (`<repo_root>/state`) rather
    than re-resolving one via `_resolve_state_root()` — this is the seam a
    caller (a test, or a future in-process caller with an already-known
    root) uses to pin the root explicitly without mutating process-global
    state (`CWS_TEST_STATE_ROOT`) or process cwd, the same rationale
    `check_weekly_staleness._git_root`'s own `cwd` parameter documents.
    `repo_root=None` (the default, and what `__init__.py`'s dispatch uses)
    resolves the root the same way every OTHER read in this module already
    does — `_resolve_state_root()` — never a second root-resolution path.
    `None` here (an unresolvable root) means `None` out, same as
    `_read_backlog_readiness`/`_read_phase_6_review_scale`'s own posture on
    an unresolvable state root.

    READ-ONLY throughout (AC3): the only disk touch below is `Path.is_file()`
    — a stat, not a write — checked against `state/mise-inventory/` without
    ever creating that directory or any file under it. This reader's own
    Phase-0-vs-Phase-6 self-gate (module docstring) is the directory's
    ABSENCE; a minter that created it would flip every future Phase-0 call
    into looking like Phase 6, which is why this function must never
    `mkdir` it — even implicitly, by writing a file inside it — no matter
    how tempting that would be for the Phase-1 scout's convenience.

    Every candidate is asserted against `_RUN_ID_ARG_RE` (AC2) — the SAME
    pattern `_named_run_record` validates a caller-supplied `--run-id`
    against, referenced here rather than re-declared, per this module's own
    negative-spec against a second copy. A candidate failing that assertion
    is this function's own bug, not a caller input to reject quietly —
    hence a raised `AssertionError` naming the offending candidate, not a
    silent retry or a `None`.

    Collision check (AC4's disk half): a candidate whose
    `state/mise-inventory/<candidate>.md` already exists on disk is
    discarded and a fresh one drawn, up to `_MINT_MAX_ATTEMPTS` times. The
    random suffix (AC4's same-second half) makes two mints in the same
    process and the same clock second differ with overwhelming probability
    on the FIRST draw; the retry loop is the backstop for the residual case,
    never the primary mechanism.
    """
    if cadence != CADENCE:
        return None

    if repo_root is not None:
        state_root = Path(repo_root) / "state"
    else:
        state_root_str = _resolve_state_root()
        if not state_root_str:
            return None
        state_root = Path(state_root_str)

    inventory_dir = state_root / _MISE_INVENTORY_DIRNAME

    for _ in range(_MINT_MAX_ATTEMPTS):
        candidate = _generate_candidate_run_id()
        if not _RUN_ID_ARG_RE.match(candidate):
            raise AssertionError(
                f"mint_run_id: generated candidate {candidate!r} fails its "
                "own _RUN_ID_ARG_RE — a minted id must always be a value "
                "the very next `brief --run-id` accepts (AC2)"
            )
        candidate_path = inventory_dir / f"{candidate}.md"
        if not candidate_path.is_file():
            relative_inventory_path = "/".join(
                ("state", _MISE_INVENTORY_DIRNAME, f"{candidate}.md")
            )
            return MintedRunId(run_id=candidate, inventory_path=relative_inventory_path)

    raise RuntimeError(
        f"mint_run_id: {_MINT_MAX_ATTEMPTS} consecutive collisions against "
        f"{inventory_dir} — refusing to mint a colliding run id"
    )


def collect(cadence: str, *, run_id: Optional[str] = None) -> ReaderResult:
    """Compute this reader family's directives/judgment_points for
    `cadence`. A no-op `ReaderResult()` for every cadence other than
    `"mise-en-place"` — see `CADENCE` above and the module docstring.

    `run_id` is the caller's `--run-id`, threaded uniformly to all five
    readers by the `__init__.py` seam and self-gated here exactly as
    `cadence` is: this reader consumes it, the other four ignore it, and the
    seam never learns which is which. Only the Phase-6 tail surface and the
    baton-unification surface read it; the other two sub-readers below are
    run-invariant."""
    if cadence != CADENCE:
        return ReaderResult()

    results = [
        _read_backlog_readiness(),
        _read_executor_dispatch_template(),
        _read_haiku_verifier_dispatch(),
        _read_phase_6_review_scale(run_id),
        _read_baton_unification(run_id),
    ]

    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []
    for result in results:
        directives.extend(result.directives)
        judgment_points.extend(result.judgment_points)
    return ReaderResult(directives=directives, judgment_points=judgment_points)
