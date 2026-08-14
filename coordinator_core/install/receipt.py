"""
coordinator_core.install.receipt — the install receipt: what a writer
actually wrote ON THIS MACHINE, derived from its declaration
(`coordinator_core.install.write_surface`) rather than hand-maintained
beside it.

Spec backlink: pln-writer-declared-write-surface-49d3bd,
chunk C5

Purpose: the manifest (`write_surface.py`) says what a writer CAN write in
general — its declared surface, independent of any one machine. The receipt
says what WAS written HERE: which repos discovery actually found, whether
the operator consented to the SSH block, whether the Windows PATH steps
fired on this platform. Uninstall reasoning from the manifest alone is
reasoning from a document about a generic install; it cannot answer "does
this machine have this entry to remove."

This module generalizes `coordinator.installed_repos` — the shape
precedent DoE's `commands/uninstall.md:141` names ("install records what it
touched, uninstall drives off the record") — rather than sitting beside it.
That key has ZERO writers on our engine path today (verified this session:
`grep -rn "installed_repos"` across `coordinator_core/` returns no source
hits, only docs/plan prose describing it as unwritten; independently
confirmed absent when this was relayed to DoE, see
`state/memo-outbox/sent/uninstall-reverse-legs-accepted.md`). So this module
is not generalizing a live mechanism — it is building the recording leg the
design presupposes, honouring `installed_repos` only as a shape precedent
(one append-only, per-writer record of concrete facts observed at install
time), not as working code extended in place.

Derivation, not authorship — the asymmetry between the two clause forms:

  - `StaticClause`: every entry is already concrete in the declaration
    itself (e.g. `configure_git`'s two fixed git-config keys). A receipt
    entry for each is derivable directly from the clause — no runtime
    input needed, because there is nothing left to discover.
  - `ShapedClause`: the whole point of this form (`write_surface.py`'s
    docstring: "the dominant case") is that its concrete entry set is
    knowable only at run time — a discovered repo set, a platform branch,
    a consent gate. The receipt is precisely where that set gets PINNED
    DOWN to what happened on this run: `derive_receipt_entries` requires a
    `ClauseResolution` (the concrete `WriteSurfaceEntry` values the writer
    actually produced, keyed by clause index) for every `ShapedClause` it
    is asked to derive from, and raises `UnresolvedShapedClauseError`
    rather than silently skipping or guessing when one is missing. A
    `StaticClause` needs no resolution — supplying one for it is inert
    (the static entries are used either way) rather than an error, since a
    caller deriving from a mixed-clause declaration should not have to
    special-case which clauses are static.

This module does not go and find declarations itself — per the C5 brief,
collection is C4's problem (`_registry_map.py`'s cold-start-tax avoidance
applies to it, not here). Every function here consumes a
`WriteSurfaceDeclaration` (and, for shaped clauses, its resolutions) passed
in by the caller.

Persistence (design note: docs/research/2026-08-06-install-receipt-persistence-design.md,
chunk C2): `persist_receipt`/`load_receipt` write/read one JSON file under
settings-home, atomically, via `_shared.atomic_write_bytes`. `load_receipt`
degrades to `None` — never raises, never a confidently-wrong removal set —
on every corruption/absence shape it can detect; a receipt drives what
uninstall deletes from the operator's machine, so a wrong answer here is
worse than an honest "I don't know."

Coverage — the fix that unblocks incremental writer wiring (see the design
note's "The design decision that unblocks incremental delivery"): a receipt
must never let "this writer did not report" render indistinguishably from
"this writer wrote nothing" (declared-empty). `InstallReceipt` therefore
carries `reported_writer_ids` (writers `build_receipt` actually derived
entries for, possibly zero) alongside `unreported_writer_ids` (writers
`build_receipt` was explicitly told did not report this run) as two
disjoint, both-explicit sets — `InstallReceipt.reported()` answers "did
writer X report?" as `True`/`False`/`None` (never asked about at all),
distinct from "how many entries did it produce" (`for_writer()`, which
returns `()` for both an empty-but-reported writer and an unreported one —
callers that need the coverage distinction use `reported()`, not entry
count). `derive_receipt_entries` keeps its existing loud raise for a
`ShapedClause` with no resolution unweakened — a caller asking to derive
from an unresolved clause is still an error; recording a writer as
unreported is a SEPARATE, deliberate seam (`build_receipt`'s
`unreported_writer_ids` keyword), not a fallback for a failed derive.

Negative spec — this module does NOT:
  - collect `WriteSurfaceDeclaration` values from writer modules (C4's
    remit, and importing every writer module eagerly would reintroduce the
    cold-start tax `_registry_map.py` exists to avoid);
  - validate a declaration's shape (`write_surface.validate` already does
    that) — `derive_receipt_entries` assumes a clean declaration and is not
    a second validator;
  - resolve a `ShapedClause` itself (discover repos, probe platform,
    check a consent gate) — that is each writer's own runtime logic; this
    module only accepts the writer-reported result via `ClauseResolution`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.install._shared import (
    RequireHomeError,
    atomic_write_bytes,
    require_home,
)
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)


class UnresolvedShapedClauseError(RuntimeError):
    """Raised when `derive_receipt_entries` is asked to derive a receipt
    from a `ShapedClause` with no matching `ClauseResolution` supplied.

    A `ShapedClause`'s concrete entries are only knowable at run time (see
    module docstring) — there is no honest default to fall back to, so
    this raises rather than silently emitting zero entries (which would be
    indistinguishable from "this writer legitimately wrote nothing here")
    or guessing at the template placeholder (which would fabricate a
    receipt entry no write ever produced).
    """


class ClauseResolutionMismatchError(RuntimeError):
    """Raised when a `ClauseResolution` entry does not match the declared
    shape of the `ShapedClause` it is pinning down.

    # Review: code-reviewer (Finding 2, P2) — `derive_receipt_entries`
    # previously accepted a caller-supplied resolution with zero check
    # against `clause.entry_template`: not `kind`, not a root/path-prefix
    # relationship. `write_surface.validate()` only validates a
    # declaration's own shape, never a resolution against it, so nothing
    # else in the pipeline compensated. A resolved entry of the wrong
    # `kind`, or one whose `key`/`path` does not extend the template's own
    # (pre-placeholder) prefix, produced a receipt entry indistinguishable
    # from a correctly-resolved one — and that receipt entry later drives
    # uninstall's removal decision (`uninstall_legs.classify_entry_disposition`).
    """


def _template_prefix(value: str | None) -> str | None:
    """The literal, non-placeholder portion of a `ShapedClause` template's
    `key`/`path` — everything before the first `<` placeholder marker (or
    the whole string if it has none). `None` in, `None` out."""
    if value is None:
        return None
    return value.split("<", 1)[0]


def _check_resolution_matches_template(
    writer_id: str, clause_index: int, entry_index: int, template: WriteSurfaceEntry, entry: WriteSurfaceEntry
) -> None:
    """Verify one resolved `WriteSurfaceEntry` matches the shape its
    `ShapedClause.entry_template` declared — see
    `ClauseResolutionMismatchError`. Fails loud rather than silently
    pinning down a wrong-kind or wrong-root entry."""

    if entry.kind != template.kind:
        raise ClauseResolutionMismatchError(
            f"writer {writer_id!r} clause {clause_index} resolution entry {entry_index} "
            f"has kind {entry.kind!r}, but the clause's entry_template declares kind "
            f"{template.kind!r} — a resolution must match the shape it resolves"
        )

    key_prefix = _template_prefix(template.key)
    if key_prefix and not (entry.key or "").startswith(key_prefix):
        raise ClauseResolutionMismatchError(
            f"writer {writer_id!r} clause {clause_index} resolution entry {entry_index} "
            f"has key {entry.key!r}, which does not extend the entry_template's key prefix "
            f"{key_prefix!r} ({template.key!r})"
        )

    path_prefix = _template_prefix(template.path)
    if path_prefix and not (entry.path or "").startswith(path_prefix):
        raise ClauseResolutionMismatchError(
            f"writer {writer_id!r} clause {clause_index} resolution entry {entry_index} "
            f"has path {entry.path!r}, which does not extend the entry_template's path prefix "
            f"{path_prefix!r} ({template.path!r})"
        )


@dataclass(frozen=True)
class ReceiptEntry:
    """One concrete fact: this writer wrote (or deleted) this exact thing
    on this machine. Unlike `WriteSurfaceEntry`, which may carry template
    placeholders inside a `ShapedClause`, every field here is resolved —
    a receipt entry never contains a placeholder.
    """

    writer_id: str
    kind: str
    key: str | None = None
    path: str | None = None
    begin_marker: str | None = None
    end_marker: str | None = None
    effect: str = "write"


@dataclass(frozen=True)
class ClauseResolution:
    """The runtime-observed concrete entries a `ShapedClause` resolved to
    on THIS install run — the pin-down the module docstring describes.
    Supplied by the caller (the writer's own call site, once C-later wires
    that up), never derived by this module.
    """

    entries: tuple[WriteSurfaceEntry, ...] = field(default_factory=tuple)


def _entry_to_receipt(writer_id: str, entry: WriteSurfaceEntry, clause_effect: str) -> ReceiptEntry:
    return ReceiptEntry(
        writer_id=writer_id,
        kind=entry.kind,
        key=entry.key,
        path=entry.path,
        begin_marker=entry.begin_marker,
        end_marker=entry.end_marker,
        effect=entry.effect if entry.effect else clause_effect,
    )


def derive_receipt_entries(
    declaration: WriteSurfaceDeclaration,
    resolutions: Mapping[int, ClauseResolution] | None = None,
) -> tuple[ReceiptEntry, ...]:
    """Derive this writer's receipt entries from its declaration.

    A `StaticClause` at index `i` derives directly, with no resolution
    needed — its entries are already concrete. A `ShapedClause` at index
    `i` requires `resolutions[i]`; its absence raises
    `UnresolvedShapedClauseError` rather than skipping the clause or
    inventing entries (see module docstring). `resolutions` is keyed by
    clause index (not writer_id) since one writer may carry several
    `ShapedClause`s independently resolved (e.g. `scaffold_structure`'s
    four clauses).

    `clauses=()` (declared-empty, see `write_surface.py`) derives to `()`
    — a writer that asserts it writes nothing has an empty receipt, which
    is itself a valid, meaningful fact, not an error.
    """

    resolutions = resolutions or {}
    out: list[ReceiptEntry] = []
    for clause_index, clause in enumerate(declaration.clauses):
        if isinstance(clause, StaticClause):
            for entry in clause.entries:
                out.append(_entry_to_receipt(declaration.writer_id, entry, clause.effect))
        elif isinstance(clause, ShapedClause):
            resolution = resolutions.get(clause_index)
            if resolution is None:
                raise UnresolvedShapedClauseError(
                    f"writer {declaration.writer_id!r} clause {clause_index} "
                    f"(discovered_by={clause.discovered_by!r}) has no resolution — "
                    "a ShapedClause's concrete entries are only knowable at run time; "
                    "supply a ClauseResolution for this clause index"
                )
            for entry_index, entry in enumerate(resolution.entries):
                _check_resolution_matches_template(
                    declaration.writer_id, clause_index, entry_index, clause.entry_template, entry
                )
                out.append(_entry_to_receipt(declaration.writer_id, entry, clause.effect))
        else:
            raise TypeError(f"clause is neither StaticClause nor ShapedClause: {type(clause)!r}")
    return tuple(out)


@dataclass(frozen=True)
class InstallReceipt:
    """The full receipt for one install run: every `ReceiptEntry` derived
    across every writer that ran. Flat and writer-agnostic by design — a
    consumer (e.g. a future uninstall leg) filters by `writer_id` or `kind`
    itself rather than this module pre-partitioning it.

    `reported_writer_ids`/`unreported_writer_ids` carry the coverage fact
    the module docstring's "Coverage" section describes — two disjoint,
    both-explicit sets (`build_receipt` raises if a writer_id appears in
    both). Neither set is a derived view of `entries`: a writer can be in
    `reported_writer_ids` with zero entries (declared-empty, a legitimate
    fact — see `derive_receipt_entries`'s `clauses=()` note) and that is
    NOT the same recorded fact as the writer being in `unreported_writer_ids`
    (did not report at all this run). A writer_id absent from BOTH sets was
    simply never asked about by whatever assembled this receipt — `reported()`
    returns `None` for that case, not `False`, since this module never
    infers "did not report" from silence (see Negative spec).
    """

    entries: tuple[ReceiptEntry, ...] = field(default_factory=tuple)
    reported_writer_ids: frozenset[str] = field(default_factory=frozenset)
    unreported_writer_ids: frozenset[str] = field(default_factory=frozenset)

    def for_writer(self, writer_id: str) -> tuple[ReceiptEntry, ...]:
        return tuple(e for e in self.entries if e.writer_id == writer_id)

    def for_kind(self, kind: str) -> tuple[ReceiptEntry, ...]:
        return tuple(e for e in self.entries if e.kind == kind)

    def reported(self, writer_id: str) -> bool | None:
        """Did `writer_id` report this run? `True` (in `reported_writer_ids`,
        entries may still be empty — declared-empty), `False` (explicitly in
        `unreported_writer_ids`), or `None` (this receipt was never told
        either way about `writer_id` — not a claim of non-report, an honest
        "not asked")."""
        if writer_id in self.reported_writer_ids:
            return True
        if writer_id in self.unreported_writer_ids:
            return False
        return None


def build_receipt(
    derivations: Iterable[tuple[WriteSurfaceDeclaration, Mapping[int, ClauseResolution] | None]],
    *,
    unreported_writer_ids: Iterable[str] = (),
) -> InstallReceipt:
    """Assemble an `InstallReceipt` from a caller-supplied sequence of
    `(declaration, resolutions)` pairs — one per writer that ran this
    install AND reported. Does not go collect declarations itself (see
    Negative spec); the caller is whichever future recording leg walks
    writers as they execute and passes each one's declaration plus its
    resolved shaped clauses (if any) in.

    `unreported_writer_ids` is the seam the module docstring's "Coverage"
    section describes: a writer_id the caller knows did NOT report this
    run (e.g. its writer module isn't wired to the journal yet, or its
    process phase never ran) is named here instead of being either (a)
    forced through `derive_receipt_entries` with no resolution — which
    still raises `UnresolvedShapedClauseError`, unweakened, see that
    function's docstring — or (b) silently omitted, which would collapse
    "did not report" into "wrote nothing" (the exact defect this design
    exists to prevent). `derive_receipt_entries` and "mark unreported" are
    two DIFFERENT, deliberate calls a caller makes per writer — never one
    falling back to the other.

    Raises `ValueError` if a writer_id appears in both `derivations` (i.e.
    it was actually derived) and `unreported_writer_ids` — those two facts
    are mutually exclusive for one writer on one run.
    """

    entries: list[ReceiptEntry] = []
    reported: set[str] = set()
    for declaration, resolutions in derivations:
        entries.extend(derive_receipt_entries(declaration, resolutions))
        reported.add(declaration.writer_id)

    unreported = set(unreported_writer_ids)
    overlap = reported & unreported
    if overlap:
        raise ValueError(
            f"writer_id(s) both derived (reported) and passed in "
            f"unreported_writer_ids: {sorted(overlap)} — a writer either "
            "reported this run or it did not, never both"
        )

    return InstallReceipt(
        entries=tuple(entries),
        reported_writer_ids=frozenset(reported),
        unreported_writer_ids=frozenset(unreported),
    )


# ---------------------------------------------------------------------------
# Persistence — C2 of docs/research/2026-08-06-install-receipt-persistence-design.md
# ---------------------------------------------------------------------------

RECEIPT_SCHEMA_VERSION = 1
"""Bumped whenever the on-disk shape changes incompatibly. `load_receipt`
treats any value it does not recognize as corrupt (returns `None`), never
as "close enough" — see that function's docstring."""

_RECEIPT_FILENAME = "install-receipt.json"


class ReceiptPersistenceError(RuntimeError):
    """Raised by `persist_receipt` when the write is refused (machine-
    mutation guard) or the settings-home location cannot be resolved. Never
    raised by `load_receipt` — see that function's degrade-to-`None`
    contract."""


def _receipt_path(settings_home_override: str | Path | None = None) -> Path:
    """Resolve the on-disk receipt path.

    Resolution mirrors `uninstall_legs._settings_home_from` EXACTLY (per
    this chunk's brief: "do not invent a second resolution ladder") —
    `COORDINATOR_SETTINGS_HOME` first, else `<resolved-home>/
    .coordinator-claude-settings`. This calls that function directly via a
    DEFERRED import rather than re-deriving its logic: `uninstall_legs.py`
    already imports `InstallReceipt` from this module at module load time
    (see its own top-of-file imports), so a module-level import of
    `uninstall_legs` here would be a circular import — deferring to call
    time (same pattern `shell_rc_guard.py`/`substrate_migrate.py` use for
    their own back-imports of `substrate.py`) resolves it cleanly, because
    by the time `persist_receipt`/`load_receipt` actually run, this module
    is already fully imported and sitting in `sys.modules`.

    `settings_home_override`, when given, skips home/env resolution
    entirely — the test seam (mirrors every other install-side function
    that takes an explicit override rather than only reading `os.environ`).
    """
    if settings_home_override is not None:
        return Path(settings_home_override) / _RECEIPT_FILENAME

    from coordinator_core.install.uninstall_legs import _settings_home_from

    resolved_home = require_home("receipt._receipt_path")
    settings_home = _settings_home_from(resolved_home)
    return Path(settings_home) / _RECEIPT_FILENAME


def _receipt_to_jsonable(receipt: InstallReceipt) -> dict:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "entries": [
            {
                "writer_id": entry.writer_id,
                "kind": entry.kind,
                "key": entry.key,
                "path": entry.path,
                "begin_marker": entry.begin_marker,
                "end_marker": entry.end_marker,
                "effect": entry.effect,
            }
            for entry in receipt.entries
        ],
        "reported_writer_ids": sorted(receipt.reported_writer_ids),
        "unreported_writer_ids": sorted(receipt.unreported_writer_ids),
    }


