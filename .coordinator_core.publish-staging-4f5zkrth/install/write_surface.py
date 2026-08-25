"""
coordinator_core.install.write_surface — the write-surface declaration
protocol every install-side writer in this repo exports its write surface
through.

Spec backlink: pln-writer-declared-write-surface-49d3bd,
chunk C1

Purpose: the writer-declared write-surface manifest exists so that "what did
install touch on disk" has one authoritative, machine-checkable answer
instead of three hand-maintained prose mirrors going stale independently
(the registry-manifest precedent this plan cites, `docs/wiki/
coordinator-core-engine.md`). This module is the vocabulary a writer
declares AGAINST — the frozen, external, eight-kind taxonomy DoE accepted
2026-08-06, plus the container shapes (`WriteSurfaceEntry`, its two
sibling clause forms, and `WriteSurfaceDeclaration`) a peer chunk (C2/C3)
imports to write one `WriteSurfaceDeclaration` per writer module.

Two declaration forms are co-equal first-class citizens, not one primary
and one bolt-on:

  - STATIC form (`StaticClause`): a literal, enumerable list of entries.
    The natural fit for a writer whose surface is a fixed constant already
    in source (e.g. `configure_git._SETTINGS`).
  - SHAPED form (`ShapedClause`): a discovery mechanism (free text naming
    the function/mechanism that computes the surface at runtime) plus an
    entry TEMPLATE (a `WriteSurfaceEntry` with template placeholders in its
    `key`/`path`, e.g. ``"repos.<derived-key>"``) describing the shape
    entries take without enumerating them. The dominant case: 7 of 11
    surveyed writers cannot enumerate their key set in source at all — the
    keys themselves are only known at runtime (a discovered input set),
    not merely conditional on one. A platform branch or consent gate over
    an otherwise KNOWN, literal key set stays STATIC; the condition is
    carried in the entry's `reason=` text, not in the declaration's shape.

Worked example — STATIC form, one writer, one clause:

    >>> from coordinator_core.install.write_surface import (
    ...     WriteSurfaceDeclaration, StaticClause, WriteSurfaceEntry,
    ... )
    >>> decl = WriteSurfaceDeclaration(
    ...     writer_id="configure-git",
    ...     source_module="coordinator_core.ops.configure_git",
    ...     clauses=(
    ...         StaticClause(
    ...             entries=(
    ...                 WriteSurfaceEntry(kind="git-config-key", key="gc.autoDetach"),
    ...                 WriteSurfaceEntry(kind="git-config-key", key="core.checkStat"),
    ...             ),
    ...         ),
    ...     ),
    ... )

Worked example — SHAPED form, one writer, one generator-derived clause:

    >>> decl2 = WriteSurfaceDeclaration(
    ...     writer_id="register-discovered-repos",
    ...     source_module="coordinator_core.ops.register_discovered_repos",
    ...     clauses=(
    ...         ShapedClause(
    ...             discovered_by="discover_working_repos",
    ...             entry_template=WriteSurfaceEntry(
    ...                 kind="machine-local-key",
    ...                 key="repos.<derived-key>",
    ...             ),
    ...         ),
    ...     ),
    ... )

A writer's declaration is a COLLECTION of clauses (`clauses: tuple[...]`),
never a single clause — `install-substrate` carries four, `scaffold_structure`
carries four. Mixing static and shaped clauses on one writer is expected,
not an error.

Marker-delimited surfaces (`rc-block`, `hook-gate-region`) carry a marker
PAIR on the entry: `begin_marker` and `end_marker`. `end_marker` is a
tri-state field, not `Optional[str]` collapsed to "absent means error" —
its literal value `"absent-on-legacy-installs"` is a permanent, valid state
(pre-existing installs from before an END marker was added will never be
retrofitted; see chunk C6). `validate()` treats that literal as clean, not
as a defect.

`declared-empty` and `undeclared` are different, both first-class,
representable facts about a writer, not the presence/absence of a Python
value:

  - UNDECLARED: no `WriteSurfaceDeclaration` exists for the writer at all.
    This module cannot represent "nothing" as a value for an undeclared
    writer — the C4 emission op detects it by absence from whatever
    registry of declarations it walks, and that is deliberately out of
    this module's remit (see Negative spec).
  - DECLARED-EMPTY: a writer explicitly asserts it writes nothing, via
    `WriteSurfaceDeclaration(clauses=())` — an explicit empty tuple is a
    claim, not a gap. `validate()` accepts `clauses=()` as clean.

Every clause carries an `effect` discriminator (`"write"` or `"delete"`),
defaulting to `"write"`. A `"delete"` clause describes a removal this
writer performs (e.g. the consent-gated Windows AppX stub removal) rather
than something it writes.

Negative spec — this module does NOT:
  - emit the manifest file or any rendered/serialized output (C4's job);
  - author any writer's declaration (C2/C3's job — this module supplies
    only the vocabulary and container shapes those chunks import);
  - read or write any file, environment variable, or registry key on disk
    — every value here is in-memory only;
  - validate that a declared surface matches what a writer's source code
    actually does at runtime (a lockstep/drift check, out of scope here).
"""

