"""
coordinator_core.ops.records_query — JSON-RPC "records.query" operation.

Purpose: COMPUTE_ONLY read op that queries claude-klabauter's own state records
(handoffs, archived handoffs, plans) using an equality-AND ``--where``
filter, returning repo-relative paths or full frontmatter JSON.  Covers
the bounded subset of ``query-records.js``'s query surface this repo's
own callers need.

This is a per-repo live-state query surface (claude-klabauter's class per the
tri-plane ownership boundary).  It does NOT touch rag's fleet relational
store, does NOT spawn any git subprocess, and does NOT mutate any file.

Self-registration: importing this module calls
``register_op("records.query", _handler)`` as a side-effect.  Add this
module to ``coordinator_core/ops/__init__.py`` to trigger registration at
start_server() time.

Worktree resolution (mirrors roadmap_serve.py / handoff_children.py):
  - When ``repo_root`` is provided (router-supplied git common dir), the
    worktree root is derived via ``main_worktree_root(repo_root)``.
  - If ``repo_root`` is absent the op returns a well-formed empty payload
    with a logged warning — an unknown worktree is NOT a 500.

Spec backlink: pln-strang-11-c11-12-native-record-e92436 § C1
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4d-g1
  (query-records.js grammar EXTEND — freeze-query-records-grammar.md is the parity
  oracle; the FULL ``--where``/``--since``/``--older-than``/``--sort``/``--format``
  grammar and the ``liveness()`` predicate table below are byte-parity ports of the
  matching surfaces in DoE ``coordinator/bin/query-records.js``, not a fresh design.)

Grammar surface (post-T4d-g1c EXTEND):
  - ``--where`` supports the full operator set — ``=``, ``!=``, ``<``, ``>``, ``<=``,
    ``>=``, ``field in (a,b,c)``, and a bare-field presence filter — with the SAME
    ``parseClause`` operator-scan order as query-records.js (``!=``,``<=``,``>=``,
    ``<``,``>``,``=``, in that sequence; array-aware ``in`` on list-valued frontmatter
    fields).  Only a genuinely unparseable clause (no recognised operator, not a bare
    word field name) raises ``SystemExit(1)`` — the equality-only bounded subset
    this module shipped with pre-T4d-g1c is now a strict subset of the supported
    grammar, not the ceiling.
  - ``since``/``older_than`` params (``Nd``/``Nw``/``Nm``/``YYYY-MM-DD``) filter on
    ``created`` — mirror-image ``>=``/``<`` string-lexicographic comparisons, both
    excluding records with no ``created`` field.
  - ``sort`` param (bare field ascending, ``-``-prefixed field descending) uses the
    same numeric-first-then-string ``compareValues`` as ``--where``.
  - ``format`` adds ``markdown-list`` (also the default when omitted) — per-type
    render functions, imported from ``coordinator_core.text.query_record_display
    .TYPE_DISPLAY`` (the single shared registry — see that module's docstring
    and this module's own note above ``_apply_sibling_exclusion``) for 17 of
    the 22 types this op serves (``handoff``, ``handoff-archived``, ``plan``,
    ``cross-repo-memo``, ``decision``, ``review``, ``lesson``, ``handoff-
    ledger``, ``research-claim``, ``debt``, ``bug``, ``improvement``,
    ``decision-guide``, ``completion``, ``research-synthesis``, ``gap-
    report``, ``coverage-audit``); the remaining 5 (``tracker``, ``roadmap``,
    ``health-status``, ``goal``, ``archived-memo``) fall back to the bare-path
    default renderer, which DOES mirror query-records.js's own
    ``formatRecords`` default-fn fallback for these — verified the oracle's
    own ``TYPE_DISPLAY`` has no entry for ``tracker``/``roadmap``/
    ``health-status``/``goal`` either (bin/query-records.js:307-339);
    ``archived-memo`` is simply not yet ported (see
    ``coordinator_core/text/query_record_display.py``'s Negative-spec).
  - ``liveness(fm, record_type)`` is a byte-exact port of the full 13-type-plus-
    graceful-default predicate table (freeze-query-records-grammar.md Surface 2),
    injected onto every record's frontmatter as ``fm['liveness']`` BEFORE
    since/older-than/where filtering (same ordering as query-records.js:1401-1406),
    so ``--where liveness=BLOCKED`` composes correctly.
  - ``archived`` (``fm['archived']``, always present) is the collection-origin
    boolean threaded through ``_collect_type_records`` -> ``_load_record``,
    true exactly for records collected from ``_ARCHIVE_GLOB_FOR_TYPE``'s glob
    — never derived from the emitted ``path`` string (see ``_load_record``'s
    docstring negative-spec). ``include_body`` (opt-in param, default off)
    additionally projects ``fm['body']`` — post-frontmatter text for ``.md``
    records, ``null`` for ``.yaml`` whole-file records — and is rejected for
    the synthetic types (``handoff-ledger``, ``research-claim``), which have
    no body to project.

Negative-spec:
  - ``_TYPE_TO_GLOB`` covers 22 query-records.js record types (widened from
    the original 4 — ``handoff``/``handoff-archived``/``plan``/
    ``cross-repo-memo`` — with 13 more: ``bug``/``debt``/``improvement``/
    ``tracker``/``roadmap``/``health-status``/``decision-guide``/
    ``completion``/``decision``/``review``/``lesson``/``handoff-ledger``/
    ``research-claim``; see query-records.js ``_buildTypeToGlob``,
    bin/query-records.js:211-272, for the derivation each glob mirrors) plus
    ``goal`` (``state/goals/*.yaml``, added when a sibling repo's
    goal-coverage-scan port reported false-empty against this op — the type
    was schema-recognised by ``build_type_to_glob`` but absent from this
    hand-maintained map) plus ``research-synthesis``/``gap-report``/
    ``coverage-audit`` (added 2026-07-22 — cross-repo/inbox/2026-07-22-
    claude-central-em-records-query-excluded-types-doe-needs.md named these
    three as having LIVE DoE runtime consumers reproducing the ``goal``
    false-empty shape) plus ``archived-memo`` (``cross-repo/archive/*.md`` —
    see the ``_TYPE_TO_GLOB`` entry's own comment for the liveness rule) plus
    ``sizing-object`` (``state/sizings/*.yaml``) plus ``cutover``
    (``state/roadmap/**/cutovers/*.md`` — added 2026-07-25; the first WIRED
    type whose glob needs arbitrary-DEPTH ``**`` support rather than a
    single-level ``*`` dir component, which is why ``_walk_glob_segments``
    gained general ``**`` handling as part of wiring this type in — see that
    function's docstring) plus ``priority-intent``/``priority-ledger``
    (``state/priority-intent-inbox/*.yaml``, ``state/priority-ledger/*.yaml``
    — added 2026-07-27, same "schema-recognised with a live producer already
    landed" precedent as ``goal``/``sizing-object``/``cutover``; both roots
    resolve centrally but, from within this repo, land in this repo's own
    ``state/`` tree — see the ``_TYPE_TO_GLOB`` entry's own comment).
    ``_TYPE_TO_GLOB`` is intentionally NOT a blanket
    repoint to ``coordinator_core.frontmatter.schema_validate.build_type_to_glob``
    — that derivation is a clean 51-type superset of this map (verified
    2026-07-22 against DoE ``coordinator/schemas/*.schema.json``) but includes
    several schema-recognised types that are NOT query-servable record
    collections: single fixed-path files (e.g. ``docs-roadmap`` ->
    ``docs/ROADMAP.md``, no wildcard — "querying a record set" is meaningless
    for exactly one file) and JSON single-file/glob types (e.g.
    ``capability-manifest``, ``review-trail``) that would hit this module's
    ``.md``/``.yaml`` frontmatter parser branches and silently collect zero
    records rather than parsing. Schema-recognised != query-servable. The
    remaining delta types (record-shaped collections not yet wired,
    e.g. ``atlas-doc``/``initiative``/``workstream``) are
    out of scope for this fix and are named, with reasons, in
    ``coordinator_core/ops/tests/test_records_query.py``'s
    ``_TYPE_TO_GLOB_DELIBERATE_EXCLUSIONS`` — that test fails loud if a
    NEW record-shaped schema type appears upstream and lands in neither
    ``_TYPE_TO_GLOB`` nor the exclusion set, which is exactly how the
    ``goal`` gap went undetected until a cross-repo memo surfaced it.
    ``decision``/``review``/``lesson`` needed only static glob entries plus
    the EXISTING ``.md``/``.yaml`` parsers below — porting ``schema.js``'s
    full ``loadSchemas()``/``matchSchemaForPath`` (which requires the
    complete DoE schema tree as a runtime dependency) was NOT required for
    THESE three: neither ``docs/decisions/*.md`` nor ``state/reviews/*.md``
    has a co-located sibling glob among any WIRED ``_TYPE_TO_GLOB`` entry, so
    ``_apply_sibling_exclusion`` (below ``_collect_files``) is a no-op for
    them. ``research-synthesis``/``gap-report``/``coverage-audit`` are the
    first WIRED types where the oracle's generalized sibling-exclusion filter
    (query-records.js:1319-1335) is NOT a no-op — ``research-synthesis``'s
    ``docs/research/*.md`` is a superset glob of the other two's
    suffix-narrowed patterns, all three sharing the ``docs/research/``
    directory. ``_apply_sibling_exclusion`` ports the oracle's filter
    self-containedly, derived from ``_TYPE_TO_GLOB`` itself rather than the
    full DoE schema set: verified 2026-07-22 that only 4 DoE schemas declare
    an ``applies_to`` glob under ``docs/research/`` — the 3 above plus
    ``research-claim`` (``docs/research/*.claims.json``, disjoint from the
    other three's ``.md`` globs by extension and therefore never a filename-
    regex collision). Every DoE schema that could collide with these three is
    therefore already a ``_TYPE_TO_GLOB`` member once they are wired — the
    map-local derivation and the oracle's all-schemas filter are provably
    equivalent over the wired set. See ``_apply_sibling_exclusion``'s own
    docstring for the specificity-ordering mechanics (mirrors
    ``schema_validate.py``'s ``_specificity_key``) and
    ``test_records_query.py``'s ``TestSiblingExclusionDerivedFromWiredSet``
    for the gate that keeps this equivalence honest as new DoE schemas land.
    ``handoff-ledger``
    and ``research-claim`` are SYNTHETIC types (N records per source file —
    see ``_collect_handoff_ledger_records``/``_collect_research_claim_records``
    below) and bypass ``_TYPE_TO_GLOB``'s single-base-dir fast path entirely,
    same as query-records.js's dedicated branches (bin/query-records.js:
    1264-1316); their ``_TYPE_TO_GLOB`` entries exist only so ``--type``
    validation and ``_collect_files`` reuse (for ``handoff-ledger``'s live-
    handoff half and ``research-claim``'s glob) stay in this one map.
    ``cross-repo-memo`` carries its own memo-shape guard (``from``/``to``
    frontmatter presence, mirroring query-records.js:1394-1400) rather than a
    schema-derived filter hook.  ``liveness()`` itself already covers all 13
    named types plus the graceful default (it is a pure function of
    ``{status, deployment_state}`` + a type string, independent of which types
    ``_TYPE_TO_GLOB`` enumerates) — ``review``, ``handoff-ledger``, and
    ``research-claim`` fall through to the graceful default in BOTH the
    oracle and this port (verified against bin/query-records.js:882-1030).
  - Applies ``normalizeRoadmapStatus`` (port of query-records.js:1073-1080) for
    ``--type roadmap`` only, at the same pipeline point the JS applies it
    (immediately before the ``liveness`` injection, query-records.js:1437-1441).
  - Does NOT call any git subprocess (zero-spawn COMPUTE_ONLY SLA).
  - Does NOT use ``read_fm_field`` for equality matching — it returns raw
    YAML text without dequoting, so quoted values like
    ``roadmap_id: "claude-klabauter-strangler-2026-07-04"`` mis-match.  Uses
    ``coordinator_core.frontmatter.schema_validate.parse_frontmatter``
    (the byte-parity port of ``schema.js``'s ``parseFrontmatter``/``parseYaml``)
    for a full YAML dict parse of every ``.md`` record, and that same module's
    ``parse_yaml`` for whole-file ``.yaml`` records — NOT ``coordinator_core.dag
    ._read_meta``, which is a separate, independently-hand-rolled parser (dag.py's
    own DFS-node-metadata reader) that has drifted from the ``schema.js`` oracle
    on scalar/list-item edge cases (see ``_load_record``'s docstring negative-spec).
  - Sorts output alphabetically within each directory — mirroring ``query-records.js``
    which uses libuv ``uv_fs_readdir`` / POSIX ``scandir(3)`` (which sorts) on macOS/Linux
    (limit-50 faithful strangle, AC8).  ``Path.glob()`` returns APFS/ext4 hash order in
    Python 3.14+ and is NOT used here.
  - ``--limit`` IS implemented at default 50 — faithful strangle of the
    ``query-records.js`` ``parseArgs`` default.  ``limit <= 0`` yields the
    full unfiltered set (unlimited), faithful to ``query-records.js``'s
    ``opts.limit && opts.limit > 0`` guard.
"""

from __future__ import annotations

import difflib
import functools
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter, parse_yaml
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import (
    HANDOFF_TERMINAL_DEPLOYMENT,
    HANDOFF_TERMINAL_STATUS,
)
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.text.query_record_display import (
    TYPE_DISPLAY as _TYPE_DISPLAY,
    _default_display,
)
from coordinator_core.wire_paths import rel_id

_LOG = logging.getLogger(__name__)


