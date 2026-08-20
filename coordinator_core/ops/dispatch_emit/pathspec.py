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
target``'s co-located-test-file check) each probe only fully-derived
candidate paths with ``Path.is_file()`` — targeted existence checks on
paths derived from what the caller already named, never a directory
listing. That distinction matters: an existence check on a known path
cannot discover anything the spine didn't already declare, so it can never
smuggle tree-survey back in.

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
``_map_written_path_to_test_target`` (an uncovered path maps to nothing)
rather than returning the written-path union itself.

This package's own convention — the one this module encodes because no
repo-wide "locate a module's tests" helper exists — is the co-located
``tests/test_<stem>.py`` file next to the written path (see
``spine_read.py`` -> ``tests/test_spine_read.py``, ``wave_map.py`` ->
``tests/test_wave_map.py``, this very module -> ``tests/test_pathspec.py``).
A path with no such file present maps to nothing.

The derivation is by STEM, not by suffix: a data fixture whose driver test
is named for it (``engine-root-conformance.json`` ->
``tests/test_engine_root_conformance.py``) resolves the same way a module
does, so a config-only row alone in its wave is emittable rather than
refused on a surface that is in fact covered. What it must never become is
resolution by directory proximity, or by scanning test bodies for a
citation of the written path — that looser cut resolves a wiki doc to an
unrelated test that merely names it in an assertion message. An uncovered
doc still maps to nothing, and a row writing only such paths still refuses.

## The sharp edge AC16 exists for

A doc-only spine (every row's ``writes:`` names only docs) DOES declare
``writes:`` — it passes AC10's literal wording, which only checks whether
any row declared the field. But if every declared path maps to no test
target, ``terminal_test_scope`` would otherwise return an EMPTY list, which
is a green test run over nothing — strictly worse than refusing, because it
reports success rather than absence. ``terminal_test_scope`` refuses
(``NoTestTargetError``) in that case too, naming the paths that mapped to
nothing.

## ``writes: []`` vs an absent ``writes:`` key — the asymmetry is real, and deliberate (finding A4)

These are NOT interchangeable authoring choices, and the difference is
sharper than it looks. Read ``spine_read``'s ``UNDECLARED`` sentinel
docstring first: omitting ``writes:`` entirely (or leaving it empty/``null``)
gets a row the ``UNDECLARED`` sentinel; writing ``writes: []`` explicitly
gets it a real, empty list — a structurally different value, by design.

For an ``epistemic-premise``-gated row (one whose predecessor decides
whether the row should even exist, so there is no surface to author against
yet), OMISSION is the safe choice and ``[]`` is the hazardous one — the
opposite of what the two spellings look like they'd mean to an author
reaching for "I don't have a value yet." Omitting the key:

  - forces ``wave_map._writes_overlap`` to treat the row as unable to share
    a wave with ANYTHING (UNDECLARED can't be proven disjoint from
    anything, including another UNDECLARED row) — so ``build_waves`` always
    lands it alone in its own wave;
  - which means ``commit_pathspec`` sees it solo, and the AC4
    ``declares_writes`` check refuses immediately and loudly, before any
    other row's work is anywhere near this row's fate.

Declaring ``writes: []`` does neither of those things: an empty list is
NOT the UNDECLARED sentinel, so ``_writes_overlap`` sees no claimed overlap
with anything and the row can freely land in a wave alongside rows that DO
declare real writes. That is exactly the shape finding A4 closed — see
``commit_pathspec``'s docstring for the per-row zero-contribution check
that catches it, and staff review finding P0-1 for why that check WARNS
rather than raises when the row shares a wave with a real-contributing
sibling. The two spellings end in DIFFERENT outcome classes, not the same
one, and by different mechanisms and at different moments: an
omitted-``writes:`` row lands solo in its own wave and hits the AC4
whole-wave ``NoWritesDeclaredError`` refusal immediately, before any
dispatch happens for that row (nothing in the wave declares writes at
all); a ``writes: []`` row sharing a wave with a real-contributing row
instead gets a named log warning at ``commit_pathspec`` time — which, per
this module's own negative spec above, runs to derive the commit phase
FOLLOWING a wave that has, by then, already been dispatched and executed —
and is silently excluded from that wave's pathspec, loud-not-silent but
not illegal. A `[]`-declared epistemic-premise row therefore still wastes
a real dispatch before the warning fires; it merely no longer LOSES that
dispatch's output without a trace. The one shape that still raises for
``writes: []`` is when NOTHING in the wave contributes — every row
declared ``[]`` — because an empty pathspec is never a legal return
regardless of which spelling produced it (``commit_pathspec``'s docstring,
refusal 2).

This is left as a documented asymmetry rather than reconciled into one
mechanism: reconciling would mean either (a) treating ``writes: []`` as
UNDECLARED, which destroys the one thing AC2 exists to preserve — a spine
author's ability to assert "this row genuinely writes nothing" as a fact
distinct from "unknown" — or (b) relaxing UNDECLARED to tolerate wave-
sharing, which reopens the exact "cannot prove disjoint" hazard point 1 of
this module's own docstring (spine_read's AC2 discussion) already rejected.
Neither is a fix; both trade one hazard for a worse one. The correct
authoring guidance, not a mechanical guarantee, is: for a row whose surface
is genuinely unknown (epistemic-premise, unresolved predecessor), OMIT
``writes:`` — never spell it ``[]``. ``writes: []`` is for a row whose
author is CERTAIN, right now, that it writes nothing at all: a real,
non-code declaration (operational/EM-owned rows, ``change_kind:
verification`` rows) that this module must accept, not an illegal spelling
(staff review finding P0-1; see ``commit_pathspec``'s docstring for the
per-row zero-contribution check that WARNS on it rather than refusing when
a real-contributing sibling is present).

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

