"""Shared row-resolution helper for the register-inventory sweep.

Answers one question, for one register row: does the subject it names still
exist? This module declares the shared types and resolution primitive that
`docs/plans/2026-08-26-every-register-either-derives-or-fails-on-its-dead-rows.md`
fans out to its consumers -- C1 is the fan-out root, so the types below are
pinned once, here, rather than re-derived per consumer.

RESOLUTION IS INDEX-FIRST, NOT AST-ONLY (AMENDED 2026-08-26, superseding an
earlier AST-only draft of this module):

  - `repo-path` / `bare-filename` rows resolve against git's tracked-file
    set: one `git ls-files` per run (31.2ms / 1 spawn over 36758 tracked
    files in this repo), loaded once and shared across every row. A
    `repo-path` row is an exact-membership test against that set; a
    `bare-filename` row is a basename lookup into a derived
    basename -> [repo-relative paths] index built from the same one-shot
    listing. Both are then O(1) set/dict lookups per row -- 0.0ms each,
    measured over 1202 path rows repo-wide.
  - `module` / `symbol` rows resolve by AST: parse the candidate file (found
    via the same tracked-file index, never a fresh git call) and look for
    the definition. This is the only place per-row cost is paid, and it is
    bounded to the small number of dotted-subject registers (6 in the core
    inventory).
  - `opaque` rows are unadjudicable by construction -- the row declares a
    subject this helper has no reliable way to check, so resolution reports
    `unadjudicable` rather than guessing.

WHY NOT AST FOR PATH ROWS. A path row's declared subject already spells out
a filesystem location; asking "is it in the tracked-file set" is the exact
question the row poses, and answering it by parsing an unrelated file (or
worse, importing the subject) would be answering a different question. AST
is reserved for rows whose subject is a name living *inside* a file, where
membership in a set of paths cannot settle the question.

WHY NOT IMPORT THE SUBJECT. Per-row resolution never imports the named
subject: the prohibition is against importing an arbitrary, unbounded
subject up to N times (repeated per row, potentially side-effecting), not
against every import full stop. A single bounded module-level import of a
known-inert holder (as C3 does for its gate module) is a different shape --
one import, at module load, of something already known not to run
side-effecting code at import time. This helper performs no per-row import
of any kind, dotted or otherwise.

WHY NOT A PARSE CACHE. Considered and rejected: core rows are dominated by
path-existence checks that resolve at 0.0ms as set lookups and never reach
AST at all, so a parse cache buys nothing measurable for the shape actually
swept. Do not add one without a fresh measurement showing dotted-row AST
cost dominating the sweep.

MEASURED FIGURES (core-45 inventory, 220 rows,
`state/audits/2026-08-26-the-core-register-inventory.md`):

    git ls-files:              31.2ms / 1 spawn (the only subprocess)
    1202 path rows (repo-wide): 0.0ms total, set/dict lookups post-index
    6 dotted-row registers:     AST-bounded, the only per-row parse cost
    total process time:        ~250ms, 1 spawn -- against the 500ms brightline

RegisterId keying. Register constant names collide across the population
(`_ALLOWLIST` appears in three modules; `_CASES`, `EXCLUDED_PATHS`,
`_EXEMPT_SITES`, `COHORT`, `_ALLOWLISTED_RELPATHS`, and `_NON_GUARD_MODULES`
each appear in two) -- every artifact key, canary member, declaration-table
entry, and audit row downstream is keyed on the `(repo-relative path,
constant name)` tuple, never on the bare constant name alone.

THE TWO READINGS, KEPT SEPARATELY ASSERTABLE (AC2). Non-emptiness of a
derived result does not by itself discriminate a wrong root (e.g. an empty
tracked-file index silently "resolving" nothing) from a right one (a
genuinely clean sweep). Two assertions exist for two different failure
directions:

  - `rows_that_do_not_resolve` answers "which named subjects are gone" --
    the reads-as-CLOSURE check. A non-empty result here is the actual
    finding this sweep exists to produce.
  - `assert_canary_present` answers "did the derivation itself stay sound"
    -- the reads-as-THE-OLD-VALUE check. It asserts that some named,
    known-good minimum membership still survives inside a derived set,
    which is how a caller catches "the index came back empty" or "the
    derivation silently dropped everything" before trusting an empty
    `rows_that_do_not_resolve` result as a clean bill of health.

The two are not substitutable: a caller that only calls the first has no
way to tell "nothing is dead" from "the resolver is broken and reports
nothing is dead by default."

NOT PER-ROW: no `git` call per row, no subprocess per row, no import of any
subject per row. The one `git ls-files` call happens once per run, its
result shared by every row resolved in that run.
"""