class _RecordsCollectError(Exception):
    """Raised by ``_collect_files`` when a directory scan fails (not merely absent).

    Distinguishes "the directory couldn't be read" from "the directory has no
    matching files" — both would otherwise silently collapse to an empty
    candidate list, and the ``records.query`` handler's caller (ledgers/audits/
    dashboards) cannot tell "zero records exist" from "the scan failed" unless
    this is raised. Caught by the handler, which turns it into an explicit
    ``incomplete``/``error`` signal on the returned payload.
    """


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Positive canonical-plan allowlist — mirrors query-records.js _CANONICAL_PLAN_RE.
# Include ONLY plan files whose basename matches this pattern; everything else
# (sidecar suffixes, timestamped/doubled sidecars, README.md) is excluded.
_CANONICAL_PLAN_RE = re.compile(r'^\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.md$')

# Consumed-marker regex — ported from DoE lib/consumed-marker.js CONSUMED_MARKER_RE.
# Matches `<!-- consumed: YYYY-MM-DD [optional notes] -->` anywhere in a body.
_CONSUMED_MARKER_RE = re.compile(
    r'<!--\s*consumed:\s*(\d{4}-\d{2}-\d{2})(?:\s+(.*?))?\s*-->',
    re.IGNORECASE,
)

# Terminal deployment states — consumed-marker guard (DoE lib/consumed-marker.js).
# SSOT: coordinator_core.lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT.
_TERMINAL_DEPLOYMENT = HANDOFF_TERMINAL_DEPLOYMENT

# Terminal record-lifecycle statuses — single source of truth, mirrors DoE
# lib/consumed-marker.js TERMINAL_STATUS (used by liveness()'s graceful default
# and the handoff/handoff-archived two-axis rule below).
# SSOT: coordinator_core.lifecycle_constants.HANDOFF_TERMINAL_STATUS.
_TERMINAL_STATUS = HANDOFF_TERMINAL_STATUS

# Memo-specific terminal statuses (incl. back-compat aliases) — independent of the
# handoff lifecycle enum. Mirrors query-records.js's _MEMO_TERMINAL_STATUS.
_MEMO_TERMINAL_STATUS = frozenset({
    'actioned',
    'reviewed',
    'action_taken',
    'closed',
    'superseded',
})

# Supported record types → glob patterns (relative to worktree root).
# handoff-archived/roadmap/completion use recursive or wildcard-directory globs;
# the rest are a single non-recursive directory.
# cross-repo-memo: glob alone is NOT sufficient (unlike the other types) — it also
# requires the memo-shape guard applied in the handler loop below (from/to frontmatter
# presence), mirroring query-records.js's _GLOB_OVERRIDES entry (bin/query-records.js:138)
# PLUS its separate queryRecords guard (bin/query-records.js:1394-1400). Source of truth
# for the glob string itself is query-records.js:138, confirmed at port time.
#
# The 8 entries below (bug/debt/improvement/tracker/roadmap/health-status/
# decision-guide/completion) are ported from query-records.js's schema-derived
# _buildTypeToGlob (bin/query-records.js:211-272 — Part 1 derives these straight
# from each type's schemas/*.yaml applies_to glob; Part 2 supplements the
# non-schema'd types). bug/debt/improvement are ``.yaml`` whole-file frontmatter
# (no ``---`` fences — see ``_load_record``'s extension branch below); roadmap
# and completion have a wildcard DIRECTORY component (``*/``) and are collected
# via ``_walk_glob_segments`` rather than the single-base-dir fast path.
#
# ``tracker`` KEPT LIVE despite the tracker-render retirement plan
# (docs/plans/2026-08-14-retire-the-handoff-tracker-and-project-tracker-renders.md):
# ``coordinator_core.ops.emit.sections.trackers._query_tracker_records`` still calls
# ``query_records("tracker", ...)`` in-process and feeds claude-central-em's frozen
# ``contract/cockpit_schema/entities/tracker_summary.py`` — explicitly out-of-scope for
# this plan's C4 (see the plan's Out-of-scope list) until a coordinated cross-repo cut
# is memo'd (plan § C6) and the underlying ``docs/project-tracker.md``/section porter
# are removed (plan § AC8). Removing this entry ahead of that cut makes
# ``query_records`` raise ``SystemExit(1)``, which ``_query_tracker_records``'s
# fail-open ``except`` silently swallows into a permanently-empty TrackerSummary —
# ``test_all_wired_types_present``/``test_glob_values_match_oracle`` in
# ``coordinator_core/ops/tests/test_records_query.py`` already assert this entry's
# presence and value and would fail loud if it were dropped again.
_TYPE_TO_GLOB: dict[str, str] = {
    'handoff':          'state/handoffs/*.md',
    'handoff-archived': 'archive/handoffs/**/*.md',
    'plan':             'docs/plans/*.md',
    'cross-repo-memo':  'cross-repo/inbox/*.md',
    'bug':              'state/bug-backlog/*.yaml',
    'debt':             'state/debt-backlog/*.yaml',
    'improvement':      'state/improvement-queue/*.yaml',
    'tracker':          'docs/project-tracker.md',
    'roadmap':          'state/roadmap/*/OVERVIEW.md',
    'health-status':    'state/health/*.md',
    'decision-guide':   'docs/guides/*-decisions.md',
    'completion':       'archive/completed/*/*.md',
    # decision/review/lesson: static globs derived straight from each type's
    # schemas/*.schema.json `applies_to` (query-records.js's schema-derived Part 1
    # of _buildTypeToGlob) plus, for lesson, the explicit non-schema'd supplement
    # (bin/query-records.js:253) — no sibling-schema collision on either glob (see
    # module Negative-spec above), so these need nothing beyond the entry itself.
    'decision':         'docs/decisions/*.md',
    'review':           'state/reviews/*.md',
    'lesson':           'state/lessons/*.yaml',
    # goal: schema-recognised by build_type_to_glob but missing from this
    # hand-maintained map until a sibling repo's goal-coverage-scan port
    # reported false-empty results (see module Negative-spec above). `.yaml`
    # whole-file frontmatter, same shape as bug/debt/improvement/lesson.
    'goal':             'state/goals/*.yaml',
    # sizing-object: schema-recognised, genuine record-shaped collection
    # (state/sizings/*.yaml, per coordinator/schemas/sizing-object.schema.json's
    # applies_to) with a live producer already landing (coordinator_core.sizing_assemble
    # computes the routing fields; the sizing skill persists them). `.yaml`
    # whole-file frontmatter, same shape as bug/debt/improvement/lesson/goal.
    'sizing-object':    'state/sizings/*.yaml',
    # spike-result: same "schema-recognised with a live producer already landed"
    # precedent as goal/sizing-object/cutover. The glob is the RELOCATED home,
    # not state/handoffs/ — DoE's spike-verdict-records-stable-evidence-home
    # work moved these records out to docs/research/spike-verdicts/ and struck
    # `spike-result` from handoff.schema.json's kind enum (a major bump, taken
    # because state/handoffs/ was verified to carry zero live spike-result
    # records). handoff-archived.schema.json deliberately still admits the kind
    # — the archived corpus permanently retains it as history — so archived
    # spike verdicts remain reachable through 'handoff-archived', and this
    # entry serves only the live evidence home. Matches build_type_to_glob's
    # own derivation, so the two surfaces agree.
    'spike-result':     'docs/research/spike-verdicts/*.md',
    # handoff-ledger/research-claim: SYNTHETIC types (N records per source file) —
    # this glob is only the PRIMARY half (handoff-ledger's live-handoff glob; its
    # archive/handoffs/**/*.md half is added at collection time, mirroring
    # query-records.js's own runtime-appended archive glob, bin/query-records.js
    # :256-258) resp. the sole glob (research-claim). Neither is walked through
    # the single-base-dir fast path in `_collect_files` — see
    # `_collect_handoff_ledger_records`/`_collect_research_claim_records`.
    'handoff-ledger':   'state/handoffs/*.md',
    'research-claim':   'docs/research/*.claims.json',
    # research-synthesis/gap-report/coverage-audit: three live-consumer DoE
    # types named in cross-repo/inbox/2026-07-22-claude-central-em-records-
    # query-excluded-types-doe-needs.md. All three share the docs/research/
    # directory and research-synthesis's glob is a SUPERSET of the other
    # two's suffix-narrowed patterns — `_apply_sibling_exclusion` (below
    # `_collect_files`) is what keeps a `--type research-synthesis` query
    # from swallowing gap-report/coverage-audit files. See that function's
    # docstring for the full equivalence argument against the oracle's
    # all-schemas sibling-exclusion filter.
    'research-synthesis': 'docs/research/*.md',
    'gap-report':         'docs/research/*-gap-report.md',
    'coverage-audit':     'docs/research/*-coverage-audit.md',
    # archived-memo: cross-repo/archive/*.md — the terminal-flipped mirror of
    # cross-repo-memo's cross-repo/inbox/*.md. Unlike cross-repo-memo, no
    # memo-shape guard is applied here: JS parity (bin/query-records.js's
    # existing archived-memo support) treats the archive directory's glob
    # alone as sufficient, since every file archived out of inbox/ already
    # passed the inbox-side shape guard at archive time.
    # Review: code-reviewer (F7) — deliberately weaker guarantee than
    # cross-repo-memo's explicit from/to shape guard: this trusts directory
    # placement alone, unenforced/untested from this module's side, in case
    # a future reader assumes parity between the two.
    'archived-memo':      'cross-repo/archive/*.md',
    # cutover: schema-recognised (coordinator/schemas/cutover.schema.json),
    # genuine record-shaped collection with a live producer landing
    # (coordinator_core.ops.cutover_gate) — same "wire it" precedent as
    # goal/sizing-object. `applies_to` is `state/roadmap/**/cutovers/*.md`:
    # the `**` is load-bearing (arbitrary namespace depth under
    # state/roadmap/), not a `*/cutovers/*.md` one-level approximation — see
    # `_walk_glob_segments`'s general `**` support, added specifically so this
    # type does not silently under-collect the way `goal` did.
    'cutover':            'state/roadmap/**/cutovers/*.md',
    # priority-intent/priority-ledger: schema-recognised
    # (coordinator/schemas/priority-intent.schema.json,
    # coordinator/schemas/priority-ledger.schema.json), genuine record-shaped
    # collections with live producers already landed (priority.set writes
    # ledger entries, priority.drain consumes intent records and applies them
    # to the ledger — coordinator_core/ops/priority_set.py,
    # coordinator_core/ops/priority_drain.py). Both resolve their root via
    # coordinator_state_root(central=True) Rule 4, which — run from within
    # this repo — is this repo's own `state/` tree (see
    # coordinator_core/op_scopes.py's priority.set/priority.drain entries),
    # so both globs are genuinely this repo's own on-disk paths, not a
    # cross-repo reference. `.yaml` whole-file frontmatter, same shape as
    # bug/debt/improvement/lesson/goal/sizing-object — see
    # _YAML_WHOLE_FILE_TYPES below.
    'priority-intent':    'state/priority-intent-inbox/*.yaml',
    'priority-ledger':    'state/priority-ledger/*.yaml',
}

# Query types whose files are `.yaml` whole-file frontmatter (no `---` fences) —
# port of query-records.js's `path.extname(file) === '.yaml'` branch
# (bin/query-records.js:1400-1410).
_YAML_WHOLE_FILE_TYPES: frozenset[str] = frozenset({'bug', 'debt', 'improvement', 'lesson', 'goal', 'sizing-object', 'priority-intent', 'priority-ledger'})

# Query types whose glob has a wildcard DIRECTORY component (not just a wildcard
# filename) — `_collect_files` routes these through the generic segment walker
# instead of the single-base-dir fast path, which cannot express a `*` dir level.
# `handoff-archived` (`archive/handoffs/**/*.md`) and `cutover`
# (`state/roadmap/**/cutovers/*.md`) use the arbitrary-depth `**` form;
# `roadmap`/`completion` use a single-level `*` dir component. Both shapes route
# through the same `_walk_glob_segments` walker — see that function's docstring.
_WILDCARD_DIR_TYPES: frozenset[str] = frozenset({
    'roadmap', 'completion', 'handoff-archived', 'cutover',
})

# Synthetic types: one source FILE yields N records (one `## Session Ledger`
# table block, or one array element of a `.claims.json` file) rather than one
# record per file. Bypass `_collect_files`/`_load_record`'s one-file-one-record
# loop entirely — port of query-records.js's dedicated branches
# (bin/query-records.js:1264-1316). See `_collect_handoff_ledger_records`/
# `_collect_research_claim_records`.
_SYNTHETIC_TYPES: frozenset[str] = frozenset({'handoff-ledger', 'research-claim'})

