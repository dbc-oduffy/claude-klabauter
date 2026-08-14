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
  - sidecars cohort = sidecar_sweep's active-reference-guard-cleared deletion
    candidates (cockpit §6.1 — pre-routed, never entering harvest/skip).
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
into fixed-size batches (default 30, the ~20-50-file target band's midpoint).

Negative-spec: performs no LLM call, no nugget extraction, no wiki-guide synthesis,
no reality-check-scout judgment, no contradiction adjudication, no PM disposal
decision — this op stops at classification/partition/batch-table emission (plan
§ Negative spec, AC12).

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C10
Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coordinator_core.distill import harvest_debt as _harvest_debt
from coordinator_core.distill import manifest_schema as _schema
from coordinator_core.distill import ripe_filter as _ripe_filter
from coordinator_core.distill.sidecar_sweep import sweep_sidecars
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
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
    "compute_scope",
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
    CohortSpec(name="week_changelog", glob="state/week-changelog/**/*.md", filter=None, mode="harvest"),
    CohortSpec(name="handoffs", glob="archive/handoffs/**/*.md", filter="handoff_resolution_gate", mode="check-only"),
)


def _scan_cohort(worktree_root: Path, spec: CohortSpec) -> list[str]:
    """Glob+filter one CohortSpec row; returns sorted worktree-relative paths."""
    filter_fn = _COHORT_FILTERS[spec.filter] if spec.filter is not None else None
    matches: list[str] = []
    for candidate in sorted(worktree_root.glob(spec.glob)):
        if not candidate.is_file():
            continue
        if filter_fn is not None:
            text = candidate.read_text(encoding="utf-8")
            if not filter_fn(text):
                continue
        matches.append(rel_id(candidate, worktree_root))
    return sorted(matches)


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


@dataclass(frozen=True)
class ScopeResult:
    """Result of compute_scope: the fully-assembled scope-manifest dict (C9 shape)
    plus the human-summary fields the CLI/caller renders separately from the
    manifest body (kept out of context per central-em #3)."""

    manifest: dict[str, Any]
    counts: dict[str, int]


def compute_scope(
    worktree_root: Path,
    *,
    run_id: str,
    specs_dir: str = "archive/specs",
    handoffs_dir: str = "archive/handoffs",
    memos_dir: str = "cross-repo/archive",
    log_path: str = "state/distillation-log.md",
    wiki_dirs: list[str] | None = None,
    batch_size: int = 30,
    cohort_specs: list[CohortSpec] | None = None,
) -> ScopeResult:
    """Compute the full scope-manifest for one distill.scope run.

    All directory params are worktree_root-relative. wiki_dirs defaults to
    ["docs/wiki"] plus "coordinator/docs/wiki" when the latter exists on disk
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

    Fails loud (propagates DistillationLogMissingError) when log_path is absent,
    per harvest_debt.compute_harvest_debt's own contract — an absent log is never
    silently treated as "harvest everything."
    """
    specs_path = worktree_root / specs_dir
    log_full_path = worktree_root / log_path

    ripe_result = _ripe_filter.scan_spec_dir(specs_path) if specs_path.is_dir() else _ripe_filter.RipeFilterResult(harvest=[], skip=[])
    debt_result = _harvest_debt.compute_harvest_debt(specs_path, log_full_path)

    # ripe_filter and harvest_debt both key their output specs_dir-relative
    # (their own documented convention); every OTHER cohort here (sidecars,
    # handoffs, memos) is worktree-relative via rel_id. Re-key to
    # worktree-relative here so every path in the emitted manifest shares one
    # convention — a consumer must not have to know which cohort uses which base.
    specs_dir_posix = Path(specs_dir).as_posix()
    harvest_cohort = sorted(
        f"{specs_dir_posix}/{p}"
        for p in (set(ripe_result.harvest) & set(debt_result.harvest_debt))
    )
    skip_cohort = sorted(f"{specs_dir_posix}/{r.path}" for r in ripe_result.skip)

    sidecar_result = sweep_sidecars(specs_path, worktree_root)
    sidecar_cohort = sorted(row["path"] for row in sidecar_result.deletion_manifest)
    sidecar_retained_cohort = sorted(row["path"] for row in sidecar_result.retained)

    resolved_cohort_specs = (
        list(cohort_specs)
        if cohort_specs is not None
        else _default_cohort_specs(handoffs_dir, memos_dir)
    )
    data_driven_cohorts: dict[str, list[str]] = {}
    harvest_mode_names: list[str] = []
    for spec in resolved_cohort_specs:
        data_driven_cohorts[spec.name] = _scan_cohort(worktree_root, spec)
        if spec.mode == "harvest":
            harvest_mode_names.append(spec.name)

    resolved_wiki_dirs = list(wiki_dirs) if wiki_dirs is not None else _default_wiki_dirs(worktree_root)
    wiki_slugs_map: dict[str, str] = {}
    for wiki_dir in resolved_wiki_dirs:
        wiki_path = worktree_root / wiki_dir
        if not wiki_path.is_dir():
            continue
        for md_file in sorted(wiki_path.glob("*.md")):
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

    scannable_set = set(harvest_cohort)
    for name in harvest_mode_names:
        scannable_set |= set(data_driven_cohorts[name])
    scannable = sorted(scannable_set, key=_date_sort_key)
    batches = [scannable[i : i + batch_size] for i in range(0, len(scannable), max(1, batch_size))]

    cohorts = {
        "harvest": harvest_cohort,
        "skip": skip_cohort,
        "sidecars": sidecar_cohort,
        "sidecars_retained": sidecar_retained_cohort,
        **data_driven_cohorts,
    }
    cohort_modes = {"harvest": "harvest", "skip": "check-only", "sidecars": "check-only"}
    cohort_modes.update({spec.name: spec.mode for spec in resolved_cohort_specs})

    manifest = _schema.make_scope_manifest(
        run_id=run_id,
        batches=batches,
        wiki_dirs=resolved_wiki_dirs,
        wiki_slugs=wiki_slugs,
        cohorts=cohorts,
    )
    # cohort_modes is NOT part of manifest_schema's required scope-manifest
    # shape (that schema is C9's, not this chunk's surface to redefine) — an
    # open additive key, so a consumer can distinguish a "harvest" cohort
    # (batched, scanned) from a "check-only" one (reported, never batched —
    # the 2026-08-06 ruling's distinction for archived handoffs) without
    # re-deriving it from cohort_specs itself.
    manifest["cohort_modes"] = cohort_modes

    counts = {
        "harvest": len(harvest_cohort),
        "skip": len(skip_cohort),
        "sidecars": len(sidecar_cohort),
        "scannable": len(scannable),
        "batches": len(batches),
        "harvest_debt_total": debt_result.total_specs,
        "harvest_debt_warn": int(debt_result.warn),
    }
    counts.update({name: len(paths) for name, paths in data_driven_cohorts.items()})

    return ScopeResult(manifest=manifest, counts=counts)


