"""
coordinator_core.artifact_id_slug — the one definition of a slug's shape INSIDE an id.

Purpose: an artifact id is ``<prefix>-<slug>-<6hex>``, and every site that
composes one truncates the slug to a fixed width first. A cut landing on a
separator leaves a trailing dash, and the id then carries a doubled dash at the
hex boundary: ``hnd-corpus-knowledge-delivery-raw--b8256d``. It is schema-valid
under ``[a-z0-9-]+`` and reads fine to a human, which is exactly why it survived
-- but the sidecar writer collapses the run when it names the file, so an
id-keyed lookup reports a record absent from the directory it just read.

Measured 2026-09-11 across the fleet: 45 landed ids in ``state/`` and ``docs/``
already carry one, and a missing-integration-record check written by someone who
knew about neither the hazard nor the collapse reported a false positive on one.
Those 45 are join keys and cannot be renamed; this module is how the set stops
growing.

WHY AT COMPOSITION, NOT ON READ. A consumer that normalises on read has to be
right at every call site and stay right forever, and the check that found this
was written by someone who did not know to. One normalisation where the id is
built removes the class.

Negative-spec: this is for composing a NEW id only. A CARRIED id is never routed
through here -- an id already on disk is a join key, and normalising it in
passing points the carrying artifact at a record nothing else names. Fix a bad
carried id at its source or leave it.

Negative-spec: does NOT slugify. Callers already lower-case and replace their own
separator runs; this operates on the result, at and after truncation.
"""

from __future__ import annotations

import re

#: Any run of separators is one separator. Idempotent, so a caller that also
#: collapses loses nothing by calling this too.
_DASH_RUN = re.compile(r"-{2,}")


def id_slug(raw: str, limit: int | None = None) -> str:
    """The slug as it may appear inside an id: truncated, then made boundary-safe.

    ``limit`` is applied BEFORE the strip, which is the whole point -- stripping
    first and truncating after is what produces the trailing dash, and is what
    every site this replaces was doing.
    """
    out = raw or ""
    if limit is not None:
        out = out[:limit]
    return _DASH_RUN.sub("-", out).strip("-")
