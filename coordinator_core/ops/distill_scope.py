"""
coordinator_core.ops.distill_scope — JSON-RPC "distill.scope" operation.

Purpose: one call composing the artifact-distillation Phase 0 scoping walk
(reflection §1 / cockpit §1 / central-em #3 — DoE PIPELINE.md steps 6-11) that a
`/distill` run today performs by hand-reading harvest-debt + ripe-filter + sidecar-
sweep + a handoff-frontmatter scan + an actioned-memo enumeration + a wiki-inventory
walk, then batching and minting a run id. This op composes the existing pure library
(``coordinator_core.distill.harvest_debt`` / ``ripe_filter`` / ``sidecar_sweep``) plus
two new inline scans (handoff resolution-gate, actioned-memo cohort, wiki inventory)
and emits:

  (a) the Workflow INPUT JSON at
      ``state/scratch/artifact-distillation/<run-id>/input.json`` — keeps the
      (potentially ~25KB) batch table out of the calling EM's context entirely
      (central-em #3);
  (b) a short human-readable scope summary, returned as the op's result (the
      caller/CLI prints it to stdout).

MUTATING (DR-208 fail-closed default: any file write disqualifies COMPUTE_ONLY, and
the scratch-tier INPUT JSON write is exactly that write). Sanctioned as one of DR-228's
scratch-tier writers (D6): write-confined to its own
``state/scratch/artifact-distillation/<run-id>/`` subdirectory, create-or-full-rewrite
only (never partial in-place mutation), no delete, no commit — see
``coordinator_core/authz/classification.py``'s DR-208 five-question affirmation block
for this op, which cites DR-228 § D6(i)-(v).

Cohort composition (DEC-5 — this module is a thin orchestrator; no business logic
migrates up from the library it composes):
  - harvest cohort = ripe_filter's harvest set ∩ harvest_debt's undebted set — a spec
    that is both harvest-ripe (status: implemented/shipped) AND not yet logged as
    harvested is the actual Phase-0 scan candidate; a ripe spec already logged
    harvested needs no re-scan.
  - sidecars cohort = sidecar_sweep's candidates, unguarded (cockpit §6.1 — pre-routed,
    never entering harvest/skip). The active-reference guard runs at delete time in
    delete_guard, so this op spawns no rg.
  - handoffs / memos / any further cohort (2026-08-06 PM ruling) = DATA-DRIVEN
    ``CohortSpec`` rows ({name, glob, filter, mode} — see the CohortSpec/
    PM_RULING_2026_08_06_COHORT_SPECS docstrings below), never hardcoded scan
    trees: the historical handoffs (Phase-0 resolution gate: status
    ``consumed``/``claimed`` AND ``shipped_in:`` present AND
    ``deployment_state:`` != ``abandoned`` — DoE PIPELINE.md § "Phase 0
    resolution gate (handoffs)", default-on-ambiguity SKIP) and memos
    (cross-repo/archive/*.md) cohorts are now the DEFAULT CohortSpec rows
    (compute_scope's cohort_specs=None case), reproduced byte-identically;
    the ruling's actual cohort set — memos narrowed to the distill_fate
    self-declared-recording subset, archive/completed/ and
    state/week-changelog/ as new harvest cohorts, archived handoffs demoted
    to "check-only" (a deletability check, never a harvest — they are
    ephemeral by design) — is a caller-supplied cohort_specs config edit, not
    a code change.
  - wiki inventory = wiki_dirs (ordered; first is the default NEW-file home) +
    wiki_slugs (flat filename-stem -> repo-relative-path index across every wiki_dir).
    DR-146 stem-normalization constants (DR146_STRIP_SUFFIXES / DR146_DATE_PREFIX_RE /
    DR146_MIN_STEM_LEN / STEM_PREFIX_LEN) are ported here byte-for-byte from DoE's
    ``coordinator/pipelines/artifact-distillation/distill-harvest.workflow.js`` so the
    two sites can be grep-asserted for drift (this module does not itself run the
    fuzzy-match consolidation — that stays in DoE's Wave-2 synth script, which is the
    consumer of this op's emitted wiki_slugs index).

Chronological batching: harvest ∪ handoffs ∪ memos cohorts (sidecars/skip are NOT
batched — they are cohort annotations only) are sorted by an embedded
``YYYY-MM-DD`` filename-prefix (empty string when absent, so undated files sort
first, deterministically, never by filesystem mtime — mtime is stable run-over-run
on an unmodified tree but is NOT stable across a fresh clone/checkout, and AC1's
determinism test compares two runs, not two checkouts) then by path, and chunked
into batches packed under SCAN_BATCH_BYTE_BUDGET (``batch_size`` is the hard file-count cap).

Negative-spec: performs no LLM call, no nugget extraction, no wiki-guide synthesis,
no reality-check-scout judgment, no contradiction adjudication, no PM disposal
decision — this op stops at classification/partition/batch-table emission (plan
§ Negative spec, AC12).

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C10
Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6
"""

from __future__ import annotations

import bisect
import functools
import hashlib
import inspect
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from coordinator_core.distill import _common as _distill_common
from coordinator_core.distill import guide_index as _guide_index
from coordinator_core.contract import run_budget as _run_budget
from coordinator_core.distill import harvest_debt as _harvest_debt
from coordinator_core.distill import manifest_schema as _schema
from coordinator_core.distill import ripe_filter as _ripe_filter
from coordinator_core.distill import scan_digest as _scan_digest
from coordinator_core.distill._common import is_sidecar_filename, parse_distillation_log
from coordinator_core.frontmatter import primitives as _fm_primitives
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core import run_watermark as _run_watermark
from coordinator_core.testing.suite_run_record import read_head_sha as _head_sha  # file read, no git spawn (spawn audit)
from coordinator_core.machine_resolver import registry_get
from coordinator_core.memo_corpus import memo_corpus_root
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session import record_homes
from coordinator_core.wire_paths import rel_id

__all__ = [
    "DR146_STRIP_SUFFIXES",
    "DR146_DATE_PREFIX_RE",
    "DR146_MIN_STEM_LEN",
    "STEM_PREFIX_LEN",
    "dr146_normalize",
    "slugify_stem",
    "wiki_slugs_as_dict",
    "ScopeResult",
    "CohortSpec",
    "VALID_COHORT_MODES",
    "PM_RULING_2026_08_06_COHORT_SPECS",
    "LOGS_COHORT",
    "LOGS_COHORT_SPECS",
    "compute_scope",
    "scannable_count",
    "PLANS_COHORT",
    "pack_batches",
    "attach_digests",
    "SCAN_BATCH_BYTE_BUDGET",
    "DEFAULT_BATCH_MAX_FILES",
    "SCAN_BATCH_ENTRY_BUDGET",
    "write_scope_manifest",
    "render_summary",
]

# ---------------------------------------------------------------------------
# DR-146 stem-normalization — ported verbatim from DoE's
# coordinator/pipelines/artifact-distillation/distill-harvest.workflow.js
# (DR146_STRIP_SUFFIXES / DR146_DATE_PREFIX_RE / DR146_MIN_STEM_LEN /
# STEM_PREFIX_LEN, dr146Normalize / leadingStem). Both sites MUST carry
# identical values — grep-assert this file's constants against the JS
# constants of the same name on drift; there is no shared-import seam
# between a Python engine and a JS Workflow script, so citation is the
# drift-detection mechanism, not import-time equality.
# ---------------------------------------------------------------------------

DR146_STRIP_SUFFIXES: tuple[str, ...] = ("-shape", "-design", "-v2")
DR146_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
DR146_MIN_STEM_LEN: int = 8
STEM_PREFIX_LEN: int = 6

_FILENAME_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def dr146_normalize(slug: str) -> str:
    """Port of distill-harvest.workflow.js's dr146Normalize(slug): lowercase,
    strip a leading YYYY-MM-DD- date prefix, strip one DR146_STRIP_SUFFIXES
    match (first hit only, in listed order), then de-pluralize a trailing
    non-"ss" "s"."""
    s = slug.lower()
    s = DR146_DATE_PREFIX_RE.sub("", s)
    for suffix in DR146_STRIP_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    if s.endswith("s") and not s.endswith("ss"):
        s = s[:-1]
    return s


def slugify_stem(filename_stem: str) -> str:
    """Port of distill-harvest.workflow.js's slugifyTopic(): lowercase,
    collapse runs of non-[a-z0-9] to a single hyphen, trim leading/trailing
    hyphens. Used to build the wiki_slugs flat index key from a wiki file's
    bare filename stem (not the DR-146-normalized form — that normalization
    is applied downstream by the fuzzy-match consumer, not by this index)."""
    s = filename_stem.lower()
    s = _SLUG_NON_ALNUM_RE.sub("-", s)
    return s.strip("-")


# ---------------------------------------------------------------------------
# Handoff Phase-0 resolution gate (DoE PIPELINE.md § "Phase 0 resolution gate
# (handoffs)") — restated here per the plan's paraphrase: status consumed|claimed
# AND shipped_in present AND NOT deployment_state == abandoned. Default on
# ambiguity is SKIP — a missing/unparseable field never resolves eligible.
# ---------------------------------------------------------------------------

_HANDOFF_ELIGIBLE_STATUSES = frozenset({"consumed", "claimed"})


def _handoff_eligible(text: str) -> bool:
    split = split_frontmatter(text)
    if split is None:
        return False
    fm = split.fm_text
    status = read_fm_field_unquoted(fm, "status")
    shipped_in = read_fm_field_unquoted(fm, "shipped_in")
    deployment_state = read_fm_field_unquoted(fm, "deployment_state")
    if status not in _HANDOFF_ELIGIBLE_STATUSES:
        return False
    if not shipped_in:
        return False
    if deployment_state == "abandoned":
        return False
    return True


# ---------------------------------------------------------------------------
# Memo self-declared-recording filter (2026-08-06 PM ruling) — a memo "wants
# recording" when it carries memo_triage.py's own distill_fate promote-signal
# (``ratification``/``commitment``, DoE's C2 schema) in its frontmatter. A
# memo with no distill_fate field, or ``ephemeral``/unknown, is never
# self-declared — default-on-ambiguity is exclude, matching the handoff
# resolution gate's own default-SKIP posture. This mirrors, but does not
# import, memo_triage.py's ``_FATE_PROMOTE`` set — that module's own set is
# scoring/triage-specific (talks to a pre-filter heuristic this cohort filter
# has no business depending on); duplicating the two-value set here is the
# smaller coupling.
# ---------------------------------------------------------------------------

_MEMO_FATE_PROMOTE = frozenset({"ratification", "commitment"})


def _memo_distill_fate_promote(text: str) -> bool:
    split = split_frontmatter(text)
    if split is None:
        return False
    fate = read_fm_field_unquoted(split.fm_text, "distill_fate")
    return fate in _MEMO_FATE_PROMOTE


#: Frontmatter fates settled without a reading agent (``manifest_schema.VALID_MECHANICAL_FATES``).
_MECHANICAL_FATE_VALUES = frozenset(_schema.VALID_MECHANICAL_FATES)

