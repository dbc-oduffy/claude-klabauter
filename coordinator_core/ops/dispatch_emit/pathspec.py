"""
coordinator_core.ops.dispatch_emit.pathspec — waves -> commit pathspecs and a
terminal test scope.

Purpose: the single pathspec-derivation entry point for the dispatch-emit
pipeline (docs/plans/2026-08-12-emitter-turns-a-spine-into-one-workflow.md
§ C3). Consumes the ``WaveRow`` lists ``wave_map.build_waves`` produces and
derives two DIFFERENT things from them — a per-wave commit pathspec (AC3,
AC4) and a whole-spine terminal test scope (AC9, AC10, AC16). Both share one
derivation rule (declared ``writes:``, falling back to ``surface:`` only
when it is a concrete path) and one refusal shape (name the rows, never
emit an empty result), but they are NOT the same union — see § Pathspec vs
test scope below.

## Negative spec (AC3, AC9) — read this before "fixing" an empty result

There is NO code path in this module that enumerates the tree, globs a
directory, or shells out to git status. Concretely, this module never calls
``os.walk``, ``glob.glob``, ``Path.glob``/``Path.rglob``/``Path.iterdir``,
or any ``subprocess`` function. The two places this module DOES touch disk
(``is_concrete_surface``'s no-suffix branch, ``_map_written_path_to_test_
target``'s co-located-test-file check) each probe exactly ONE fully-derived
candidate path with ``Path.is_file()`` — a targeted existence check on a
path the caller already named, never a directory listing. That distinction
matters: an existence check on a known path cannot discover anything the
spine didn't already declare, so it can never smuggle tree-survey back in.

The tempting fix for an empty pathspec or an empty test scope is exactly
the forbidden one — "just glob for what changed" or "ask git status" — and
the next reader will reach for it unless this file says not to. Refuse
instead (see ``NoWritesDeclaredError``/``NoTestTargetError`` below); a
fail-loud refusal is the correct behaviour for an under-declared spine, not
a bug to route around by widening the derivation.

## Pathspec vs test scope — different codomains (staff review correction)

A commit pathspec's members are ANY path a commit may carry — a doc, a
shebang script, a config file, anything ``writes:``/``surface:`` names.
``commit_pathspec`` returns exactly those paths, deduplicated, in row
order.

A terminal test scope's members must be RUNNABLE TEST TARGETS. Copying the
pathspec union in would put ``coordinator_core/subagent_sandbox/
CONTRACT.md`` and ``coordinator/bin/coordinator-doc-new.py`` — a markdown file
and a shebang script — into a pytest invocation, which is not a thing that
runs. ``terminal_test_scope`` therefore maps each written path through
``_map_written_path_to_test_target`` (a doc or non-``.py`` path maps to
nothing) rather than returning the written-path union itself.

This package's own convention — the one this module encodes because no
repo-wide "locate a module's tests" helper exists — is the co-located
``tests/test_<stem>.py`` file next to the source module (see
``spine_read.py`` -> ``tests/test_spine_read.py``, ``wave_map.py`` ->
``tests/test_wave_map.py``, this very module -> ``tests/test_pathspec.py``).
A path with no such file present, or with no ``.py`` suffix at all, maps to
nothing.

## The sharp edge AC16 exists for

A doc-only spine (every row's ``writes:`` names only docs) DOES declare
``writes:`` — it passes AC10's literal wording, which only checks whether
any row declared the field. But if every declared path maps to no test
target, ``terminal_test_scope`` would otherwise return an EMPTY list, which
is a green test run over nothing — strictly worse than refusing, because it
reports success rather than absence. ``terminal_test_scope`` refuses
(``NoTestTargetError``) in that case too, naming the paths that mapped to
nothing.

## The executed premise this module's output inherits (AC14)

The orphan-claim gate that ``git-commit-agent`` enforces at runtime binds
the DISPATCHED committer, not the EM. Verified 2026-08-12: a dispatched
agent refused a heredoc-written path (``BLOCKED ... (orphan — dir...``),
while the EM's own ``scoped-git-commit`` accepted the IDENTICAL path at
``01e183d584c5``. A correctly-derived pathspec from this module is not
automatically claimable at runtime — this module derives provenance only
(AC3/AC4); it has no way to prove or enforce claimability, because that
gate evaluates state (working-tree diff, orphan status) this module never
touches. A future reader must not assume EM latitude carries over to an
emitted commit phase: it does not, and an emitted phase that assumes
otherwise will refuse at runtime with nobody present to widen it.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_REPO_ROOT = Path(__file__).resolve().parents[3]


class NoWritesDeclaredError(ValueError):
    """Raised when no row in scope declares ``writes:`` (AC4, AC10).

    Covers both granularities this module derives at: a single wave (AC4,
    ``commit_pathspec``) and the whole spine (AC10, ``terminal_test_scope``).
    Never resolved by emitting an empty pathspec/scope — see module
    docstring.
    """


class NoTestTargetError(ValueError):
    """Raised when every written path maps to no runnable test target (AC16).

    Distinct from ``NoWritesDeclaredError``: this fires even though
    ``writes:`` WAS declared (satisfying AC10's literal wording) because
    every declared path is a doc or non-Python file with no co-located test
    file — see module docstring § The sharp edge AC16 exists for.
    """


def is_concrete_surface(surface: str, *, repo_root: Path | None = None) -> bool:
    """True if ``surface`` is a concrete path the pathspec fallback may use.

    The fallback rule (AC3) says "use ``surface:`` only when it is a
    concrete path, never when it names a subsystem or a directory-shaped
    concept." Three cases decide False explicitly:

      - A trailing ``/`` (``coordinator_core/ops/`` — directory-shaped by
        construction, never a single committable file).
      - A path with no suffix that matches no file on disk (a targeted,
        single-path existence check — see module docstring's negative
        spec; NOT a directory listing).
      - A bare package/subsystem name (e.g. ``dispatch_emit``) — this is
        the same case as the one above: no suffix, and no file at that
        exact path exists.

    A path WITH a suffix (``pathspec.py``, ``CONTRACT.md``) is concrete
    unconditionally — no disk check needed, since a suffixed path names a
    specific file by construction, not a subsystem.

    Hidden-dotfile edge case: ``PurePosixPath.suffix`` treats a leading dot
    as the filename, not an extension marker, so a hidden file with no
    further extension (``.gitignore``, ``.env``) has an EMPTY ``.suffix``
    and falls through to the disk-existence check like a bare
    package/subsystem name — it is only judged concrete if it actually
    exists on disk at that exact path, not unconditionally. A hidden file
    that DOES carry a further extension (``.github/workflows/x.yml``,
    ``.env.local``) has a non-empty ``.suffix`` (``.yml``, ``.local``) and
    is concrete unconditionally, same as any other suffixed path — the
    leading-dot directory/filename segment plays no special role there.
    """
    if not surface or surface.endswith("/"):
        return False
    candidate = PurePosixPath(surface)
    if candidate.suffix:
        return True
    root = repo_root or _REPO_ROOT
    return (root / surface).is_file()


def _declared_paths(row: WaveRow) -> list[str]:
    """Resolve one row's contribution to a pathspec: declared ``writes:``,
    falling back to ``surface:`` only when concrete (AC3). A row with
    neither (UNDECLARED writes and a non-concrete surface) contributes
    nothing — it does not, by itself, trigger the wave/spine-level refusal;
    see ``NoWritesDeclaredError``'s docstring for that boundary.
    """
    if row.writes is not UNDECLARED:
        return list(row.writes)
    if is_concrete_surface(row.surface):
        return [row.surface]
    return []


def _dedupe(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _named_rows(rows) -> str:
    return ", ".join(repr(row.id) for row in rows)


def commit_pathspec(wave: list[WaveRow]) -> list[str]:
    """Derive the commit pathspec for the commit phase following ``wave``
    (AC3). Refuses (``NoWritesDeclaredError``) if NO row in ``wave``
    declares ``writes:`` (AC4) — naming every row in the wave, since with
    zero declarations there is no way to single out a subset as "the"
    offenders.
    """
    declares_writes = [row for row in wave if row.writes is not UNDECLARED]
    if not declares_writes:
        raise NoWritesDeclaredError(
            "wave has no row declaring writes: refusing to emit a commit "
            f"phase pathspec (rows: {_named_rows(wave)})"
        )
    paths: list[str] = []
    for row in wave:
        paths.extend(_declared_paths(row))
    paths = _dedupe(paths)
    if not paths:
        # Review: code-reviewer (wsc-B) -- every row declared writes: (an
        # explicit []), not UNDECLARED, so the check above didn't fire, but
        # zero rows contributed a path. Emitting [] here would produce a
        # commit phase with an empty pathspec -- a dispatched commit agent
        # told to commit nothing. Refuse instead of emitting.
        raise NoWritesDeclaredError(
            "every row in the wave declared an empty writes: [], refusing "
            f"to emit an empty commit phase pathspec (rows: {_named_rows(wave)})"
        )
    return paths


def _map_written_path_to_test_target(path: str, *, repo_root: Path | None = None) -> str | None:
    """Map one written path to its runnable test target, or ``None``.

    Encodes this package's own co-located ``tests/test_<stem>.py``
    convention (``spine_read.py`` -> ``tests/test_spine_read.py``, and so
    on) — no repo-wide "locate a module's tests" helper exists to defer to.
    A non-``.py`` path (a doc, a shebang script) maps to ``None``
    immediately: there is no Python test file that could exercise it under
    this convention. A ``.py`` path with no co-located test file present
    also maps to ``None`` — a single, targeted ``Path.is_file()`` probe on
    the one derived candidate, never a directory listing (see module
    docstring's negative spec).
    """
    candidate = PurePosixPath(path)
    if candidate.suffix != ".py":
        return None
    root = repo_root or _REPO_ROOT
    test_name = f"test_{candidate.stem}.py"
    coloc_tests_dir = candidate.parent / "tests" / test_name
    if (root / coloc_tests_dir).is_file():
        return coloc_tests_dir.as_posix()
    coloc_same_dir = candidate.parent / test_name
    if (root / coloc_same_dir).is_file():
        return coloc_same_dir.as_posix()
    return None


def terminal_test_scope(waves: list[list[WaveRow]], *, repo_root: Path | None = None) -> list[str]:
    """Derive the terminal ``coordinator:test-runner`` scope across every
    wave in ``waves`` (AC9). Refuses (``NoWritesDeclaredError``) if NO row
    in the whole spine declares ``writes:`` (AC10). Refuses
    (``NoTestTargetError``) if every declared path maps to no runnable test
    target — a doc-only spine (AC16) satisfies AC10's literal wording while
    still producing nothing runnable, which is the sharper failure AC16
    exists to catch; see module docstring.

    NOT the same union as ``commit_pathspec`` — every written path is
    mapped through ``_map_written_path_to_test_target`` first, so a doc or
    non-Python written path drops out rather than landing in the scope
    verbatim (staff review correction; see module docstring § Pathspec vs
    test scope).
    """
    all_rows = [row for wave in waves for row in wave]
    declares_writes = [row for row in all_rows if row.writes is not UNDECLARED]
    if not declares_writes:
        raise NoWritesDeclaredError(
            "spine has no row declaring writes: refusing to emit a "
            f"terminal test scope (rows: {_named_rows(all_rows)})"
        )

    written_paths: list[str] = []
    for row in all_rows:
        written_paths.extend(_declared_paths(row))
    written_paths = _dedupe(written_paths)

    targets: list[str] = []
    unmapped: list[str] = []
    for path in written_paths:
        target = _map_written_path_to_test_target(path, repo_root=repo_root)
        if target is None:
            unmapped.append(path)
        else:
            targets.append(target)
    targets = _dedupe(targets)

    if not targets:
        raise NoTestTargetError(
            "every written path mapped to no runnable test target, "
            f"refusing an empty terminal test scope (paths: {unmapped!r})"
        )
    return targets