from __future__ import annotations

from dataclasses import dataclass, field

WRITE_SURFACE_KINDS: tuple[str, ...] = (
    "git-config-key",
    "machine-local-key",
    "os-env-var",
    "file-path",
    "rc-block",
    "hook-gate-region",
    "structured-file-key",
    "line-membership",
)
"""The frozen, externally-agreed eight-kind vocabulary. Final as of DoE's
2026-08-06 acceptance memo; DoE's schema warns rather than fails on an
unrecognized kind, so a ninth kind is a one-line addition on their side and
never blocks emission on ours — but it is never a reason to invent a kind
here without updating this tuple first."""

WRITE_SURFACE_EFFECTS: tuple[str, ...] = ("write", "delete")
"""The effect discriminator. Defaults to "write"; "delete" names a removal
(e.g. the consent-gated Windows AppX stub removal) rather than a write."""

ABSENT_ON_LEGACY_INSTALLS = "absent-on-legacy-installs"
"""The literal sentinel for a marker-delimited entry's end marker when
pre-existing installs predate that end marker being added. A permanent,
valid, non-error state — never retrofitted onto installs that predate it.

Public because every consumer of the tri-state — a declaring writer (C3),
the END-marker chunk (C6), and the uninstall classifier that routes this
state to cannot-reverse-precisely (C8) — must compare against ONE spelling.
A private constant would have each of them retyping the string literal,
which is the transcribed-copy defect this plan exists to delete."""


@dataclass(frozen=True)
class WriteSurfaceEntry:
    """One concrete (or, inside a `ShapedClause`, template) write-surface
    entry: a stable kind plus whichever of `key`/`path` that kind uses, and
    — for the two marker-delimited kinds — a marker pair.

    `kind` MUST be one of `WRITE_SURFACE_KINDS`; `validate()` reports a
    structured error rather than raising if it is not.

    `key` is used by key-shaped kinds (`git-config-key`,
    `machine-local-key`, `os-env-var`, `structured-file-key`); `path` is
    used by path/file-shaped kinds (`file-path`, `rc-block`,
    `hook-gate-region`, `line-membership`). Exactly one of `key`/`path`
    need be populated for a given kind — `validate()` does not enforce
    which, since a `ShapedClause` template may legitimately use a
    placeholder in either field (e.g. `key="repos.<derived-key>"`).

    `begin_marker`/`end_marker` apply only to `rc-block`/`hook-gate-region`
    entries. `end_marker` is a three-state field:
      - a literal end-marker string — the common case;
      - `None` — not applicable / not yet declared;
      - the literal string `"absent-on-legacy-installs"` — a
        BEGIN-only marker region whose END marker was added after some
        installs already existed, and will never be retrofitted onto them.
        This is a valid, expected value, not an error state.
    """

    kind: str
    key: str | None = None
    path: str | None = None
    begin_marker: str | None = None
    end_marker: str | None = None
    effect: str = "write"
    reason: str | None = None
    """Free-text justification, used by the stated-reason escape hatch for
    an entry whose true surface is not yet knowable (e.g. a cross-plane
    call site pending the other side's declaration)."""
    synthetic: bool = False
    """True only for a placeholder `WriteSurfaceEntry` fabricated by a
    consumer (e.g. `uninstall_legs.render_uninstall_dry_run_report`'s
    per-unreported-writer placeholder) to stand in for a writer with no
    real declared entry to report -- never set by a genuine declared
    surface. Review: code-reviewer (P3) -- a consumer of `report.records`
    that inspects `.entry.kind`/`.entry.path` directly (bypassing reason
    text) previously had no structural signal a record was synthetic
    rather than a real declared surface. Deliberately NOT a new `kind`
    value: `WRITE_SURFACE_KINDS` is the frozen, externally-agreed
    eight-kind vocabulary DoE's schema consumes (see that tuple's
    docstring) -- a marker field avoids touching that cross-repo contract."""
    unset_group: str | None = None
    """Non-None only for an entry whose reversal must be treated as an
    atomic unit with its group siblings by the uninstall leg (C6): entries
    sharing the same `unset_group` value must all be reversed together, so
    an uninstall can never strand a partially-removed group. Deliberately
    NOT a new `kind` value: `WRITE_SURFACE_KINDS` is the frozen,
    externally-agreed eight-kind vocabulary DoE's schema consumes (see
    that tuple's docstring) -- a marker field avoids touching that
    cross-repo contract."""


@dataclass(frozen=True)
class StaticClause:
    """The STATIC declaration form: a literal, enumerable tuple of
    `WriteSurfaceEntry` values. The natural fit for a writer whose surface
    is already a fixed constant in source."""

    entries: tuple[WriteSurfaceEntry, ...] = field(default_factory=tuple)
    effect: str = "write"