def _receipt_from_jsonable(data: object) -> InstallReceipt:
    """Reconstruct an `InstallReceipt` from parsed JSON. Raises
    (`ValueError`/`TypeError`/`KeyError`) on ANY shape mismatch — never
    degrades itself; `load_receipt` is the sole degrade-to-`None` boundary,
    so this stays a strict, symmetric inverse of `_receipt_to_jsonable`."""
    if not isinstance(data, dict):
        raise ValueError(f"receipt document is not a JSON object: {type(data)!r}")

    schema_version = data["schema_version"]
    if schema_version != RECEIPT_SCHEMA_VERSION:
        raise ValueError(
            f"unrecognized receipt schema_version {schema_version!r} "
            f"(this reader understands {RECEIPT_SCHEMA_VERSION!r})"
        )

    raw_entries = data["entries"]
    if not isinstance(raw_entries, list):
        raise ValueError(f"'entries' is not a JSON array: {type(raw_entries)!r}")

    entries = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError(f"receipt entry is not a JSON object: {type(raw_entry)!r}")
        # Review: code-reviewer (Finding, P3) — `key`/`path`/`begin_marker`/
        # `end_marker` were previously taken via bare `.get(...)` with no
        # type check, so a corrupted receipt (e.g. an integer `path`) would
        # reconstruct into a `ReceiptEntry` with a non-string field instead
        # of being treated as a shape mismatch. Validated here the same way
        # `writer_id`/`kind` already were, each optional (`None` allowed,
        # any other non-string type is a shape mismatch).
        raw_key = raw_entry.get("key")
        raw_path = raw_entry.get("path")
        raw_begin_marker = raw_entry.get("begin_marker")
        raw_end_marker = raw_entry.get("end_marker")
        raw_effect = raw_entry.get("effect", "write")
        for field_name, field_value in (
            ("key", raw_key),
            ("path", raw_path),
            ("begin_marker", raw_begin_marker),
            ("end_marker", raw_end_marker),
            ("effect", raw_effect),
        ):
            if field_value is not None and not isinstance(field_value, str):
                raise ValueError(
                    f"receipt entry field {field_name!r} is not a string or null: {field_value!r}"
                )

        entries.append(
            ReceiptEntry(
                writer_id=raw_entry["writer_id"],
                kind=raw_entry["kind"],
                key=raw_key,
                path=raw_path,
                begin_marker=raw_begin_marker,
                end_marker=raw_end_marker,
                effect=raw_effect,
            )
        )
        if not isinstance(entries[-1].writer_id, str) or not isinstance(entries[-1].kind, str):
            raise ValueError(f"receipt entry has a non-string writer_id/kind: {raw_entry!r}")

    raw_reported = data["reported_writer_ids"]
    raw_unreported = data["unreported_writer_ids"]
    if not isinstance(raw_reported, list) or not all(isinstance(w, str) for w in raw_reported):
        raise ValueError(f"'reported_writer_ids' is not a JSON array of strings: {raw_reported!r}")
    if not isinstance(raw_unreported, list) or not all(isinstance(w, str) for w in raw_unreported):
        raise ValueError(f"'unreported_writer_ids' is not a JSON array of strings: {raw_unreported!r}")

    # Review: code-reviewer (Finding, P1) — `build_receipt` enforces
    # reported/unreported disjointness at construction (raises `ValueError`
    # on overlap), but that invariant was previously bypassed entirely on
    # this load path: a writer_id present in both lists round-tripped into
    # an `InstallReceipt` whose `reported()` silently resolved to `True`
    # (it checks `reported_writer_ids` first) — the exact "claims coverage
    # it does not have" ambiguity the design's negative spec forbids, and a
    # detectable corruption shape `load_receipt`'s contract says must
    # degrade to `None`.
    overlap = set(raw_reported) & set(raw_unreported)
    if overlap:
        raise ValueError(
            f"writer_id(s) present in both 'reported_writer_ids' and "
            f"'unreported_writer_ids': {sorted(overlap)} — a writer either "
            "reported this run or it did not, never both"
        )

    return InstallReceipt(
        entries=tuple(entries),
        reported_writer_ids=frozenset(raw_reported),
        unreported_writer_ids=frozenset(raw_unreported),
    )


