"""Stamp-time advisory: does a memo's ``realized_by`` commit touch its own
``declared surface`` (``scoped_to.artifact`` / ``surface:``)?

Spec: docs/plans/2026-09-11-realized-by-vs-declared-surface-reconcile.md (P080-C1),
AC1-AC3, AC7 (first/third sentences), AC8.

TWO CLOSED FORKS, both deliberate, both measured:

  1. ADVISE, NEVER BLOCK. This module never raises past its own entry point,
     never returns a refusal shape, and performs no filesystem write, no
     sentinel, no ledger append. A legitimate case exists where a stamped
     commit lands OUTSIDE the declared surface on purpose -- one file moves
     one module over from where the sender declared it (the "one-module-over"
     case: declared ``pickup_assemble/apply.py``, touched
     ``pickup_assemble/__init__.py``) -- and the ruling for that case is
     REPORTED, not refused: it verdicts ``out-of-surface`` and the write still
     lands. A blocking design would need its own surfacing (see the plan's
     Anti-scope).
  2. STAMP-TIME, NOT SWEEP. This module judges ONE ``realized_by`` SHA against
     ONE declared surface at the moment a memo is stamped. It is not a
     retrospective report over the archive corpus -- that is a separate
     requirement with its own cost shape (a naive parse-everything sweep
     measured ~1.4s corpus-wide; a cheap pre-filter is a different, unbuilt
     design) and is explicitly out of this plan's scope.

MEASURED REACH (2026-09-12, plan's census reach row -- re-ask via that row, not
this docstring, since the corpus moves): 21 of 78 (27%) of September's
SHA-shaped ``realized_by`` stamps declared a surface, and 256 of 612 (42%) did
corpus-wide. The rest compute ``no-declared-surface``, which is NOT a stronger
check -- it means the sender never named a surface to judge against. This
advisory adds signal only on the declaring minority; it does not close the
defect class corpus-wide. Widening the declaring share is a separate,
producer-side successor (the plan's Anti-scope), not this module's job.

FOREIGN-SURFACE IS A WORKING-TREE JUDGMENT, NOT A CROSS-REPO CLAIM. The
``foreign-surface`` verdict's existence probe (``_declared_path_exists_in_tree``)
reads the WORKING TREE of the memo's own git root, never ``HEAD``. It means
"the declared paths are not in this tree right now" -- which is also true of a
declared path a LATER commit deletes, not only a path that genuinely belongs to
another repo (the census's own note: "foreign-surface is an upper bound"). A
``git cat-file -e HEAD:<path>`` probe per declared path would cost a second
spawn per path and break the one-spawn budget (AC3); the working-tree
``os.path.exists``/glob probe costs nothing extra because it never spawns. An
uncommitted deletion, or a mid-rebase tree on a shared checkout, can therefore
flip one call's verdict between ``foreign-surface`` and ``out-of-surface``; that
is accepted, because the verdict is advisory.

WHY NEITHER OF TWO NEARBY, ALREADY-BUILT SPAWNS IS REUSED HERE:

  - ``coordinator_core.distill.delete_guard.resolve_realized_by`` — its
    SHA-shape DISPATCH (lowercase, then ``^[0-9a-f]{7,40}$``) is mirrored here,
    but its own git leg is a plain ``git cat-file -e`` EXISTENCE check, which
    answers "does an object with this hash exist" -- true for a TREE or a BLOB,
    not only a commit. That existence spawn is subsumed by this module's own
    ``<sha>^{{commit}}`` peel (a tree/blob-object SHA fails the peel and lands
    in ``unresolved-sha``, never mistaken for a resolvable commit), so calling
    ``resolve_realized_by`` first would be a second, redundant spawn.
  - ``coordinator_core.coverage._commit_touched_paths`` — its batched
    ``git log --no-walk`` maps an UNRESOLVABLE sha to an EMPTY frozenset, the
    same value it gives a genuinely empty/root commit (by design, for its own
    caller's fail-closed-CODE contract). This module's ``paper-realization``
    verdict treats an empty touched set as PAPER, so reusing that helper would
    silently misfile every unresolvable SHA as paper-realization instead of
    unresolved-sha. "Unresolvable" and "empty" must stay two different values
    here, which is exactly what ``_commit_touched_paths`` collapses.

NEGATIVE SPEC: never refuses (no non-zero exit, no withheld write, no gate);
never writes (no filesystem mutation, sentinel or ledger append); the SHA-shape
check runs in exactly one place (``surface_advisory``'s own entry, mirroring
but not calling ``resolve_realized_by``); the single touched-path git spawn is
``git log -1 --root --diff-merges=first-parent --name-only --format=
<sha>^{{commit}}`` (AC3) -- never ``_commit_touched_paths``'s batched form.
"""