# OPT-IN archive coverage (default OFF — see the `include_archived` param on
# `_collect_type_records` and the `records.query` handler below). Maps a
# *live* record type to the glob that additionally surfaces its archived
# counterpart when a caller explicitly asks for it. Every existing caller
# (ceremony renderers, the tracker, refresh-queries) passes nothing and is
# therefore unaffected — `_TYPE_TO_GLOB`/`_collect_files` themselves are NOT
# touched by this map; it is consulted only from the opt-in merge step.
#
# `handoff` and `cross-repo-memo` reuse the archive globs already wired as
# first-class types (`handoff-archived`, `archived-memo`) rather than
# duplicating the glob string. `plan`'s archive home
# (`archive/specs/**/*.md`, month-bucketed, same canonical-filename shape as
# `docs/plans/` — verified against on-disk archived specs at fix time) has no
# first-class `_TYPE_TO_GLOB` entry of its own: nothing outside this opt-in
# path needs to query archived plans independently of their live
# counterpart, so it is named only here.
# NEGATIVE-SPEC — do NOT widen these globs to absorb a single repo's stray
# archive location. Fleet-verified 2026-08-11 by example-cockpit-repo-em, who ran
# `handoff.columns` across all six coordinator repos on this disk and compared
# served rows against on-disk handoff files: DoE-claude 550/550, claude-klabauter
# 431/431, example-retrieval-repo 371/371, example-market-data-repo 194/194, example-cockpit-repo
# 169/169 — five of six exact on the globs above. The sixth, example-store-repo, served
# 18 of 25 because seven handoffs sit in a FLAT `state/handoffs/archive/`
# alongside the canonical month-bucketed `archive/handoffs/<YYYY-MM>/` (verified
# on disk here, both locations present; claude-klabauter's own tree is clean). That is
# residue from a one-off manual audit in their repo (example-store-repo `838853f`),
# not a third live fleet convention.
#
# The fix belongs in their tree — move the files — and cockpit has already routed
# it there and declined to touch it themselves. Widening this map would bake a
# location nothing else in the fleet uses into a shared surface, and would make
# every future stray directory look like this map's problem to absorb. If
# example-store-repo asks for the glob change directly, that is the standing answer,
# and cockpit's independent read agrees with it.
_ARCHIVE_GLOB_FOR_TYPE: dict[str, str] = {
    'handoff':         _TYPE_TO_GLOB['handoff-archived'],
    'cross-repo-memo': _TYPE_TO_GLOB['archived-memo'],
    'plan':            'archive/specs/**/*.md',
}

# ---------------------------------------------------------------------------
# Legacy prose-queue invisibility signal (DR-115 — DoE
# docs/decisions/DR-115-queue-shape-is-a-scope-collision-not-a-staleness.md).
#
# Six sibling repos still carry pre-migration line-per-row prose queues at a
# fixed path (e.g. ``state/improvement-queue.md``) alongside — or instead of —
# the per-entry YAML directory this module's ``_TYPE_TO_GLOB`` glob reads.
# Those prose entries are on disk, git-tracked, and were previously completely
# unread by ``records.query`` with no signal that anything was missed: an
# empty/absent legacy file and a repo silently sitting on ~287 unmigrated
# entries produced the exact same payload. This block makes that silence loud,
# by the same mechanism ``_RecordsCollectError`` already established for
# directory-scan failures (flat top-level payload keys, not a new return
# shape) — see the "Tier 2" block in ``_handler`` below for where this plugs
# in.
#
# Deliberately NOT a second reader: this counts entry LINES for an operator-
# facing signal only; it never parses the prose into frontmatter-shaped
# records (DR-115's decision — teaching the reader a second format would
# entrench a shape the fleet is migrating away from).
_LEGACY_PROSE_QUEUE_PATH: dict[str, str] = {
    'improvement': 'state/improvement-queue.md',
    'bug': 'state/bug-backlog.md',
    'debt': 'state/debt-backlog.md',
}

# Per-type migrator CLI named in the operator-facing remediation string —
# each queue kind's legacy-prose evacuation tool, one-shot per DR-115 Part 5.
_LEGACY_PROSE_MIGRATOR: dict[str, str] = {
    'improvement': 'coordinator/bin/migrate-improvement-queue-project.py',
    'bug': 'coordinator/bin/migrate-bug-backlog.py',
    'debt': 'coordinator/bin/migrate-debt-backlog.py',
}

# Entry-line shape for the legacy-prose scale signal.
#
# DELIBERATE DIVERGENCE from ``coordinator_core.write_guards
# .nudge_improvement_queue_write._ENTRY_LINE_RE`` (``^- \d{4}-\d{2}-\d{2} \|``,
# MULTILINE) — this constant used to be a byte-identical re-derivation of that
# write-guard regex, and the byte-identity is exactly what made it dead on two
# of the three DR-115 queue families: measured against the real fleet
# (``/*/state/{bug,debt}-backlog.md``), the dated-pipe shape the write guard
# polices matches 0 of the real bug/debt rows, which instead use a markdown
# TABLE shape (`` | BS-2026-06-14-11 | ... | `` /`` | DSR-2026-04-11-2 | ... |``)
# or a bulleted ID-first shape (`` - **DSR-2026-06-16-1** [...] `` / ``
# - DSR-2026-05-24-1 | 2026-05-24 | ... ``). ``records.query`` reported "0
# unindexed entries" for files that were, empirically, full of them.
#
# The two regexes answer different questions and were never good candidates
# for byte-identity in the first place:
#   - the write guard counts entry-line DELTAS inside a single ``Write``/
#     ``Edit`` payload, to decide whether to nudge a same-format append —
#     it only ever needs to recognise the ONE canonical dated-pipe shape this
#     repo's own writers are taught to emit.
#   - this constant counts entries in a WHOLE REAL FILE, accumulated by
#     multiple sibling repos' own bespoke `/bug-sweep`/`/debt-triage` skills
#     over months, each with its own row convention that predates any shared
#     write-guard schema. Recognising only the write guard's one shape here
#     under-counts by construction.
# Do NOT re-unify these two constants — the read-side's job is coverage
# across shapes that already exist on disk; the write-side's job is exactly
# one shape it can safely nudge on. See
# ``TestEntryLineRegexDivergesFromWriteGuard`` in this module's test file for
# the pinned assertion of this divergence.
#
# Recognises, per line (MULTILINE, first-of-line only — an indented
# sub-bullet or a mid-line embedded pipe never matches):
#   (a) the legacy dated-pipe bullet this constant used to be limited to
#       (``- YYYY-MM-DD | ...`` — still the ONLY shape real improvement-queue.md
#       files use, so this branch alone keeps that family's count unchanged).
#       The whitespace around the date/pipe is intentionally WIDENED relative
#       to the old regex (``\s+``/``\s*`` vs. the old single-space
#       ``^- \d{4}-\d{2}-\d{2} \|``) — a strict superset that can only add
#       matches, never drop a real improvement-queue.md row; it is not
#       byte-identical to the old shape, contrary to the "unchanged" framing
#       this comment used to imply.
#       # Review: code-reviewer (Finding 4) — called out the whitespace
#       # widening explicitly rather than leaving it undocumented.
#   (b) a markdown table row whose first cell is an ID-shaped token, e.g.
#       ``| BS-2026-06-14-11 | ... |`` or a struck-through closed row
#       ``| ~~BS-2026-04-09-1~~ | ... |`` — the real bug/debt-backlog shape.
#       Deliberately EXEMPT from the digit-lookahead branches (c)/(e) carry:
#       adding ``(?=[A-Za-z0-9-]*\d)`` here drops a real corpus row —
#       ``example-stats-repo/state/debt-backlog.md``'s ``| G-OVR | G | Build script
#       divergent OVR formula | ... |``, a genuine hyphenated ID with no
#       digit. No case constraint either, for the same reason (a bare-letter
#       glossary/legend row like ``| high-priority | ... |`` is
#       hypothetically indistinguishable from an ID cell by this branch
#       alone — verified no such row exists in the current 10-file bug/debt
#       corpus, but nothing guards against one appearing; see the negative
#       test added for this shape);
#   (c) a bulleted bold ALL-CAPS identifier containing a digit, e.g.
#       ``- **DSR-2026-06-16-1** ...`` or ``- **F-C-02 false-positive
#       lesson:** ...`` — deliberately excludes both ``**C3-priming:**``
#       (a narrative section label, not an entry) and a mixed-case
#       natural-language bold lead-in (``**Round-2 regressions
#       retrospective:**``), both observed as real false-positive candidates
#       in the fleet corpus. The exclusion mechanism for BOTH is the same:
#       a lower-case segment follows the first hyphen (``priming``,
#       ``regressions retrospective``), so this branch's
#       ``[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+`` (every segment must stay
#       upper-case/digit) cannot match — digit presence is irrelevant to the
#       exclusion; ``C3-priming``'s leading token ``C3`` plainly contains a
#       digit, it is the trailing lower-case ``priming`` that disqualifies it.
#       # Review: code-reviewer (Finding 1) — corrected the "no digit"
#       # misattribution; the actual mechanism is case, not digit presence.
#   (d) a bulleted bold all-lowercase multi-segment slug, e.g. ``-
#       **embed-sidecar-anyio-portal-flaky-crash** — ...`` (real spinoff-entry
#       shape) — the leading-lowercase-letter requirement is what excludes
#       the same "C3-priming"-shaped narrative labels above (those start
#       uppercase);
#   (e) an unbolded bulleted ALL-CAPS identifier containing a digit, e.g.
#       ``- DSR-2026-05-27-1 | ...``.
# Precision over recall (operator-facing "N invisible entries" signal): a
# handful of exotic real IDs are known misses rather than risking a false
# positive — e.g. ``BS-2026-06-14-3+8`` (a ``+``-joined dual-ID cell,
# `example-retrieval-repo-ue-addon/state/bug-backlog.md`) does not match any branch
# above, since none tolerate a bare ``+`` inside the identifier token.
_LEGACY_PROSE_ENTRY_LINE_RE = re.compile(
    r'^(?:'
    r'-\s+\d{4}-\d{2}-\d{2}\s*\|'
    r'|'
    r'\|\s*~{0,2}[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+~{0,2}\s*\|'
    r'|'
    r'-\s+\*\*(?=[A-Z0-9-]*\d)[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+(?:\s|\*\*)'
    r'|'
    r'-\s+\*\*[a-z][a-z0-9]*(?:-[a-z0-9]+)+\*\*'
    r'|'
    r'-\s+(?=[A-Z0-9-]*\d)[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b'
    r')',
    re.MULTILINE,
)


def _legacy_prose_signal(worktree_root: Path, record_type: str) -> Optional[dict]:
    """Return ``{"count", "path"}`` when ``record_type`` has unindexed legacy
    prose-queue entries on disk, else ``None``.

    ``None`` when: this type has no known legacy prose path, the path is
    absent, or the file has zero pipe-row entry lines (whitespace-only /
    tombstone content, matching an existing file that legitimately carries no
    entries — no false alarm). Deliberately independent of whether the
    ``_TYPE_TO_GLOB`` per-entry YAML directory for this type is itself empty
    or already carries records: a repo mid-migration (both shapes populated)
    still has genuinely invisible prose entries the YAML side never sees.
    """
    rel_path = _LEGACY_PROSE_QUEUE_PATH.get(record_type)
    if rel_path is None:
        return None
    fpath = worktree_root / rel_path
    if not fpath.is_file():
        return None
    try:
        text = fpath.read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        _LOG.warning('records.query: cannot read legacy prose queue %s: %s', fpath, exc)
        return None
    count = len(_LEGACY_PROSE_ENTRY_LINE_RE.findall(text))
    if count == 0:
        return None
    return {'count': count, 'path': rel_path}


# Types included in the ``--unattached`` multi-type union lens — port of
# query-records.js's ``UNATTACHED_TYPES`` (bin/query-records.js:148).
#
# Purpose: ``records.query`` with ``unattached=true`` and no ``type`` returns
# every record across these types whose ``initiative`` frontmatter is
# null/absent, spanning queues + roadmap spinoff-stubs + handoffs + plans.
# This constant is the ENGINE-side definition of which record types can carry
# an ``initiative`` FK — claude-klabauter owns ``state/initiatives/``, so "which types
# join to an initiative" is engine schema knowledge, not a DoE-side client
# loop's business. This is why the set lives here rather than being
# recomputed per-caller.
#
# Spec backlink: DoE-claude:pln-initiative-govern-sweep-priori-6cf808 § C3 (AC4)
UNATTACHED_TYPES: tuple[str, ...] = ('bug', 'debt', 'improvement', 'roadmap', 'handoff', 'plan')


# ---------------------------------------------------------------------------
# liveness() — byte-exact port of query-records.js's 13-type-plus-graceful-
# default liveness predicate table (freeze-query-records-grammar.md Surface 2).
# ---------------------------------------------------------------------------

