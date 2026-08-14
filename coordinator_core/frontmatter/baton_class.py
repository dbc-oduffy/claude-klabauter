"""
coordinator_core.frontmatter.baton_class — the ONE canonical `baton_class`
derivation (D2 of the baton-kind-vocabulary migration).

Purpose: retire the five independently hand-rolled "is this a spinoff /
roadmap / ..." `kind`-membership sets scattered across `pickup_assemble`,
`orientation.regenerate_cache`, `frontmatter.schema_validate`,
`ops.ceremony.renderers`, and `ops.handoff_close_origin_stub` — each of
which answered the same underlying question ("what family does this baton
belong to?") with its own, mutually incompatible membership. Every one of
those five sites becomes a caller of `baton_class()` here instead of
re-declaring its own frozenset/tuple/dict.

The `kind` -> `baton_class` mapping itself is NOT hand-authored in this
module — it is read live from the `x-baton-class` key inside the vendored
`handoff.schema.json` (homed there by C2 of the same migration), resolved
through this repo's existing vendored-schema location
(`schema_drift_watch.VENDORED_SCHEMAS_DIR`) rather than a path literal here.
A hand-authored copy beside the schema is exactly the two-sources-of-truth
drift this migration exists to close (see the schema's own `x-baton-class`
key description for the "never hand-authored independently" rule and the
C2 chunk body in the plan).

`baton_class` is never written to on-disk frontmatter (D2) — this function
computes it on read; no handoff record ever carries a `baton_class:` key.

Vocabulary bridge, confined to this module on purpose
=======================================================
`baton_class()` also accepts the still-live PRE-rename `kind` values
(`spinoff-roadmap`, `spinoff-roadmap-creator`, `spinoff-goal`) via
`_PRE_RENAME_ALIASES` below, aliasing each to its D1 TARGET name
(`roadmap-baton`, `roadmap-seed`, `goal-seed`) before the `x-baton-class`
lookup. This is deliberate, not scope creep, and it is NOT the dual-accept
tolerance-window chunk (a separate, later chunk covering the full call-site
list across this codebase) — it exists here because `baton_class()` itself
must answer correctly for the CURRENT live corpus: as of this chunk, no
on-disk `kind:` value has been physically migrated to its D1 target yet,
and D2's whole promise is that a rename's old/new duality is reconciled in
exactly one place. Confining the alias table to this module is also what
keeps every call site elsewhere in the codebase to at most ONE `kind`
string-literal comparison (never a two-or-more-literal collection) — see
the structural test that enforces this,
`coordinator_core/tests/test_baton_class_is_the_only_membership_set.py`.

Negative-spec
=============
- Does NOT write `baton_class` to any frontmatter file. Read/derive-only.
- Does NOT retire `_PRE_RENAME_ALIASES` on its own — that happens once the
  D1 physical rename of on-disk `kind:` values has landed (out of scope for
  this chunk; tracked by the plan, not by a TODO here).
- Does NOT re-declare the `kind` -> class mapping; it reads it from the
  vendored schema every call site would otherwise have had to trust a
  second, hand-typed copy of.

Spec backlink: DoE-claude:pln-baton-kind-vocabulary-one-axis-d1ce8f
               § D1, D2, C2, C3.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# Not imported from `schema_drift_watch.VENDORED_SCHEMAS_DIR` — that module
# imports `check_schema_drift_advisory` FROM `schema_validate`, and
# `schema_validate` itself becomes a caller of this module (C3), so a
# module-level import of `schema_drift_watch` here would close an import
# cycle (schema_validate -> baton_class -> schema_drift_watch ->
# schema_validate). Computed identically instead — same `Path(__file__)`
# parent-plus-"schemas" derivation, same physical directory, both modules
# live side-by-side in `coordinator_core/frontmatter/` — so this is not a
# hardcoded path, just a cycle-safe recomputation of the same one.
VENDORED_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

_HANDOFF_SCHEMA_FILENAME = "handoff.schema.json"

# D1 pre-rename `kind` literal -> D1 TARGET `kind` literal. Confined here per
# this module's own "Vocabulary bridge" section above — do not duplicate
# elsewhere. Retired once every on-disk `kind:` value is physically migrated
# to its D1 target (not this chunk's job).
_PRE_RENAME_ALIASES: dict[str, str] = {
    "spinoff-roadmap": "roadmap-baton",
    "spinoff-roadmap-creator": "roadmap-seed",
    "spinoff-goal": "goal-seed",
}


class BatonClassSchemaError(RuntimeError):
    """The vendored handoff schema is missing, unreadable, not valid JSON,
    or lacks a usable `x-baton-class.mapping` key."""


def _mapping_path(schemas_dir: Optional[Path] = None) -> Path:
    directory = Path(schemas_dir) if schemas_dir is not None else VENDORED_SCHEMAS_DIR
    return directory / _HANDOFF_SCHEMA_FILENAME


def _load_mapping(schemas_dir: Optional[Path] = None) -> dict[str, str]:
    """Read `x-baton-class.mapping` fresh off disk every call — this schema
    file is tiny (~60KB) and read at most once per call site invocation, so
    a cache would trade a real-but-small read cost for stale-mapping risk
    across a process lifetime (e.g. a doctor probe re-vendoring the schema
    mid-run). Not memoized; callers wanting to avoid repeated I/O in a tight
    loop should read once at their own call boundary and pass `kind`s through
    a local dict comprehension over `baton_class`, not ask this module to
    cache silently.
    """
    schema_path = _mapping_path(schemas_dir)
    try:
        raw = schema_path.read_text(encoding="utf-8")
    except OSError as e:
        raise BatonClassSchemaError(
            f"cannot read vendored handoff schema at {schema_path}: {e}"
        ) from e
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BatonClassSchemaError(
            f"vendored handoff schema at {schema_path} is not valid JSON: {e}"
        ) from e
    x_baton_class = parsed.get("x-baton-class")
    mapping = x_baton_class.get("mapping") if isinstance(x_baton_class, dict) else None
    if not isinstance(mapping, dict):
        raise BatonClassSchemaError(
            f'vendored handoff schema at {schema_path} has no usable "x-baton-class.mapping" key'
        )
    return dict(mapping)


def baton_class(kind: Optional[str], *, schemas_dir: Optional[Path] = None) -> Optional[str]:
    """Derive `baton_class` (`continuation` | `deflection` | `intention`) from
    a handoff `kind` value.

    Looks the `kind` up directly in the vendored schema's `x-baton-class`
    mapping first; if not found there, aliases a still-live PRE-rename D1
    source name (see `_PRE_RENAME_ALIASES`) to its target and looks that up
    instead. Returns `None` for an absent/falsy `kind` or a `kind` this
    migration's known vocabulary does not cover (e.g. a schema value from
    outside the handoff family, or a genuinely unknown string) — callers
    that need a fallback (the schema's own documented default is
    `session-handoff`) apply that at their own read boundary, same as they
    already do for the raw `kind` field itself.

    `schemas_dir` is a test-only override; production callers never pass it.
    """
    if not kind:
        return None
    mapping = _load_mapping(schemas_dir)
    if kind in mapping:
        return mapping[kind]
    aliased = _PRE_RENAME_ALIASES.get(kind)
    if aliased is not None:
        return mapping.get(aliased)
    return None


def canonical_kind(kind: Optional[str]) -> str:
    """Normalise a handoff `kind` string and, if it is one of the retired
    D1 pre-rename values (`spinoff-roadmap`, `spinoff-roadmap-creator`,
    `spinoff-goal`), map it to its D1 successor
    (`roadmap-baton`/`roadmap-seed`/`goal-seed`) via `_PRE_RENAME_ALIASES`.
    Any other value (including an already-canonical one) is returned
    unchanged after the same case/whitespace normalisation call sites
    already apply (`.strip().lower()`).

    This is the single normaliser call sites outside this module must use
    instead of re-declaring their own retired/canonical pair — see the
    module docstring's "Vocabulary bridge" section and the structural test
    `test_baton_class_is_the_only_membership_set.py`, which forbids any
    other module from spelling a literal collection that pairs a retired
    value with its successor.

    Unlike `baton_class()`, this does NOT touch the vendored schema — it is
    a pure string normalisation over `_PRE_RENAME_ALIASES`, safe for call
    sites that want the specific (normalised, de-aliased) `kind` value
    rather than its coarser `baton_class` bucket. Preserves membership
    exactly: does not widen a specific-`kind` comparison into a
    `baton_class`-level one (e.g. this must never cause a `roadmap-baton`
    site to also match `roadmap-seed`).

    Returns `""` for an absent/falsy `kind`, matching the empty string a
    call site's own `.strip().lower()` normalisation already produces for
    a missing field.
    """
    normalised = (kind or "").strip().lower()
    if not normalised:
        return ""
    return _PRE_RENAME_ALIASES.get(normalised, normalised)


def kind_values_for_canonical(canonical: str) -> list[str]:
    """Return every on-disk `kind` spelling — the canonical D1 value itself
    plus any still-live retired pre-rename spelling(s) that alias to it —
    for use by a caller that must query a data store DIRECTLY on the raw
    `kind` field (e.g. a `records.query` `where=` string) rather than on a
    value it can normalise in Python after the fact.

    This exists so a query-string call site never has to spell the
    retired/canonical pair itself: it asks this module for "every spelling
    that currently means `roadmap-baton`" and gets the list back, derived
    from `_PRE_RENAME_ALIASES` at call time. The pairing stays declared in
    exactly one place (`_PRE_RENAME_ALIASES` above); this function is a
    read-only view over it, not a second copy — see the module docstring's
    "Vocabulary bridge" section and
    `coordinator_core/tests/test_baton_class_is_the_only_membership_set.py`,
    which forbids any OTHER module from spelling a literal collection that
    pairs a retired value with its successor.

    Order is stable: canonical value first, then retired spellings in
    `_PRE_RENAME_ALIASES` iteration order. Returns `[canonical]` unchanged
    if no retired value aliases to it.
    """
    values = [canonical]
    for retired, target in _PRE_RENAME_ALIASES.items():
        if target == canonical:
            values.append(retired)
    return values