import logging
from pathlib import Path, PurePosixPath
from typing import cast

from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]


class NoWritesDeclaredError(ValueError):
    """Raised in two shapes, both from ``commit_pathspec`` and
    ``terminal_test_scope``'s equivalent whole-spine check — see each
    function's own docstring for the exact boundary:

      1. No row in scope declares ``writes:`` at all — every row is
         UNDECLARED with a non-concrete surface (AC4, AC10). With zero
         declarations there is no way to single out a subset as "the"
         offenders, so this names every row in scope.
      2. Every row DOES declare, but the union of their contributions is
         still empty — every row declared ``writes: []``. Names the
         zero-contributing rows (in this shape, every row in scope). This
         shape exists because a per-row ``writes: []`` is NOT, by itself,
         an error (see case below) — but a wave/spine whose declared
         writes contribute nothing must still refuse rather than emit an
         empty pathspec/scope, per the module docstring's negative spec
         ("name the rows, never emit an empty result").

    A row that declares ``writes: []`` while SHARING scope with a
    real-contributing row is a different case and does NOT raise this
    error — that per-row zero contribution is a named ``_logger.warning``,
    not a refusal (staff review finding P0-1; see ``commit_pathspec``'s
    docstring). Making ``writes: []`` categorically fatal, for every row
    regardless of its wave-mates, made it an unrepresentable state for a
    row whose author is certain, right now, that it writes nothing at all
    (operational/EM-owned rows, ``change_kind: verification`` rows) — see
    module docstring's § ``writes: []`` vs an absent ``writes:`` key.
    """


class NoTestTargetError(ValueError):
    """Raised when every written path maps to no runnable test target (AC16).

    Distinct from ``NoWritesDeclaredError``: this fires even though
    ``writes:`` WAS declared (satisfying AC10's literal wording) because
    no declared path has a co-located test file named for its stem — see
    module docstring § The sharp edge AC16 exists for.
    """


