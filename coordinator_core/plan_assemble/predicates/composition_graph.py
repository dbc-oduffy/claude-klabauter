"""
coordinator_core.plan_assemble.predicates.composition_graph — Layer 0 Branch C
(b): the `gates.composition.*` rows that answer "does this plan collide,
structurally or semantically, with other work on the same tree" — chunk
overlap within the plan itself (`:151`), rename/move drift against a cited
path's own history (`:156`), scope collision against sibling plans (`:160`),
and candidate assumption-text overlap against sibling plans (`:162`).

Each of the four public functions takes a `predicates.PredicateContext` and
returns a JSON-shaped dict, or `predicates.undetermined(reason)` when a
required input (`--plan`, a readable task-spine, a readable `docs/plans/`
directory) is absent.

Composition, not re-derivation — three of the four rows sit directly on top
of a shipped reader rather than re-solving a problem this repo already
solved:

  :151 chunk_overlap     — `coordinator_core.ops.dispatch_emit.spine_read.
                            read_spine` parses this plan's OWN task-spine into
                            `EmitterRow` objects exactly as the dispatch-emit
                            pipeline does. The REUSE is narrower than "wave_map
                            already over-covers this, re-emit from it": only
                            `wave_map._paths_overlap` (the pairwise path-
                            containment comparator) is reused — the pairwise
                            loop over this row's task-spine, and the report
                            shape (every overlapping pair, not a minimal
                            parallel-safe partition), are hand-rolled here.
                            No second intersection ALGORITHM, i.e. no second
                            path-containment comparator — but yes, a second
                            pairwise loop.
  :156 path_rename_or_move — walks the same task-spine's cited `writes`/
                            `reads` paths through `git log --follow
                            --diff-filter=R`, native rename detection, rather
                            than a hand-rolled path-similarity heuristic.
  :160 cross_plan_conflict — scans every `docs/plans/*.md` document's
                            declared write sites — frontmatter `scope:`, the
                            `## Tasks` spine's `writes:` keys (read by a line
                            scanner, NOT `read_spine` or `load_rows`; see the
                            helper's docstring for why a sibling's malformed
                            spine must not blank this answer, and for the
                            measured cost that rules the YAML parse out), and
                            backtick-cited paths in the
                            Acceptance Criteria body — for intersection with
                            this plan's own, via `_paths_overlap` again,
                            skipping `status ==
                            "closed"` siblings (same filter `:162` already
                            applies) — see the function's own docstring for
                            why. `ops/plan_match.
                            _collect_plans`'s own return shape is
                            `{id, title, text}` — `text` is the TITLE only,
                            deliberately (it exists to feed a fuzzy title
                            matcher), so it carries no `scope:` field this
                            row needs. Rather than adding a second,
                            independently-driftable frontmatter split-and-
                            `yaml.safe_load` parser alongside `_collect_
                            plans`'s, this row reuses `parse_frontmatter` —
                            the SAME shipped reader `PredicateContext.
                            from_paths` itself already uses for the plan
                            this context was built from — over the identical
                            `sorted(plans_dir.glob("*.md"))` enumeration
                            `_collect_plans` uses. Composition over the
                            reader primitive, not over a return shape that
                            structurally cannot carry the field this row
                            needs.
  :162 amends_assumption  — GENUINELY NEW; nothing in the fleet extracts or
                            text-matches assumption prose today. Reads this
                            plan's own `## Cross-plan coordination` table
                            (the same table shape this plan document itself
                            carries — see its "Assumption it carries"
                            column) and difflib-matches each row's assumption
                            text against every OTHER open plan's body.

Negative-spec:
  - Does NOT decide whether a flagged overlap/collision/candidate is a REAL
    problem. `:151`/`:156`/`:160` report mechanically-computed facts;
    `:162`'s `.candidate` is explicitly CANDIDATE EVIDENCE ONLY per the
    plan's Anti-scope — "is it really the same assumption" is a judgment
    call this module never makes, and no field here is named `.recommended`,
    `.verdict`, or `.fires` in a way that would resolve it (`:156`'s `.fires`
    is a mechanical git-rename-detection bit, not a judgment).
  - Does NOT re-run `wave_map.build_waves`'s wave assignment — it only
    reuses that module's pairwise `_paths_overlap` comparator. Wave
    scheduling is a different problem shape (this row wants EVERY
    overlapping pair reported, not a minimal parallel-safe partition).
  - Does NOT scan the live `docs/plans/` during tests — the test module
    below builds fixture plan directories under `tmp_path`.
  - Does NOT touch `residue.py` — envelope wiring is chunk C13's exclusive
    write target.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C6
"""
from __future__ import annotations