@dataclass(frozen=True)
class ShapedClause:
    """The SHAPED declaration form: a discovery mechanism plus an entry
    template, for a writer whose surface is computed at runtime (a
    platform branch, a consent gate, or a discovered input set) rather
    than enumerable in source.

    `discovered_by` is free text naming the function/mechanism that
    computes the surface (e.g. `"discover_working_repos"`,
    `"_derive_agent_helper_target_map"`). `entry_template` is a
    `WriteSurfaceEntry` whose `key`/`path` may contain template
    placeholders (e.g. `"repos.<derived-key>"`) describing the shape
    entries take without enumerating them. Reserved for a key set that is
    genuinely unknowable in source — not for a known, literal key set that
    is merely written conditionally (platform branch, consent gate); that
    case stays `StaticClause` with the condition carried in `reason=`.
    """

    discovered_by: str
    entry_template: WriteSurfaceEntry
    effect: str = "write"


WriteSurfaceClause = StaticClause | ShapedClause
"""A writer's declaration is a tuple of these — static and shaped clauses
may be freely mixed on one writer."""


@dataclass(frozen=True)
class WriteSurfaceDeclaration:
    """One writer's full declaration: a stable `writer_id`, the module it
    lives in, and a COLLECTION of clauses — never a single clause, since a
    single writer (e.g. `install-substrate`, `scaffold_structure`) may
    legitimately carry multiple clauses of either form.

    `clauses=()` is the explicit DECLARED-EMPTY assertion: this writer has
    been examined and asserts it writes nothing. This is different from,
    and must remain distinguishable from, UNDECLARED — the absence of any
    `WriteSurfaceDeclaration` for a writer at all, which this module
    cannot represent as a value (see module docstring, Negative spec).
    """

    writer_id: str
    source_module: str
    clauses: tuple[WriteSurfaceClause, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ValidationError:
    """One structured problem found in a `WriteSurfaceDeclaration`, keyed
    to the clause/entry index it came from so a caller can report it
    without re-deriving location."""

    writer_id: str
    clause_index: int
    entry_index: int | None
    message: str


def validate(declaration: WriteSurfaceDeclaration) -> tuple[ValidationError, ...]:
    """Check one `WriteSurfaceDeclaration` and return structured errors —
    never raises. An empty return means the declaration is clean,
    including the DECLARED-EMPTY case (`clauses=()`), which is always
    clean.

    Exists so the C4 emission op can emit a partial manifest with a loud
    per-writer problem entry rather than blowing up the whole emission on
    one bad declaration.
    """

    errors: list[ValidationError] = []

    def _check_entry(clause_index: int, entry_index: int | None, entry: WriteSurfaceEntry) -> None:
        if entry.kind not in WRITE_SURFACE_KINDS:
            errors.append(
                ValidationError(
                    writer_id=declaration.writer_id,
                    clause_index=clause_index,
                    entry_index=entry_index,
                    message=f"unrecognized kind {entry.kind!r}; must be one of {WRITE_SURFACE_KINDS}",
                )
            )
        if entry.effect not in WRITE_SURFACE_EFFECTS:
            errors.append(
                ValidationError(
                    writer_id=declaration.writer_id,
                    clause_index=clause_index,
                    entry_index=entry_index,
                    message=f"unrecognized effect {entry.effect!r}; must be one of {WRITE_SURFACE_EFFECTS}",
                )
            )
        if entry.key is None and entry.path is None and entry.reason is None:
            errors.append(
                ValidationError(
                    writer_id=declaration.writer_id,
                    clause_index=clause_index,
                    entry_index=entry_index,
                    message="entry declares neither key, path, nor a stated reason",
                )
            )

    for clause_index, clause in enumerate(declaration.clauses):
        if clause.effect not in WRITE_SURFACE_EFFECTS:
            errors.append(
                ValidationError(
                    writer_id=declaration.writer_id,
                    clause_index=clause_index,
                    entry_index=None,
                    message=f"unrecognized clause effect {clause.effect!r}; must be one of {WRITE_SURFACE_EFFECTS}",
                )
            )
        if isinstance(clause, StaticClause):
            if not clause.entries:
                errors.append(
                    ValidationError(
                        writer_id=declaration.writer_id,
                        clause_index=clause_index,
                        entry_index=None,
                        message="static clause declares no entries; use clauses=() on the declaration for declared-empty",
                    )
                )
            for entry_index, entry in enumerate(clause.entries):
                _check_entry(clause_index, entry_index, entry)
        elif isinstance(clause, ShapedClause):
            if not clause.discovered_by:
                errors.append(
                    ValidationError(
                        writer_id=declaration.writer_id,
                        clause_index=clause_index,
                        entry_index=None,
                        message="shaped clause has an empty discovered_by",
                    )
                )
            _check_entry(clause_index, None, clause.entry_template)
        else:
            errors.append(
                ValidationError(
                    writer_id=declaration.writer_id,
                    clause_index=clause_index,
                    entry_index=None,
                    message=f"clause is neither StaticClause nor ShapedClause: {type(clause)!r}",
                )
            )

    return tuple(errors)
