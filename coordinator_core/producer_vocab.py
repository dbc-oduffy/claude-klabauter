"""
Producer-axis machine-vs-human authorship vocabulary — the ``ProducerOpIdentity``
literal, extracted to a leaf module.

Placement rationale: this is the single common ancestor of the two layers that
both need this vocabulary — ``coordinator_core/session/producer_resolve.py``
(session layer) and ``coordinator_core/contract/cockpit_schema/entities/
summaries.py`` (contract layer, pydantic-backed). Putting it under either
layer would invert the other's dependency direction; putting it at the
package root, alongside the other leaf/no-import modules here (see
``lifecycle_constants.py`` for the established pattern), avoids that. This
module imports nothing from ``coordinator_core`` and nothing third-party
(plain ``typing`` only) so importing it never drags pydantic or the
Cockpit-contract package onto a hot path — that is the entire point of the
extraction: ``producer_resolve`` previously imported ``ProducerOpIdentity``
from ``summaries.py``, which pulls in pydantic and the whole cockpit-contract
package (~53ms) just to read two string literals off a ``Literal``, on every
handoff-creation call.

Vocabulary contract (settled cross-repo; carried across from the two modules
this was extracted out of — do not re-litigate here without updating both
call sites' docstrings too):
  - ``machine-minted`` covers BOTH creation doors — ``queue_scaffold_baton``
    (op-minted by construction) and ``handoff_author_fork`` (EM-initiated).
    Both are machine; the finer op-minted-vs-EM-initiated distinction is
    deliberately NOT carried on this axis — a third member of this closed,
    cross-repo-shared enum is not this axis's to invent.
  - ``hand-authored``: the record was hand-edited, independent of what (if
    anything) the session typed to get there — its truth-condition does not
    depend on the ``typed_command`` axis.
  - Matches example-doctrine-repo's vendored enum exactly (``machine-minted`` /
    ``hand-authored``) — this is a closed, fail-closed cross-repo contract;
    the two sides must not drift.

Spec backlink: docs/plans/2026-08-12-producer-axis-on-the-baton-contract.md § C6a.
Spec backlink (perf extraction): docs/plans/2026-08-12-agent-facing-messages-not-apology.md
"""

from __future__ import annotations

from typing import Literal

ProducerOpIdentity = Literal["machine-minted", "hand-authored"]
"""The closed, cross-repo-shared machine-vs-human authorship vocabulary. See
module docstring for the full contract and why the two members are exactly
these two."""