def liveness(fm: dict, record_type: str) -> str:
    """Compute the canonical liveness derived state for a frontmatter record.

    Pure function of ``{status, deployment_state}`` + a type string — matches
    query-records.js:882 field-for-field, including the roadmap
    ``status: blocked`` → LIVE negative spec (BLOCKED is reserved for handoff
    ``deployment_state: awaiting_gate``) and the ``health-status`` type keying
    on ``status`` (lifecycle), not ``health`` (posture).

    Returns one of ``'LIVE'``, ``'BLOCKED'``, ``'DONE'``.
    """
    status = str(fm.get('status')) if fm.get('status') else ''
    deployment_state = str(fm.get('deployment_state')) if fm.get('deployment_state') else ''

    # --- Handoff two-axis combination rule ---
    if record_type in ('handoff', 'handoff-archived'):
        if status in _TERMINAL_STATUS or deployment_state in _TERMINAL_DEPLOYMENT:
            return 'DONE'
        if deployment_state == 'awaiting_gate':
            return 'BLOCKED'
        return 'LIVE'

    # --- Memo single-axis rule ---
    if record_type == 'cross-repo-memo':
        if status in _MEMO_TERMINAL_STATUS:
            return 'DONE'
        return 'LIVE'

    # --- Archived-memo: unconditionally terminal by directory placement ---
    # Review: code-reviewer (F1) — every file under cross-repo/archive/ is
    # already resolved (that's why it's archived, not in inbox/); do not fall
    # through to the graceful default's handoff-vocabulary _TERMINAL_STATUS
    # check, which silently reports LIVE for memo-vocabulary statuses like
    # "actioned" that aren't in that set.
    if record_type == 'archived-memo':
        return 'DONE'

    # --- Plan single-axis rule (deployment_state IGNORED) ---
    # status=='landed' (C8a, plan-line-item-resolution-model, D9) is deliberately NOT
    # named in an explicit clause here — it falls through to the unconditional 'LIVE'
    # default below, which is the correct bucket: landed means code is on the branch
    # but spine rows aren't fully dispositioned, i.e. work is still in flight.
    #
    # Question answered (C8b, 2026-07-27): "what LIVE/BLOCKED/DONE cockpit
    # liveness bucket does this status fall into?" This is the SSOT this
    # module's own emit_artifact_shape_contract.LIVENESS_MAPPING["types"]["plan"]
    # derives from (see that module's comment for the direction/rationale) --
    # but it is NOT the same partition as the other two "terminal"-named plan
    # sets elsewhere in the codebase, and none of the three are expected to
    # agree with each other:
    #   - lifecycle_constants.PLAN_ARCHIVABLE_STATUS (alias: PLAN_TERMINAL_STATUS)
    #     answers "can this plan's file be git-mv'd into archive/?" — 'deferred'
    #     is excluded there (stays revisitable in docs/plans/), while here it
    #     resolves BLOCKED, not folded into a binary terminal/non-terminal set.
    #   - ops.plan_status_transition._FROZEN_STATUSES answers "is this status
    #     frozen against the stamp-implemented flip?" — 'deferred' IS frozen
    #     there (a straight yes/no), unlike the three-way LIVE/BLOCKED/DONE
    #     answer this branch gives it.
    if record_type == 'plan':
        if status == 'deferred':
            return 'BLOCKED'
        if status in ('implemented', 'abandoned', 'superseded'):
            return 'DONE'
        return 'LIVE'

    # --- Decision single-axis rule (deployment_state IGNORED) ---
    if record_type == 'decision':
        if status in ('accepted', 'deprecated', 'superseded'):
            return 'DONE'
        return 'LIVE'

    # --- Queue types: debt / bug / improvement ---
    if record_type in ('debt', 'bug', 'improvement'):
        if status in ('closed', 'wontfix'):
            return 'DONE'
        if status == 'deferred':
            return 'BLOCKED'
        return 'LIVE'

    # --- Lesson liveness (stored-status mapping) ---
    if record_type == 'lesson':
        if status in ('applied', 'closed', 'resolved'):
            return 'DONE'
        if status == 'deferred':
            return 'BLOCKED'
        return 'LIVE'

    # --- Roadmap single-axis rule — NEGATIVE SPEC: status='blocked' maps to
    # LIVE, not BLOCKED (BLOCKED is reserved for handoff awaiting_gate). ---
    if record_type == 'roadmap':
        if status in ('shipped', 'archived'):
            return 'DONE'
        return 'LIVE'

    if record_type == 'tracker':
        if status == 'archived':
            return 'DONE'
        return 'LIVE'

    # health-status keys on fm.status (lifecycle), NOT fm.health (posture).
    if record_type == 'health-status':
        if status == 'archived':
            return 'DONE'
        return 'LIVE'

    if record_type == 'decision-guide':
        if status == 'archived':
            return 'DONE'
        return 'LIVE'

    # --- Graceful default for remaining/unwired types ---
    if status in _TERMINAL_STATUS:
        return 'DONE'
    return 'LIVE'


# ---------------------------------------------------------------------------
# TYPE_DISPLAY — markdown-list render functions.
#
# ``_TYPE_DISPLAY`` is imported from ``coordinator_core.text.query_record_display``
# (see this module's top-of-file imports), not hand-maintained here. This module
# and that one used to independently duplicate the SAME renderer set, and the
# 2026-07-22 records-query-widen diff added 3 new renderers to the text module's
# copy without touching this one — `_handler`'s markdown-list output silently
# fell back to the bare-path default for exactly the 3 types that diff was meant
# to widen (Review: code-reviewer — F1). Importing rather than re-copying is what
# keeps a future type addition from repeating that divergence. The shared table
# now also covers ``cross-repo-memo``/``decision-guide``/``completion`` here for
# the first time — those 3 had the identical latent gap (an oracle
# ``TYPE_DISPLAY`` entry existed but this op's own copy never carried it), caught
# as a side effect of collapsing onto the one registry rather than as a separate
# finding.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def _apply_consumed_marker(fm: dict, body: str) -> None:
    """Normalize deployment fields in-place when a consumed-marker is found.

    Ports ``applyConsumedMarker`` from DoE ``bin/query-records.js:965-980``
    (applied at queryRecords:1328).  Before ``--where`` filtering, if the
    record body carries ``<!-- consumed: YYYY-MM-DD ... -->`` AND frontmatter
    ``deployment_state`` is NOT already terminal (``shipped`` / ``abandoned``),
    normalize:
      - ``deployment_state`` → ``shipped``
      - ``status`` → ``claimed``
      - ``claimed_at`` → date captured from the consumed-marker regex group 1

    This mirrors node's full normalization for the non-terminal branch so that
    ``--format json`` output agrees on ``status`` and the claim-timestamp field,
    not only ``deployment_state``.  The consumed-marker regex already captures
    the date in group 1; no additional parse is needed.

    DR-084 divergence from the JS original (deliberate, plan C5): the marker
    *syntax* keeps its ``consumed`` name, but the frontmatter fields it derives
    follow the field rename — otherwise a marker-bearing record would be
    normalized back onto the retired vocabulary and fall out of a
    ``--where status=claimed`` filter against the migrated corpus. This is also
    load-bearing for ``coordinator_core.ops.ceremony.renderers`` /
    ``ceremony.records_query``'s own consumers, which assert the new
    vocabulary (``coordinator_core/ops/ceremony/tests/test_records_query.py``
    ``TestConsumedMarkerNormalization``). DoE's ``query-records.js`` stays
    old-vocabulary until the fleet cutover, so the node parity oracle is
    expected to disagree here for the duration of the window (oracles are
    test-only and gate-exempt); it re-converges only once DoE's own
    ``query-records.js`` narrows onto the new vocabulary on DoE's own
    schedule (not asserted here) — see ``test_records_query_parity.py``'s
    "Deliberate divergence" docstring section for the test-side
    accommodation.
    """
    if not body:
        return
    m = _CONSUMED_MARKER_RE.search(body)
    if not m:
        return
    if fm.get('deployment_state') not in _TERMINAL_DEPLOYMENT:
        fm['deployment_state'] = 'shipped'
        fm['status'] = 'claimed'
        fm['claimed_at'] = m.group(1)  # YYYY-MM-DD from consumed-marker group 1


# ---------------------------------------------------------------------------
# Roadmap status normalization — byte-exact port of query-records.js's
# normalizeRoadmapStatus (bin/query-records.js:1052-1080).
# ---------------------------------------------------------------------------

# coordinator:roadmap-planning uses a richer lifecycle vocabulary (final-approved,
# draft, in-review, approved) than the liveness contract enum
# [planning, active, blocked, shipped, archived]. Rather than rewrite roadmap
# OVERVIEW.md frontmatter (owned by coordinator:roadmap-planning), normalize at
# the query layer, before liveness() fires — mirrors query-records.js verbatim.
_ROADMAP_STATUS_MAP: dict[str, str] = {
    'final-approved': 'active',
    'approved':       'active',
    'draft':          'planning',
    'in-review':      'planning',
    'planning':       'planning',
    'active':         'active',
    'blocked':        'blocked',
    'shipped':        'shipped',
    'archived':       'archived',
}


def _normalize_roadmap_status(fm: dict, record_type: str) -> None:
    """Normalize ``roadmap``-type frontmatter ``status`` to the contract enum, in place.

    Port of query-records.js's ``normalizeRoadmapStatus`` (bin/query-records.js:1073-1080).
    No-op for every other type. Unmapped values fall back to ``'active'`` (open
    posture, same as the JS). ORDERING: must run AFTER ``_apply_consumed_marker``
    and BEFORE the ``liveness`` assignment — same position query-records.js
    enforces (queryRecords:1436-1441, immediately preceding ``frontmatter.liveness``).
    """
    if record_type != 'roadmap':
        return
    raw = fm.get('status')
    if raw is None:
        return
    fm['status'] = _ROADMAP_STATUS_MAP.get(str(raw), 'active')


# --where grammar — byte-exact port of query-records.js's parseClause/matchesClause/
# compareValues (freeze-query-records-grammar.md Surface 3). OPS scan order matters:
# '<='/'>=' must be checked before bare '<'/'>', and '!=' before nothing-else-conflicts.
_WHERE_IN_RE = re.compile(r'^([\w.]+)\s+in\s*\(([^)]*)\)$', re.IGNORECASE)
_WHERE_BARE_RE = re.compile(r'^([\w.]+)$')
_WHERE_SCAN_OPS = ('!=', '<=', '>=', '<', '>', '=')


def _resolve_field(fm: dict, field: str):
    """Resolve a ``where``/``sort`` field name against frontmatter, dotted-path aware.

    A literal flat key always wins: frontmatter may legitimately carry a key
    containing a dot, and that reading is the one that was queryable before
    dotted paths existed, so it must not change meaning now.  Only when no such
    key exists is ``field`` split on ``.`` and walked as a nested path.

    Any non-mapping encountered mid-walk (or a missing segment) yields ``None`` —
    the same value a missing flat key yields, so callers need no new branch.

    Deliberate divergence from the retired query-records.js original, which had
    no nested-field support at all. Documented rather than smoothed because the
    absence was not inert: ``fm.get('chain_loe.tshirt')`` returned ``None`` and
    coerced to ``''``, so every ``=`` comparison on a dotted field returned a
    SILENT zero rows and every ``!=`` silently matched EVERY row. That made
    /workweek-complete Step 11's LoE high-water check — specified entirely in
    dotted fields (``chain_loe.tshirt``, ``loe.tshirt``) and marked MANDATORY —
    report a clean, plausible "zero entries" on every run in every repo. Found
    by example-retrieval-repo-ue-addon-em against a 27-entry corpus whose true answer was
    2. Note the asymmetry this repairs: ``field in (...)`` on a dotted field was
    already a LOUD failure (SystemExit(1), the field-name regexes above), so the
    grammar rejected the shape it could not evaluate in one place while
    silently mis-evaluating it in another.
    """
    if field in fm:
        return fm[field]
    if '.' not in field:
        return None
    cursor = fm
    for segment in field.split('.'):
        if not isinstance(cursor, dict):
            return None
        if segment not in cursor:
            return None
        cursor = cursor[segment]
    return cursor


def _parse_clause(raw: str) -> dict:
    """Parse a single ``where`` clause — port of query-records.js's ``parseClause``.

    Checks the ``field in (a,b,c)`` shape first (regex), then linear-scans
    ``_WHERE_SCAN_OPS`` IN ORDER (first substring match wins — same semantics as
    JS's ``clause.indexOf(op)`` loop, including its quirk that an operator
    character embedded in a value can shift which op is recognized first; this
    is the frozen behavior, not a bug to fix).  A clause matching neither shape
    falls back to a bare-field presence filter when it is a single ``\\w+``
    token; anything else is a hard parse failure (``SystemExit(1)``).
    """
    raw = raw.strip()
    m = _WHERE_IN_RE.match(raw)
    if m:
        values = [v.strip() for v in m.group(2).split(',')]
        return {'field': m.group(1), 'op': 'in', 'values': values}

    for op in _WHERE_SCAN_OPS:
        idx = raw.find(op)
        if idx != -1:
            field = raw[:idx].strip()
            value = raw[idx + len(op):].strip()
            return {'field': field, 'op': op, 'value': value}

    if _WHERE_BARE_RE.match(raw):
        return {'field': raw, 'op': 'exists'}

    sys.stderr.write(f'records.query: cannot parse where clause: "{raw}"\n')
    sys.exit(1)


def _parse_where(where_str: str) -> list[dict]:
    """Parse a ``where`` expression (`` AND ``-joined clauses) into clause dicts.

    Full-grammar port of query-records.js's ``parseWhereExpr`` — supports
    ``=``, ``!=``, ``<``, ``>``, ``<=``, ``>=``, ``field in (a,b,c)``, and the
    bare-field presence filter.  Splits on `` AND `` / `` and `` (case-
    insensitive).  A clause with no recognisable shape raises ``SystemExit(1)``
    with a stderr message (trips the caller's PATH-fallback).

    A field name may be a dotted path into nested frontmatter
    (``chain_loe.tshirt``); resolution rules and the silent-zero bug this
    repaired are in ``_resolve_field``.

    Returns a list of clause dicts: ``{"field", "op", "value"}`` for scalar
    comparison ops, ``{"field", "op": "in", "values": [...]}`` for ``in``, or
    ``{"field", "op": "exists"}`` for the bare-field form.
    """
    raw_clauses = re.split(r'\s+and\s+', where_str.strip(), flags=re.IGNORECASE)
    return [_parse_clause(raw) for raw in raw_clauses if raw.strip()]


def _js_number(s: str) -> float:
    """Approximate JS ``Number(s)`` coercion for ``compareValues``'s numeric probe.

    JS ``Number('')`` / ``Number('  ')`` is ``0`` (not ``NaN``) — Python's
    ``float('')`` raises instead, so blank/whitespace-only strings are special-
    cased to ``0.0`` before falling back to ``float()``; anything else that
    fails to parse yields ``NaN`` (matching JS's ``NaN`` fallback for a non-
    numeric string).
    """
    stripped = s.strip()
    if stripped == '':
        return 0.0
    try:
        return float(stripped)
    except ValueError:
        return float('nan')