class DirectoryShapedWriteError(ValueError):
    """Raised when a row's declared ``writes:`` entry is directory-shaped
    (a trailing ``/``, e.g. ``state/memo-outbox/sent/``) rather than a
    single committable file.

    ``scoped-git-commit`` refuses a directory pathspec by design — it has
    no unscoped mode by construction. ``is_concrete_surface`` already
    applies this exact trailing-slash check to the ``surface:`` fallback
    (AC3), but the ``writes:`` primary path skipped it entirely whenever a
    row declared ``writes:`` at all — the fallback path was guarded, the
    primary path was not. Left uncaught, a directory-shaped ``writes:``
    entry survives spine-derivation and schema validation alike and is only
    discovered when a dispatched committer refuses it at runtime, by which
    point the whole wave's work is stranded uncommitted. This check moves
    that refusal to spine-derivation time, where the offending row is still
    identifiable — named in the message, unlike the runtime refusal, which
    fires against the whole wave's union with no row attribution.
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

    Every declared ``writes:`` entry is checked for directory shape (a
    trailing ``/`` or, since a Windows-authored spine is first-class here,
    ``\\``) before being returned — the same trailing-separator-SHAPE check
    ``is_concrete_surface`` already applies to the ``surface:`` fallback
    below, closing the hole where that check ran on the fallback path but
    not the primary one (see ``DirectoryShapedWriteError``). Deliberately
    NOT an on-disk ``is_dir()`` test (Review: staff-eng, Finding 13): a bare
    ``state/memo-outbox/sent`` (no trailing separator) naming a real
    directory is not caught here — spine derivation must not depend on
    worktree state, so parity with ``is_concrete_surface`` is trailing-
    separator-shape only, not full on-disk discrimination.
    """
    if row.writes is not UNDECLARED:
        # `WaveRow.writes` is typed `object` so one field can carry either a
        # real list or the UNDECLARED sentinel. Identity against the sentinel
        # is the documented gate (never truthiness — `writes: []` is a
        # POSITIVE declaration), but it does not narrow for a type checker,
        # and the sentinel is that field's only non-list inhabitant.
        declared = cast("list[str]", row.writes)
        for path in declared:
            if path.endswith("/") or path.endswith("\\"):
                raise DirectoryShapedWriteError(
                    f"{row.id}'s `writes:` entry {path!r} is directory-shaped"
                )
        return list(declared)
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
    (AC3). Two distinct refusals, both ``NoWritesDeclaredError``, both
    naming rows, because both guard the same invariant — this function
    never returns an empty pathspec (module docstring's negative spec:
    "name the rows, never emit an empty result"):

      1. NO row in ``wave`` declares ``writes:`` at all (AC4) — every row
         is UNDECLARED with a non-concrete surface. Fires before any
         per-row contribution is computed, naming every row in the wave,
         since with zero declarations there is no way to single out a
         subset as "the" offenders.
      2. Every row DOES declare (none UNDECLARED), but the union of their
         contributions is still empty — every row declared ``writes: []``.
         Fires AFTER the union, naming the rows that contributed nothing
         (which, in this all-empty shape, is every row in the wave).

    Between those two refusals sits the WARN-not-raise case (staff review
    finding P0-1): if SOME but not all rows in ``wave`` declare ``writes:
    []`` while others contribute real paths, the zero-contributing rows are
    excluded from the returned pathspec and named in a ``_logger.warning``,
    but the wave is not refused — the real-contributing rows' paths are
    still returned. That row's dispatched executor still ran and may have
    written real files this pathspec never carries, and no later wave's
    (file-pathspec, not directory-pathspec) commit phase sweeps them up
    either — the warning exists to surface that loudly, not silently. An
    earlier version of this check RAISED per row instead of warning here;
    that made ``writes: []`` an unrepresentable state — a single such row
    anywhere in a spine failed ``dispatch.emit`` for the WHOLE plan, even
    when sharing a wave with real-contributing rows, including for rows
    whose author declared it deliberately (operational/EM-owned rows,
    ``change_kind: verification`` rows — real, open, non-deferred rows on
    disk do this). See module docstring § ``writes: []`` vs an absent
    ``writes:`` key and ``NoWritesDeclaredError``'s docstring.

    Refusal 2 is what keeps the ALL-empty case from falling through that
    same warn-and-continue path into an empty return: an all-``writes: []``
    wave has nothing UNDECLARED, so refusal 1's ``declares_writes`` check
    does not fire, and every row is zero-contributing, so the union is
    empty — the warning still logs (both fire together in this shape), but
    the empty result additionally raises, because an empty pathspec handed
    to a commit phase is never a legal return regardless of which spelling
    produced it.

    An UNDECLARED row that also contributes zero paths (unknown surface, no
    concrete-surface fallback) is a different case, not covered by either
    of these — ``wave_map._writes_overlap`` forces every UNDECLARED row
    into its own solitary wave, so it can only ever reach here alongside
    other rows via a hand-built wave bypassing ``build_waves`` (as this
    module's own test suite does for coverage) — refusal 1 above already
    covers the real, ``build_waves``-reachable UNDECLARED case.
    """
    declares_writes = [row for row in wave if row.writes is not UNDECLARED]
    if not declares_writes:
        raise NoWritesDeclaredError(
            "wave has no row declaring writes: refusing to emit a commit "
            f"phase pathspec (rows: {_named_rows(wave)})"
        )

    zero_contribution = [
        row
        for row in wave
        if row.writes is not UNDECLARED and not _declared_paths(row)
    ]
    if zero_contribution:
        _logger.warning(
            "writes: [] declared, excluded from this wave's commit "
            "pathspec (rows: %s)",
            _named_rows(zero_contribution),
        )

    paths: list[str] = []
    for row in wave:
        paths.extend(_declared_paths(row))
    paths = _dedupe(paths)
    if not paths:
        raise NoWritesDeclaredError(
            "wave's declared writes contribute no paths: refusing to emit "
            f"an empty commit phase pathspec (rows: {_named_rows(wave)})"
        )
    return paths


def _normalized_test_name(stem: str) -> str:
    """``engine-root-conformance`` -> ``test_engine_root_conformance.py``.

    Separator normalization is the point: a hyphenated stem (a CLI script, a
    JSON fixture) cannot appear in an importable Python test module name, so
    a verbatim ``f"test_{stem}.py"`` derives a candidate that can never
    exist on disk — the derivation would be inert for exactly the paths the
    non-``.py`` rungs below exist to resolve.
    """
    return f"test_{stem.replace('-', '_')}.py"


def _map_written_path_to_test_target(path: str, *, repo_root: Path | None = None) -> str | None:
    """Map one written path to its runnable test target, or ``None``.

    Encodes this package's own co-located ``tests/test_<stem>.py``
    convention (``spine_read.py`` -> ``tests/test_spine_read.py``, and so
    on) — no repo-wide "locate a module's tests" helper exists to defer to.
    A path with no such file present maps to ``None`` — a single, targeted
    ``Path.is_file()`` probe per derived candidate, never a directory
    listing (see module docstring's negative spec).

    The same stem derivation runs for a data fixture as for a module. A
    driver test bound to ``engine-root-conformance.json`` is named for that
    fixture, so a config-only row resolves to it and becomes emittable
    instead of refusing emission on a surface that IS covered.

    Negative spec (DoE improvement-queue entry
    ``2026-08-20-engine-s-map-written-path-to-test-target-72c8a48a56c7``): a
    non-``.py`` path resolves ONLY through this stem derivation — never by
    directory proximity, and never because some test cites the path in an
    assertion message. An earlier cut of that rule scanned test bodies for a
    citation and resolved a wiki doc to an unrelated test that merely
    mentioned it. A doc with no test named for it still maps to ``None``, so
    a row writing only uncovered non-code refuses rather than emitting an
    empty terminal scope.
    """
    candidate = PurePosixPath(path)
    root = repo_root or _REPO_ROOT
    test_name = _normalized_test_name(candidate.stem)
    for target in (candidate.parent / "tests" / test_name, candidate.parent / test_name):
        if (root / target).is_file():
            return target.as_posix()
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