import difflib
import functools
import re
import subprocess
import sys
from typing import Any

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.dispatch_emit.spine_read import (
    UNDECLARED,
    SpineReadError,
    read_spine,
)
from coordinator_core.ops.dispatch_emit.wave_map import _normalize_path, _paths_overlap
from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined
from coordinator_core.win_portability import no_console_creationflags

_NO_CONSOLE = no_console_creationflags()
_GIT_TIMEOUT_SEC = 15

# `:162`'s minimum difflib.SequenceMatcher ratio for "candidate, worth
# surfacing" — deliberately permissive (this row is candidate evidence
# only; a downstream `U`-classified row is what decides "is it really the
# same assumption", never this module).
_ASSUMPTION_SIMILARITY_THRESHOLD = 0.6

# Matches one data row of a "## Cross-plan coordination" markdown table:
# `| sibling plan cell | assumption text cell | disposition cell |`.
_ASSUMPTION_ROW_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*[^|]*\|\s*$")

# `:160`'s AC-body path harvester. A backtick-quoted token carrying at least
# one `/` and a file extension — the repo's own citation convention for a
# concrete path ("cite by enclosing function, not line number" still cites the
# FILE in backticks). Deliberately narrow: an unquoted prose token, a bare
# symbol name (`_reap_stale_sessions`), and a directory (`docs/plans/`) are all
# rejected, because a false path here manufactures a cross-plan hit out of
# nothing and this row's output is already noise-sensitive.
_AC_PATH_CITATION_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_.\-]+)+\.[A-Za-z0-9]{1,6})`")


# `:160`'s spine `writes:` line scanner (see `_spine_write_sites` for why this
# is a scanner and not a YAML parse). Three shapes, matched on the stripped
# line: the `writes:` key opening a block sequence, one sequence item under
# it, and the inline flow form `writes: [a, b]`.
_SPINE_WRITES_KEY_RE = re.compile(r"^writes:\s*$")
_SPINE_INLINE_WRITES_RE = re.compile(r"^writes:\s*\[(.*)\]\s*$")
_SPINE_SEQUENCE_ITEM_RE = re.compile(r"^-\s+(.+?)\s*$")

# The task-spine fence's opening line, matched by `str.find` rather than the
# shipped `locate_fenced_block` — see `_spine_write_sites` for the cost that
# buys and the narrowing it gives up.
_SPINE_FENCE_OPEN = "```yaml plan-tasks"

# `## Acceptance Criteria` in any of the spellings the corpus carries ("##
# Acceptance criteria / Definition of Done", "## Acceptance Criteria (mirrors
# the baton)" — four spellings today and a fifth one plan away), and the next
# `##` heading that ends its section.
_AC_HEADING_RE = re.compile(r"^##\s+acceptance criteria.*$", re.IGNORECASE | re.MULTILINE)
# Bounds the AC section on ANY line starting `##`, spacing after the hashes
# irrelevant — deliberately over-inclusive of what counts as "the next
# heading" so the failure mode is early truncation (a missed candidate,
# priced and safe) rather than over-running a malformed heading (`##Foo`,
# no space) into the next section and harvesting its disclaimed citations
# as if they were this plan's own AC content.
_NEXT_H2_RE = re.compile(r"^##", re.MULTILINE)