#: Bytes read per scannable file to decide a mechanical fate; a frontmatter block that
#: does not close inside the head parses as None and the row stays batched.
_FATE_HEAD_BYTES = 2048


def mechanical_fate(text: str) -> str | None:
    """Fate decidable from frontmatter alone, or None when an agent must read the prose.

    Pure: ``distill_fate: ephemeral`` (the artifact declares nothing is owed) is the one
    class today. A missing, unparseable or unknown fate is None, never a fate. Triage only:
    delete guards still re-run at apply_disposal."""
    split = split_frontmatter(text)
    if split is None:
        return None
    fate = read_fm_field_unquoted(split.fm_text, "distill_fate")
    return fate if fate in _MECHANICAL_FATE_VALUES else None


def _head_fate(path: Path | str) -> tuple[bool, str | None]:
    """(readable, mechanical fate of the file's head). Unreadable is (False, None), never a fate."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_FATE_HEAD_BYTES)
    except OSError:
        return False, None
    return True, mechanical_fate(head.decode("utf-8", errors="replace"))


#: Named per-cohort eligibility filters a CohortSpec row may reference by
#: string key (never a raw callable in the row itself — rows are meant to be
#: DATA, reproducible as config, not code).
_COHORT_FILTERS: dict[str, Any] = {
    "handoff_resolution_gate": _handoff_eligible,
    "memo_distill_fate_promote": _memo_distill_fate_promote,
}

#: Valid values for a CohortSpec's ``mode`` field. "harvest" cohorts enter the
#: chronological scannable/batches table; "check-only" cohorts are computed
#: and reported (for a deletability check) but NEVER batched for a Phase-0
#: harvest scan — the 2026-08-06 PM ruling's distinction for archived
#: handoffs ("ephemeral by design ... need a deletability check, not a
#: harvest").
VALID_COHORT_MODES: frozenset[str] = frozenset({"harvest", "check-only"})


@dataclass(frozen=True)
class CohortSpec:
    """One data-driven cohort row: {name, glob, filter, mode}.

    ``glob`` is a worktree_root-relative glob pattern (``Path.glob`` syntax,
    ``**`` supported for recursive) — e.g. ``"cross-repo/archive/**/*.md"``.
    ``filter`` is an OPTIONAL string key into ``_COHORT_FILTERS`` (None means
    pass-all — every glob match is eligible). ``mode`` is "harvest" or
    "check-only" (VALID_COHORT_MODES) — see VALID_COHORT_MODES's docstring.

    The whole point of this shape (per the 2026-08-06 PM ruling) is that the
    cohort SET becomes a config edit, never a code change: today's hardcoded
    handoffs-always-on / memos-never-narrowed / archive/completed+state/week-
    changelog-not-expressible-at-all gap is closed by adding/removing/
    reshaping CohortSpec rows, not by touching compute_scope's body.
    """

    name: str
    glob: str
    filter: str | None = None
    mode: str = "harvest"

    def __post_init__(self) -> None:
        if self.mode not in VALID_COHORT_MODES:
            raise ValueError(
                f"CohortSpec {self.name!r}: mode must be one of "
                f"{sorted(VALID_COHORT_MODES)}; got {self.mode!r}"
            )
        if self.filter is not None and self.filter not in _COHORT_FILTERS:
            raise ValueError(
                f"CohortSpec {self.name!r}: unknown filter {self.filter!r}; "
                f"known filters: {sorted(_COHORT_FILTERS)}"
            )


def _default_cohort_specs(handoffs_dir: str, memos_dir: str) -> list[CohortSpec]:
    """The historical (pre-2026-08-06-ruling) hardcoded cohort set, ported to
    CohortSpec rows so compute_scope's default (no ``cohort_specs`` param
    supplied) behavior is BYTE-IDENTICAL to before this chunk: handoffs always
    scanned+gated, memos always scanned unfiltered, both "harvest" mode. This
    is what a caller gets when it retargets scan roots via the legacy
    ``specs_dir``/``handoffs_dir``/``memos_dir`` params without opting into
    ``cohort_specs`` — the sibling-repo compatibility this chunk must not
    break."""
    handoffs_glob = f"{Path(handoffs_dir).as_posix()}/**/*.md"
    memos_glob = f"{Path(memos_dir).as_posix()}/**/*.md"
    return [
        CohortSpec(name="handoffs", glob=handoffs_glob, filter="handoff_resolution_gate", mode="harvest"),
        CohortSpec(name="memos", glob=memos_glob, filter=None, mode="harvest"),
    ]


#: The 2026-08-06 PM ruling ("plans/specs, memos that self-declared they want
#: recording, and completion entries from workstream/day/week; NOT archived
#: handoffs — they are ephemeral by design and need a deletability check, not
#: a harvest"), expressed purely as CohortSpec config — the manifest this
#: ruling required was hand-built mid-run before this chunk; a caller now
#: passes this constant (or a caller-authored equivalent) as compute_scope's
#: ``cohort_specs`` param instead. "plans/specs" is the harvest/skip core
#: cohort compute_scope always computes (DEC-5 business logic, not
#: config-expressible — see module docstring's Cohort composition section)
#: and is therefore NOT a row here.
PM_RULING_2026_08_06_COHORT_SPECS: tuple[CohortSpec, ...] = (
    CohortSpec(name="memos", glob="cross-repo/archive/**/*.md", filter="memo_distill_fate_promote", mode="harvest"),
    CohortSpec(name="completed", glob="archive/completed/**/*.md", filter=None, mode="harvest"),
)
# No handoffs row (excluded from every cohort, DoE phase-0-scoping.md) and no
# week_changelog row (its source is not in the target set yet).

#: The daily and weekly logs: the first admission target, and the source of every plan citation
#: that admits a plan. Undebted members only; `HEADER*` files are not entries.
LOGS_COHORT = "logs"
LOGS_COHORT_SPECS: tuple[CohortSpec, ...] = (
    CohortSpec(name=LOGS_COHORT, glob="archive/daily-summaries/**/*.md"),
    CohortSpec(name=LOGS_COHORT, glob=record_homes.home_rel("week-changelog") + "/**/*.md"),
    CohortSpec(name=LOGS_COHORT, glob="archive/week-changelogs/**/*.md"),
)
_LOG_NON_ENTRY_PREFIX = "HEADER"


_LEGACY_MEMO_ARCHIVE = "cross-repo/archive"


def _memo_archive_reldir(worktree_root: Path) -> str:
    """This repo's own memo archive, worktree-relative: `state/cross-repo/archive`, or legacy
    `cross-repo/archive` in a repo not yet migrated (memo_corpus_root decides)."""
    root = os.path.relpath(memo_corpus_root(str(worktree_root)), str(worktree_root))
    return f"{Path(root).as_posix()}/archive"


def _rebase_memo_glob(spec: CohortSpec, memo_archive: str) -> CohortSpec:
    """A cohort row that names the memo archive by its legacy path (as
    PM_RULING_2026_08_06_COHORT_SPECS does) scans it where this repo's corpus lives."""
    legacy_prefix = f"{_LEGACY_MEMO_ARCHIVE}/"
    if memo_archive == _LEGACY_MEMO_ARCHIVE or not spec.glob.startswith(legacy_prefix):
        return spec
    return replace(spec, glob=f"{memo_archive}/{spec.glob[len(legacy_prefix):]}")


def _date_sort_key(rel_path: str) -> tuple[str, str]:
    """Chronological sort key: embedded YYYY-MM-DD filename-prefix (or "" when
    absent, sorting undated files first), then the path itself — never
    filesystem mtime (AC1 determinism is checked across repeated runs over the
    same tree, not across checkouts; mtime is stable for the former but not the
    latter, so a pure path-derived key is the more portable choice)."""
    name = rel_path.rsplit("/", 1)[-1]
    match = _FILENAME_DATE_PREFIX_RE.match(name)
    date_prefix = match.group(1) if match else ""
    return (date_prefix, rel_path)


#: Worktree-relative directory holding one subdirectory per distill run.
RUN_DIR_REL = "state/scratch/artifact-distillation"

#: How much input one Haiku scan agent is given: ~100K tokens at bytes/4, half
#: of a 200K-token context, leaving the rest for the prompt, guide list and output.
SCAN_BATCH_BYTE_BUDGET: int = 400_000

#: Bytes of per-file entry (path, plus digest path when digested) one batch may carry: a
#: batch file is relayed whole and must stay under distill_workflow_input.SLUG_SHARD_MAX_BYTES
#: (12,000), leaving headroom for its envelope, formatHints and relevantGuides.
SCAN_BATCH_ENTRY_BUDGET: int = 9_000

#: Hard upper file count per batch; the byte budget is the normal limiter.
DEFAULT_BATCH_MAX_FILES: int = 100


def pack_batches(
    paths: list[str],
    *,
    size_of: Callable[[str], int],
    byte_budget: int,
    max_files: int,
    entry_cost_of: Callable[[str], int] | None = None,
    entry_budget: int = SCAN_BATCH_ENTRY_BUDGET,
) -> list[list[str]]:
    """Greedy in-order packing: close the current batch when adding the next path
    would exceed ``byte_budget``, ``max_files``, or (when ``entry_cost_of`` is given)
    ``entry_budget`` bytes of per-file batch-JSON entry. A path over budget gets a
    batch of its own — never dropped. Order is preserved, so output is as
    chronological as ``paths``."""
    cap = max(1, max_files)
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    current_entry = 0
    for path in paths:
        size = max(0, size_of(path))
        entry = max(0, entry_cost_of(path)) if entry_cost_of is not None else 0
        if current and (
            len(current) >= cap
            or current_bytes + size > byte_budget
            or current_entry + entry > entry_budget
        ):
            batches.append(current)
            current, current_bytes, current_entry = [], 0, 0
        current.append(path)
        current_bytes += size
        current_entry += entry
    if current:
        batches.append(current)
    return batches


@dataclass(frozen=True)
class ScopeResult:
    """Result of compute_scope: the fully-assembled scope-manifest dict (C9 shape)
    plus the human-summary fields the CLI/caller renders separately from the
    manifest body (kept out of context per central-em #3)."""

    manifest: dict[str, Any]
    counts: dict[str, int]
    digests: dict[str, str] = field(default_factory=dict)
    #: ``(mtime_ns, size)`` of every scannable file as the scan saw it: the guide-ranking cache key.
    keys: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: The watermark this run proposes (admit mode, HEAD resolvable); ``_handler`` stages it and
    #: ``distill.apply_disposal`` promotes it in its commit. None when no watermark applied.
    watermark: _run_watermark.Watermark | None = None
    #: ``settle(extra_deferred)`` re-proposes the watermark with files a later stage also deferred;
    #: None whenever ``watermark`` is.
    settle: Callable[[list[str]], _run_watermark.Watermark] | None = None


