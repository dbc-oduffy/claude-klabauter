"""The commit-phase brief has a ceiling and a place to put evidence.

`_PROVENANCE_HEADING` is paid once per COMMIT PHASE of every emitted run, so a
four-wave run pays it four times. It grew by accretion because nothing measured
it: each incident appended its own narrative beside the rule it justified, and
one sitting added ~1.5 KB. The register the text is held to
(`docs/wiki/guard-messaging.md` § Register) forbids exactly that class --
repeated fact, self-legitimacy, reassurance -- but a register rule with no
measurement is a rule the next author does not experience.

Two pins, together:

1. A SHRINK-ONLY byte ceiling. Raising `_CEILING_BYTES` is the deliberate act
   this test exists to make visible; the correct move for a new incident is (2).
2. The brief cites the wiki section that holds the evidence, and that section
   exists. Deleting a narrative and pointing at a page that does not discuss it
   is the failure this pins out -- an agent that cannot reach the evidence
   talks itself out of the rule instead.

Negative spec: this is not a prose-quality lint and must not grow into one.
It measures bytes and checks one pointer resolves.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.emit import (
    _PROVENANCE_HEADING,
    _PROVENANCE_WIKI_POINTER,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Shrink-only. Measured after the 2026-09-18 register pass took the brief from
#: 15,207 B to 10,609 B by relocating its incident narratives to the wiki. A new
#: incident adds a clause here at most; its narrative goes to the page
#: `_PROVENANCE_WIKI_POINTER` names.
_CEILING_BYTES = 11_000


def test_the_commit_brief_stays_under_its_ceiling():
    size = len(_PROVENANCE_HEADING.encode("utf-8"))
    assert size <= _CEILING_BYTES, (
        f"_PROVENANCE_HEADING is {size} B, over the {_CEILING_BYTES} B "
        "ceiling. Every commit phase of every emitted run pays this. Move the "
        f"narrative to {_PROVENANCE_WIKI_POINTER} and keep one clause of "
        "anchor here, rather than raising the ceiling."
    )


def test_the_brief_cites_where_its_evidence_lives():
    assert _PROVENANCE_WIKI_POINTER in _PROVENANCE_HEADING, (
        "the brief no longer names the page carrying its incident evidence"
    )


def test_the_cited_wiki_page_and_section_exist():
    """A pointer to a page that does not discuss the relocated incidents is
    worse than the inline narrative it replaced."""
    rel, _, section = _PROVENANCE_WIKI_POINTER.partition(" § ")
    page = _REPO_ROOT / rel
    assert page.is_file(), f"the brief cites a nonexistent page: {rel}"
    text = page.read_text(encoding="utf-8")
    assert f"## {section}" in text, (
        f"{rel} carries no `## {section}` heading -- the brief points at a "
        "section that does not exist"
    )


def test_the_relocated_incidents_are_actually_discussed_there():
    """Each anchor the brief keeps must be reachable from the cited page. A
    bare commit sha in the brief with no prose behind it is an anchor an agent
    cannot act on."""
    rel, _, _ = _PROVENANCE_WIKI_POINTER.partition(" § ")
    text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
    for anchor in (
        "ef3bbb1663",  # per-file, not per-hunk
        "874cf35dd",  # no_delta drops the path the wave existed to deliver
        "anti-correlated",  # presentation quality vs. whether checking happened
        "touch-record.jsonl",  # claim liveness, per-path narrowing
        "include_orphans",  # the determinate-orphan route
        "gitattributes",  # which paths refuse a checkin conversion
    ):
        assert anchor in text, f"{rel} does not discuss {anchor!r}"