def chunk_overlap(ctx: PredicateContext) -> dict[str, Any]:
    """`:151` — `gates.composition.chunk_overlap.pairs`.

    Pairwise scope intersection over the CURRENT plan's own task-spine rows,
    re-emitted from `spine_read.read_spine` + `wave_map._paths_overlap`
    rather than a second intersection algorithm. A pair where either row's
    `writes` is `UNDECLARED` is skipped — undeclared is unknown, not empty,
    and this row can only report concrete overlapping PATHS, which an
    unknown write set cannot supply (wave_map's own AC2 treats UNDECLARED as
    "must separate", a different question than "list the paths").
    """
    if ctx.plan_path is None:
        return undetermined("no --plan supplied; chunk_overlap needs this plan's own task-spine")
    try:
        rows = read_spine(ctx.plan_path)
    except SpineReadError as exc:
        return undetermined(f"plan task-spine unreadable: {exc}")

    pairs: list[dict[str, Any]] = []
    for i, row_a in enumerate(rows):
        if row_a.writes is UNDECLARED:
            continue
        for row_b in rows[i + 1 :]:
            if row_b.writes is UNDECLARED:
                continue
            overlapping = sorted(
                {
                    path_a
                    for path_a in row_a.writes
                    if any(_paths_overlap(path_a, path_b) for path_b in row_b.writes)
                }
                | {
                    path_b
                    for path_b in row_b.writes
                    if any(_paths_overlap(path_b, path_a) for path_a in row_a.writes)
                }
            )
            if overlapping:
                pairs.append(
                    {
                        "chunk_a": row_a.id,
                        "chunk_b": row_b.id,
                        "overlapping_paths": overlapping,
                    }
                )
    return {"pairs": pairs}


def _run_git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT_SEC,
        stdin=subprocess.DEVNULL,
        **_NO_CONSOLE,
    )