def _compare_values(a: str, b: str) -> float:
    """Port of query-records.js's ``compareValues`` — numeric-first, string fallback.

    Tries ``Number(a) - Number(b)`` first when BOTH sides parse as numbers
    (this is the trap for a naive port: a string-only comparator would put
    ``priority: 10`` before ``priority: 3``); falls back to Python string
    comparison (works for ISO date strings, which sort correctly
    lexicographically) when either side is non-numeric.
    """
    na = _js_number(a)
    nb = _js_number(b)
    if na == na and nb == nb:  # not-NaN check (NaN != NaN)
        return na - nb
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def _clause_matches(fm: dict, clause: dict) -> bool:
    """Port of query-records.js's ``matchesClause`` for one parsed clause."""
    field = clause['field']
    op = clause['op']
    raw = _resolve_field(fm, field)

    # Bare-presence filter: populated non-empty array, non-empty string, or any
    # other defined/non-null value. Checked before String()-coercion so an
    # empty array is correctly treated as absent, matching the scalar path.
    if op == 'exists':
        if raw is None:
            return False
        if isinstance(raw, list):
            return any(len(str(el)) > 0 for el in raw)
        return len(str(raw)) > 0

    # Array-aware `in` membership: an array-valued frontmatter field (e.g.
    # origin_goal_id) matches if ANY element is in clause['values'].
    if op == 'in' and isinstance(raw, list):
        return any(str(el) in clause['values'] for el in raw)

    # Review: code-reviewer — F2: lowercase Python bools to match JS String(true)→'true'
    if raw is None:
        fm_val = ''
    elif raw is True:
        fm_val = 'true'
    elif raw is False:
        fm_val = 'false'
    else:
        fm_val = str(raw)

    if op == '=':
        return fm_val == clause['value']
    if op == '!=':
        return fm_val != clause['value']
    if op == 'in':
        return fm_val in clause['values']
    if op == '<':
        return _compare_values(fm_val, clause['value']) < 0
    if op == '>':
        return _compare_values(fm_val, clause['value']) > 0
    if op == '<=':
        return _compare_values(fm_val, clause['value']) <= 0
    if op == '>=':
        return _compare_values(fm_val, clause['value']) >= 0
    return False


def _matches_where(fm: dict, clauses: list[dict]) -> bool:
    """Return True iff the frontmatter dict matches ALL parsed ``where`` clauses."""
    return all(_clause_matches(fm, clause) for clause in clauses)


# ---------------------------------------------------------------------------
# --since / --older-than (freeze-query-records-grammar.md Surface 4)
# ---------------------------------------------------------------------------

_SINCE_ISO_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_SINCE_REL_RE = re.compile(r'^(\d+)(d|w|m)$')


def _parse_relative_date(value: Optional[str], flag_name: str) -> Optional[str]:
    """Shared parser for ``since``/``older_than`` — ``Nd``/``Nw``/``Nm`` or ISO literal.

    Port of query-records.js's ``parseSince``/``parseOlderThan`` (identical regex
    shape in both).  ``w`` = 7 days, ``m`` = 30 days — calendar-naive, not actual
    months.  Invalid input is a hard failure (``SystemExit(1)``), matching JS's
    ``process.exit(1)`` on regex mismatch.

    Deliberate divergence from the JS original: the cutoff clock is
    ``datetime.now(timezone.utc)``, not machine-local time. The JS original (and
    this port, before this fix) used local wall-clock time, but frontmatter dates
    are UTC-authored — on a non-UTC machine, local-time ``now()`` skews the cutoff
    boundary by up to a day, silently including/excluding a day's worth of records
    depending on the operator's timezone and wall-clock hour. No parity test pins
    this function's output (searched ``coordinator_core/ops/tests/test_records_query.py``
    and ``coordinator_core/ops/ceremony/tests/test_records_query.py`` — neither
    references ``_parse_relative_date``/``_parse_since``/``_parse_older_than``
    directly, and no existing test exercises the relative-date branch under a
    controlled clock), so this is a real-bug fix, not a recorded parity break.
    """
    if not value:
        return None
    if _SINCE_ISO_RE.match(value):
        return value
    m = _SINCE_REL_RE.match(value)
    if not m:
        sys.stderr.write(f'Invalid --{flag_name} value: {value}\n')
        sys.exit(1)
    n = int(m.group(1))
    unit = m.group(2)
    days = n if unit == 'd' else (n * 7 if unit == 'w' else n * 30)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime('%Y-%m-%d')


def _parse_since(value: Optional[str]) -> Optional[str]:
    return _parse_relative_date(value, 'since')


def _parse_older_than(value: Optional[str]) -> Optional[str]:
    return _parse_relative_date(value, 'older-than')


# ---------------------------------------------------------------------------
# --sort (freeze-query-records-grammar.md Surface 5)
# ---------------------------------------------------------------------------

def _sort_records(records: list[dict], sort_str: str) -> list[dict]:
    """Sort ``records`` (each ``{"path", "frontmatter"}``) by ``sort_str``.

    Bare field name → ascending; ``-``-prefixed → descending. Applied AFTER
    since/older-than/where filtering, BEFORE --limit slicing (same ordering as
    query-records.js:1462-1476). Uses the same numeric-first ``_compare_values``
    as ``--where`` on ``String(frontmatter.get(field, ''))``.
    """
    desc = sort_str.startswith('-')
    field = sort_str[1:] if desc else sort_str

    def _key(r: dict) -> str:
        v = _resolve_field(r['frontmatter'], field)
        return '' if v is None else str(v)

    def _cmp(a: dict, b: dict) -> float:
        c = _compare_values(_key(a), _key(b))
        return -c if desc else c

    return sorted(records, key=functools.cmp_to_key(_cmp))


def _segment_to_regex(segment: str) -> re.Pattern:
    """Compile one glob path-segment to an anchored regex — port of query-records.js's
    ``filePatternToRegex`` (bin/query-records.js:822-831): ``*`` → ``[^/]*``,
    ``?`` → ``[^/]``, everything else escaped literally.
    """
    out: list[str] = []
    for c in segment:
        if c == '*':
            out.append('[^/]*')
        elif c == '?':
            out.append('[^/]')
        elif c in '.+^${}()|[]\\':
            out.append('\\' + c)
        else:
            out.append(c)
    return re.compile('^' + ''.join(out) + '$')


def _walk_glob_segments(base: Path, segments: list[str]) -> list[Path]:
    """Recursively expand glob path-segments against the filesystem, readdir-sorted
    at every level — port of query-records.js's ``walkSegments`` (bin/query-records.js
    :762-820). Used for globs with a wildcard DIRECTORY component (``roadmap``'s
    ``state/roadmap/*/OVERVIEW.md``, ``completion``'s ``archive/completed/*/*.md``,
    ``handoff-archived``'s ``archive/handoffs/**/*.md``, ``cutover``'s
    ``state/roadmap/**/cutovers/*.md``), which the single-base-dir fast path in
    ``_collect_files`` cannot express.

    A literal ``**`` segment (checked BEFORE the generic wildcard branch, since
    it also contains ``*`` characters) matches ZERO-OR-MORE directory levels —
    genuine arbitrary-depth recursion, not the one-level approximation a plain
    ``*``-per-character regex would produce (``_segment_to_regex('**')`` collapses
    to the same ``[^/]*[^/]*`` == "one directory name" regex as a single ``*``,
    which is why this needs its own branch rather than falling through). The
    zero-level case matches ``tail`` directly against ``base`` (so a record living
    immediately under the ``**`` anchor is found, not just deeper descendants);
    the one-or-more-level case re-enters this function on EACH subdirectory,
    keeping the same ``['**', *tail]`` segment list so it can recurse arbitrarily
    deep. Zero-level is evaluated before descending into subdirectories — same
    top-down ordering as ``os.walk`` (current directory's matches before its
    children's), which is what ``handoff-archived``'s formerly-bespoke ``os.walk``
    branch in ``_collect_files`` already produced; folding that branch onto this
    general path is therefore an ordering-preserving no-op for existing callers.

    Each directory level is listed via ``sorted(os.listdir(...))`` (scandir-alphasort
    parity, same rationale as ``_collect_files``'s non-wildcard-dir path) rather than
    raised on scan failure — a wildcard-dir type has no established Tier-2
    incomplete-signal contract, so an unreadable level is skipped (fail-open),
    matching query-records.js's own try/catch-and-continue posture in ``walkSegments``.
    """
    if not segments:
        return []
    if not base.is_dir():
        return []

    head, *tail = segments
    is_last = not tail

    if head == '**':
        # Zero-or-more directory levels — see docstring above.
        results: list[Path] = list(_walk_glob_segments(base, tail))
        try:
            entries = sorted(os.listdir(str(base)))
        except OSError:
            return results
        for entry in entries:
            nxt = base / entry
            if nxt.is_dir():
                results.extend(_walk_glob_segments(nxt, segments))
        return results

    if not any(ch in head for ch in ('*', '?')):
        # Literal segment — fast path.
        nxt = base / head
        if not nxt.exists():
            return []
        if is_last:
            return [nxt] if nxt.is_file() else []
        return _walk_glob_segments(nxt, tail) if nxt.is_dir() else []

    # Wildcard segment — enumerate directory entries in scandir-alphasort order.
    try:
        entries = sorted(os.listdir(str(base)))
    except OSError:
        return []

    head_re = _segment_to_regex(head)
    results: list[Path] = []
    for entry in entries:
        if not head_re.match(entry):
            continue
        nxt = base / entry
        if is_last:
            if nxt.is_file():
                results.append(nxt)
        elif nxt.is_dir():
            results.extend(_walk_glob_segments(nxt, tail))
    return results


def _specificity_key(glob: str) -> tuple[int, int]:
    """Specificity sort key for one ``_TYPE_TO_GLOB`` glob string.

    Mirrors ``coordinator_core.frontmatter.schema_validate``'s
    ``_specificity_key`` (fewer wildcards is more specific; among equal
    wildcard counts, a longer literal string is more specific — e.g.
    ``docs/research/*-gap-report.md`` beats ``docs/research/*.md``).
    Deliberately re-derived rather than imported: the two operate over
    different corpora (this module's flat ``_TYPE_TO_GLOB`` values vs. a
    loaded schema tree's ``applies_to`` strings) and must not share a
    runtime dependency on the full DoE schema set — see
    ``_apply_sibling_exclusion``'s docstring for why that independence is
    load-bearing.
    """
    return (glob.count('*'), -len(glob))


# Types whose `_TYPE_TO_GLOB` entry is not a plain "literal directory +
# wildcard filename" pattern — wildcard-DIRECTORY globs (single-level `*` or
# arbitrary-depth `**`, incl. the recursive `handoff-archived` glob, now a
# `_WILDCARD_DIR_TYPES` member itself) and the two SYNTHETIC types (whose
# glob exists only for `_collect_files` reuse, not as "the" glob for that
# type — see module Negative-spec). None of these compare meaningfully
# against a sibling glob via the plain filename-regex check in
# `_apply_sibling_exclusion`, so that filter is a no-op for them.
_SIBLING_EXCLUSION_INELIGIBLE: frozenset[str] = _WILDCARD_DIR_TYPES | _SYNTHETIC_TYPES


def _apply_sibling_exclusion(
    files: list[Path], record_type: str, base_dir: Path,
) -> list[Path]:
    """Drop files whose best-matching sibling glob belongs to a DIFFERENT
    wired type — self-contained port of query-records.js's generalized
    sibling-exclusion filter (bin/query-records.js:1319-1335).

    The oracle's filter walks ALL loaded DoE schemas' ``applies_to`` globs
    (specificity-sorted via ``schema.js``'s ``_byGlob``) and drops a
    collected file whenever some OTHER schema's glob matches it more
    specifically than the queried type's own. Porting that verbatim would
    make this op depend on the full DoE schema tree at runtime — claude-klabauter's
    vendored ``coordinator_core/frontmatter/schemas/`` holds only 12 schemas
    and does not include ``research-synthesis``/``gap-report``/
    ``coverage-audit``, and a sibling-checkout dependency is not acceptable
    for a per-repo op.

    Instead, this derives the SAME comparison from ``_TYPE_TO_GLOB`` itself:
    only entries sharing ``record_type``'s own directory are ever candidate
    siblings (a different directory can never match the same file), and
    ``_SIBLING_EXCLUSION_INELIGIBLE`` types are skipped as non-comparable
    shapes. This is narrower than the oracle's all-schemas filter but
    provably equivalent over the set of WIRED types today: verified
    2026-07-22 that only 4 DoE schemas declare an ``applies_to`` glob under
    ``docs/research/`` — ``research-synthesis``, ``gap-report``,
    ``coverage-audit`` (all three now wired) plus ``research-claim``
    (``docs/research/*.claims.json``, disjoint by extension and never a
    filename-regex collision). No unwired DoE schema is a more-specific
    sibling of any wired glob, so the map-local derivation and the oracle's
    all-schemas filter agree everywhere this op can be asked to look.
    ``test_records_query.py``'s ``TestSiblingExclusionDerivedFromWiredSet``
    is the derive-and-gate check that fails loud the moment a NEW unwired
    DoE schema glob would break that equivalence.

    Ties (identical specificity — two wired ELIGIBLE siblings under the same
    directory whose globs produce an equal ``_specificity_key``) are resolved
    in ``record_type``'s favor: a file is dropped only when some OTHER type's
    glob is STRICTLY more specific (``<``, never ``<=``). ``handoff-ledger``
    sharing ``handoff``'s exact glob string is NOT such a case — it never
    reaches this branch at all, since ``handoff-ledger`` is a member of
    ``_SYNTHETIC_TYPES`` ⊂ ``_SIBLING_EXCLUSION_INELIGIBLE`` and is filtered
    out of the ``siblings`` candidate list up front. No tie currently exists
    among wired ELIGIBLE siblings either — verified 2026-07-22 that
    ``research-synthesis``/``gap-report``/``coverage-audit``'s globs
    (``docs/research/*.md``, ``docs/research/*-gap-report.md``,
    ``docs/research/*-coverage-audit.md``) all differ in length, hence in
    ``_specificity_key`` — so this branch is UNREACHABLE with today's real
    data. It exists anyway as a DELIBERATE divergence from the oracle:
    ``matchSchemaForPath`` breaks ties by schema-LOAD ORDER (first match in a
    specificity-sorted array, ``schema.js``'s ``_byGlob``), with no
    "favor the queried type" rule at all — this port adds one so a future
    wired sibling landing with an identical-specificity glob can't
    spuriously drop the queried type's own collection. See
    ``TestSiblingExclusionTieBreak`` (test_records_query.py) for direct
    coverage of this branch via two equal-specificity fixture types,
    independent of "no current data defeats it."
    """
    if record_type in _SIBLING_EXCLUSION_INELIGIBLE:
        return files

    own_dir = Path(_TYPE_TO_GLOB[record_type]).parent
    own_key = _specificity_key(_TYPE_TO_GLOB[record_type])
    siblings = [
        (_segment_to_regex(Path(glob).name), _specificity_key(glob))
        for other_type, glob in _TYPE_TO_GLOB.items()
        if other_type != record_type
        and other_type not in _SIBLING_EXCLUSION_INELIGIBLE
        and Path(glob).parent == own_dir
    ]
    if not siblings:
        return files

    kept: list[Path] = []
    for fpath in files:
        name = fpath.name
        best_key = own_key
        for sib_re, sib_key in siblings:
            if sib_key < best_key and sib_re.match(name):
                best_key = sib_key
        if best_key == own_key:
            kept.append(fpath)
    return kept


