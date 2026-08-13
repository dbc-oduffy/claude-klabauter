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

Spec backlink: docs/plans/2026-08-13-spec-backlinks-cite-a-stable-deliverable-id.md § C3
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
# JSON-serializable dict with an "outcome" key of "HIT" / "MISS" /
# "AMBIGUITY". On "HIT" the dict also carries "plan_id" and
# "deliverable_id" (either may be None/absent -- "real" means present,
# non-None, and non-empty after stripping).
Resolver = Callable[[str], Dict[str, object]]


def _default_resolver() -> Resolver:
    """Lazily import C1's resolver so this module stays importable even if
    `spec_backlink_resolve.py` has not landed yet in a given wave."""
    from coordinator_core.ops.spec_backlink_resolve import resolve as _resolve

    return _resolve


def _is_real_id(value: object) -> bool:
    """A "real" id: present, not None/null, not the empty/whitespace string."""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _emit_id(outcome: Dict[str, object]) -> Optional[str]:
    """Apply the pln->dlv preference order to a HIT outcome dict. Returns
    None if the record carries neither id as a real value (unresolvable)."""
    plan_id = outcome.get("plan_id")
    if _is_real_id(plan_id):
        return f"pln-{plan_id}"
    deliverable_id = outcome.get("deliverable_id")
    if _is_real_id(deliverable_id):
        return f"dlv-{deliverable_id}"
    return None


def resolve_citation(cited_path: str, resolver: Resolver) -> Optional[str]:
    """Resolve one cited `docs/plans/...md` path to its `pln-`/`dlv-`
    replacement string, or None if unresolvable (MISS, AMBIGUITY, or a HIT
    with neither id real)."""
    outcome = resolver(cited_path)
    if outcome.get("outcome") != "HIT":
        return None
    return _emit_id(outcome)


def rewrite_file(
    full_path: PathLike,
    resolver: Optional[Resolver] = None,
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
    resolver = resolver or _default_resolver()
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
) -> Dict[str, object]:
    """Batch entry point over an explicit file list (C4 fans this out across
    executors on disjoint directory scopes -- this function does not walk
    the filesystem itself). Returns the aggregate reported set consumed by
    C7 (deferred cross-repo) and C8 (unresolvable disposition):

      {"rewritten": {<path>: [<cited_path>, ...]},
       "unresolvable": {<path>: [<cited_path>, ...]}}
    """
    resolver = resolver or _default_resolver()
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