from __future__ import annotations

import ast
import enum
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

if sys.platform == "win32":
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    _NO_WINDOW = 0


class SubjectClass(enum.Enum):
    """The closed set of subject shapes a register row can declare.

    A row's declared class selects the resolution strategy in
    `resolve_row`: path classes resolve against the tracked-file index,
    dotted classes resolve by AST, and `OPAQUE` never resolves at all.
    """

    REPO_PATH = "repo-path"
    BARE_FILENAME = "bare-filename"
    MODULE = "module"
    SYMBOL = "symbol"
    OPAQUE = "opaque"


_DOTTED_CLASSES = frozenset({SubjectClass.MODULE, SubjectClass.SYMBOL})


class RegisterId(NamedTuple):
    """Identity of one register: `(repo-relative path, constant name)`.

    Bare constant names collide across the population (see module
    docstring) -- every downstream artifact keys on this tuple, never on
    the bare name alone.
    """

    repo_relative_path: str
    constant_name: str


@dataclass(frozen=True)
class Row:
    """One entry in a register: the subject it names, and its declared class."""

    register: RegisterId
    subject: str
    declared_class: SubjectClass


class ResolutionKind(enum.Enum):
    """The three outcomes `resolve_row` can report for a single row."""

    RESOLVED = "resolved"
    ABSENT = "absent"
    UNADJUDICABLE = "unadjudicable"


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one `Row`, plus why."""

    kind: ResolutionKind
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.kind is ResolutionKind.RESOLVED

    @property
    def absent(self) -> bool:
        return self.kind is ResolutionKind.ABSENT

    @property
    def unadjudicable(self) -> bool:
        return self.kind is ResolutionKind.UNADJUDICABLE


class TrackedFileIndex:
    """The one-shot `git ls-files` result, shared across every row in a run.

    Built once per run (one `git ls-files` spawn) and consulted by every
    subsequent row as pure in-memory set/dict lookups. Never re-spawns git
    per row, per register, or per resolve call.
    """

    def __init__(self, tracked_relpaths: frozenset[str]) -> None:
        self._paths = tracked_relpaths
        by_basename: dict[str, list[str]] = defaultdict(list)
        for relpath in tracked_relpaths:
            by_basename[Path(relpath).name].append(relpath)
        self._by_basename = {name: tuple(paths) for name, paths in by_basename.items()}

    @classmethod
    def build(cls, repo_root: Path) -> "TrackedFileIndex":
        """Run the single sanctioned `git ls-files` spawn for this run."""
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            creationflags=_NO_WINDOW,
        )
        relpaths = frozenset(
            line.strip() for line in result.stdout.splitlines() if line.strip()
        )
        return cls(relpaths)

    def has_path(self, relpath: str) -> bool:
        return relpath.replace("\\", "/") in self._paths

    def paths_for_basename(self, basename: str) -> tuple[str, ...]:
        return self._by_basename.get(basename, ())


def _resolve_dotted(subject: str, index: TrackedFileIndex, repo_root: Path) -> Resolution:
    """Resolve a `module` or `symbol` row by parsing its containing file's AST.

    The candidate file is located via the tracked-file index (never a fresh
    git call), then parsed once. No subject is ever imported.
    """
    parts = subject.split(".")
    if len(parts) < 2:
        return Resolution(ResolutionKind.UNADJUDICABLE, detail=f"not dotted: {subject!r}")

    module_parts, member_name = parts[:-1], parts[-1]
    candidate_relpaths = [
        "/".join(module_parts) + ".py",
        "/".join(module_parts) + "/__init__.py",
    ]
    for relpath in candidate_relpaths:
        if index.has_path(relpath):
            file_path = repo_root / relpath
            try:
                tree = ast.parse(file_path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                return Resolution(ResolutionKind.UNADJUDICABLE, detail=f"parse failed: {exc}")
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == member_name:
                        return Resolution(ResolutionKind.RESOLVED)
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == member_name:
                            return Resolution(ResolutionKind.RESOLVED)
                if isinstance(node, ast.AnnAssign):
                    if isinstance(node.target, ast.Name) and node.target.id == member_name:
                        return Resolution(ResolutionKind.RESOLVED)
            return Resolution(ResolutionKind.ABSENT, detail=f"not found in {relpath}")

    module_dotted = ".".join(module_parts)
    return Resolution(ResolutionKind.ABSENT, detail=f"module not tracked: {module_dotted}")


def resolve_row(row: Row, index: TrackedFileIndex, repo_root: Path) -> Resolution:
    """Answer, for one register row, whether the subject it names still exists.

    Branches on `row.declared_class`: path classes resolve against `index`
    as a set/dict lookup (no per-row spawn, no per-row parse); dotted
    classes resolve by AST against the file the index locates (no per-row
    import). `OPAQUE` rows always report unadjudicable.
    """
    declared = row.declared_class

    if declared is SubjectClass.OPAQUE:
        return Resolution(ResolutionKind.UNADJUDICABLE, detail="declared opaque")

    if declared is SubjectClass.REPO_PATH:
        relpath = row.subject.replace("\\", "/")
        if index.has_path(relpath):
            return Resolution(ResolutionKind.RESOLVED)
        return Resolution(ResolutionKind.ABSENT, detail=f"not tracked: {relpath}")

    if declared is SubjectClass.BARE_FILENAME:
        matches = index.paths_for_basename(row.subject)
        if matches:
            return Resolution(ResolutionKind.RESOLVED, detail=",".join(matches))
        return Resolution(ResolutionKind.ABSENT, detail=f"no tracked file named: {row.subject}")

    if declared in _DOTTED_CLASSES:
        return _resolve_dotted(row.subject, index, repo_root)

    return Resolution(ResolutionKind.UNADJUDICABLE, detail=f"unknown class: {declared}")


def rows_that_do_not_resolve(
    rows: list[Row], index: TrackedFileIndex, repo_root: Path
) -> list[tuple[Row, Resolution]]:
    """The reads-as-CLOSURE check: which named subjects are gone.

    Returns every row whose resolution is `absent` -- a non-empty result
    means named subjects no longer exist on disk. This is the finding the
    sweep exists to surface; it says nothing on its own about whether the
    derivation that produced `rows` was itself sound (see
    `assert_canary_present`).
    """
    dead: list[tuple[Row, Resolution]] = []
    for row in rows:
        resolution = resolve_row(row, index, repo_root)
        if resolution.absent:
            dead.append((row, resolution))
    return dead


def assert_canary_present(derived_set: frozenset, canary: frozenset) -> None:
    """The reads-as-THE-OLD-VALUE check: a named minimum membership must survive.

    Non-emptiness of a derived result does not discriminate a wrong root
    (an empty or degenerate index that trivially "passes" every check) from
    a right one (a genuinely sound derivation). This asserts that `canary`
    -- a fixed, known-good minimum membership -- is still a subset of
    `derived_set`, catching the case where a derivation silently produces
    an empty or truncated result that would otherwise read as "no dead
    rows found."
    """
    missing = canary - derived_set
    if missing:
        raise AssertionError(
            f"canary members missing from derived set: {sorted(missing)!r} -- "
            "the derivation likely regressed to an empty or truncated result, "
            "not a genuinely clean sweep"
        )