def _collect_files(worktree_root: Path, record_type: str) -> list[Path]:
    """Collect candidate record files in node-parity order.

    Node's ``fs.readdirSync()`` delegates to libuv ``uv_fs_readdir`` which on
    macOS/Linux calls POSIX ``scandir(3)``.  ``scandir`` returns entries sorted
    alphabetically (via its built-in ``alphasort`` comparator), so Node output is
    effectively alphabetically sorted on these platforms.

    Python's ``Path.glob()`` returns filesystem iteration order (unsorted OS readdir),
    which diverges from ``scandir(3)`` alphabetical order on limit-50 queries over
    51+ matching files.  ``os.listdir()`` + ``sorted()`` is used instead to match
    ``scandir``'s alphabetical order — this applies to all Python versions, not just
    a specific release.  (Review: F8 — removed incorrect Python 3.14+ specificity.)

    Fix: collect via ``os.listdir()`` (or ``os.walk()``) then sort alphabetically
    within each directory, mirroring the scandir behaviour faithfully.  This is the
    "readdir parity" fix mandated by AC8 and the parity test — sort IS the faithful
    strangle because node sorts via scandir.
    """
    glob_pat = _TYPE_TO_GLOB[record_type]

    if record_type in _WILDCARD_DIR_TYPES:
        # Wildcard DIRECTORY component — e.g. state/roadmap/*/OVERVIEW.md (single
        # level) or archive/handoffs/**/*.md, state/roadmap/**/cutovers/*.md
        # (arbitrary depth). The single-base-dir derivation below cannot express
        # either shape. `handoff-archived` was formerly a bespoke `os.walk`
        # branch here; folded onto the general walker once it gained `**`
        # support (same top-down alpha-sort ordering — see
        # `_walk_glob_segments`'s docstring).
        return _walk_glob_segments(worktree_root, glob_pat.split('/'))

    # Non-recursive: derive base dir from the glob pattern's directory portion,
    # and match the filename portion (which may be a bare literal like
    # 'OVERVIEW.md', a suffix pattern like '*-decisions.md', or an
    # extension pattern like '*.yaml') via the same segment-regex the wildcard-
    # dir path uses, rather than a hardcoded `.md` endswith check.
    # e.g. 'state/handoffs/*.md' → worktree_root / 'state' / 'handoffs'
    base_dir = worktree_root / Path(glob_pat).parent
    filename_re = _segment_to_regex(Path(glob_pat).name)
    if not base_dir.is_dir():
        return []
    try:
        names = sorted(os.listdir(str(base_dir)))  # scandir sorts alphabetically
    except OSError as exc:
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        # A directory that exists but can't be listed (permission error,
        # transient I/O failure, etc.) is a SCAN FAILURE, not "zero records of
        # this type exist" -- those two outcomes were previously
        # indistinguishable (both silently returned []). Log a diagnostic
        # naming exactly what was skipped, and raise so the handler can surface
        # an explicit incomplete/error signal to callers (ledgers/audits/
        # dashboards) that need to tell failure apart from legitimate-empty.
        _LOG.warning(
            'records.query: cannot list directory %s: %s', base_dir, exc,
        )
        raise _RecordsCollectError(f'cannot list directory {base_dir}: {exc}') from exc
        # --- end Tier 2 ---
    candidates = [
        base_dir / name for name in names
        if filename_re.match(name) and (base_dir / name).is_file()
    ]
    return _apply_sibling_exclusion(candidates, record_type, base_dir)


# ---------------------------------------------------------------------------
# Synthetic types — handoff-ledger / research-claim (N records per source file).
# ---------------------------------------------------------------------------

_LEDGER_HEADING_RE = re.compile(r'^##\s+Session Ledger\s*$')
_LEDGER_ANY_HEADING_RE = re.compile(r'^#+\s')
_LEDGER_SEPARATOR_RE = re.compile(r'^\|[-\s|]+\|$')
_LEDGER_ROW_RE = re.compile(r'^\|([^|]+)\|([^|]+)\|')


def _parse_handoff_ledger_blocks(content: str) -> list[dict]:
    """Parse every ``## Session Ledger`` table block out of a handoff body.

    Byte-exact port of query-records.js's ``parseHandoffLedger`` state machine
    (bin/query-records.js:1154-1236), minus the path-fragment assembly (left to
    the caller, which owns the source file's rel_path). Field names are
    lowercased and space/hyphen-normalized to ``snake_case``; a comma-separated
    ``em_tokens`` value has its commas stripped so numeric ``--where``/``--sort``
    comparison works (``_compare_values``'s numeric probe would otherwise treat
    ``"482,000"`` as non-numeric, same rationale as the oracle's own comment).

    A blank line ends the current block only AFTER table rows have been seen
    (the blank line between the heading and the table header is markdown
    formatting, not a terminator); a new heading always ends the block.
    """
    records: list[dict] = []
    current_fields: Optional[dict] = None
    in_table = False

    def _flush() -> None:
        nonlocal current_fields, in_table
        if current_fields:
            if isinstance(current_fields.get('em_tokens'), str):
                current_fields['em_tokens'] = current_fields['em_tokens'].replace(',', '')
            records.append(current_fields)
        current_fields = None
        in_table = False

    for line in content.split('\n'):
        trimmed = line.strip()

        if _LEDGER_HEADING_RE.match(trimmed):
            _flush()
            current_fields = {}
            in_table = False
            continue

        if current_fields is None:
            continue  # not inside a ledger block yet

        if _LEDGER_ANY_HEADING_RE.match(trimmed):
            _flush()
            continue

        if trimmed == '' and in_table:
            _flush()
            continue

        if trimmed == '' and not in_table:
            continue  # blank line before table rows — stay in block

        if _LEDGER_SEPARATOR_RE.match(trimmed):
            continue  # |---|---| separator row

        row = _LEDGER_ROW_RE.match(trimmed)
        if row:
            in_table = True
            field = re.sub(r'[\s-]+', '_', row.group(1).strip().lower())
            value = row.group(2).strip()
            if field == 'field' and value.lower() == 'value':
                continue  # header row
            current_fields[field] = value
        elif in_table:
            _flush()  # non-table content after rows started — end of block

    _flush()  # flush final block if the file ends mid-ledger
    return records


def _collect_handoff_ledger_records(worktree_root: Path) -> list[dict]:
    """Collect synthetic ``handoff-ledger`` records — port of query-records.js's
    dedicated branch (bin/query-records.js:1274-1288).

    Crawls BOTH live handoffs (reusing ``_collect_files``'s own ``handoff``
    glob/sort) and archived handoffs (reusing its ``handoff-archived``
    recursive glob/sort) — live files first, then archive files, same order as
    the oracle's ``[...liveFiles, ...archiveFiles]`` spread. An absent
    ``state/handoffs/`` or ``archive/handoffs/`` yields an empty list from the
    reused collector, never an error.
    """
    live_files = _collect_files(worktree_root, 'handoff')
    archive_files = _collect_files(worktree_root, 'handoff-archived')

    records: list[dict] = []
    for fpath in [*live_files, *archive_files]:
        rel_path = rel_id(fpath, worktree_root)
        try:
            content = fpath.read_text(encoding='utf-8')
        except OSError as exc:
            _LOG.warning('records.query: cannot read %s: %s', fpath, exc)
            continue
        for idx, fields in enumerate(_parse_handoff_ledger_blocks(content)):
            fields['liveness'] = liveness(fields, 'handoff-ledger')
            records.append({'path': f'{rel_path}#ledger-{idx}', 'frontmatter': fields})
    return records


def _collect_research_claim_records(worktree_root: Path) -> list[dict]:
    """Collect synthetic ``research-claim`` records — port of query-records.js's
    dedicated branch (bin/query-records.js:1289-1316).

    One record per JSON-array element of every ``docs/research/*.claims.json``
    file. A non-array top-level value is silently skipped (mirrors the
    oracle's ``if (!Array.isArray(claims)) continue``); unparseable JSON is
    warned to stderr and skipped (mirrors the oracle's own
    ``process.stderr.write`` + continue on a ``JSON.parse`` failure — a
    producer bug should be visible, not silently swallowed). An absent
    ``docs/research/`` yields an empty list, never an error.
    """
    files = _collect_files(worktree_root, 'research-claim')

    records: list[dict] = []
    for fpath in files:
        rel_path = rel_id(fpath, worktree_root)
        try:
            raw = fpath.read_text(encoding='utf-8')
        except OSError as exc:
            _LOG.warning('records.query: cannot read %s: %s', fpath, exc)
            continue
        try:
            claims = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.stderr.write(
                f'records.query: failed to parse claims JSON: {rel_path}: {exc}\n'
            )
            continue
        if not isinstance(claims, list):
            continue
        for idx, claim in enumerate(claims):
            if not isinstance(claim, dict):
                continue
            fm = dict(claim)
            fm['liveness'] = liveness(fm, 'research-claim')
            records.append({'path': f'{rel_path}#claim-{idx}', 'frontmatter': fm})
    return records


def _apply_plan_filename_filter(files: list[Path]) -> list[Path]:
    """Positive canonical-plan allowlist for ``--type plan`` candidates.

    Port of query-records.js's ``--type plan`` sidecar-exclusion block
    (bin/query-records.js:1360-1389): keep only ``YYYY-MM-DD-<slug>.md``
    filenames; silently drop known sidecar shapes (``_is_known_sidecar``);
    warn-and-drop anything else (anomaly detector — never silently include
    on uncertainty). Shared by the JSON-RPC handler and
    ``ceremony.records_query.query_records`` so the allowlist logic lives in
    exactly one place.
    """
    kept: list[Path] = []
    for fpath in files:
        basename = fpath.name
        if _CANONICAL_PLAN_RE.match(basename):
            kept.append(fpath)
            continue
        if not _is_known_sidecar(basename):
            sys.stderr.write(
                f'records.query: anomalous plan filename excluded: {basename}\n'
            )
    return kept