def wiki_slugs_as_dict(manifest: dict[str, Any]) -> dict[str, str]:
    """Reconstruct the slug -> path mapping from a scope-manifest's wiki_slugs
    list-of-{"slug","path"}-dicts field (see compute_scope's wiki_slugs_map
    comment for why the manifest carries a list, not a dict, at C9's schema)."""
    return {row["slug"]: row["path"] for row in manifest["wiki_slugs"]}


def _default_wiki_dirs(worktree_root: Path) -> list[str]:
    dirs = ["docs/wiki"]
    if (worktree_root / "coordinator" / "docs" / "wiki").is_dir():
        dirs.append("coordinator/docs/wiki")
    return dirs


def write_scope_manifest(worktree_root: Path, manifest: dict[str, Any]) -> Path:
    """Write the scope-manifest to
    state/scratch/artifact-distillation/<run-id>/input.json, atomically
    (mkstemp + os.replace, DR-228 § D6(ii) create-or-full-rewrite-only —
    never a partial in-place edit of an existing artifact).

    Write-confined (DR-228 § D6(i)): only this run-id's own subdirectory under
    state/scratch/artifact-distillation/ is touched — never a sibling run-id's
    directory, never any other state/ path.
    """
    run_dir = worktree_root / "state" / "scratch" / "artifact-distillation" / manifest["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "input.json"

    # Plain (insertion-order-preserving) serialization — NOT
    # manifest_schema.canonical_manifest_bytes(), which sort_keys=True
    # alphabetizes for the disposal-manifest's sha-stability contract (F3, a
    # concern this schema_version-only, no-stamp-field scope-manifest does not
    # share). schema_version stays the first on-disk key, per SCHEMA_VERSION's
    # own "always the first key written on disk" contract.
    body = json.dumps(manifest, indent=2).encode("utf-8")
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
                pass
    return target


#: Fixed counts keys compute_scope always emits regardless of cohort_specs —
#: everything else in `counts` is a data-driven cohort name (see CohortSpec).
_FIXED_COUNT_KEYS = frozenset(
    {"harvest", "skip", "sidecars", "scannable", "batches", "harvest_debt_total", "harvest_debt_warn"}
)


def render_summary(manifest: dict[str, Any], counts: dict[str, int]) -> str:
    """Render the short human scope summary (b) — stdout-shaped, never the
    ~25KB batch table itself (central-em #3).

    # Review: review-integrator — the data-driven cohort names in `counts`
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
        f"{counts['scannable']} scannable across {counts['batches']} batch(es)",
        f"  harvest={counts['harvest']} skip={counts['skip']} "
        f"sidecars={counts['sidecars']}" + (f" {dynamic_part}" if dynamic_part else ""),
        f"  harvest_debt_total={counts['harvest_debt_total']} "
        f"warn={'yes' if counts['harvest_debt_warn'] else 'no'}",
        f"  wiki_dirs={manifest['wiki_dirs']} wiki_slugs_count={len(manifest['wiki_slugs'])}",
    ]
    return "\n".join(lines)


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
        batch_size (int, optional, default 30) — chronological batch chunk size.
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
    kwargs: dict[str, Any] = {"run_id": run_id}
    for key in ("specs_dir", "handoffs_dir", "memos_dir", "log_path", "wiki_dirs", "batch_size"):
        if key in params:
            kwargs[key] = params[key]

    if "cohort_specs" in params:
        raw_specs = params["cohort_specs"]
        if raw_specs == "pm_ruling_2026_08_06":
            kwargs["cohort_specs"] = list(PM_RULING_2026_08_06_COHORT_SPECS)
        elif isinstance(raw_specs, list):
            # Review: review-integrator — a malformed row (typo'd/extra key) must
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

    result = compute_scope(worktree_root, **kwargs)
    written_path = write_scope_manifest(worktree_root, result.manifest)

    return {
        "run_id": run_id,
        "input_json_path": rel_id(written_path, worktree_root),
        "counts": result.counts,
        "summary": render_summary(result.manifest, result.counts),
    }
