"""coordinator_core.ops.docgen.tests.test_dr_corpus_ids_unique — corpus-level
uniqueness sweep over this repo's ``docs/decisions/`` records.

Purpose: assert that no two decision records claim the same DR id, reading the
on-disk corpus as a whole rather than one proposed write at a time.

Why this exists, and why it is NOT a duplicate of the write-time guard:
``dr_allocator.assert_dr_id_unique`` and the ``doc.scaffold`` tripwire in
``test_dr_write_site_guard.py`` both police a SINGLE WRITE — they answer "is
the id I am about to write already taken". Neither can see a collision that is
already on disk, and neither runs at all when a record is hand-authored past
``coordinator-doc-new``. That gap is not hypothetical: ``DR-345`` carried two
records for four days (2026-08-21 to 2026-08-25) because at least one of them
was written by hand, and before that ``DR-EXAMPLE-GAME-REPO-006`` collided eight ways
and sat unnoticed for five weeks. In both incidents a check existed and was
structurally unable to fire.

This sweep is the complementary DETECTOR: it does not stop the wrong write, it
makes an existing collision fail the suite on the next run. It reads corpus
state, so it fires no matter which tool — or which pair of hands — produced the
records.

Numeric equivalence, not string equality, matching ``assert_dr_id_unique``'s
own comparison: ``DR-002`` and ``DR-0002`` are the same id, and a namespaced
``DR-EXAMPLE-GAME-REPO-006`` is a different id from ``DR-006``. The key is
``(prefix, int(number))``.

Effective id, matching ``allocate_dr_number``'s own view of the corpus: a
record's id is its ``id:`` frontmatter when it carries one, otherwise the id
leading its filename. Records with neither are not decision records for this
purpose and are skipped rather than failed — the same leniency
``_read_frontmatter_dr_id`` applies, for the same reason (a malformed record
must not break the mechanism that reads around it).

Negative-spec:
    Do NOT relax this into a warning, an ``xfail``, or a skip when it fires.
    A red here means two records claim one id RIGHT NOW; the decision graph is
    ambiguous until one of them moves. The remedy is renumbering the record
    with the smaller blast radius (fewest live citations, and never the side
    holding a cross-DR edge), per the 2026-08-25 precedent recorded in
    ``state/bug-backlog/2026-08-25-a-hand-authored-dr-bypasses-the-write-time-collision-guard.yaml``.
    Do NOT widen the scan past ``docs/decisions/``: archived and frozen
    artifacts describe what was true when written and are deliberately left
    stale by that same precedent.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from coordinator_core.ops.docgen.dr_allocator import DR_ID_RE

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DECISIONS_DIR = _REPO_ROOT / "docs" / "decisions"

_FRONTMATTER_MAX_LINES = 20
_FRONTMATTER_ID_LINE_RE = re.compile(r"^id:\s*(\S+)\s*$")


def _parse_dr_key(raw: str) -> tuple[str, int] | None:
    """Parse a DR id into its ``(prefix, number)`` comparison key, or None.

    Accepts a bare id (``DR-291``), a filename stem (``DR-291-some-slug``), and
    a full filename (``DR-291-some-slug.md``) alike: ``DR_ID_RE`` anchors on a
    trailing hyphen, so a bare id is normalized by appending one — the same
    trick ``assert_dr_id_unique`` uses to fold both shapes onto one pattern.
    """
    match = DR_ID_RE.match(raw) or DR_ID_RE.match(f"{raw}-")
    if match is None:
        return None
    return (match.group(1) or "", int(match.group(2)))


def _frontmatter_id(path: Path) -> str | None:
    """Return the record's ``id:`` frontmatter value, or None.

    Bounded read of the leading lines only, mirroring
    ``dr_allocator._read_frontmatter_dr_id``. Any read failure returns None so
    an unreadable file is skipped by the caller rather than crashing the sweep.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            for _ in range(_FRONTMATTER_MAX_LINES):
                line = handle.readline()
                if not line:
                    break
                match = _FRONTMATTER_ID_LINE_RE.match(line.rstrip("\n"))
                if match:
                    return match.group(1)
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _effective_dr_keys(decisions_dir: Path) -> dict[tuple[str, int], list[str]]:
    """Map each ``(prefix, number)`` key to the record filenames claiming it.

    Frontmatter wins over the filename when both are present and parse: the
    frontmatter ``id:`` is what every downstream citation and cross-DR
    ``related:`` edge joins on, so it is the record's identity even where a
    filename disagrees.
    """
    claims: dict[tuple[str, int], list[str]] = defaultdict(list)
    for path in sorted(decisions_dir.glob("*.md")):
        raw = _frontmatter_id(path)
        key = _parse_dr_key(raw) if raw else None
        if key is None:
            key = _parse_dr_key(path.name)
        if key is None:
            continue
        claims[key].append(path.name)
    return claims


def _render_key(key: tuple[str, int]) -> str:
    prefix, number = key
    return f"DR-{prefix}-{number}" if prefix else f"DR-{number}"


def test_every_decision_record_claims_a_unique_dr_id():
    """No two records in docs/decisions/ share a DR id.

    Guard name: dr-corpus-id-uniqueness. If you are grepping for why this test
    exists: it is the on-disk detector for collisions the write-time guard
    cannot see, armed after DR-345 carried two records for four days because
    one of them was hand-authored past ``coordinator-doc-new``.
    """
    assert _DECISIONS_DIR.is_dir(), (
        f"dr-corpus-id-uniqueness: expected a decisions corpus at "
        f"{_DECISIONS_DIR} and found none — this sweep resolves the repo root "
        f"from its own module path, so a moved test file breaks it silently "
        f"unless this assertion fires."
    )

    contested = {
        key: names
        for key, names in _effective_dr_keys(_DECISIONS_DIR).items()
        if len(names) > 1
    }

    detail = "\n".join(
        f"  {_render_key(key)} claimed by {len(names)} records:\n"
        + "\n".join(f"    - {name}" for name in sorted(names))
        for key, name_list in sorted(contested.items())
        for names in (name_list,)
    )

    assert not contested, (
        "dr-corpus-id-uniqueness FIRED: "
        f"{len(contested)} DR id(s) are claimed by more than one record in "
        f"docs/decisions/. Every citation of a contested id is ambiguous, and "
        f"every cross-DR `related:` edge into one is unresolvable, until one "
        f"record moves.\n\n"
        f"{detail}\n\n"
        "Remedy: renumber the record with the smaller blast radius — fewest "
        "live citations in code, tests, other decision records, and live "
        "plans — and never the side holding a cross-DR edge. Allocate the new "
        "id via `coordinator_core.ops.docgen.dr_allocator.allocate_dr_number`, "
        "update the record's `id:` frontmatter AND its filename, add a "
        "renumbering note to the record body, and update its live citations. "
        "Leave archived and frozen artifacts stale: they describe what was "
        "true when written. Precedent: "
        "state/bug-backlog/2026-08-25-a-hand-authored-dr-bypasses-the-write-time-collision-guard.yaml"
    )