def _load_record(
    fpath: Path, worktree_root: Path, record_type: str,
    *, archived: bool = False, include_body: bool = False,
) -> Optional[dict]:
    """Read+parse one candidate file into a ``{"path", "frontmatter"}`` record.

    Returns ``None`` when the file should be silently skipped (no frontmatter,
    a genuine parse failure already warned to stderr, or a shape guard like
    cross-repo-memo's from/to requirement excludes it).

    Shared by the JSON-RPC handler and ``ceremony.records_query.query_records``
    so the ``.yaml``-whole-file-vs-``.md``-delimited parse branch, the
    cross-repo-memo shape guard, consumed-marker normalization, roadmap-status
    normalization, and ``liveness`` injection live in exactly ONE place —
    avoiding the silently-diverging-copy risk the ceremony module's own
    docstring warns against for the ``--where``/consumed-marker helpers it
    already reuses from here.

    ``archived`` (default ``False``, keyword-only): the COLLECTION ORIGIN, set
    by the caller — ``_collect_type_records`` passes ``True`` only for files it
    walked off ``_ARCHIVE_GLOB_FOR_TYPE``'s glob, never inferred here from
    ``fpath`` or the emitted ``path`` string. Injected onto
    ``fm['archived']`` the same way ``fm['liveness']`` is injected below.
    Negative-spec: do NOT replace this parameter with a substring/prefix test
    against the emitted path — that form is already wrong for live records
    whose slug merely contains the word "archive" (e.g. a live cross-repo-memo
    titled about the archived-ness gap itself).

    ``include_body`` (default ``False``, keyword-only, OPT-IN): when true and
    this is a ``.md``-branch record, projects the already-parsed post-
    frontmatter body onto ``fm['body']``. The ``.yaml`` whole-file branch has
    no body text to project — ``fm['body']`` is set to ``None`` there so a
    consumer can tell "no body exists" apart from "body was empty", rather
    than an empty string collapsing both cases.

    The projection is an unconditional assignment, so a record whose real
    on-disk frontmatter carries its own ``body:`` key has that value replaced
    by the post-frontmatter text — the same silent-overwrite the ``liveness``
    injection below already accepts, and deliberately the same shape rather
    than a second, divergent convention. No record type in either corpus
    declares ``body:`` in frontmatter (verified across all 1756 collected
    ``cross-repo-memo`` records, live plus archived, 2026-08-19), so the
    collision is contract-prevented, not merely unobserved: a schema adding a
    frontmatter ``body:`` field would have to reconcile with this projection
    first. Reviewer WARN, 2026-08-19 slice C1C2 — recorded, not fixed, because
    diverging from the ``liveness`` precedent for a collision no corpus
    produces buys nothing.

    Negative-spec (parser choice): both branches below parse through
    ``coordinator_core.frontmatter.schema_validate`` — ``parse_yaml`` for the
    whole-file ``.yaml`` branch, ``parse_frontmatter`` for the ``.md`` branch —
    the ONE byte-parity port of ``schema.js``'s ``parseYaml``/``parseFrontmatter``
    in this tree. Neither branch uses ``yaml.safe_load`` (strict-mode PyYAML
    rejects real on-disk records the lenient JS parser accepts — e.g. an
    unquoted title starting with a backtick, or a bare value containing ``: ``
    mid-line) nor ``coordinator_core.dag._read_meta`` (a separate,
    independently hand-rolled mapping-block parser used by dag.py's own DFS
    node-metadata reader, which had drifted from the ``schema.js`` oracle on
    two edge cases: no ``isfinite`` guard on scientific-notation scalars like
    ``9e015366`` — silently coerced to ``inf`` instead of staying a string —
    and no list-item nested-mapping handling for ``- key: value`` entries).
    """
    body = ''
    if record_type in _YAML_WHOLE_FILE_TYPES:
        # .yaml whole-file frontmatter — port of query-records.js's
        # `path.extname(file) === '.yaml'` branch (bin/query-records.js:1400-1410).
        # No `---` fences; the entire file IS the frontmatter document.
        try:
            raw = fpath.read_bytes()
        except OSError as exc:
            _LOG.warning('records.query: cannot read %s: %s', fpath, exc)
            return None
        try:
            fm = parse_yaml(raw.decode('utf-8', errors='replace'))
        except Exception as exc:  # noqa: BLE001 — mirrors query-records.js's catch-all _parseYaml try/except
            sys.stderr.write(
                f'records.query: YAML parse failed for '
                f'{rel_id(fpath, worktree_root)}: {exc}\n'
            )
            return None
        if not isinstance(fm, dict) or not fm:
            return None  # empty/non-mapping file — silent skip (not corruption)
        if include_body:
            fm['body'] = None  # no post-frontmatter text in the whole-file .yaml branch
    else:
        try:
            text = fpath.read_text(encoding='utf-8')
        except OSError as exc:
            _LOG.warning('records.query: cannot read %s: %s', fpath, exc)
            return None

        # Parse frontmatter + body in one pass via the byte-parity
        # `parse_frontmatter` port (same module as the .yaml branch above).
        # `parse_frontmatter` returns `{"frontmatter": None, ...}` both when no
        # `---`-delimited block is found AND when a delimited block's YAML fails
        # to parse or parses to an empty mapping — mirroring schema.js's
        # `parseFrontmatter` negative-spec (both cases collapse to the same
        # no-frontmatter result). Skip silently either way, matching
        # query-records.js's own silent-skip default (includeUnparseable=false).
        parsed = parse_frontmatter(text)
        fm = parsed['frontmatter']
        if fm is None:
            return None
        body = parsed['body']
        if include_body:
            fm['body'] = body

    # cross-repo-memo memo-shape guard: skip files whose frontmatter lacks the
    # expected memo fields (from + to) — port of query-records.js's queryRecords guard
    # at bin/query-records.js:1394-1400. Applied here (not via _TYPE_TO_GLOB) because
    # the glob alone cannot express a frontmatter-shape filter.
    if record_type == 'cross-repo-memo':
        if not fm.get('from') or not fm.get('to'):
            return None

    # For handoff types: apply consumed-marker normalization BEFORE filtering.
    if record_type in ('handoff', 'handoff-archived'):
        _apply_consumed_marker(fm, body)

    # Roadmap status normalization — MUST run before the liveness injection
    # (query-records.js:1436-1441 ordering); no-op for every other type.
    _normalize_roadmap_status(fm, record_type)

    # Inject synthetic liveness field so --where liveness= and --format json
    # both work without special-casing the filter/format layers.
    fm['liveness'] = liveness(fm, record_type)

    # Inject the collection-origin `archived` boolean — set by the caller from
    # which segment (_collect_files vs _ARCHIVE_GLOB_FOR_TYPE's glob) walked
    # this file, never re-derived here from fpath/path. See this function's
    # docstring negative-spec.
    fm['archived'] = archived

    rel_path = rel_id(fpath, worktree_root)
    return {'path': rel_path, 'frontmatter': fm}


def _is_unattached(fm: dict) -> bool:
    """True iff ``fm['initiative']`` is null/absent — the ``--unattached`` predicate.

    Port of query-records.js's inline filter (bin/query-records.js:1457-1462):
    absent counts as null (``dict.get`` returning ``None`` for a missing key
    already collapses both cases, same as the JS ``=== null || === undefined``
    check).
    """
    return fm.get('initiative') is None


def _apply_since_where_filters(
    results: list[dict],
    *,
    since_cutoff: Optional[str] = None,
    older_than_cutoff: Optional[str] = None,
    clauses: Optional[list[dict]] = None,
) -> list[dict]:
    """Apply since/older-than/where filters, in that order.

    Factored out of ``_handler`` so the single-type ``records.query`` path and
    the ``--unattached`` union lens's per-type filtering
    (``_query_unattached_all``) share one definition rather than drifting into
    two copies — same ordering as query-records.js's ``queryRecords``
    (bin/query-records.js:1468-1495).
    """
    if since_cutoff:
        results = [
            r for r in results
            if r['frontmatter'].get('created')
            and str(r['frontmatter']['created']) >= since_cutoff
        ]
    if older_than_cutoff:
        results = [
            r for r in results
            if r['frontmatter'].get('created')
            and str(r['frontmatter']['created']) < older_than_cutoff
        ]
    if clauses:
        results = [r for r in results if _matches_where(r['frontmatter'], clauses)]
    return results


def _collect_type_records(
    worktree_root: Path, record_type: str, *,
    include_archived: bool = False, include_body: bool = False,
) -> list[dict]:
    """Collect every record of one type — no since/older-than/where/sort/limit applied.

    Factored out of ``_handler`` (and reused by ``_query_unattached_all``) so
    the synthetic-type dispatch (``handoff-ledger``/``research-claim``) and the
    ``--type plan`` canonical-filename allowlist live in exactly one place.
    Raises ``_RecordsCollectError`` on a directory-scan failure (propagated
    from ``_collect_files``) — the caller decides how to surface that (the
    single-type handler turns it into an ``incomplete``/``error`` payload; the
    union lens warns-and-skips that one type, matching query-records.js's
    ``queryUnattachedAll`` try/catch-and-continue).

    ``include_archived`` (default ``False``, OPT-IN): when true and
    ``record_type`` has an entry in ``_ARCHIVE_GLOB_FOR_TYPE``, additionally
    walks that archive glob and appends its files to the live candidate list
    BEFORE loading — live files first, then archive files (same ordering
    ``_collect_handoff_ledger_records`` already uses for its own live+archive
    merge). ``_collect_files`` itself is never called with anything but the
    live ``record_type``, so the default-off path is byte-identical to
    pre-existing behaviour: this parameter only ever ADDS files on top of
    what ``_collect_files(worktree_root, record_type)`` already returns.

    The live and archive candidate lists are loaded through separate
    ``_load_record`` calls (``archived=False`` / ``archived=True``) so each
    record's ``fm['archived']`` reflects which segment actually collected it —
    the origin flag, never a post-hoc test against the emitted ``path``.

    ``include_body`` (default ``False``, OPT-IN): threaded straight through to
    every ``_load_record`` call this function makes.
    """
    if record_type == 'handoff-ledger':
        return _collect_handoff_ledger_records(worktree_root)
    if record_type == 'research-claim':
        return _collect_research_claim_records(worktree_root)

    candidates = _collect_files(worktree_root, record_type)
    if record_type == 'plan':
        candidates = _apply_plan_filename_filter(candidates)

    archive_candidates: list[Path] = []
    if include_archived:
        archive_glob = _ARCHIVE_GLOB_FOR_TYPE.get(record_type)
        if archive_glob is not None:
            archive_candidates = _walk_glob_segments(worktree_root, archive_glob.split('/'))
            if record_type == 'plan':
                archive_candidates = _apply_plan_filename_filter(archive_candidates)

    results: list[dict] = []
    for fpath in candidates:
        rec = _load_record(
            fpath, worktree_root, record_type,
            archived=False, include_body=include_body,
        )
        if rec is not None:
            results.append(rec)
    for fpath in archive_candidates:
        rec = _load_record(
            fpath, worktree_root, record_type,
            archived=True, include_body=include_body,
        )
        if rec is not None:
            results.append(rec)
    return results


def _query_unattached_all(
    worktree_root: Path,
    *,
    clauses: Optional[list[dict]] = None,
    since_cutoff: Optional[str] = None,
    older_than_cutoff: Optional[str] = None,
    sort_str: Optional[str] = None,
    limit: int = 0,
) -> list[dict]:
    """Union ``--unattached`` results across every ``UNATTACHED_TYPES`` member.

    Port of query-records.js's ``queryUnattachedAll`` (bin/query-records.js
    ~1520-1560). For each type: collect, then filter to records whose
    ``initiative`` is null/absent, then apply since/older-than/where — same
    per-type pipeline order as the single-type path (``_is_unattached`` sits
    where the oracle's inline unattached filter does, before since/where).
    ``sort``/``limit`` are deliberately NOT applied per type — they are
    applied ONCE to the assembled union, after every type has contributed its
    records, so the final result is globally ordered and capped (a per-type
    limit would silently truncate before the union is complete and produce a
    different, wrong result set — this ordering is load-bearing).

    Each surviving record is tagged with ``_type`` (the source query type) so
    markdown-list rendering can pick the right per-type display function.

    A type whose collection raises ``_RecordsCollectError`` (directory-scan
    failure) is skipped with a stderr warning — mirrors the oracle's
    try/catch-and-continue with its own diagnostic write — rather than
    aborting the whole union.
    """
    all_records: list[dict] = []
    for record_type in UNATTACHED_TYPES:
        try:
            type_records = _collect_type_records(worktree_root, record_type)
        except _RecordsCollectError as exc:
            sys.stderr.write(
                f'records.query --unattached: skipping type "{record_type}" — {exc}\n'
            )
            continue
        type_records = [r for r in type_records if _is_unattached(r['frontmatter'])]
        type_records = _apply_since_where_filters(
            type_records,
            since_cutoff=since_cutoff,
            older_than_cutoff=older_than_cutoff,
            clauses=clauses,
        )
        for r in type_records:
            all_records.append({**r, '_type': record_type})

    if sort_str:
        all_records = _sort_records(all_records, sort_str)
    if limit > 0:
        all_records = all_records[:limit]
    return all_records


def _format_output(results: list[dict], fmt: str, *, record_type: Optional[str]) -> dict:
    """Render ``results`` per ``fmt`` — shared by the single-type and
    ``--unattached`` union dispatch paths.

    Port of query-records.js's ``formatRecords`` (bin/query-records.js:1601-
    1638): when ``record_type`` is set, every record uses that type's display
    function; when it's ``None`` (the multi-type union lens), each record
    falls back to its own ``_type`` tag (stamped by ``_query_unattached_all``)
    so a bug record renders with the bug display and a plan record with the
    plan display in the same result list.
    """
    if fmt == 'json':
        return {'records': results}

    if fmt == 'paths':
        return {'records': '\n'.join(r['path'] for r in results)}

    # format=markdown-list (also the default when omitted).
    global_display_fn = _TYPE_DISPLAY.get(record_type) if record_type else None
    lines = [
        (global_display_fn or _TYPE_DISPLAY.get(r.get('_type')) or _default_display)(
            r['path'], r['frontmatter'],
        )
        for r in results
    ]
    return {'records': '\n'.join(lines)}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

# Closed param set for the "records.query" op — the ONE canonical spelling per
# key (snake_case; no kebab-case aliasing, so `older-than` is rejected loudly
# rather than aliased into `older_than` and hiding the CLI/params-dict spelling
# divergence). Any key outside this set fail-louds in `_handler` with a
# did-you-mean suggestion — a silently-dropped filter key returns the
# unfiltered SUPERSET with exit 0, which reads as healthy and is worse than
# the hard failure (2026-07-22 claude-central-em silent-param-drop memo).
_KNOWN_PARAM_KEYS = frozenset(
    {
        "type", "where", "since", "older_than", "sort", "format", "limit",
        "unattached", "include_archived", "include_body",
    }
)