def persist_receipt(
    receipt: InstallReceipt, *, settings_home_override: str | Path | None = None
) -> Path:
    """Write `receipt` to disk atomically as JSON, under settings-home (see
    `_receipt_path`). Returns the path written.

    This IS a real machine write, gated by the same
    `_refuse_machine_mutation` belt-and-braces guard every other install-
    side writer in this package gates its real writes behind
    (`COORDINATOR_DISABLE_MACHINE_MUTATION=1` refuses unconditionally; a
    target resolving under the OS temp dir is NOT refused here —
    `check_temp_path=False`, matching `substrate_migrate.py`'s rc-block/
    real-file write sites — because a filesystem-path write is exactly the
    case the test sandbox correctly redirects via a `tmp_path`-rooted
    `settings_home_override`, per that guard's own docstring). Imported
    deferred, matching `shell_rc_guard.py`'s identical back-import-of-
    `substrate` pattern, to avoid a module-load-time cycle (`substrate.py`
    does not import this module, so there is no cycle risk today, but the
    deferred-import convention is kept consistent with every other back-
    import in this package rather than special-cased here).

    Raises `ReceiptPersistenceError` if the write is refused. Never
    swallows a resolution failure (e.g. no `$HOME`/`$CLAUDE_HOME`/
    `$USERPROFILE` set) — an install run that cannot resolve where to put
    its own receipt is a real configuration error, not the honest-unknown
    case `load_receipt` exists to represent.
    """
    # Review: code-reviewer (Finding, P2) — this docstring and
    # `ReceiptPersistenceError`'s own docstring both document an
    # unresolvable settings-home as surfacing `ReceiptPersistenceError`,
    # but the code previously let `RequireHomeError` propagate raw here
    # (uncaught), disagreeing with `load_receipt`, which does catch it
    # explicitly. Wrapped to match the documented contract.
    try:
        target = _receipt_path(settings_home_override)
    except RequireHomeError as exc:
        raise ReceiptPersistenceError(f"persist_receipt: settings-home could not be resolved: {exc}") from exc

    from coordinator_core.install import substrate as _substrate_mod

    blocked = _substrate_mod._refuse_machine_mutation(
        str(target), what="persist install receipt", check_temp_path=False
    )
    if blocked:
        raise ReceiptPersistenceError(f"persist_receipt REFUSED: {blocked}")

    payload = json.dumps(_receipt_to_jsonable(receipt), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write_bytes(target, payload)
    return target


def load_receipt(*, settings_home_override: str | Path | None = None) -> InstallReceipt | None:
    """Load the persisted receipt, or `None` if none can be honestly
    reconstructed.

    Degrades to `None` — NEVER raises, NEVER returns a confidently-wrong
    receipt — for every one of: the file is absent; the settings-home
    location itself cannot be resolved (`RequireHomeError`); the file is
    not valid JSON (truncated, malformed); the parsed JSON does not match
    the expected document shape (missing/mistyped keys); the document
    names a `schema_version` this reader does not recognize. This receipt
    drives what an uninstall run deletes from the operator's machine — a
    wrong answer here is worse than no answer, per the design note's
    negative spec, so every one of these is a `None`, not a raise.
    """
    try:
        target = _receipt_path(settings_home_override)
    except RequireHomeError:
        return None

    try:
        raw = target.read_bytes()
    except OSError:
        return None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    try:
        return _receipt_from_jsonable(data)
    except (ValueError, TypeError, KeyError):
        return None


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="install-receipt",
    source_module="coordinator_core.install.receipt",
    clauses=(
        # `persist_receipt` is the sole real-machine write this module
        # performs — everything else here (`derive_receipt_entries`,
        # `build_receipt`) is pure in-memory assembly, per the module
        # docstring's Negative spec. Declared even though the AST-walk
        # enforcement test (`test_write_reaching_modules_declare.py`)
        # would not itself flag this module (the write goes through the
        # `atomic_write_bytes`/`_refuse_machine_mutation` NAME-call seam,
        # not one of that test's flagged attribute names) — the doctrine
        # `_shared.py`'s own allowlist entry states applies here too: the
        # CALLER pointing the generic write mechanic at a real target is
        # where the WRITE_SURFACE clause belongs.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path=f"<settings_home>/{_RECEIPT_FILENAME}",
                    reason="persist_receipt: writes the install receipt JSON file",
                ),
            ),
        ),
    ),
)
