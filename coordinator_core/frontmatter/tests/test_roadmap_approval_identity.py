"""
Corpus lint: every `state/roadmap/*/OVERVIEW.md` records its approvals against
the person-identity axis, not as freetext.

Rationale: `shape_approved_by` / `final_approved_by` are the only record of who
ratified a roadmap, and nothing in the engine reads them — so nothing ever
objected to what they held. Measured 2026-08-31, the eleven live OVERVIEWs
carried FIVE spellings of one human ("PM", "example-operator", "Donal example-operator",
"Donal (PM) 2026-07-28", "dbc-example-operator (standing delegation, EM-stamped)"), plus
a twelfth shape (`fact-layer-core`, `status: final-approved` with no approver
field at all and the ratification in a prose `pm_approval:` key).

Freetext is not merely untidy here. A `gate_evidence` leg that wants to assert
"the PM ratified this roadmap" has to compare against SOMETHING; against a
field that five sessions each spelled differently, the only honest leg kind is
`human`, which `reconcile/gate_eval.py` resolves permanently `indeterminate`
by construction (D4). That is how `sat-08` sat wedged: a real ratification,
recorded, machine-unreadable. This guard makes the field carry the same
`resolve_operating_person()["github"]` slug that `minted_by` and
`human_claimant` already carry, so an equality leg can name it.

`*_approved_via` distinguishes a direct PM ratification from one an EM stamped
under a standing delegation. That distinction lived inside two of the freetext
values and would have been destroyed by normalizing them; it is a separate
axis from identity and gets its own field rather than being smuggled back into
the name.

Negative-spec:
  - Does NOT tighten `roadmap.schema.json`. That schema is validated by
    DoE-claude against its own corpus (CLAUDE.md: its leniency is contract),
    so a `pattern` there would reject a sibling's records for a convention
    that is ours. `additionalProperties` is already true, so `*_approved_via`
    needs no schema edit to be legal. This guard polices claude-klabauter's corpus only.
  - Does NOT bump any schema version — a bump inverts the live guard until the
    mirror publishes.
  - Does NOT assert WHICH human approved, only that the value is slug-shaped.
    A slug check cannot catch a wrong-but-well-formed alias of the right
    person (`example-operator`, the `display` alias, is shaped legally while
    `dbc-example-operator`, the `github` alias, is the axis) — closing that needs an
    alias-registry lookup this corpus does not yet have.
  - Does NOT require approval fields on a `draft` roadmap; only a
    `final-approved` one must name its final approver.
"""
from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ROADMAP_DIR = _REPO_ROOT / "state" / "roadmap"

#: `resolve_operating_person()` casefolds the `github` alias, so a legal value
#: is lowercase; the rest of the shape mirrors a GitHub login.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VIA_VALUES = {"pm-direct", "standing-delegation"}
_PHASES = ("shape", "final")


def _overviews() -> list[Path]:
    if not _ROADMAP_DIR.is_dir():
        return []
    return sorted(_ROADMAP_DIR.glob("*/OVERVIEW.md"))


def _fields(path: Path) -> dict[str, str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    if split is None:
        return {}
    keys = [f"{p}_approved_{s}" for p in _PHASES for s in ("by", "at", "via")]
    keys.append("status")
    out = {}
    for key in keys:
        value = read_fm_field_unquoted(split.fm_text, key)
        if value is not None and value.strip():
            out[key] = value.strip()
    return out


def test_roadmap_overview_corpus_is_present():
    assert _overviews(), f"no */OVERVIEW.md found under {_ROADMAP_DIR} — corpus glob is likely wrong"


def test_approved_by_is_a_person_slug():
    """A recorded approver is a person-axis slug, never prose or a name-plus-date."""
    bad = []
    for path in _overviews():
        fields = _fields(path)
        for phase in _PHASES:
            value = fields.get(f"{phase}_approved_by")
            if value is not None and not _SLUG.match(value):
                bad.append(f"{path.relative_to(_REPO_ROOT).as_posix()}: {phase}_approved_by={value!r}")
    assert not bad, (
        "roadmap approvals must name the person-identity slug "
        "(resolve_operating_person()['github']), not freetext:\n  " + "\n  ".join(bad)
    )


def test_approval_carries_a_bare_date_and_a_via():
    """An approval that names a person also says when, and under what authority."""
    bad = []
    for path in _overviews():
        fields = _fields(path)
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for phase in _PHASES:
            if f"{phase}_approved_by" not in fields:
                continue
            at = fields.get(f"{phase}_approved_at")
            if at is None or not _ISO_DATE.match(at):
                bad.append(f"{rel}: {phase}_approved_at={at!r} is not a bare YYYY-MM-DD")
            via = fields.get(f"{phase}_approved_via")
            if via not in _VIA_VALUES:
                bad.append(f"{rel}: {phase}_approved_via={via!r} not in {sorted(_VIA_VALUES)}")
    assert not bad, "incomplete roadmap approval records:\n  " + "\n  ".join(bad)


def test_final_approved_roadmap_names_its_final_approver():
    """`status: final-approved` with no `final_approved_by` is an unattributable ratification."""
    bad = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _overviews()
        if _fields(path).get("status") == "final-approved" and "final_approved_by" not in _fields(path)
    ]
    assert not bad, "final-approved roadmaps with no recorded approver:\n  " + "\n  ".join(bad)