@register_op("records.query")
def _handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC "records.query" handler.

    Returns records matching the given ``type``/``where``/``since``/
    ``older_than``/``sort``/``format``/``unattached`` params.

    Params:
        type:        str — one of the keys of ``_TYPE_TO_GLOB`` (``handoff``,
                     ``handoff-archived``, ``plan``, ``cross-repo-memo``,
                     ``bug``, ``debt``, ``improvement``, ``tracker``,
                     ``roadmap``, ``health-status``, ``decision-guide``,
                     ``completion``, ``decision``, ``review``, ``lesson``,
                     ``handoff-ledger``, ``research-claim``). May be omitted
                     when ``unattached`` is true — see below.
        where:       str (optional) — full-grammar expression, e.g.
                     ``"kind=roadmap-baton AND priority>3"`` — supports
                     ``=``, ``!=``, ``<``, ``>``, ``<=``, ``>=``,
                     ``field in (a,b,c)``, and a bare-field presence filter.
        since:       str (optional) — ``Nd``/``Nw``/``Nm``/``YYYY-MM-DD``;
                     filters ``created >= cutoff`` (records lacking ``created``
                     are excluded).
        older_than:  str (optional) — same parser as ``since``; filters
                     ``created < cutoff``.
        sort:        str (optional) — bare field ascending, ``-``-prefixed
                     field descending; numeric-first-then-string compare.
        format:      str (optional, default ``markdown-list``) — ``paths``,
                     ``json``, or ``markdown-list`` (also the default).
        limit:       int (optional, default 50) — max records after
                     filtering+sorting.  ``limit <= 0`` → unlimited (no
                     truncation), matching query-records.js's
                     ``opts.limit && opts.limit > 0`` guard.
        unattached:  bool (optional, default false) — keep only records whose
                     ``initiative`` frontmatter is null/absent. When ``type``
                     is ALSO omitted, this triggers the multi-type union lens
                     (``_query_unattached_all``) over ``UNATTACHED_TYPES``
                     (``bug``, ``debt``, ``improvement``, ``roadmap``,
                     ``handoff``, ``plan``) — ``sort``/``limit`` apply ONCE to
                     the assembled union, not per type. When ``type`` is
                     present alongside ``unattached``, the predicate scopes to
                     that single type instead.
        include_archived: bool (optional, default false) — OPT-IN archive
                     coverage. When true and ``type`` has an archived
                     counterpart (``handoff`` -> ``archive/handoffs/**/*.md``,
                     ``plan`` -> ``archive/specs/**/*.md``, ``cross-repo-memo``
                     -> ``cross-repo/archive/*.md`` — see
                     ``_ARCHIVE_GLOB_FOR_TYPE``), archived records are
                     additionally collected alongside the live set. Default
                     OFF and scoped to the single-``type`` path only (does NOT
                     extend the ``--unattached``-without-``type`` union lens)
                     — every existing caller that omits this param sees
                     EXACTLY today's result set, unchanged.
        include_body: bool (optional, default false) — OPT-IN body
                     projection. Every ``fm['archived']``-bearing record type
                     (every type with a ``.md`` ``_load_record`` branch) gains
                     ``frontmatter['body']``: the post-frontmatter text for
                     the ``.md`` branch, or ``null`` for the ``.yaml``
                     whole-file branch (which has no body text — ``null``
                     distinguishes "no body exists" from "body was empty").
                     Rejected (non-zero exit) for the synthetic types
                     (``handoff-ledger``, ``research-claim``), which never
                     route through ``_load_record`` and so have no body to
                     project. Default OFF, scoped to the single-``type``
                     path only — same "existing callers see byte-identical
                     output" contract as ``include_archived``.

    Every record's frontmatter also carries an injected ``archived`` boolean
    (``fm['archived']``), true exactly when the record was collected from
    ``_ARCHIVE_GLOB_FOR_TYPE``'s glob rather than the live one — set from the
    collection origin ``_collect_type_records`` threads into ``_load_record``,
    never re-derived from the emitted ``path`` string.

    Returns:
        ``format=paths``: ``{"records": <newline-joined repo-relative paths>}``
        ``format=json``:  ``{"records": [{"path": ..., "frontmatter": {...}}, ...]}``
        ``format=markdown-list`` (or omitted): ``{"records": <newline-joined
        per-type rendered markdown lines>}``

        For ``type in _LEGACY_PROSE_QUEUE_PATH`` (``improvement``, ``bug``),
        the single-type path ALSO carries three extra top-level keys whenever
        a non-empty legacy prose queue exists on disk at that type's fixed
        path — ``legacy_prose_unindexed_count`` (int), ``legacy_prose_
        unindexed_path`` (str, repo-relative), and ``legacy_prose_unindexed_
        remediation`` (str, names the one-shot migrator CLI) — same flat-key
        convention as the ``incomplete``/``error`` directory-scan-failure
        signal below. See ``_legacy_prose_signal`` (DR-115).

    Every record's frontmatter carries an injected ``liveness`` field
    (``liveness(fm, record_type)``) BEFORE since/older-than/where filtering,
    so ``--where liveness=BLOCKED`` composes correctly (query-records.js:1406
    ordering).

    Unknown param KEY (anything outside ``_KNOWN_PARAM_KEYS``, e.g. kebab-case
    ``older-than``) → non-zero exit + stderr naming the key with a did-you-mean
    suggestion — never silently dropped (a dropped filter returns the
    unfiltered superset with exit 0, which is the dangerous direction).
    Unknown ``type`` → non-zero exit + stderr (trips PATH-fallback), mirroring
    query-records.js and _parse_where. This guard is skipped for the
    ``unattached``-without-``type`` union dispatch, which needs no ``type``.
    Absent ``repo_root`` → well-formed empty payload, no raise.

    Worktree resolution mirrors roadmap_serve.py:
        repo_root provided → main_worktree_root(repo_root) → worktree_root
        repo_root absent  → logged warning, empty payload
    """
    # ---- Strict param allowlist (fail-loud on unknown keys) -----------------
    # Same loud-exit shape as the unknown-type guard below: stderr + non-zero
    # exit. A dropped key is a correctness bug (unfiltered superset, exit 0),
    # not a style issue — see _KNOWN_PARAM_KEYS.
    unknown_keys = sorted(set(params) - _KNOWN_PARAM_KEYS)
    if unknown_keys:
        valid = ', '.join(sorted(_KNOWN_PARAM_KEYS))
        for key in unknown_keys:
            suggestion = difflib.get_close_matches(key, _KNOWN_PARAM_KEYS, n=1)
            hint = f' Did you mean {suggestion[0]!r}?' if suggestion else ''
            sys.stderr.write(
                f'records.query: unknown param: {key!r}.{hint} Valid: {valid}.\n'
            )
        sys.exit(1)

    record_type: Optional[str] = params.get('type')
    where_str: Optional[str] = params.get('where', '')
    since_str: Optional[str] = params.get('since')
    older_than_str: Optional[str] = params.get('older_than')
    sort_str: Optional[str] = params.get('sort')
    fmt: str = params.get('format') or 'markdown-list'
    limit: int = int(params.get('limit', 50))
    unattached: bool = bool(params.get('unattached'))
    include_archived: bool = bool(params.get('include_archived'))
    include_body: bool = bool(params.get('include_body'))

    # ---- Multi-type --unattached union lens: type absent, unattached=true ---
    # Dispatch mirrors query-records.js's `opts.unattached && !opts.type` gate
    # (bin/query-records.js:1954-1956) — `type` present alongside `unattached`
    # instead falls through to the single-type path below, which also applies
    # the unattached predicate (same as the oracle's unconditional in-
    # queryRecords filter), just scoped to that one type.
    if unattached and not record_type:
        if repo_root is None:
            _LOG.warning(
                'records.query: repo_root absent — returning empty payload for --unattached',
            )
            return _empty_payload(fmt)

        worktree_root = main_worktree_root(repo_root)

        clauses: list[dict] = []
        if where_str:
            clauses = _parse_where(where_str)  # may sys.exit(1) on unparseable clause
        since_cutoff = _parse_since(since_str)  # may sys.exit(1) on invalid value
        older_than_cutoff = _parse_older_than(older_than_str)

        results = _query_unattached_all(
            worktree_root,
            clauses=clauses,
            since_cutoff=since_cutoff,
            older_than_cutoff=older_than_cutoff,
            sort_str=sort_str,
            limit=limit,
        )
        return _format_output(results, fmt, record_type=None)

    # ---- Empty-payload guards (no raise) ------------------------------------

    if record_type not in _TYPE_TO_GLOB:
        valid = ', '.join(sorted(_TYPE_TO_GLOB))
        sys.stderr.write(
            f'records.query: unknown type: {record_type!r}. Valid: {valid}. '
            f'Non-zero exit to trip PATH-fallback.\n'
        )
        sys.exit(1)

    # `include_body` has no effect on the synthetic types: neither
    # `_collect_handoff_ledger_records` nor `_collect_research_claim_records`
    # routes through `_load_record` (they build frontmatter dicts directly —
    # see `_SYNTHETIC_TYPES`), so there is no post-frontmatter body to
    # project. Fail loud rather than silently ignoring the flag — same
    # rationale as the unknown-param-key guard above.
    if include_body and record_type in _SYNTHETIC_TYPES:
        sys.stderr.write(
            f'records.query: include_body is not supported for type {record_type!r} '
            f'(synthetic record type — no post-frontmatter body exists).\n'
        )
        sys.exit(1)

    if repo_root is None:
        _LOG.warning(
            'records.query: repo_root absent — returning empty payload for type=%r',
            record_type,
        )
        return _empty_payload(fmt)

    worktree_root = main_worktree_root(repo_root)

    # ---- Parse where/since/older-than (invalid input → sys.exit(1)) --------

    clauses = []
    if where_str:
        clauses = _parse_where(where_str)  # may sys.exit(1) on unparseable clause

    since_cutoff = _parse_since(since_str)  # may sys.exit(1) on invalid value
    older_than_cutoff = _parse_older_than(older_than_str)

    # ---- Collect ALL candidate records (no filtering/limiting yet) ---------
    # query-records.js collects the FULL matching set before applying
    # since/older-than/where/sort/limit, in that order (queryRecords:1433-1476)
    # — a limit slice before sort would truncate wrong once --sort reorders,
    # so this loop must not early-break at `limit` (unlike the pre-T4d-g1c
    # equality-only version, where sort didn't exist and early-break was safe).

    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # Previously a directory-scan failure inside _collect_files was swallowed
    # to [] here, making it indistinguishable from a legitimately-empty result
    # set. Surface it explicitly: the payload still carries a well-formed
    # `records` value (empty), but callers that check `incomplete`/`error` can
    # now tell "scan failed" apart from "zero records exist".
    try:
        results = _collect_type_records(
            worktree_root, record_type,
            include_archived=include_archived, include_body=include_body,
        )
    except _RecordsCollectError as exc:
        payload = _empty_payload(fmt)
        payload['incomplete'] = True
        payload['error'] = str(exc)
        return payload
    # --- end Tier 2 ---

    # ---- Apply --unattached predicate (single-type scope) --------------------
    # bin/query-records.js:1457-1462 — applied before since/where, same as the
    # union lens's per-type filtering above.

    if unattached:
        results = [r for r in results if _is_unattached(r['frontmatter'])]

    # ---- Apply --since/--older-than/--where -----------------------------------

    results = _apply_since_where_filters(
        results,
        since_cutoff=since_cutoff,
        older_than_cutoff=older_than_cutoff,
        clauses=clauses,
    )

    # ---- Apply --sort -----------------------------------------------------------

    if sort_str:
        results = _sort_records(results, sort_str)

    # ---- Apply --limit (AFTER sort, matching query-records.js ordering) -------

    if limit > 0:
        results = results[:limit]

    # ---- Format output -------------------------------------------------------

    payload = _format_output(results, fmt, record_type=record_type)

    # ---- Legacy prose-queue invisibility signal (DR-115) ----------------------
    # Unconditional — computed regardless of where/since/older-than/sort/limit,
    # since the legacy entries are invisible to those filters too; this is a
    # "you are missing data" flag, not a filtered result. See
    # `_legacy_prose_signal`'s docstring for the "both present" rationale.
    legacy_signal = _legacy_prose_signal(worktree_root, record_type)
    if legacy_signal is not None:
        migrator = _LEGACY_PROSE_MIGRATOR.get(record_type, '')
        payload['legacy_prose_unindexed_count'] = legacy_signal['count']
        payload['legacy_prose_unindexed_path'] = legacy_signal['path']
        payload['legacy_prose_unindexed_remediation'] = (
            f"{legacy_signal['count']} entries in {legacy_signal['path']} are not "
            f"indexed by this query (unmigrated legacy prose queue). Migrate with "
            f"{migrator} --dry-run."
        )

    return payload


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SIDECAR_SUFFIXES = (
    '.plan-coverage-check.',  # subsumes both classic (.plan-coverage-check.md) and timestamped variants
    '.prior-art-check.',       # subsumes both classic (.prior-art-check.md) and timestamped variants
    '.review.',                # review-sidecar schema (docs/plans/*.review.md)
    '.docs-check.',            # docs-check-sidecar schema (docs/plans/*.docs-check.md)
    # Review: F6 — removed redundant .prior-art-check.md / .plan-coverage-check.md entries;
    # the dot-terminated forms already match them as substrings via _is_known_sidecar.
    # Review: code-reviewer (ops-records-cruft-hierarchy F2) — added .review./.docs-check.
    # so this set covers all 4 docs/plans/* sidecar schemas the oracle's
    # _buildPlanSidecarRegexes derives, not just 2 of 4.
)

# Numbered review-iteration sidecar: <stem>.review-N.md (second/Nth reviewer).
# Port of query-records.js's module-scope _REVIEW_ITERATION_RE.
_REVIEW_ITERATION_RE = re.compile(r'\.review-\d+\.md$')


def _is_known_sidecar(basename: str) -> bool:
    """Return True if the basename looks like a known sidecar variant.

    Mirrors query-records.js's plan-sidecar exclusion: schema-derived
    suffixes, the numbered review-iteration form (``.review-N.md``), and
    ``README.md`` (case-insensitive) — all excluded silently, never routed
    through the anomaly-warn path.
    """
    for suffix in _SIDECAR_SUFFIXES:
        if suffix in basename:
            return True
    if _REVIEW_ITERATION_RE.search(basename):
        return True
    if basename.lower() == 'readme.md':
        return True
    return False


def _empty_payload(fmt: str) -> dict:
    """Return a well-formed empty payload for the given format."""
    if fmt == 'json':
        return {'records': []}
    return {'records': ''}