def path_rename_or_move(ctx: PredicateContext) -> dict[str, Any]:
    """`:156` — `gates.composition.path_rename_or_move.fires` / `.paths`.

    The current plan's own task-spine's cited `writes`/`reads` paths,
    diffed against `git log --follow --diff-filter=R` — native git rename
    detection, never a hand-rolled path-similarity heuristic. `.fires` is
    True iff at least one cited path has a rename record in its history;
    `.paths` names exactly those paths.
    """
    if ctx.plan_path is None:
        return undetermined("no --plan supplied; path_rename_or_move needs this plan's cited paths")
    try:
        rows = read_spine(ctx.plan_path)
    except SpineReadError as exc:
        return undetermined(f"plan task-spine unreadable: {exc}")

    cited_paths: set[str] = set()
    for row in rows:
        if row.writes is not UNDECLARED:
            cited_paths.update(row.writes)
        cited_paths.update(row.reads)
    if not cited_paths:
        return {"fires": False, "paths": []}

    # One `git log --follow` subprocess per cited path, run sequentially,
    # with no early exit once `.fires` is already known True: `.paths`
    # names EXACTLY the cited paths with a rename record (see docstring),
    # so stopping early would silently truncate that list rather than
    # merely computing `.fires` faster — a behavior change this row does
    # not make on its own. A plan citing many paths does pay the full
    # linear `git log` cost; that cost is real (each call is a real
    # subprocess with its own `_GIT_TIMEOUT_SEC` budget) but narrowing
    # `.paths` to fix it is a scope decision for the row's contract, not
    # a style fix.
    fired: list[str] = []
    for path in sorted(cited_paths):
        try:
            proc = _run_git(
                ["log", "--follow", "--diff-filter=R", "--name-status", "--", path],
                cwd=ctx.repo_root,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return undetermined(f"git log --follow failed for {path!r}: {exc}")
        if proc.returncode == 0 and proc.stdout.strip():
            fired.append(path)

    return {"fires": bool(fired), "paths": fired}


@functools.lru_cache(maxsize=1 << 16)
def _overlaps(path_a: str, path_b: str) -> bool:
    """Memoized `wave_map._paths_overlap`.

    Not a second comparator — the module's negative-spec forbids that, and
    this is the same function, called through a cache. `_paths_overlap`
    builds two `Path` objects and walks `.parents` on every call, and `:160`
    calls it once per (own site, other site) pair across ~565 documents whose
    declared paths repeat heavily; the cache turns that quadratic re-derivation
    into one derivation per distinct pair.
    """
    return _paths_overlap(path_a, path_b)


def _spine_write_sites(source: str) -> set[str]:
    """Declared `writes:` paths from a plan document's `## Tasks` spine.

    NEITHER `spine_read.read_spine` NOR `plan_tasks_render.load_rows` — a
    deliberate departure from this module's compose-over-shipped-readers rule,
    with a measured reason. `read_spine` is the dispatch pipeline's reader: it
    raises five distinct `SpineReadError` subclasses on a malformed spine and
    drops closed/deferred/gated rows, both correct when the answer feeds a
    dispatch and both wrong here — a SIBLING nobody in this process controls
    must not be able to blank out this row's answer for the plan under scan,
    and a closed ROW's write site is still a live collision risk while its
    plan is open (the plan-level `status: closed` filter is where closure is
    honoured). `load_rows`, the tolerant primitive underneath it, has the
    right semantics but the wrong cost: this row reads every
    `docs/plans/*.md`, and a `yaml.safe_load` per spine measures 1703ms
    across the 565-document corpus on its own — the whole row went 406ms →
    2969ms of process time, over the 500ms brightline before the rest of the
    composition gate runs. A cheap scan of already-in-memory text costs ~0.

    So: a line scanner over the fenced block, handling two of the forms the
    spine schema permits — a block sequence deeper-indented than its
    `writes:` key, and an inline flow list. What it deliberately does NOT do
    is reconstruct YAML. Exotic-but-valid forms are missed — a quoted key, an
    anchor, a multi-line scalar, AND a block sequence at the SAME indent as
    its `writes:` key (`writes:\n- a\n- b`; `plan-tasks.schema.json` permits
    this, the corpus sampled at review time consistently uses the deeper form
    instead) — and that is the priced cost: this row is CANDIDATE EVIDENCE
    for the EM, so a missed hint is a worse hint, not a wrong fact — whereas a
    row that blows the process budget does not run at all. The same-indent
    form is deliberately left unhandled rather than widened for: accepting it
    risks swallowing the NEXT row's `- id:` entry (same indent as a sibling
    task's `writes:` sequence), and a false write site manufactures a false
    conflict — the worse direction for a row already known to over-report.

    Review: coordinator:code-reviewer (WSC-B, a676367b) — flagged the
    same-indent form as schema-legal and silently unmatched; caveat added
    here rather than widening the scanner.
    """
    # `locate_fenced_block` is the shipped locator and is NOT used here for
    # the same reason `load_rows` is not: it blanks every HTML comment in the
    # whole document with a regex substitution before it looks for the fence,
    # which is a per-document cost this row pays 565 times to answer a
    # question a `str.find` answers. The narrowing that locator adds — the
    # fence must be inside the `## Tasks` section, and there must be exactly
    # one — is real, and giving it up means a stray `plan-tasks` fence
    # elsewhere in a document contributes its paths. That is the same
    # priced trade as the scanner itself: a possible extra CANDIDATE, never
    # a wrong fact.
    start = source.find(_SPINE_FENCE_OPEN)
    if start < 0:
        return set()
    block_start = source.find("\n", start)
    if block_start < 0:
        return set()
    end = source.find("\n```", block_start)
    block = source[block_start + 1 : end if end >= 0 else len(source)]

    sites: set[str] = set()
    in_writes = False
    writes_indent = 0
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        inline = _SPINE_INLINE_WRITES_RE.match(stripped)
        if inline is not None:
            in_writes = False
            sites.update(
                entry.strip().strip("'\"")
                for entry in inline.group(1).split(",")
                if entry.strip().strip("'\"")
            )
            continue

        if _SPINE_WRITES_KEY_RE.match(stripped):
            in_writes = True
            writes_indent = indent
            continue

        if not in_writes:
            continue
        # A sequence item deeper than the `writes:` key belongs to it; the
        # next key at or above that indent (`reads:`, `depends_on:`, the next
        # row's `- id:`) ends the run.
        item = _SPINE_SEQUENCE_ITEM_RE.match(stripped)
        if item is not None and indent > writes_indent:
            entry = item.group(1).strip().strip("'\"")
            if entry:
                sites.add(entry)
            continue
        in_writes = False
    return sites


def _ac_body_path_citations(body: str) -> set[str]:
    """Backtick-cited concrete file paths from a plan's Acceptance Criteria.

    The AC table is where a plan says what it will have changed, and it is
    routinely more specific than `scope:` — an AC naming a test file the
    frontmatter never lists is the ordinary case, not the exception. Harvested
    from the AC section ONLY, not the whole body: a Problem section quoting a
    file it is arguing about, or an Anti-scope naming a file precisely to
    disclaim it, are both citations of files this plan does NOT write, and
    folding them in would invent conflicts.

    Sliced by index rather than scanned line-by-line: at 565 documents per
    call, splitting every body into lines to reach one section is the kind of
    per-document cost this row cannot afford (see `_spine_write_sites`).
    """
    match = _AC_HEADING_RE.search(body)
    if match is None:
        return set()
    section_start = match.end()
    next_heading = _NEXT_H2_RE.search(body, section_start)
    section = body[section_start : next_heading.start() if next_heading else len(body)]
    return set(_AC_PATH_CITATION_RE.findall(section))


def _declared_write_sites(
    frontmatter: dict[str, Any], body: str, source: str
) -> tuple[set[str], set[str]]:
    """Every path a plan document declares it writes, by provenance.

    Returns `(scope_sites, derived_sites)` — frontmatter `scope:`, then the
    spine `writes:` and AC-body citations. The two are kept apart rather than
    flattened so a hit can say which overlaps the old `scope:`-only scan could
    never have surfaced; that is both the evidence this widening works and the
    triage handle an EM needs on a row already known to over-report directory
    prefixes.

    `derived_sites` is NOT yet narrowed to the beyond-scope subset — that is
    `_beyond_scope`'s job, and it is deliberately deferred: the narrowing costs
    a `_overlaps` call per (derived, scope) pair, and this function runs for
    all ~565 sibling documents while roughly forty of them produce a hit.
    Narrowing here would spend that on the other five hundred to compute a
    field nobody reads.
    """
    raw_scope = frontmatter.get("scope")
    scope_sites = {
        str(entry).strip()
        for entry in (raw_scope if isinstance(raw_scope, list) else [])
        if str(entry).strip()
    }
    return scope_sites, _spine_write_sites(source) | _ac_body_path_citations(body)


def _beyond_scope(derived: set[str], scope_sites: set[str]) -> set[str]:
    """The derived sites a declared `scope:` does not already cover.

    A path already inside a declared scope entry (exactly, or by directory
    containment) is not "beyond scope" — reporting `docs/plans/foo.md` as
    newly-discovered under a `scope:` of `docs/plans/` would make every
    directory-shaped scope entry look like a gap.
    """
    return {
        site
        for site in derived
        if not any(_overlaps(site, scope_site) for scope_site in scope_sites)
    }


@functools.lru_cache(maxsize=1 << 16)
def _head(site: str) -> str:
    """A declared path's first component, its overlap bucket key.

    Memoized for the same reason `_overlaps` is: `_normalize_path` builds a
    `PurePosixPath` per call, and this runs once per declared site on both
    sides of every one of ~565 sibling documents, whose declared paths repeat
    heavily. Delegating to the shared normalizer (see below) put the widest
    plan in the corpus at a 531ms median, over the 500ms brightline; the cache
    is what pays that back without reintroducing a private normalizer.

    Delegates to `wave_map._normalize_path` — the SAME normalizer
    `_paths_overlap` runs before every comparison — rather than a hand-rolled
    strip/replace/lstrip/split. Soundness as a partition rests on sharing
    that one normalizer, not on a second, independently-maintained notion of
    path shape: `_paths_overlap` returns True only on equality or
    directory-ancestor containment of the *normalized* paths, and both
    relations force the two normalized paths to share a first component.
    Two sites in different buckets therefore cannot overlap under
    `_paths_overlap`, and are never compared.

    Review: coordinator:code-reviewer (WSC-B, a676367b) — the prior
    hand-rolled split diverged from `_normalize_path` on a `./`-prefixed
    site (bucketed under `"."` instead of the real first component) and on
    backslash-folding, silently dropping genuine cross-plan overlaps.
    """
    parts = _normalize_path(site).parts
    return parts[0] if parts else ""


def _overlapping_sites(own_sites: set[str], other_sites: set[str]) -> list[str]:
    """The paths from either side that overlap the other, bucketed by head.

    `_overlaps` is still the ONLY decider — bucketing changes which pairs are
    ASKED about, never the answer. The full cross product across 565 sibling
    documents is what took this row over budget once each side's site set grew
    past a short `scope:` list; comparing only same-head pairs cuts it to the
    handful that can possibly match.
    """
    own_by_head: dict[str, list[str]] = {}
    for site in own_sites:
        own_by_head.setdefault(_head(site), []).append(site)

    overlapping: set[str] = set()
    for other in other_sites:
        candidates = own_by_head.get(_head(other))
        if not candidates:
            continue
        for own in candidates:
            if _overlaps(own, other):
                overlapping.add(own)
                overlapping.add(other)
    return sorted(overlapping)


def cross_plan_conflict(ctx: PredicateContext) -> dict[str, Any]:
    """`:160` — `gates.composition.cross_plan_conflict.hits`.

    This plan's declared WRITE SITES diffed against every OTHER
    `docs/plans/*.md` document's, via `_paths_overlap`. See the module
    docstring for why this reads `docs/plans/*.md` via `parse_frontmatter`
    (the same reader `PredicateContext` itself uses) rather than
    `plan_match._collect_plans` directly — that function's return shape
    carries no `scope:` field.

    Three sources per side, not `scope:` alone — frontmatter `scope:`, the
    `## Tasks` spine's declared `writes:` keys, and backtick-cited concrete
    file paths in the Acceptance Criteria body. `scope:` alone was a real
    blind spot, not a theoretical one:
    `docs/plans/2026-08-26-the-reaper-identifies-sessions-positively.md`
    recorded a live co-edit of `coordinator_core/ops/session/reap.py` by
    `docs/plans/2026-08-25-the-touched-files-record-gets-a-designed-shape.md`
    whose frontmatter `scope:` did not list that file — a hand-written
    Concurrency paragraph was the only thing that caught it, and this row,
    keying on `scope:`, would not have surfaced it to EITHER side. A plan
    declares what it writes in the spine and names it in its ACs whether or
    not `scope:` was kept in step; that divergence is ordinary, so the scan
    reads all three rather than trusting the one field an author can forget.

    `beyond_declared_scope` on each hit names the overlapping paths no
    `scope:` on either side covers — the hits this row could not previously
    produce. It never shrinks `overlapping_paths`; a hit the old scan would
    have found reports the same paths with `beyond_declared_scope: []`.

    Skips sibling plans with `status == "closed"`, matching `:162`
    (`amends_assumption`)'s own filter, deliberately: a closed/superseded
    plan's `scope:` is stale by definition, and a `:160` hit against it is
    noise the EM cannot distinguish from a genuine live conflict — this
    row is candidate evidence for the EM, not a computed fact the EM can
    independently discount, so surfacing a hit the EM has no way to
    triage is worse than omitting it. If a closed plan's path collision
    is ever worth surfacing anyway (e.g. an in-flight rename that a
    closed plan still names), that is a deliberate widening of this
    filter with its own test, not a silent side effect of scanning
    unconditionally.
    """
    if ctx.plan_frontmatter is None:
        return undetermined(
            "no --plan supplied, or plan frontmatter unparseable; "
            "cross_plan_conflict needs this plan's own declared write sites"
        )

    own_source = ""
    if ctx.plan_path is not None:
        try:
            own_source = ctx.plan_path.read_text(encoding="utf-8")
        except OSError as exc:
            # Unlike a SIBLING's malformed spine (tolerated by design — see
            # `_spine_write_sites`), this is the plan `--plan` itself names;
            # the sibling-tolerance rationale does not apply, so the
            # degradation is signalled rather than swallowed silently.
            print(
                f"skip: cross_plan_conflict: own_source = ctx.plan_path.read_text() failed: {exc}",
                file=sys.stderr,
            )
            own_source = ""
    own_scope, own_derived = _declared_write_sites(
        ctx.plan_frontmatter, ctx.plan_body or "", own_source
    )
    own_sites = own_scope | own_derived
    own_beyond = _beyond_scope(own_derived, own_scope)
    if not own_sites:
        return {"hits": []}

    plans_dir = ctx.repo_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return {"hits": []}

    this_plan_path = ctx.plan_path.resolve() if ctx.plan_path is not None else None
    hits: list[dict[str, Any]] = []
    for fpath in sorted(plans_dir.glob("*.md")):
        if this_plan_path is not None and fpath.resolve() == this_plan_path:
            continue
        try:
            raw = fpath.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = parse_frontmatter(raw)
        fm = parsed.get("frontmatter")
        if not isinstance(fm, dict):
            continue
        if fm.get("status") == "closed":
            continue
        other_scope, other_derived = _declared_write_sites(
            fm, str(parsed.get("body") or ""), raw
        )
        other_sites = other_scope | other_derived
        if not other_sites:
            continue

        overlapping = _overlapping_sites(own_sites, other_sites)
        if overlapping:
            # Narrowed only now, on the ~40 documents that hit — see
            # `_declared_write_sites` for why this is not done for all 565.
            other_beyond = _beyond_scope(other_derived, other_scope)
            try:
                # `.as_posix()`, not `str()`: this value is reported to the EM
                # and asserted on by tests as a repo-relative plan path, and
                # `str()` emits backslashes on Windows — a first-class platform
                # here, where the same predicate must produce the same string.
                plan_path_str = fpath.relative_to(ctx.repo_root).as_posix()
            except ValueError:
                plan_path_str = fpath.as_posix()
            hits.append(
                {
                    "plan_path": plan_path_str,
                    "overlapping_paths": overlapping,
                    # The subset no `scope:` on either side named — exactly the
                    # class of overlap the scope-only scan was structurally
                    # unable to see. Empty on a hit the old scan would also
                    # have found, so a reader can tell the two apart without
                    # re-deriving either side.
                    "beyond_declared_scope": sorted(
                        path
                        for path in overlapping
                        if path in (own_beyond | other_beyond)
                    ),
                }
            )

    return {"hits": hits}


def _extract_assumption_rows(body: str) -> list[str]:
    """Pull the "Assumption it carries" column out of a `## Cross-plan
    coordination` markdown table in `body`.

    Tolerant, single-purpose scan: only the section whose heading starts
    `## Cross-plan coordination` (case-insensitive) is considered; the
    header row and the `---`-only separator row are both skipped.
    """
    rows: list[str] = []
    in_section = False
    header_row_seen = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_section = stripped.lstrip("#").strip().lower().startswith("cross-plan coordination")
            header_row_seen = False
            continue
        if not in_section or not stripped.startswith("|"):
            continue
        if not header_row_seen:
            header_row_seen = True
            continue
        if set(stripped) <= set("|-: "):
            continue
        match = _ASSUMPTION_ROW_RE.match(stripped)
        if match:
            assumption_text = match.group(2).strip()
            if assumption_text:
                rows.append(assumption_text)
    return rows


def amends_assumption(ctx: PredicateContext) -> dict[str, Any]:
    """`:162` — `gates.composition.amends_assumption.candidate` /
    `.matched_plan`. GENUINELY NEW; nothing in the fleet computes this today.

    Text-matches this plan's own `## Cross-plan coordination` table's
    assumption strings against every OTHER `docs/plans/*.md` document with
    `status != closed`, via `difflib.SequenceMatcher`. CANDIDATE EVIDENCE
    ONLY per the plan's Anti-scope — "is it really the same assumption"
    stays a `U`-classified judgment this module never resolves; `.candidate`
    is a `bool` flag, not a verdict field.
    """
    if ctx.plan_path is None or ctx.plan_body is None:
        return undetermined(
            "no --plan supplied, or plan body unparseable; amends_assumption "
            "needs this plan's own assumption text"
        )
    own_assumptions = _extract_assumption_rows(ctx.plan_body)
    if not own_assumptions:
        return undetermined(
            "plan body has no '## Cross-plan coordination' table; "
            "no assumption strings to match"
        )

    plans_dir = ctx.repo_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return {"candidate": False, "matched_plan": None}

    this_plan_path = ctx.plan_path.resolve()
    for fpath in sorted(plans_dir.glob("*.md")):
        if fpath.resolve() == this_plan_path:
            continue
        try:
            raw = fpath.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = parse_frontmatter(raw)
        fm = parsed.get("frontmatter")
        if isinstance(fm, dict) and fm.get("status") == "closed":
            continue
        other_body = parsed.get("body") or raw

        for own_text in own_assumptions:
            for line in other_body.splitlines():
                candidate_line = line.strip()
                if not candidate_line:
                    continue
                ratio = difflib.SequenceMatcher(
                    None, own_text.lower(), candidate_line.lower()
                ).ratio()
                if ratio >= _ASSUMPTION_SIMILARITY_THRESHOLD:
                    try:
                        plan_path_str = fpath.relative_to(ctx.repo_root).as_posix()
                    except ValueError:
                        plan_path_str = fpath.as_posix()
                    return {"candidate": True, "matched_plan": plan_path_str}

    return {"candidate": False, "matched_plan": None}


__all__ = [
    "chunk_overlap",
    "path_rename_or_move",
    "cross_plan_conflict",
    "amends_assumption",
]
