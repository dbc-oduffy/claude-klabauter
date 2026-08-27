"""
Corpus lint: every `state/sizings/*.yaml` past `status: draft` must carry a
non-null `deliverable_id`.

Rationale: a sizing-object is the EARLIEST artifact in the deliverable chain
(`sizing-object.schema.json`'s own `deliverable_id` description), so its id is
the spine's root — every downstream plan and handoff is supposed to carry it
verbatim rather than mint its own. `coordinator-doc-new`'s `_scaffold_sizing`
mints that id at scaffold time, but emits a bare `null` when the mint returns
nothing, and a hand-authored sizing can simply omit the key. Neither path warns
at the emit site, and nothing downstream re-mints it.

What a null root actually costs, measured on the record that produced this
guard (`state/sizings/2026-08-26-the-http-leg-owns-the-powershell-rewrite.yaml`):
`coordinator-doc-new`'s plan arm consults the cited sizing for its carry, reads
the null, and falls through to mint-from-title; the predecessor handoff had
already minted its own from the same stem. One piece of work, two ids
(`...-099302` and `...-89747f`), each cemented in its own commits' `Deliverable-Id`
trailers. `deliverable.cascade_terminal` joins on the RAW id — the declared
fork-equivalence map that used to absorb this was condemned and removed
2026-08-21 (see `state/deliverable-equivalence.yaml`'s header) — so the fork is
permanently unjoinable and `close-out-and-stamp` advances neither the sizing nor
the handoff.

The gate is `status`, not existence: `draft` is a scaffold that has not yet
been routed anywhere, and refusing to mint from a PLACEHOLDER title is
deliberate (`coordinator-doc-new._is_placeholder_title` — a well-formed id
derived from a placeholder is worse than an absent one, because it RESOLVES).
The moment a sizing leaves `draft`, it has a real title and a real route, and
its id is load-bearing for every artifact downstream of it.

Negative-spec:
  - Does NOT police `draft` sizings — see the placeholder-refusal rationale
    above; an unrouted scaffold with no id is a legal, transient state.
  - Does NOT check that downstream artifacts CARRY the root id. That is the
    carry cascade's own job (`coordinator_core/ops/deliverable_carry.py`) and
    a fork already landed is not repairable by a corpus lint; this guard
    exists to stop the ROOT going null, which is what makes the fork possible.
  - Does NOT validate the rest of the sizing-object schema — `validate_frontmatter`
    over this corpus is a separate, wider sweep.
  - Does NOT re-mint or repair anything. A violation is an authoring defect and
    must be fixed on the record, not papered over by a fixture.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SIZINGS_DIR = _REPO_ROOT / "state" / "sizings"

_EXEMPT_STATUSES = frozenset({"draft"})


def _iter_sizing_files() -> list[Path]:
    if not _SIZINGS_DIR.is_dir():
        return []
    return sorted(_SIZINGS_DIR.glob("*.yaml"))


def test_every_routed_sizing_carries_a_deliverable_id() -> None:
    offenders: list[str] = []
    checked = 0
    for path in _iter_sizing_files():
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            # Parse failures are a different defect with a different owner;
            # this guard reads only what parses.
            continue
        if not isinstance(doc, dict):
            continue
        status = doc.get("status")
        if status in _EXEMPT_STATUSES:
            continue
        checked += 1
        if not doc.get("deliverable_id"):
            offenders.append(f"{path.name} (status: {status!r})")

    assert checked, "no non-draft sizing objects found — the corpus walk is not reaching state/sizings/"
    assert not offenders, (
        "sizing-object(s) past `draft` carry no `deliverable_id`, so the deliverable "
        "spine has no root and every downstream artifact mints its own forked id:\n  "
        + "\n  ".join(offenders)
        + "\nFix on the record: carry the id its routed plan already uses."
    )
