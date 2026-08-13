"""
coordinator_core.ops.rewrite_spec_backlinks — path-form citation to id-form,
in place.

Purpose: `spec_backlink.rewrite` (registered in C1's registry rows) rewrites a
`docs/plans/YYYY-MM-DD-slug.md` citation on a spec-backlink convention line to
its stable `pln-<id>` / `dlv-<id>` form via C1's `spec_backlink_resolve`
resolver, so a later `fleet.archive_plans` move never dangles the citation
(that repoint-on-move belt is C5's gate, this module is the one-shot healer).

Emit order (pinned in plan prose, not derivable from code alone): resolve the
target path; a real `plan_id` on the resolved record always wins (collision-
free, plan-scoped) over a real `deliverable_id` (group-stamped across a plan +
its handoff + its completion entry); a MISS, an AMBIGUITY (N>1), or a HIT
whose record carries neither id as a real (non-null, non-empty) value is
UNRESOLVABLE and is reported, never guessed, dropped, or partially rewritten.

Negative-spec (RAG-bait):
    This module never shells out to `perl` (see `_fix_file` in
    `assert_no_dangling_plan_backlinks.py` for the shape it deliberately does
    NOT reproduce) -- the in-place edit is a literal `str.replace` on the
    already-matched span, the faithful naked-Python port of the perl
    one-liner's `\\Q...\\E` quotemeta literal match, never `re.sub`.

    An unresolvable citation is never deleted or invented — a no-op on that
    line, reported in the returned/unresolvable set, is the only correct
    outcome (AC4). This extends to AMBIGUITY: the resolver refusing to pick
    one of N>1 candidates is treated identically to a MISS.

    A decoy prose mention of a `docs/plans/...md` path OUTSIDE a
    spec-backlink convention line is never touched — the two-stage filter
    (imported verbatim from `assert_no_dangling_plan_backlinks.py`, not
    re-authored) is what distinguishes a citation from incidental prose.

Spec backlink: pln-spec-backlinks-cite-a-stable-d-451b3e § C3
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

from coordinator_core.ops.assert_no_dangling_plan_backlinks import (
    _BACKLINK_LINE_RE,
    _PLAN_PATH_RE,
)
from coordinator_core.session.declared_writes import declare_write

PathLike = Union[str, Path]

# Resolver contract (C1, coordinator_core.ops.spec_backlink_resolve): a
# callable taking the cited docs/plans/...md path and returning a
# JSON-serializable dict with an "outcome" key of "hit" / "miss" /
# "ambiguity" (case-insensitive on read here -- the stub-based unit tests in
# this file's test module pin uppercase "HIT"/"MISS"/"AMBIGUITY" values, C1's
# real resolver emits lowercase; both must keep working unchanged). On a hit
# the dict also carries "plan_id" and "deliverable_id" (either may be
# None/absent -- "real" means present, non-None, and non-empty after
# stripping). C1's real values already carry their own `pln-`/`dlv-` prefix
# on disk (e.g. `plan_id: "pln-foo-451b3e"`) -- `_emit_id` below must not
# double-prefix.
Resolver = Callable[[str], Dict[str, object]]


def _default_resolver(worktree_root: PathLike) -> Resolver:
    """Bind C1's real `resolve_path_with_index` (index+path -> ids) into the
    1-arg `Resolver` shape this module calls (`resolver(cited_path)`). Lazily
    imported so this module stays importable even if `spec_backlink_resolve.py`
    has not landed yet in a given wave.

    The index is built exactly ONCE here (`build_index(worktree_root)`), at
    resolver-construction time, and closed over for every subsequent
    `resolver(cited_path)` call this resolver instance serves -- NOT rebuilt
    per lookup. This is the fix for the measured defect: `rewrite_file`
    (single-file) and `rewrite_spec_backlinks` (multi-file batch) both
    construct exactly one `_default_resolver` per invocation and reuse it
    across every candidate on every line of every file, so one invocation
    over N files with M total citations does ONE corpus walk, not N or M.

    Cache-lifetime choice: this index is scoped to the resolver closure, not
    memoised at module level or cached across process invocations. Each
    fresh call to `rewrite_file`/`rewrite_spec_backlinks` (a fresh process,
    or a fresh COMPUTE_ONLY op invocation) gets its own `_default_resolver`
    and therefore its own freshly-built index -- deliberately, because
    `backfill_deliverable_spine` mutates the very frontmatter this index
    reads (stamping `plan_id`/`deliverable_id` onto records), so a
    process-global or cross-invocation cache would risk serving ids from
    before a spine-stamping run landed. No invalidation logic is needed
    because there is nothing to invalidate: the index's lifetime is exactly
    one invocation's worth of lookups, matching the plan's "build once per
    invocation" requirement verbatim (not "once per process").

    `resolve_path_with_index`/`resolve_path` are the INVERSE of C1's id ->
    path `resolve()` -- this module has a cited PATH, not an id, and needs
    that record's ids back.
    """
    from coordinator_core.ops.spec_backlink_resolve import build_index, resolve_path_with_index

    root = Path(worktree_root)
    index = build_index(root)

    def _resolve(cited_path: str) -> Dict[str, object]:
        return resolve_path_with_index(index, root, cited_path)

    return _resolve


def _is_real_id(value: object) -> bool:
    """A "real" id: present, not None/null, not the empty/whitespace string."""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _with_single_prefix(value: str, prefix: str) -> str:
    """Prepend `prefix` unless `value` already carries it -- C1's real id
    values are already fully prefixed on disk (`plan_id: "pln-foo-451b3e"`),
    so a bare `f"{prefix}{value}"` would double-prefix into `pln-pln-...`."""
    value = value.strip()
    return value if value.startswith(prefix) else f"{prefix}{value}"


def _emit_id(outcome: Dict[str, object]) -> Optional[str]:
    """Apply the pln->dlv preference order to a HIT outcome dict. Returns
    None if the record carries neither id as a real value (unresolvable)."""
    plan_id = outcome.get("plan_id")
    if _is_real_id(plan_id):
        return _with_single_prefix(str(plan_id), "pln-")
    deliverable_id = outcome.get("deliverable_id")
    if _is_real_id(deliverable_id):
        return _with_single_prefix(str(deliverable_id), "dlv-")
    return None


def resolve_citation(cited_path: str, resolver: Resolver) -> Optional[str]:
    """Resolve one cited `docs/plans/...md` path to its `pln-`/`dlv-`
    replacement string, or None if unresolvable (MISS, AMBIGUITY, or a HIT
    with neither id real)."""
    outcome = resolver(cited_path)
    if str(outcome.get("outcome", "")).lower() != "hit":
        return None
    return _emit_id(outcome)


def rewrite_file(
    full_path: PathLike,
    resolver: Optional[Resolver] = None,
    worktree_root: Optional[PathLike] = None,
) -> Dict[str, object]:
    """Rewrite path-form spec-backlink citations to id-form in one file, in
    place. Only lines passing the two-stage filter (spec.?backlink AND the
    literal "docs/plans/" substring) are scanned for candidate paths; only
    the matched `docs/plans/...md` span on such a line is replaced, byte-for-
    byte preserving everything else including the `§ <anchor>` suffix.

    Returns a report dict:
      {"path": <str>, "rewritten": [<cited_path>, ...],
       "unresolvable": [<cited_path>, ...]}

    An unresolvable citation leaves the line untouched (AC4) and is reported,
    never dropped or guessed.
    """
    resolver = resolver or _default_resolver(worktree_root or Path.cwd())
    full_path = str(full_path)

    with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
        original = fh.read()

    lines = original.splitlines(keepends=True)
    rewritten: List[str] = []
    unresolvable: List[str] = []
    changed = False

    new_lines: List[str] = []
    for line in lines:
        if not _BACKLINK_LINE_RE.search(line):
            new_lines.append(line)
            continue
        if "docs/plans/" not in line:
            new_lines.append(line)
            continue

        candidates = _PLAN_PATH_RE.findall(line)
        if not candidates:
            new_lines.append(line)
            continue

        new_line = line
        for cited_path in candidates:
            replacement = resolve_citation(cited_path, resolver)
            if replacement is None:
                if cited_path not in unresolvable:
                    unresolvable.append(cited_path)
                continue
            if cited_path not in new_line:
                # already rewritten earlier in this same line pass
                continue
            new_line = new_line.replace(cited_path, replacement)
            changed = True
            if cited_path not in rewritten:
                rewritten.append(cited_path)
        new_lines.append(new_line)

    if changed:
        new_content = "".join(new_lines)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        # DR-276: declared AFTER the in-place edit lands, never before.
        declare_write(full_path)

    return {"path": full_path, "rewritten": rewritten, "unresolvable": unresolvable}


def rewrite_spec_backlinks(
    paths: List[PathLike],
    resolver: Optional[Resolver] = None,
    worktree_root: Optional[PathLike] = None,
) -> Dict[str, object]:
    """Batch entry point over an explicit file list (C4 fans this out across
    executors on disjoint directory scopes -- this function does not walk
    the filesystem itself). Returns the aggregate reported set consumed by
    C7 (deferred cross-repo) and C8 (unresolvable disposition):

      {"rewritten": {<path>: [<cited_path>, ...]},
       "unresolvable": {<path>: [<cited_path>, ...]}}
    """
    resolver = resolver or _default_resolver(worktree_root or Path.cwd())
    rewritten: Dict[str, List[str]] = {}
    unresolvable: Dict[str, List[str]] = {}
    for path in paths:
        report = rewrite_file(path, resolver=resolver)
        if report["rewritten"]:
            rewritten[report["path"]] = report["rewritten"]
        if report["unresolvable"]:
            unresolvable[report["path"]] = report["unresolvable"]
    return {"rewritten": rewritten, "unresolvable": unresolvable}


def main(argv: List[str]) -> int:
    """CLI/op entry point: `python -m coordinator_core.ops.rewrite_spec_backlinks
    <path> [<path> ...]`. Exit 0 always (a no-op on unresolvable citations is
    success, not failure) -- callers inspect the reported set for
    disposition, per AC4."""
    if not argv:
        print("usage: rewrite_spec_backlinks.py <path> [<path> ...]", file=sys.stderr)
        return 0
    report = rewrite_spec_backlinks(list(argv))
    for path, cited in report["unresolvable"].items():
        for c in cited:
            print(f"UNRESOLVABLE: {path}: {c}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