from __future__ import annotations

import glob
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from coordinator_core.win_portability import no_console_creationflags

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

#: single bounded git-log read, `_EVER_TRACKED_CACHE` leg: timeout=3).
_GIT_TIMEOUT_S = 3.0

VERDICTS = (
    "no-declared-surface",
    "unresolved-sha",
    "ok",
    "foreign-surface",
    "paper-realization",
    "out-of-surface",
)


def _normalize_surface_entry(raw: Any) -> str:
    entry = str(raw).strip()
    entry = entry.split(" ", 1)[0]
    entry = entry.strip("`")
    entry = entry.replace("\\", "/")
    entry = entry.split("::", 1)[0]
    if entry.startswith("./"):
        entry = entry[2:]
    entry = entry.rstrip("/")
    return entry


def declared_surface(frontmatter: Mapping[str, Any]) -> list[str]:
    raw: Any = None
    scoped_to = frontmatter.get("scoped_to")
    if isinstance(scoped_to, Mapping):
        artifact = scoped_to.get("artifact")
        if artifact:
            raw = artifact
    if raw is None:
        surface = frontmatter.get("surface")
        if surface:
            raw = surface
    if raw is None:
        return []
    entries = raw if isinstance(raw, list) else [raw]
    return [_normalize_surface_entry(e) for e in entries if str(e).strip()]


def _touched_paths(sha: str, git_root: Path) -> tuple[list[str] | None, str | None]:
    try:
        result = subprocess.run(
            [
                "git", "log", "-1", "--root", "--diff-merges=first-parent",
                "--name-only", "--format=", f"{sha}^{{commit}}",
            ],
            cwd=str(git_root),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
            **no_console_creationflags(),
        )
    except Exception:
        return None, "advisory-failed"
    if result.returncode != 0:
        return None, "not-a-commit-here"
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return paths, None


def _declared_matches_touched(declared: str, touched_paths: list[str]) -> bool:
    dir_prefix = declared.rstrip("/") + "/"
    for touched in touched_paths:
        if touched == declared:
            return True
        if touched.startswith(dir_prefix):
            return True
        stem, dot, _ext = touched.rpartition(".")
        if dot and stem == declared:
            return True
    return False


def _declared_path_exists_in_tree(declared: list[str], git_root: Path) -> bool:
    for entry in declared:
        candidate = git_root / entry
        if candidate.exists():
            return True
        if glob.glob(str(candidate) + ".*"):
            return True
    return False


def _all_paper(touched_paths: list[str]) -> bool:
    if not touched_paths:
        return True
    from coordinator_core.coverage import (
        _is_bookkeeping_path,
        _is_planning_artifact_path,
    )
    return all(
        _is_bookkeeping_path(p) or _is_planning_artifact_path(p)
        for p in touched_paths
    )


def surface_advisory(frontmatter: Mapping[str, Any], git_root: str | Path) -> dict | None:
    """Entry point (AC2/AC3). Returns `None`, with zero spawns, when
    `frontmatter["realized_by"]` is absent or not SHA-shaped
    (`^[0-9a-f]{7,40}$`, case-insensitive) -- the only place this module
    checks SHA shape. Otherwise returns
    `{"verdict", "sha", "declared", "untouched_declared", "reason"?}`, where
    `verdict` is one of `VERDICTS`, evaluated in that first-match order.
    `reason` is present only for `unresolved-sha` (`not-a-commit-here` or
    `advisory-failed`). Never raises; never writes."""
    realized_by = frontmatter.get("realized_by")
    if not isinstance(realized_by, str):
        return None
    sha = realized_by.strip().lower()
    if not _SHA_RE.match(sha):
        return None

    declared = declared_surface(frontmatter)
    if not declared:
        return {
            "verdict": "no-declared-surface",
            "sha": sha,
            "declared": [],
            "untouched_declared": [],
        }

    root = Path(git_root)
    touched, reason = _touched_paths(sha, root)
    if touched is None:
        return {
            "verdict": "unresolved-sha",
            "sha": sha,
            "declared": declared,
            "untouched_declared": list(declared),
            "reason": reason,
        }

    matched = [d for d in declared if _declared_matches_touched(d, touched)]
    if matched:
        untouched = [d for d in declared if d not in matched]
        return {
            "verdict": "ok",
            "sha": sha,
            "declared": declared,
            "untouched_declared": untouched,
        }

    if not _declared_path_exists_in_tree(declared, root):
        verdict = "foreign-surface"
    elif _all_paper(touched):
        verdict = "paper-realization"
    else:
        verdict = "out-of-surface"

    return {
        "verdict": verdict,
        "sha": sha,
        "declared": declared,
        "untouched_declared": list(declared),
    }
