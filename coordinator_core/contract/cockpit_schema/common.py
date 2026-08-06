"""
Shared scalar primitives for the cockpit work-state contract — pydantic port of
Example-doctrine-repo `coordinator/cockpit-contract/src/common.ts` (Zod source).

Every entity speaks ISO-8601 UTC for timestamps and ISO calendar dates for
day-grained periods. Centralising these here keeps the emitted JSON Schema
`format`/`pattern` hints consistent across every entity, mirroring the Zod
source's own centralisation rationale.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import StringConstraints

# ISO-8601 UTC datetime pattern, e.g. "2026-06-22T03:06:15Z" or
# "2026-06-22T03:06:15+00:00". Regex copied verbatim from the committed Zod
# `z.iso.datetime({ offset: true })` JSON Schema emission
# (coordinator/cockpit-contract/schema/provenance-envelope.schema.json's
# `observed_at` field) to preserve byte-identical accept/reject behavior —
# accepts both the bare `Z` suffix and a numeric `+00:00`/`-00:00` offset.
_ISO_DATETIME_PATTERN = (
    r"^(?:(?:\d\d[2468][048]|\d\d[13579][26]|\d\d0[48]|[02468][048]00|[13579][26]00)-02-29"
    r"|\d{4}-(?:(?:0[13578]|1[02])-(?:0[1-9]|[12]\d|3[01])"
    r"|(?:0[469]|11)-(?:0[1-9]|[12]\d|30)"
    r"|(?:02)-(?:0[1-9]|1\d|2[0-8])))"
    r"T(?:(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d+)?)?(?:Z|([+-](?:[01]\d|2[0-3]):[0-5]\d)))$"
)

IsoDateTime = Annotated[str, StringConstraints(pattern=_ISO_DATETIME_PATTERN)]
"""ISO-8601 UTC datetime, e.g. "2026-06-22T03:06:15Z". Emits `format: date-time`-equivalent."""

# ISO calendar date, e.g. "2026-06-22" — same leap-year-aware date grammar as
# the datetime pattern above, without the time-of-day suffix.
_ISO_DATE_PATTERN = (
    r"^(?:(?:\d\d[2468][048]|\d\d[13579][26]|\d\d0[48]|[02468][048]00|[13579][26]00)-02-29"
    r"|\d{4}-(?:(?:0[13578]|1[02])-(?:0[1-9]|[12]\d|3[01])"
    r"|(?:0[469]|11)-(?:0[1-9]|[12]\d|30)"
    r"|(?:02)-(?:0[1-9]|1\d|2[0-8])))$"
)

IsoDate = Annotated[str, StringConstraints(pattern=_ISO_DATE_PATTERN)]
"""ISO calendar date, e.g. "2026-06-22". Emits `format: date`-equivalent."""

Visibility = Literal["PRIVATE", "PUBLIC", "INTERNAL"]
"""GitHub repository visibility as reported by the census (tc-4)."""

MachineSlug = Annotated[str, StringConstraints(min_length=1)]
"""
Hostname slug identifying a machine in the fleet — the `@machine` component of
`owner/repo@machine` fleet identity. D11 (string-not-enum): the machine-slug
set is unstable; an enum would reproduce the exact bug D11 documents.
`min_length=1` rejects the degenerate empty-slug without reintroducing the
closed enum D11 forbids.
"""

OwnerSlug = Annotated[str, StringConstraints(min_length=1)]
"""
GitHub owner namespace — the `owner` component of `owner/repo@machine` fleet
identity. D11 (string-not-enum): the owner set is unstable (orgs, synthetic
namespaces like `local/` for remote-less repos drift over time); a closed enum
reproduces the exact bug D11 documents. `min_length=1` rejects the degenerate
empty-owner (matching `MachineSlug`) without reintroducing the closed enum D11
forbids.
"""
