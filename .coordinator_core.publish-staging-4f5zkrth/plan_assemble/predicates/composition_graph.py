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
  :160 cross_plan_conflict — scans `docs/plans/*.md` frontmatter `scope:`
                            for intersection with this plan's own `scope:`,
                            via `_paths_overlap` again, skipping `status ==
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
import re
import subprocess
from typing import Any

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.dispatch_emit.spine_read import (
    UNDECLARED,
    SpineReadError,
    read_spine,
)
from coordinator_core.ops.dispatch_emit.wave_map import _paths_overlap
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


def cross_plan_conflict(ctx: PredicateContext) -> dict[str, Any]:
    """`:160` — `gates.composition.cross_plan_conflict.hits`.

    This plan's own frontmatter `scope:` diffed against every OTHER
    `docs/plans/*.md` document's `scope:`, via `_paths_overlap`. See the
    module docstring for why this reads `docs/plans/*.md` via
    `parse_frontmatter` (the same reader `PredicateContext` itself uses)
    rather than `plan_match._collect_plans` directly — that function's
    return shape carries no `scope:` field.

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
            "cross_plan_conflict needs this plan's own scope:"
        )
    own_scope = ctx.plan_frontmatter.get("scope")
    if not isinstance(own_scope, list) or not own_scope:
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
        other_scope = fm.get("scope")
        if not isinstance(other_scope, list) or not other_scope:
            continue

        overlapping = sorted(
            {
                path_a
                for path_a in own_scope
                if any(_paths_overlap(str(path_a), str(path_b)) for path_b in other_scope)
            }
            | {
                path_b
                for path_b in other_scope
                if any(_paths_overlap(str(path_b), str(path_a)) for path_a in own_scope)
            }
        )
        if overlapping:
            try:
                plan_path_str = str(fpath.relative_to(ctx.repo_root))
            except ValueError:
                plan_path_str = str(fpath)
            hits.append({"plan_path": plan_path_str, "overlapping_paths": overlapping})

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
                        plan_path_str = str(fpath.relative_to(ctx.repo_root))
                    except ValueError:
                        plan_path_str = str(fpath)
                    return {"candidate": True, "matched_plan": plan_path_str}

    return {"candidate": False, "matched_plan": None}


__all__ = [
    "chunk_overlap",
    "path_rename_or_move",
    "cross_plan_conflict",
    "amends_assumption",
]
