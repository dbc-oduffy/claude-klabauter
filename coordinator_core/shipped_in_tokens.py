"""
coordinator_core.shipped_in_tokens

Purpose: the single definition site for the `shipped_in` value grammar — a
resolvable git SHA (`_SHA_HEX_RE`) or the sanctioned
`substantively-shipped-no-commit:<YYYY-MM-DD>` stealth-skip token
(`_NO_COMMIT_TOKEN_RE`). Spec backlink:
state/debt-backlog/DSR-2026-08-13-archive-stamp-import-order-drops-an-op-from-the-registry.yaml.

This module is a LEAF: it imports nothing from `coordinator_core` (only the
stdlib `re`), by construction. `coordinator_core.archive_stamp` previously
owned both regexes, but it also sits deep in the ops/pickup_assemble/
session_ledger import graph — a caller that imported `archive_stamp` before
`coordinator_core.ops` triggered a genuine import cycle
(archive_stamp -> pickup_assemble -> session_ledger.aggregate_chain_loe ->
pickup_assemble, half-initialised) that silently dropped
`session_ledger.aggregate_chain_loe` from the op registry. Moving the shared
regexes to a leaf module breaks the edge outright rather than hiding it
behind a lazy/deferred import, which would leave the identical trap for the
next symbol someone shares across that boundary.

Every consumer of this value grammar imports from HERE, never from
`archive_stamp` (which no longer re-exports these names) and never by
keeping an independent copy — a second, independently-driftable definition
of the same shape is exactly the fork-not-share pattern this module exists
to foreclose. Add a new shared `shipped_in`-grammar symbol here, not wherever
the next caller happens to sit.

Negative-spec:
    - Does NOT import anything from `coordinator_core` — a single such import
      would risk recreating a cycle for whichever module happens to own the
      new dependency's own import graph.
    - Does NOT redefine `_SHIPPED_IN_KIND_ENUM` (the `kind` enum these values
      pair with) — that stays owned by `coordinator_core.ops.handoff_stamp`.
"""

from __future__ import annotations

import re

# Hex range 7-64 to match the ratified schema pattern
# (`coordinator/artifact-shape-contract/artifact-shape-contract.schema.json`,
# coordinator-claude DR-096 prior wave) — a SHA-256 repo's abbreviated-or-full commit
# id can run past 40 hex chars.
_SHA_HEX_RE = re.compile(r"[0-9a-fA-F]{7,64}")
_NO_COMMIT_TOKEN_RE = re.compile(r"substantively-shipped-no-commit:\d{4}-\d{2}-\d{2}")