def run_fixed_cost(wiki_slugs: dict[str, str]) -> _run_budget.UnitCost:
    """Cost of a run before any scan batch: the manifest relay agent plus one per slug shard."""
    # Deferred: distill_workflow_input imports this module.
    from coordinator_core.ops.distill_workflow_input import SLUG_SHARD_MAX_BYTES, _slug_shards

    shards = len(_slug_shards(wiki_slugs))
    return _run_budget.UnitCost(
        agents=_schema.LOAD_RELAY_AGENTS + shards,
        tokens=_schema.LOAD_RELAY_TOKENS + shards * (SLUG_SHARD_MAX_BYTES // 4),
        minutes=_schema.LOAD_RELAY_MINUTES,
    )


def scan_batch_cost() -> _run_budget.UnitCost:
    """Cost of one scan batch: its relay agent and its scan agent."""
    return _run_budget.UnitCost(
        agents=_schema.SCAN_BATCH_AGENTS, tokens=_schema.SCAN_BATCH_TOKENS, minutes=_schema.SCAN_BATCH_MINUTES
    )


def projected_cost(fixed: _run_budget.UnitCost, batches: int) -> _run_budget.UnitCost:
    """``fixed`` plus ``batches`` scan batches."""
    unit = scan_batch_cost()
    return _run_budget.UnitCost(
        agents=fixed.agents + batches * unit.agents,
        tokens=fixed.tokens + batches * unit.tokens,
        minutes=fixed.minutes + batches * unit.minutes,
    )


def budget_block(fixed: _run_budget.UnitCost, batches: int) -> dict[str, Any]:
    """The manifest's ``budget``: cap and projected cost per axis for a run of ``batches`` scan batches."""
    budget = _run_budget.load("distill")
    return _run_budget.budget_report(budget, projected_cost(fixed, batches), _run_budget.over_cap_fixed(budget, fixed))


#: Manifest cohort label for implemented, not-yet-harvested ``docs/plans`` plans.
PLANS_COHORT = "plans"

#: Cohorts (specs, plans, completed) admitted only when an unharvested log cites them and they
#: clear the weight floor.
_GATED_COHORTS = frozenset({"harvest", PLANS_COHORT, "completed"})
_DECISIONS_DIR = "docs/decisions"


# ---------------------------------------------------------------------------
# Incremental scope. Every per-file fact (ripe status, handoff gate, memo fate, head
# fate, digest) is cached under the file's (mtime_ns, size); a stat hit never reads the
# file, a miss reads the same bytes the uncached code read. Facts are derived values, so the
# cache schema folds in the source of every function that derives them.
# ---------------------------------------------------------------------------

#: Settings-registry key naming the cache base dir; unset falls back to the repo's
#: gitignored ``.coordinator-local/cache``.
CACHE_KEY = "paths.distill_scope_cache"
_CACHE_SUBDIR = "distill-scope"
_CACHE_LAYOUT = 1

#: An entry whose file changed within this window of being stored could be served stale by a
#: same-size rewrite inside one timestamp tick, so it is never stored.
_RACY_WINDOW_NS = 2_000_000_000


@functools.cache
def _code_fingerprint() -> str:
    """Schema of every cached derived value: changes when any fact-deriving code changes.
    Unreadable source yields a per-process token, so the cache never serves across it."""
    sources: list[Any] = [
        _ripe_filter._classify_one,
        _handoff_eligible,
        _memo_distill_fate_promote,
        mechanical_fate,
        _head_fate,
        _fm_primitives.split_frontmatter,
        _fm_primitives.read_fm_field,
        _fm_primitives.read_fm_field_unquoted,
        parse_distillation_log,
        _harvest_debt._has_content_beyond_run_headers,
        _harvest_debt._has_any_parseable_row,
        _harvest_debt._is_log_header_line,
        _scan_digest,
        _guide_index,
        _distill_common._ROW_RE.pattern,
        _distill_common._RUN_HEADER_RE.pattern,
        _harvest_debt._ACTION_TABLE_ROW_RE.pattern,
        _cite_keys,
        slugify_stem,
        _CITE_STEM_RE.pattern,
        _CITE_PATH_RE.pattern,
    ]
    consts = (
        _FATE_HEAD_BYTES,
        sorted(_HANDOFF_ELIGIBLE_STATUSES),
        sorted(_MEMO_FATE_PROMOTE),
        sorted(_MECHANICAL_FATE_VALUES),
        sorted(_ripe_filter.RIPE_STATUSES),
        sorted(_ripe_filter.SKIP_STATUSES),
        sorted(_harvest_debt.HARVESTED_DISPOSITIONS),
    )
    parts = [repr(consts)]
    for obj in sources:
        if isinstance(obj, str):
            parts.append(obj)
            continue
        try:
            parts.append(inspect.getsource(obj))
        except (OSError, TypeError):
            return f"unsourced-{os.getpid()}-{time.time_ns()}"
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{_CACHE_LAYOUT}:{digest}"


def _load_cache(path: Path, root: Path) -> dict[str, Any]:
    """The stored payload, or {} when absent, unreadable, or written under another schema or repo."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != _code_fingerprint() or data.get("root") != str(root):
        return {}
    return data


def _store_cache(path: Path, root: Path, payload: dict[str, Any]) -> bool:
    """Atomic write (tmp + os.replace); a failed store is non-fatal and reports False."""
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps({"schema": _code_fingerprint(), "root": str(root), **payload}, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _cache_dir(worktree_root: Path) -> Path:
    base = registry_get(CACHE_KEY)
    return (Path(base) if base else worktree_root / ".coordinator-local" / "cache") / _CACHE_SUBDIR


def _cache_file(worktree_root: Path, kind: str) -> Path:
    tag = hashlib.sha1(str(worktree_root).encode("utf-8")).hexdigest()[:12]
    return _cache_dir(worktree_root) / f"{kind}-{tag}.json"


def _stat_key(entry: Any) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of a walked file, or None when it cannot be stat-ed."""
    try:
        st = entry.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


class _ScopeCache:
    """Per-file facts and the parsed distillation log, persisted between scope runs.

    ``facts(rel, key)`` returns the entry's mutable fact dict, empty when the stored
    (mtime_ns, size) differs. Nothing is written until ``save``."""

    def __init__(self, worktree_root: Path) -> None:
        self.root = worktree_root
        self.path = _cache_file(worktree_root, "facts")
        data = _load_cache(self.path, worktree_root)
        files = data.get("files")
        self.files: dict[str, list[Any]] = files if isinstance(files, dict) else {}
        log = data.get("log")
        self.log: dict[str, Any] = log if isinstance(log, dict) else {}
        self._stored = set(self.files)
        self._touched: set[str] = set()
        self._log_touched = False
        self.mutated = False

    def facts(self, rel: str, key: tuple[int, int]) -> dict[str, Any]:
        self._touched.add(rel)
        cur = self.files.get(rel)
        if (
            isinstance(cur, list)
            and len(cur) == 3
            and cur[0] == key[0]
            and cur[1] == key[1]
            and isinstance(cur[2], dict)
        ):
            return cur[2]
        fresh: dict[str, Any] = {}
        self.files[rel] = [key[0], key[1], fresh]
        self.mutated = True
        return fresh

    def fact(
        self,
        rel: str,
        entry: Any,
        name: str,
        compute: Callable[[], tuple[bool, Any]],
    ) -> Any:
        """Cached fact ``name`` of a walked file. ``compute`` returns ``(cacheable, value)``;
        an uncacheable value (a transient read failure) is returned, never stored."""
        key = _stat_key(entry)
        if key is None:
            return compute()[1]
        facts = self.facts(rel, key)
        if name in facts:
            return facts[name]
        cacheable, value = compute()
        if cacheable:
            facts[name] = value
            self.mutated = True
        return value

    def log_facts(self, log_full_path: Path) -> dict[str, Any]:
        """What the harvest-debt computation needs from the distillation log, cached by the
        log's (mtime_ns, size). Raises exactly what ``compute_harvest_debt`` raises for an
        absent or unparseable log."""
        if not log_full_path.exists():
            raise _harvest_debt.DistillationLogMissingError(
                f"canonical distillation log not found at {log_full_path} — refusing to "
                "compute harvest-debt against an absent log (an absent log is NOT "
                "'nothing harvested yet'; it is an unknown state and must fail loud)"
            )
        key = _stat_key(log_full_path)
        cur = self.log
        if key is None or cur.get("key") != list(key) or not all(k in cur for k in ("canonical", "legacy", "unparseable")):
            log_text = log_full_path.read_text()
            canonical = [
                row.path for row in parse_distillation_log(log_text) if row.disposition in _harvest_debt.HARVESTED_DISPOSITIONS
            ]
            legacy: list[str] = []
            for line in log_text.splitlines():
                match = _harvest_debt._ACTION_TABLE_ROW_RE.match(line)
                if match and match.group("action") in _harvest_debt.HARVESTED_DISPOSITIONS:
                    name = Path(match.group("path")).name
                    if name not in legacy:
                        legacy.append(name)
            cur = {
                "key": list(key) if key is not None else None,
                "canonical": canonical,
                "legacy": legacy,
                "unparseable": _harvest_debt._has_content_beyond_run_headers(log_text)
                and not _harvest_debt._has_any_parseable_row(log_text),
            }
            self.log = cur
            self.mutated = True
        self._log_touched = True
        if cur["unparseable"]:
            raise _harvest_debt.DistillationLogUnparseableError(
                f"distillation log at {log_full_path} has content but zero rows "
                "parsed from it (neither the canonical parser nor the legacy "
                "action-table scan matched a single row) — refusing to report "
                "harvest-debt against an unreadable log rather than silently "
                "treating it as zero-debt"
            )
        return cur

    def save(self) -> None:
        """Persist what changed: entries for files this run saw (deleted ones are pruned),
        minus those too recently modified to trust."""
        horizon = time.time_ns() - _RACY_WINDOW_NS
        keep = {rel for rel in self._touched if self.files[rel][0] < horizon}
        log_key = self.log.get("key")
        log_storable = self._log_touched and bool(log_key) and log_key[0] < horizon
        if not self.mutated and keep == self._stored:
            return
        _store_cache(
            self.path,
            self.root,
            {"files": {rel: self.files[rel] for rel in keep}, "log": self.log if log_storable else {}},
        )


def _walk_files(root: Path, *, strict: bool) -> list[tuple[str, os.DirEntry]]:
    """Every regular file under ``root`` as ``(root-relative posix path, DirEntry)``, unordered.
    Symlinked directories are not descended. ``strict`` re-raises a directory that cannot be listed."""
    out: list[tuple[str, os.DirEntry]] = []
    stack: list[tuple[str, str]] = [(str(root), "")]
    while stack:
        path, prefix = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            if strict:
                raise
            continue
        for entry in entries:
            rel = prefix + entry.name
            if entry.is_dir(follow_symlinks=False):
                stack.append((entry.path, rel + "/"))
            elif entry.is_file():
                out.append((rel, entry))
    return out


class _PathEntry:
    """DirEntry stand-in for a path found by ``Path.glob``."""

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self._path = path

    def stat(self) -> os.stat_result:
        return self._path.stat()


#: ``<literal dir>/**/*.md``: the cohort glob shape walked with scandir instead of Path.glob.
_COHORT_DIR_GLOB_RE = re.compile(r"^((?:[^*?\[\]/]+/)*[^*?\[\]/]+)/\*\*/\*\.md$")


def _cohort_files(worktree_root: Path, spec: CohortSpec) -> list[tuple[str, Any]]:
    """Worktree-relative ``(path, entry)`` for every file ``spec.glob`` matches, unordered."""
    match = _COHORT_DIR_GLOB_RE.match(spec.glob)
    if match is not None and not ({".", ".."} & set(match.group(1).split("/"))):
        prefix = match.group(1) + "/"
        return [
            (prefix + rel, entry)
            for rel, entry in _walk_files(worktree_root / match.group(1), strict=False)
            if entry.name.endswith(".md")
        ]
    return [
        (rel_id(candidate, worktree_root), _PathEntry(candidate))
        for candidate in worktree_root.glob(spec.glob)
        if candidate.is_file()
    ]


def _filter_fact(cache: _ScopeCache, filter_name: str, rel: str, entry: Any) -> bool:
    """A cohort filter's verdict on one file; the cached fact is named for the filter."""
    fn = _COHORT_FILTERS[filter_name]
    return bool(
        cache.fact(rel, entry, filter_name, lambda: (True, fn(Path(entry.path).read_text(encoding="utf-8"))))
    )


def _is_ripe(cache: _ScopeCache, rel: str, entry: Any) -> bool:
    """True when the file's ``status:`` is harvest-ripe; an unreadable file is not ripe."""

    def compute() -> tuple[bool, bool]:
        try:
            return True, bool(_ripe_filter._classify_one(Path(entry.path))[0])
        except UnicodeDecodeError:
            return True, False
        except OSError:
            return False, False

    return bool(cache.fact(rel, entry, "ripe", compute))


def _head_fate_fact(cache: _ScopeCache, rel: str, entry: Any) -> str | None:
    def compute() -> tuple[bool, str | None]:
        readable, fate = _head_fate(entry.path)
        return readable, fate

    return cache.fact(rel, entry, "fate", compute)


#: A date-prefixed stem and a plan/spec path in prose: the two shapes a log, guide or decision uses to name a plan.
_CITE_STEM_RE = re.compile(r"\d{4}-\d{2}-\d{2}-[A-Za-z0-9][A-Za-z0-9_-]*")
_CITE_PATH_RE = re.compile(r"(?:docs/plans|archive/specs|archive/completed)/[\w./-]+?\.md")


def _cite_keys(text: str) -> list[str]:
    """Slugified stems of every plan, spec or completed file ``text`` names, sorted. A stem is
    the join key: one citation, however written, is one key."""
    keys = {slugify_stem(m.rstrip("-_")) for m in _CITE_STEM_RE.findall(text)}
    keys.update(slugify_stem(m.rsplit("/", 1)[-1][: -len(".md")]) for m in _CITE_PATH_RE.findall(text))
    keys.discard("")
    return sorted(keys)


def _cites_fact(cache: _ScopeCache, rel: str, entry: Any) -> list[str]:
    def compute() -> tuple[bool, list[str]]:
        try:
            return True, _cite_keys(Path(entry.path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return False, []

    return cache.fact(rel, entry, "cites", compute)


def _is_md(name: str) -> bool:
    return os.path.splitext(name)[1] == ".md"


def _undebted_logs(log: dict[str, Any], members: list[str]) -> list[str]:
    """``members`` the distillation log has not credited: an exact canonical row, or a legacy
    action-table row whose basename names exactly one member."""
    harvested = set(log["canonical"])
    by_name: dict[str, list[str]] = {}
    for rel in members:
        by_name.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    for name in log["legacy"]:
        matches = by_name.get(name, [])
        if len(matches) == 1:
            harvested.add(matches[0])
    return [rel for rel in members if rel not in harvested]


def _debt(log: dict[str, Any], dir_path: Path, files: set[str]) -> _harvest_debt.HarvestDebtResult:
    """``compute_harvest_debt`` over a walked tree and the cached log facts: ``files`` are the
    dir-relative non-sidecar paths."""
    harvested = {_harvest_debt._row_path_relative_to_specs_dir(p, dir_path) for p in log["canonical"]}
    if log["legacy"]:
        by_name: dict[str, list[str]] = {}
        for rel in files:
            by_name.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
        for name in log["legacy"]:
            matches = by_name.get(name, [])
            if len(matches) == 1:
                harvested.add(matches[0])
    debt = sorted(files - harvested)
    warn = len(harvested) == 0 and len(debt) > 0
    if not warn and len(harvested) > 0:
        warn = len(debt) > len(harvested) * _harvest_debt._WARN_RATIO
    return _harvest_debt.HarvestDebtResult(
        harvest_debt=debt, harvested_count=len(harvested), total_specs=len(files), warn=warn
    )


@dataclass(frozen=True)
class _Cohorts:
    harvest: list[str]
    skip: list[str]
    plans: list[str]
    sidecars: list[str]
    resolved_cohort_specs: list[CohortSpec]
    data_driven: dict[str, list[str]]
    debt_result: _harvest_debt.HarvestDebtResult
    #: Every cohort member's walked entry, keyed by worktree-relative path.
    entries: dict[str, Any]

    @property
    def harvest_mode_names(self) -> list[str]:
        return [s.name for s in self.resolved_cohort_specs if s.mode == "harvest"]


def _gather_cohorts(
    worktree_root: Path,
    cache: _ScopeCache,
    *,
    specs_dir: str,
    plans_dir: str,
    handoffs_dir: str,
    memos_dir: str | None,
    log_path: str,
    cohort_specs: list[CohortSpec] | None,
) -> _Cohorts:
    """The cohort scans compute_scope assembles: one walk per tree, facts from ``cache``."""
    specs_path = worktree_root / specs_dir
    plans_path = worktree_root / plans_dir
    entries: dict[str, Any] = {}

    # Cohort paths are worktree-relative everywhere in the manifest: the specs/plans trees are
    # walked tree-relative and re-keyed here so a consumer never needs to know which base a cohort uses.
    specs_dir_posix = Path(specs_dir).as_posix()
    specs_files = _walk_files(specs_path, strict=True) if specs_path.is_dir() else []
    log = cache.log_facts(worktree_root / log_path)

    sidecar_rels: list[str] = []
    specs_all: set[str] = set()
    specs_md: list[tuple[str, Any]] = []
    for rel, entry in specs_files:
        if is_sidecar_filename(entry.name):
            sidecar_rels.append(rel)
            continue
        specs_all.add(rel)
        if _is_md(entry.name):
            specs_md.append((rel, entry))
    debt_result = _debt(log, specs_path, specs_all)
    debt = set(debt_result.harvest_debt)
    harvest_cohort: list[str] = []
    skip_cohort: list[str] = []
    for rel, entry in specs_md:
        full = f"{specs_dir_posix}/{rel}"
        if _is_ripe(cache, full, entry):
            if rel in debt:
                harvest_cohort.append(full)
                entries[full] = entry
        else:
            skip_cohort.append(full)
    harvest_cohort.sort()
    skip_cohort.sort()

    sidecar_cohort: list[str] = []
    for rel in sidecar_rels:
        candidate = specs_path / rel
        try:
            sidecar_cohort.append(rel_id(candidate, worktree_root))
        except ValueError:
            sidecar_cohort.append(candidate.as_posix())
    sidecar_cohort.sort()

    plans_cohort: list[str] = []
    if plans_path.is_dir():
        plans_dir_posix = Path(plans_dir).as_posix()
        plans_files = _walk_files(plans_path, strict=True)
        plans_all = {rel for rel, entry in plans_files if not is_sidecar_filename(entry.name)}
        plans_debt = set(_debt(log, plans_path, plans_all).harvest_debt)
        for rel, entry in plans_files:
            if rel not in plans_debt or not _is_md(entry.name):
                continue
            if {"sidecars", "__pycache__"} & set(rel.split("/")[:-1]):
                continue
            full = f"{plans_dir_posix}/{rel}"
            if _is_ripe(cache, full, entry):
                plans_cohort.append(full)
                entries[full] = entry
        plans_cohort.sort()

    memo_archive = _memo_archive_reldir(worktree_root)
    resolved = (
        [_rebase_memo_glob(spec, memo_archive) for spec in cohort_specs]
        if cohort_specs is not None
        else _default_cohort_specs(handoffs_dir, memos_dir if memos_dir is not None else memo_archive)
    )
    data_driven: dict[str, list[str]] = {}
    for spec in resolved:
        members = data_driven.setdefault(spec.name, [])
        for rel, entry in _cohort_files(worktree_root, spec):
            if spec.name == LOGS_COHORT and rel.rsplit("/", 1)[-1].startswith(_LOG_NON_ENTRY_PREFIX):
                continue
            if spec.filter is not None and not _filter_fact(cache, spec.filter, rel, entry):
                continue
            members.append(rel)
            entries[rel] = entry
    for name, members in data_driven.items():
        data_driven[name] = sorted(set(members))
    if LOGS_COHORT in data_driven:
        data_driven[LOGS_COHORT] = _undebted_logs(log, data_driven[LOGS_COHORT])
    return _Cohorts(
        harvest=harvest_cohort,
        skip=skip_cohort,
        plans=plans_cohort,
        sidecars=sidecar_cohort,
        resolved_cohort_specs=resolved,
        data_driven=data_driven,
        debt_result=debt_result,
        entries=entries,
    )


def _split_scannable(
    cache: _ScopeCache, gathered: _Cohorts
) -> tuple[set[str], list[dict[str, str]], dict[str, str]]:
    """(scannable paths, mechanical rows, cohort of every candidate): the harvest-mode cohort
    union, minus the rows whose fate frontmatter settles without a reading agent."""
    scannable_set = set(gathered.harvest) | set(gathered.plans)
    cohort_of: dict[str, str] = {p: "harvest" for p in gathered.harvest}
    for p in gathered.plans:
        cohort_of.setdefault(p, PLANS_COHORT)
    for name in gathered.harvest_mode_names:
        scannable_set |= set(gathered.data_driven[name])
        for p in gathered.data_driven[name]:
            cohort_of.setdefault(p, name)
    mechanical: list[dict[str, str]] = []
    for rel in sorted(scannable_set):
        fate = _head_fate_fact(cache, rel, gathered.entries[rel])
        if fate is not None:
            mechanical.append({"path": rel, "cohort": cohort_of[rel], "mechanical_fate": fate})
            scannable_set.discard(rel)
    return scannable_set, mechanical, cohort_of


@dataclass(frozen=True)
class _Admitted:
    #: Admitted paths in admission order: logs newest first, cited plans by citation count, the rest by date.
    ranked: list[str]
    not_admitted: list[dict[str, str]]
    #: Plans, specs and completed files an unharvested log cites, before the weight and capture filters.
    log_cited_count: int


def _captured_keys(cache: _ScopeCache, worktree_root: Path, wiki_dirs: list[str]) -> set[str]:
    """Stems of every plan, spec or completed file a wiki guide or decision record already names."""
    files: list[tuple[str, Any]] = []
    for wiki_dir in wiki_dirs:
        wiki_path = worktree_root / wiki_dir
        if wiki_path.is_dir():
            files.extend(
                (rel_id(md, worktree_root), _PathEntry(md))
                for md in _wiki_md_files(wiki_path)
                if md.name != "_index.md" and not md.name.startswith(".")
            )
    files.extend(
        (f"{_DECISIONS_DIR}/{rel}", entry)
        for rel, entry in _walk_files(worktree_root / _DECISIONS_DIR, strict=False)
        if _is_md(entry.name)
    )
    captured: set[str] = set()
    for rel, entry in files:
        captured.update(_cites_fact(cache, rel, entry))
    return captured


def _admit(
    cache: _ScopeCache,
    gathered: _Cohorts,
    scannable_set: set[str],
    cohort_of: dict[str, str],
    captured: set[str],
) -> _Admitted:
    """Settle which candidates earn a reading agent. A gated file (spec, plan, completed) needs an
    unharvested log citing it and PLAN_ADMISSION_MIN_BYTES of weight; any non-log file a guide or
    decision already cites is captured. Logs are always admitted: they are the first target."""
    cite_counts: dict[str, int] = {}
    for rel in gathered.data_driven.get(LOGS_COHORT, []):
        for key in _cites_fact(cache, rel, gathered.entries[rel]):
            cite_counts[key] = cite_counts.get(key, 0) + 1
    logs: list[str] = []
    cited: list[tuple[int, int, tuple[str, str], str]] = []
    others: list[str] = []
    not_admitted: list[dict[str, str]] = []
    log_cited = 0
    for rel in sorted(scannable_set):
        cohort = cohort_of[rel]
        if cohort == LOGS_COHORT:
            logs.append(rel)
            continue
        stat = _stat_key(gathered.entries[rel])
        size = stat[1] if stat is not None else 0
        stem = slugify_stem(rel.rsplit("/", 1)[-1].removesuffix(".md"))
        gated = cohort in _GATED_COHORTS
        count = cite_counts.get(stem, 0) if gated else 0
        log_cited += bool(count)
        reason: str | None = None
        if gated and count == 0:
            reason = "not_log_cited"
        elif gated and size < _schema.PLAN_ADMISSION_MIN_BYTES:
            reason = "below_admission"
        elif stem in captured:
            reason = "already_captured"
        if reason is not None:
            not_admitted.append({"path": rel, "cohort": cohort, "reason": reason})
        elif gated:
            cited.append((-count, -size, _date_sort_key(rel), rel))
        else:
            others.append(rel)
    ranked = (
        sorted(logs, key=_date_sort_key, reverse=True)
        + [row[3] for row in sorted(cited)]
        + sorted(others, key=_date_sort_key)
    )
    return _Admitted(ranked=ranked, not_admitted=not_admitted, log_cited_count=log_cited)


def _cohort_excluded_count(
    worktree_root: Path, gathered: _Cohorts, handoffs_dir: str, memos_dir: str | None
) -> int:
    """Files of the legacy default cohorts (every handoff, every memo) no resolved cohort took: a
    count only, never listed."""
    memos = memos_dir if memos_dir is not None else _memo_archive_reldir(worktree_root)
    return sum(
        1
        for spec in _default_cohort_specs(handoffs_dir, memos)
        for rel, _entry in _cohort_files(worktree_root, spec)
        if rel not in gathered.entries
    )


def _with_logs(specs: list[CohortSpec] | None) -> list[CohortSpec]:
    """The cohort set an admitting run scans: ``specs`` (the PM-ruling set when omitted) with the
    logs cohort first, as every plan citation comes from it."""
    base = list(PM_RULING_2026_08_06_COHORT_SPECS) if specs is None else list(specs)
    if any(spec.name == LOGS_COHORT for spec in base):
        return base
    return [*LOGS_COHORT_SPECS, *base]


class _DigestTextCache:
    """Digest text per file under its (mtime_ns, size), in a file of its own so a run that
    rewrites nothing never loads it."""

    def __init__(self, worktree_root: Path) -> None:
        self.root = worktree_root
        self.path = _cache_file(worktree_root, "digests")
        self._text: dict[str, list[Any]] | None = None
        self._used: set[str] = set()
        self._stored: set[str] = set()
        self._dirty = False

    def _load(self) -> dict[str, list[Any]]:
        if self._text is None:
            text = _load_cache(self.path, self.root).get("text")
            self._text = text if isinstance(text, dict) else {}
            self._stored = set(self._text)
        return self._text

    def get(self, rel: str, key: tuple[int, int]) -> str | None:
        cur = self._load().get(rel)
        if isinstance(cur, list) and len(cur) == 3 and cur[0] == key[0] and cur[1] == key[1] and isinstance(cur[2], str):
            self._used.add(rel)
            return cur[2]
        return None

    def put(self, rel: str, key: tuple[int, int], text: str) -> None:
        self._load()[rel] = [key[0], key[1], text]
        self._used.add(rel)
        self._dirty = True

    def save(self) -> None:
        if self._text is None:
            return
        horizon = time.time_ns() - _RACY_WINDOW_NS
        keep = {rel for rel in self._used if self._text[rel][0] < horizon}
        if not self._dirty and keep == self._stored:
            return
        _store_cache(self.path, self.root, {"text": {rel: self._text[rel] for rel in keep}})


def _write_digest_file(digest_dir: Path, name: str, body: bytes) -> bool:
    fd, tmp = tempfile.mkstemp(dir=str(digest_dir), suffix=".tmp")
    try:
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(tmp, str(digest_dir / name))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def _write_digests(
    worktree_root: Path,
    cache: _ScopeCache,
    run_dir_rel: str,
    run_id: str,
    scannable: list[str],
    entries: dict[str, Any],
    keys: dict[str, tuple[int, int]],
) -> dict[str, tuple[str, int]]:
    """``scan_digest.write_digests`` over the cache: ``{rel_path: (digest_rel_path, bytes)}``.

    A file whose digest this run id already wrote, still on disk with the stat it was written
    with, is not rewritten; a stat hit never re-reads the source, a miss builds the same
    digest ``write_digests`` would. A file that cannot be read is omitted."""
    digest_dir = worktree_root / run_dir_rel / _scan_digest.DIGEST_DIRNAME
    digest_dir.mkdir(parents=True, exist_ok=True)
    texts = _DigestTextCache(worktree_root)
    out: dict[str, tuple[str, int]] = {}
    for rel in scannable:
        name = _scan_digest.digest_name(rel)
        digest_rel = f"{run_dir_rel}/{_scan_digest.DIGEST_DIRNAME}/{name}"
        key = keys.get(rel)
        facts = cache.facts(rel, key) if key is not None else {}
        size = facts.get("digest_bytes")
        written = facts.get("written")
        if isinstance(size, int) and isinstance(written, list) and written[:1] == [run_id]:
            try:
                st = os.stat(digest_dir / name)
            except OSError:
                st = None
            if st is not None and [st.st_mtime_ns, st.st_size] == written[1:] and st.st_size == size:
                out[rel] = (digest_rel, size)
                continue
        body: bytes | None = None
        if key is not None:
            text = texts.get(rel, key)
            body = text.encode("utf-8") if text is not None else None
        if body is None:
            try:
                source = Path(entries[rel].path).read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            digest_text = _scan_digest.build_digest(source)
            body = digest_text.encode("utf-8")
            if key is not None:
                texts.put(rel, key, digest_text)
        if not _write_digest_file(digest_dir, name, body):
            continue
        out[rel] = (digest_rel, len(body))
        if key is not None:
            try:
                st = os.stat(digest_dir / name)
            except OSError:
                continue
            facts["digest_bytes"] = len(body)
            facts["written"] = [run_id, st.st_mtime_ns, st.st_size]
            cache.mutated = True
    texts.save()
    return out


def scannable_count(
    worktree_root: Path,
    *,
    specs_dir: str = "archive/specs",
    plans_dir: str = "docs/plans",
    handoffs_dir: str = "archive/handoffs",
    memos_dir: str | None = None,
    log_path: str = "state/distillation-log.md",
    cohort_specs: list[CohortSpec] | None = None,
    cap: int | None = None,
) -> int:
    """How many files an admitting distill.scope run owes a reading agent: scanned plus deferred,
    before the batch budget. ``cohort_specs`` omitted means PM_RULING_2026_08_06_COHORT_SPECS; the
    logs cohort is always scanned first. Equals ``compute_scope(..., admit=True)`` ``counts["scannable"]
    + counts["deferred"]`` on the same tree. ``cap`` clamps the result.
    Raises DistillationLogMissingError when ``log_path`` is absent, as compute_scope does."""
    cache = _ScopeCache(worktree_root)
    gathered = _gather_cohorts(
        worktree_root,
        cache,
        specs_dir=specs_dir,
        plans_dir=plans_dir,
        handoffs_dir=handoffs_dir,
        memos_dir=memos_dir,
        log_path=log_path,
        cohort_specs=_with_logs(cohort_specs),
    )
    scannable_set, _mechanical, cohort_of = _split_scannable(cache, gathered)
    admitted = _admit(
        cache, gathered, scannable_set, cohort_of, _captured_keys(cache, worktree_root, _default_wiki_dirs(worktree_root))
    )
    cache.save()
    return len(admitted.ranked) if cap is None else min(len(admitted.ranked), cap)


#: The run kind under which distill.scope's coverage watermark is stored.
WATERMARK_KIND = "distill"


def _wm_key(rel_path: str) -> str:
    """``_date_sort_key`` as one string whose ``str`` order equals the tuple order (a space sorts
    before every digit, so undated files lead): the form ``run_watermark.select`` and the cursor use."""
    date_prefix, path = _date_sort_key(rel_path)
    return f"{date_prefix} {path}"


@dataclass(frozen=True)
class _WatermarkPlan:
    wm: _run_watermark.Watermark | None
    changed: _run_watermark.ChangedSet
    candidates: list[str]
    rel_of: dict[str, str]
    budget: int
    head: str
    scoped: set[str]
    proposed: _run_watermark.Watermark
    window: frozenset[str]

    def pack_order(self, rels: list[str]) -> list[str]:
        """Backlog-slice files first in date order, changed extras after: byte-packing overflow
        then trims the newest slice files (the cursor holds before them) or the extras (the base
        holds), so a run always advances."""
        return sorted(rels, key=lambda rel: (_wm_key(rel) not in self.window, _wm_key(rel)))


def _backlog_window(wm: _run_watermark.Watermark | None, candidates: list[str], budget: int) -> list[str]:
    start = bisect.bisect_right(candidates, wm.cursor) if wm and wm.cursor else 0
    return candidates[start : start + max(budget, 0)]


def _plan_watermark(worktree_root: Path, ranked: list[str], budget: int) -> _WatermarkPlan | None:
    """The delta plus next backlog slice over the admitted candidates. None (scope unrestricted,
    nothing staged) when HEAD is unresolvable or no delta source answers."""
    head = _head_sha(worktree_root)
    if head is None:
        return None
    wm = _run_watermark.load(worktree_root, WATERMARK_KIND)
    rel_of = {_wm_key(rel): rel for rel in ranked}
    candidates = sorted(rel_of)
    scope_dirs = sorted({"/".join(rel.split("/")[:2]) for rel in ranked if not rel.startswith(("/", ".."))})
    try:
        changed_paths = _run_watermark.changed_since(worktree_root, wm, scope_dirs=scope_dirs)
    except _run_watermark.ChangedSinceError:
        return None
    changed = _run_watermark.ChangedSet(_wm_key(rel) for rel in changed_paths if _wm_key(rel) in rel_of)
    changed.generation = changed_paths.generation
    scoped_keys, proposed = _run_watermark.select(
        candidates, wm, changed, budget, kind=WATERMARK_KIND, head_sha=head
    )
    window = frozenset(_backlog_window(wm, candidates, budget))
    return _WatermarkPlan(
        wm, changed, candidates, rel_of, budget, head, {rel_of[k] for k in scoped_keys}, proposed, window
    )


def _settle_watermark(plan: _WatermarkPlan, overflow: list[str]) -> _run_watermark.Watermark:
    """The watermark to stage once byte-packing has deferred ``overflow`` files: the cursor stops
    before the first deferred backlog file, and a deferred changed file keeps the base so the next
    run selects it again."""
    if not overflow:
        return plan.proposed
    wm = plan.wm
    overflow_keys = {_wm_key(rel) for rel in overflow}
    window = _backlog_window(wm, plan.candidates, plan.budget)
    budget = next((i for i, key in enumerate(window) if key in overflow_keys), len(window))
    _scoped, proposed = _run_watermark.select(
        plan.candidates, wm, plan.changed, budget, kind=WATERMARK_KIND, head_sha=plan.head
    )
    if wm is not None and overflow_keys & set(plan.changed):
        proposed = replace(
            proposed, base_sha=wm.base_sha, base_generation=wm.base_generation, pass_complete=wm.pass_complete
        )
    return proposed


def compute_scope(
    worktree_root: Path,
    *,
    run_id: str,
    specs_dir: str = "archive/specs",
    plans_dir: str = "docs/plans",
    handoffs_dir: str = "archive/handoffs",
    memos_dir: str | None = None,
    log_path: str = "state/distillation-log.md",
    wiki_dirs: list[str] | None = None,
    batch_size: int = DEFAULT_BATCH_MAX_FILES,
    cohort_specs: list[CohortSpec] | None = None,
    byte_budget: int = SCAN_BATCH_BYTE_BUDGET,
    size_of: Callable[[str], int] | None = None,
    write_digests: bool = False,
    admit: bool = False,
    max_scan_batches: int | None = None,
) -> ScopeResult:
    """Compute the full scope-manifest for one distill.scope run.

    ``admit`` is the op's mode: the logs cohort is added first (``cohort_specs`` omitted means the
    PM-ruling set), a spec, plan or completed file is admitted only when an unharvested log cites it
    and it clears PLAN_ADMISSION_MIN_BYTES, a file a guide or decision already cites is captured,
    and batches past ``max_scan_batches`` are deferred. ``max_scan_batches`` omitted is the count of
    batches that fit the ``distill`` run budget (``run_budget.fit_units``, at least one): an
    over-budget backlog narrows and defers, never refuses or shards. Each file settles into exactly one of
    batches, ``mechanical``, ``not_admitted`` or ``deferred``; the manifest carries the last two and
    ``counts["scannable"]`` is the scanned count. Off (the library default), scope is unadmitted.
    Admitting also applies the ``distill`` run watermark (``run_watermark``): only files changed
    since its base plus the next backlog slice are scanned, the rest settle as ``deferred``, and
    ``ScopeResult.watermark`` is the proposal a commit point promotes.

    All directory params are worktree_root-relative. wiki_dirs defaults to
    ["docs/wiki"] plus "coordinator/docs/wiki" and "docs/guides" when those exist on disk
    (repo-shape-adaptive; a single-tree repo gets just ["docs/wiki"]).

    ``cohort_specs`` (CohortSpec rows, DATA-DRIVEN per the 2026-08-06 PM
    ruling) governs every cohort BEYOND the fixed harvest/skip/sidecars core
    (DEC-5 business logic, never config-expressible). Omitted (None) —
    the default — reproduces the historical hardcoded handoffs+memos
    behavior byte-for-byte (see _default_cohort_specs), so
    specs_dir/handoffs_dir/memos_dir keep retargeting the scan exactly as
    before for callers that never opt into cohort_specs. Supplied — e.g.
    PM_RULING_2026_08_06_COHORT_SPECS — handoffs_dir/memos_dir are IGNORED
    (each row carries its own glob); specs_dir still governs the harvest/skip
    core, which is never a cohort_specs row. A "check-only" mode row is
    scanned and reported under ``cohorts[name]`` but never enters
    ``scannable``/``batches`` — it needs a deletability check, not a harvest.

    Batches are packed chronologically under ``byte_budget`` (``size_of(rel_path)``
    bytes per file, default the on-disk size) with ``batch_size`` as a hard file-count
    cap; see ``pack_batches``. With ``write_digests`` each scannable file's scan digest is
    written under the run directory first, batches are packed by digest bytes, and
    ``ScopeResult.digests`` maps each digested path to its worktree-relative digest path.

    Fails loud (propagates DistillationLogMissingError) when log_path is absent,
    per harvest_debt.compute_harvest_debt's own contract — an absent log is never
    silently treated as "harvest everything."
    """
    cache = _ScopeCache(worktree_root)
    gathered = _gather_cohorts(
        worktree_root,
        cache,
        specs_dir=specs_dir,
        plans_dir=plans_dir,
        handoffs_dir=handoffs_dir,
        memos_dir=memos_dir,
        log_path=log_path,
        cohort_specs=_with_logs(cohort_specs) if admit else cohort_specs,
    )
    harvest_cohort = gathered.harvest
    skip_cohort = gathered.skip
    plans_cohort = gathered.plans
    debt_result = gathered.debt_result
    resolved_cohort_specs = gathered.resolved_cohort_specs
    data_driven_cohorts = gathered.data_driven

    sidecar_cohort = gathered.sidecars

    resolved_wiki_dirs = list(wiki_dirs) if wiki_dirs is not None else _default_wiki_dirs(worktree_root)
    wiki_slugs_map: dict[str, str] = {}
    for wiki_dir in resolved_wiki_dirs:
        wiki_path = worktree_root / wiki_dir
        if not wiki_path.is_dir():
            continue
        for md_file in _wiki_md_files(wiki_path):
            if md_file.name == "_index.md" or md_file.name.startswith("."):
                continue
            slug = slugify_stem(md_file.stem)
            wiki_slugs_map.setdefault(slug, rel_id(md_file, worktree_root))
    # C9's manifest_schema.make_scope_manifest types wiki_slugs as list[str] (its
    # own schema — not this chunk's surface to redefine); the "flat index:
    # slugified filename-stem -> existing file" the plan specifies is preserved
    # by emitting one {"slug", "path"} dict per entry, ordered first-seen across
    # wiki_dirs (validate_scope_manifest checks list-ness only, not element shape,
    # so this is schema-conformant, not a workaround). wiki_slugs_as_dict()
    # reconstructs the mapping for any in-process consumer.
    wiki_slugs = [{"slug": slug, "path": path} for slug, path in wiki_slugs_map.items()]

    scannable_set, mechanical, cohort_of = _split_scannable(cache, gathered)
    admitted: _Admitted | None = None
    wm_plan: _WatermarkPlan | None = None
    out_of_scope: list[str] = []
    fixed = run_fixed_cost(wiki_slugs_map)
    if admit:
        admitted = _admit(
            cache, gathered, scannable_set, cohort_of, _captured_keys(cache, worktree_root, resolved_wiki_dirs)
        )
        scannable = admitted.ranked
        if max_scan_batches is None:
            max_scan_batches = _run_budget.fit_units(
                _run_budget.load("distill"), fixed, scan_batch_cost(), len(scannable)
            )
        wm_plan = _plan_watermark(worktree_root, scannable, max_scan_batches * batch_size)
        if wm_plan is not None:
            out_of_scope = [rel for rel in scannable if rel not in wm_plan.scoped]
            scannable = wm_plan.pack_order([rel for rel in scannable if rel in wm_plan.scoped])
    else:
        scannable = sorted(scannable_set, key=_date_sort_key)
    keys: dict[str, tuple[int, int]] = {}
    for rel in scannable:
        key = _stat_key(gathered.entries[rel])
        if key is not None:
            keys[rel] = key
    digests: dict[str, str] = {}
    digest_bytes: dict[str, int] = {}
    if write_digests:
        written = _write_digests(worktree_root, cache, f"{RUN_DIR_REL}/{run_id}", run_id, scannable, gathered.entries, keys)
        digests = {rel: path for rel, (path, _n) in written.items()}
        digest_bytes = {rel: n for rel, (_p, n) in written.items()}
    cache.save()
    batches = pack_batches(
        scannable,
        size_of=size_of
        if size_of is not None
        else lambda rel: digest_bytes[rel] if rel in digest_bytes else (keys[rel][1] if rel in keys else 0),
        byte_budget=byte_budget,
        max_files=batch_size,
        entry_cost_of=lambda rel: len(rel) + 4 + (len(rel) + len(digests[rel]) + 8 if rel in digests else 0),
    )
    deferred: list[dict[str, str]] | None = None
    overflow: list[str] = []
    if admitted is not None:
        overflow = [rel for batch in batches[max_scan_batches:] for rel in batch]
        batches = batches[:max_scan_batches]
        deferred = [{"path": rel, "cohort": cohort_of[rel]} for rel in out_of_scope + overflow]
        scannable = [rel for batch in batches for rel in batch]
        kept = set(scannable)
        digests = {rel: path for rel, path in digests.items() if rel in kept}
        keys = {rel: key for rel, key in keys.items() if rel in kept}

    cohorts = {
        "harvest": harvest_cohort,
        "skip": skip_cohort,
        "sidecars": sidecar_cohort,
        PLANS_COHORT: plans_cohort,
        **data_driven_cohorts,
    }
    cohort_modes = {
        "harvest": "harvest",
        "skip": "check-only",
        "sidecars": "check-only",
        PLANS_COHORT: "harvest",
    }
    cohort_modes.update({spec.name: spec.mode for spec in resolved_cohort_specs})

    manifest = _schema.make_scope_manifest(
        run_id=run_id,
        batches=batches,
        wiki_dirs=resolved_wiki_dirs,
        wiki_slugs=wiki_slugs,
        cohorts=cohorts,
        mechanical=mechanical,
        not_admitted=admitted.not_admitted if admitted is not None else None,
        deferred=deferred,
    )
    # cohort_modes is NOT part of manifest_schema's required scope-manifest
    # shape (that schema is C9's, not this chunk's surface to redefine) — an
    # open additive key, so a consumer can distinguish a "harvest" cohort
    # (batched, scanned) from a "check-only" one (reported, never batched —
    # the 2026-08-06 ruling's distinction for archived handoffs) without
    # re-deriving it from cohort_specs itself.
    manifest["cohort_modes"] = cohort_modes
    if admitted is not None:
        manifest["budget"] = budget_block(fixed, len(batches))

    counts = {
        "harvest": len(harvest_cohort),
        "skip": len(skip_cohort),
        "sidecars": len(sidecar_cohort),
        PLANS_COHORT: len(plans_cohort),
        "scannable": len(scannable),
        "batches": len(batches),
        "mechanical": len(mechanical),
        "harvest_debt_total": debt_result.total_specs,
        "harvest_debt_warn": int(debt_result.warn),
    }
    counts.update({name: len(paths) for name, paths in data_driven_cohorts.items()})
    if admitted is not None and deferred is not None:
        counts["not_admitted"] = len(admitted.not_admitted)
        counts["deferred"] = len(deferred)
        counts["log_cited_plan_count"] = admitted.log_cited_count
        if cohort_specs is not None:
            counts["cohort_excluded"] = _cohort_excluded_count(worktree_root, gathered, handoffs_dir, memos_dir)

    proposed = _settle_watermark(wm_plan, overflow) if wm_plan is not None else None
    settle = (lambda extra: _settle_watermark(wm_plan, overflow + extra)) if wm_plan is not None else None
    return ScopeResult(manifest=manifest, counts=counts, digests=digests, keys=keys, watermark=proposed, settle=settle)


def wiki_slugs_as_dict(manifest: dict[str, Any]) -> dict[str, str]:
    """Reconstruct the slug -> path mapping from a scope-manifest's wiki_slugs
    list-of-{"slug","path"}-dicts field (see compute_scope's wiki_slugs_map
    comment for why the manifest carries a list, not a dict, at C9's schema)."""
    return {row["slug"]: row["path"] for row in manifest["wiki_slugs"]}


def _wiki_md_files(wiki_path: Path) -> list[Path]:
    """``sorted(wiki_path.rglob("*.md"))`` from one scandir walk: every entry named ``*.md``,
    Path-ordered (component-wise), symlinked directories not descended."""
    found: list[Path] = []
    stack: list[tuple[str, tuple[str, ...]]] = [(str(wiki_path), ())]
    while stack:
        path, parts = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if entry.name.endswith(".md"):
                found.append(wiki_path.joinpath(*parts, entry.name))
            if entry.is_dir(follow_symlinks=False):
                stack.append((entry.path, (*parts, entry.name)))
    found.sort()
    return found


def _default_wiki_dirs(worktree_root: Path) -> list[str]:
    dirs = ["docs/wiki"]
    if (worktree_root / "coordinator" / "docs" / "wiki").is_dir():
        dirs.append("coordinator/docs/wiki")
    if (worktree_root / "docs" / "guides").is_dir():
        dirs.append("docs/guides")
    return dirs


_CANDIDATE_WIKI_DIRS: tuple[str, ...] = ("docs/wiki", "coordinator/docs/wiki", "docs/guides")


def empty_wiki_warning(worktree_root: Path, manifest: dict[str, Any]) -> str | None:
    """A loud warning when no wiki slugs were indexed although a candidate wiki/guides
    dir holds .md files; None otherwise. Zero slugs makes synth mint duplicates."""
    if manifest["wiki_slugs"]:
        return None
    populated = [
        d for d in _CANDIDATE_WIKI_DIRS
        if (worktree_root / d).is_dir() and any((worktree_root / d).rglob("*.md"))
    ]
    if not populated:
        return None
    return (
        f"WARNING: wiki_slugs_count is 0 but {populated} contain .md files; searched "
        f"wiki_dirs={manifest['wiki_dirs']}. Pass wiki_dirs explicitly or synth will "
        "mint duplicate guides."
    )


def write_scope_manifest(worktree_root: Path, manifest: dict[str, Any]) -> Path:
    """Write the scope-manifest to
    state/scratch/artifact-distillation/<run-id>/input.json, atomically
    (mkstemp + os.replace, DR-228 § D6(ii) create-or-full-rewrite-only —
    never a partial in-place edit of an existing artifact).

    Write-confined (DR-228 § D6(i)): only this run-id's own subdirectory under
    state/scratch/artifact-distillation/ is touched — never a sibling run-id's
    directory, never any other state/ path.
    """
    # Plain (insertion-order-preserving) serialization — NOT
    # manifest_schema.canonical_manifest_bytes(), which sort_keys=True
    # alphabetizes for the disposal-manifest's sha-stability contract (F3, a
    # concern this schema_version-only, no-stamp-field scope-manifest does not
    # share). schema_version stays the first on-disk key, per SCHEMA_VERSION's
    # own "always the first key written on disk" contract.
    return _write_run_file(worktree_root, manifest["run_id"], "input.json", manifest)


def workflow_input(manifest: dict[str, Any], worktree_root: Path) -> dict[str, Any]:
    """The Workflow's INPUT for this manifest, via distill.workflow_input's translator.

    Trap: the manifest's own shape is C9's pinned schema and is NOT what the
    Workflow reads — handing it the manifest yields zero batches, which the
    Workflow reports as "all-scan-agents-failed" (2026-10-12)."""
    # Deferred: distill_workflow_input imports this module.
    from coordinator_core.ops.distill_workflow_input import translate_to_workflow_input

    return translate_to_workflow_input(manifest, repo_root=worktree_root.as_posix())


def attach_digests(payload: dict[str, Any], digests: dict[str, str]) -> None:
    """Add ``digests`` ({original path: digest path}) to each batch that has digested files.
    ``files`` is untouched: the original path stays the join key for nuggets and fates."""
    for batch in payload["batches"]:
        mapped = {f: digests[f] for f in batch["files"] if f in digests}
        if mapped:
            batch["digests"] = mapped


_SCOPE_ONLY_FILE_RE = re.compile(
    r"^(?:input|workflow-input|batch-\d+|wikislugs-\d+)\.json$|^" + re.escape(_run_watermark.PENDING_NAME) + r"$|\.tmp$"
)


def _is_unconsumed_scope_dir(run_dir: Path) -> bool:
    """True when ``run_dir`` holds only what distill.scope itself wrote.

    Any other entry (disposal-manifest.json, fate-partition.json, a receipt, ...) means a
    downstream step has used the run, so the dir is kept. A dir without both input.json
    and workflow-input.json is not a scope dir at all (probe output, partial write)."""
    names = {entry.name for entry in os.scandir(run_dir)}
    if not {"input.json", "workflow-input.json"} <= names:
        return False
    return all(n == "digest" or _SCOPE_ONLY_FILE_RE.search(n) for n in names)


def _clear_downstream_files(run_dir: Path) -> None:
    """Delete everything in ``run_dir`` that distill.scope does not itself write, so a re-scope
    into a consumed run id leaves no stale disposal/partition artifacts beside the fresh input."""
    for entry in os.scandir(run_dir):
        if entry.name == "digest" or _SCOPE_ONLY_FILE_RE.search(entry.name):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except OSError:
            continue


# A scope dir looks unconsumed until invocation B writes curated.json, possibly hours after scope;
# only a dir untouched for this long is past any live run's window.
_RETIRE_MIN_AGE_S = 24 * 3600


def _newest_mtime(run_dir: Path) -> float:
    newest = run_dir.stat().st_mtime
    for entry in os.scandir(run_dir):
        newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
    return newest


def retire_prior_scope_dirs(worktree_root: Path, run_id: str) -> list[str]:
    """Remove every other unconsumed scope dir under RUN_DIR_REL so a
    repeat scope call leaves one copy; consumed runs stay. The ``run_id`` dir itself keeps its
    scope files but loses stale downstream artifacts a prior consumed run left in it.
    A dir touched within _RETIRE_MIN_AGE_S is kept: it may be a live run awaiting its B step.
    Trap: same-run_id clearing is not age-guarded; concurrent fires on one id rely on the
    autofire single-flight. A failed rmtree is skipped silently."""
    import time

    base = worktree_root / RUN_DIR_REL
    if not base.is_dir():
        return []
    cutoff = time.time() - _RETIRE_MIN_AGE_S
    removed: list[str] = []
    for entry in os.scandir(base):
        if entry.is_symlink() or not entry.is_dir():
            continue
        if entry.name == run_id:
            _clear_downstream_files(Path(entry.path))
            continue
        try:
            if _is_unconsumed_scope_dir(Path(entry.path)) and _newest_mtime(Path(entry.path)) < cutoff:
                shutil.rmtree(entry.path)
                removed.append(entry.name)
        except OSError:
            continue
    return removed


def _write_run_file(worktree_root: Path, run_id: str, name: str, obj: dict[str, Any]) -> Path:
    run_dir = worktree_root / RUN_DIR_REL / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / name
    body = json.dumps(obj, indent=2).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=str(run_dir), suffix=".tmp")
    try:
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(tmp_path, str(target))
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                # tmp file already gone (or the replace above already consumed it)
                pass
    return target


#: Fixed counts keys compute_scope always emits regardless of cohort_specs —
#: everything else in `counts` is a data-driven cohort name (see CohortSpec).
_FIXED_COUNT_KEYS = frozenset(
    {
        "harvest",
        "skip",
        "sidecars",
        "scannable",
        "batches",
        "mechanical",
        "harvest_debt_total",
        "harvest_debt_warn",
        "not_admitted",
        "deferred",
        "log_cited_plan_count",
        "cohort_excluded",
    }
)


def render_summary(manifest: dict[str, Any], counts: dict[str, int]) -> str:
    """Render the short human scope summary (b) — stdout-shaped, never the
    ~25KB batch table itself (central-em #3).

    # The data-driven cohort names in `counts`
    # (beyond the fixed harvest/skip/sidecars/scannable/batches/harvest_debt_*
    # core — see `_FIXED_COUNT_KEYS`) are rendered generically here, sorted by
    # name, instead of hardcoding `handoffs=`/`memos=`. Hardcoding those two
    # names defended by `.get(..., 0)` silently printed "handoffs=0 memos=0"
    # for a caller supplying a fully custom `cohort_specs` list with different
    # cohort names — indistinguishable from "zero handoffs/memos exist" when
    # the real story is "this run didn't use those cohort names at all", and
    # the actually-configured cohorts never appeared in the summary line.
    """
    dynamic_names = sorted(name for name in counts if name not in _FIXED_COUNT_KEYS)
    dynamic_part = " ".join(f"{name}={counts[name]}" for name in dynamic_names)
    lines = [
        f"distill.scope run {manifest['run_id']}: "
        f"{counts['scannable']} scannable across {counts['batches']} batch(es), "
        f"{counts.get('mechanical', 0)} mechanical",
        f"  harvest={counts['harvest']} skip={counts['skip']} "
        f"sidecars={counts['sidecars']}" + (f" {dynamic_part}" if dynamic_part else ""),
        f"  harvest_debt_total={counts['harvest_debt_total']} "
        f"warn={'yes' if counts['harvest_debt_warn'] else 'no'}",
        f"  wiki_dirs={manifest['wiki_dirs']} wiki_slugs_count={len(manifest['wiki_slugs'])}",
    ]
    if "not_admitted" in counts:
        lines.insert(
            1,
            f"  not_admitted={counts['not_admitted']} deferred={counts.get('deferred', 0)} "
            f"log_cited_plans={counts.get('log_cited_plan_count', 0)} "
            f"cohort_excluded={counts.get('cohort_excluded', 0)}",
        )
    return "\n".join(lines)


def _guide_fingerprint(worktree_root: Path, wiki_slugs: dict[str, str], horizon: int) -> str | None:
    """Identity of the guide set: slugs, paths and stats. None when a guide changed too recently to trust."""
    h = hashlib.sha1()
    for slug in sorted(wiki_slugs):
        path = wiki_slugs[slug]
        try:
            st = os.stat(worktree_root / path)
        except OSError:
            h.update(f"{slug}\0{path}\0-\n".encode("utf-8"))
            continue
        if st.st_mtime_ns >= horizon:
            return None
        h.update(f"{slug}\0{path}\0{st.st_mtime_ns}\0{st.st_size}\n".encode("utf-8"))
    return h.hexdigest()


def _batch_key(batch: list[str], keys: dict[str, tuple[int, int]], horizon: int) -> str | None:
    """Identity of one batch's files and stats. None when a file lacks a stat or changed too recently."""
    h = hashlib.sha1()
    for rel in batch:
        key = keys.get(rel)
        if key is None or key[0] >= horizon:
            return None
        h.update(f"{rel}\0{key[0]}\0{key[1]}\n".encode("utf-8"))
    return h.hexdigest()


def _relevant_guides(
    worktree_root: Path,
    batches: list[list[str]],
    wiki_slugs: dict[str, str],
    keys: dict[str, tuple[int, int]],
) -> list[list[dict[str, Any]]]:
    """``guide_index.relevant_guides_for_batches`` with each batch's result cached under the
    stat identity of the batch's files and of every guide; only batches that miss are ranked."""
    horizon = time.time_ns() - _RACY_WINDOW_NS
    path = _cache_file(worktree_root, "guides")
    fingerprint = _guide_fingerprint(worktree_root, wiki_slugs, horizon)
    stored = _load_cache(path, worktree_root) if fingerprint is not None else {}
    held = stored.get("batches") if stored.get("guides") == fingerprint else None
    held = held if isinstance(held, dict) else {}
    batch_keys = [_batch_key(b, keys, horizon) if fingerprint is not None else None for b in batches]
    results: list[Any] = [None] * len(batches)
    misses: list[int] = []
    for i, bk in enumerate(batch_keys):
        hit = held.get(bk) if bk is not None else None
        if isinstance(hit, list):
            results[i] = hit
        else:
            misses.append(i)
    if misses:
        ranked = _guide_index.relevant_guides_for_batches(
            worktree_root, [batches[i] for i in misses], wiki_slugs
        )
        for i, lst in zip(misses, ranked):
            results[i] = lst
    current = {bk: results[i] for i, bk in enumerate(batch_keys) if bk is not None}
    if fingerprint is not None and (misses or set(current) != set(held)):
        _store_cache(path, worktree_root, {"guides": fingerprint, "batches": current})
    return results


@register_op("distill.scope")
def _handler(params: dict, repo_root: Path | None = None) -> dict:
    """distill.scope handler.

    Params:
        run_id (str, REQUIRED) — the Workflow-minted run id (YYYY-MM-DD-HHhMM), or any
            caller-supplied stable identifier. Never minted from wallclock inside this
            handler — determinism (AC1) requires the caller supply it explicitly.
        specs_dir, handoffs_dir, memos_dir, log_path (str, optional) — worktree-relative
            overrides for the four scanned trees / the canonical log.
        wiki_dirs (list[str], optional) — override the auto-detected wiki tree list.
        batch_size (int, optional, default DEFAULT_BATCH_MAX_FILES) — hard cap on files
            per batch; batches are otherwise packed under SCAN_BATCH_BYTE_BUDGET.
        cohort_specs (str "pm_ruling_2026_08_06", or list[dict], optional) — the
            2026-08-06 PM-ruling data-driven cohort set (see CohortSpec). The
            literal string "pm_ruling_2026_08_06" selects
            PM_RULING_2026_08_06_COHORT_SPECS without the caller hand-typing it
            over JSON-RPC; a list supplies raw {"name","glob","filter","mode"}
            row dicts instead. Omitted — the default — reproduces the
            pre-ruling hardcoded handoffs+memos behavior (see
            compute_scope/_default_cohort_specs).

    repo_root (injected by ipc.dispatch_message): git_common_dir of the originating
    worktree (_OP_KEY_SCOPE="common_dir"). Derives main_worktree_root(repo_root) — a
    linked worktree's .git is a file, so the raw common_dir must not be used directly.
    Fails loud when repo_root is None (no silent meta-repo fallback, matching
    artifact.emit's AC5 precedent).
    """
    if repo_root is None:
        raise ValueError(
            "distill.scope requires a per-repo dispatch key (_origin_worktree); "
            "repo_root is None — op scope must be 'common_dir' and _origin_worktree "
            "must be present in the JSON-RPC envelope. No silent fallback to meta-repo."
        )
    run_id = params.get("run_id")
    if not run_id:
        raise ValueError(
            "distill.scope requires an explicit run_id param — never minted from "
            "wallclock inside the handler (AC1 determinism; DEC-6/plan Execution notes)."
        )

    worktree_root = main_worktree_root(repo_root)
    # The op admits: omitted cohort_specs means the PM-ruling set, not the legacy handoffs+memos
    # set its docstring (frozen into op-catalog.json) still names.
    kwargs: dict[str, Any] = {"run_id": run_id, "cohort_specs": list(PM_RULING_2026_08_06_COHORT_SPECS)}
    for key in ("specs_dir", "plans_dir", "handoffs_dir", "memos_dir", "log_path", "wiki_dirs", "batch_size"):
        if key in params:
            kwargs[key] = params[key]

    if "cohort_specs" in params:
        raw_specs = params["cohort_specs"]
        if raw_specs == "pm_ruling_2026_08_06":
            kwargs["cohort_specs"] = list(PM_RULING_2026_08_06_COHORT_SPECS)
        elif isinstance(raw_specs, list):
            # A malformed row (typo'd/extra key) must
            # not leak a raw TypeError from CohortSpec's __init__ unpacking; wrap
            # it into the same friendly ValueError posture used for the
            # not-a-literal-string-or-list branch below, so a JSON-RPC caller
            # never sees a Python implementation detail at this boundary.
            cohort_specs: list[CohortSpec] = []
            for row in raw_specs:
                if not isinstance(row, dict):
                    raise ValueError(
                        "distill.scope cohort_specs row must be a "
                        f"{{name, glob, filter, mode}} dict; got {type(row).__name__}"
                    )
                try:
                    cohort_specs.append(CohortSpec(**row))
                except TypeError as exc:
                    raise ValueError(
                        f"distill.scope cohort_specs row {row!r} is malformed: {exc}"
                    ) from exc
            kwargs["cohort_specs"] = cohort_specs
        else:
            raise ValueError(
                "distill.scope cohort_specs param must be the literal string "
                "'pm_ruling_2026_08_06' or a list of {name, glob, filter, mode} "
                f"row dicts; got {type(raw_specs).__name__}"
            )

    retire_prior_scope_dirs(worktree_root, run_id)
    result = compute_scope(worktree_root, write_digests=True, admit=True, **kwargs)
    from coordinator_core.ops import backlog_pass_ledger
    from coordinator_core.ops.distill_workflow_input import attach_relevant_guides, shard_workflow_input

    manifest = result.manifest
    slugs = wiki_slugs_as_dict(manifest)
    fixed = run_fixed_cost(slugs)
    fitted = _run_budget.fit_units(_run_budget.load("distill"), fixed, scan_batch_cost(), 10**6)
    # Shards first, manifest last: a manifest on disk names only shards already written.
    payload = workflow_input(manifest, worktree_root)
    attach_digests(payload, result.digests)
    scoped = {f for b in payload["batches"] for f in b["files"]}
    # After digests: both share each batch file's 12KB budget. A tier split past the fitted
    # batch count defers its trailing batches.
    attach_relevant_guides(
        payload,
        _relevant_guides(worktree_root, manifest["batches"], slugs, result.keys),
        max_batches=max(fitted, len(manifest["batches"])),
    )
    kept = {f for b in payload["batches"] for f in b["files"]}
    dropped = sorted(scoped - kept)
    watermark = result.watermark
    if dropped:
        owner = {rel: name for name, rels in manifest["cohorts"].items() for rel in rels}
        manifest["batches"] = [g for g in ([f for f in b if f in kept] for b in manifest["batches"]) if g]
        manifest["deferred"] = [*manifest["deferred"], *({"path": rel, "cohort": owner.get(rel, "")} for rel in dropped)]
        result.counts["scannable"] -= len(dropped)
        result.counts["deferred"] += len(dropped)
        if result.settle is not None:
            watermark = result.settle(dropped)
    result.counts["batches"] = len(payload["batches"])
    manifest["budget"] = budget_block(fixed, len(payload["batches"]))
    written_path = write_scope_manifest(worktree_root, manifest)
    shard_manifest, shards = shard_workflow_input(payload)
    for shard_name, shard_body in shards.items():
        _write_run_file(worktree_root, run_id, shard_name, shard_body)
    workflow_path = _write_run_file(worktree_root, run_id, "workflow-input.json", shard_manifest)
    if watermark is not None:
        _run_watermark.stage(worktree_root / RUN_DIR_REL / run_id, watermark)
    backlog_pass_ledger.append(
        str(worktree_root),
        "distill",
        run_id,
        manifest["budget"]["projected"]["tokens"],
        result.counts["scannable"],
        result.counts["deferred"],
    )

    result_dict = {
        "run_id": run_id,
        "input_json_path": rel_id(written_path, worktree_root),
        # Pass THIS as distill-harvest.workflow.js's args.inputFile, not input_json_path.
        "workflow_input_path": rel_id(workflow_path, worktree_root),
        "counts": result.counts,
        "budget": manifest["budget"],
        "summary": render_summary(manifest, result.counts),
    }
    warning = empty_wiki_warning(worktree_root, result.manifest)
    if warning is not None:
        result_dict["warning"] = warning
        result_dict["summary"] += "\n  " + warning
    return result_dict
