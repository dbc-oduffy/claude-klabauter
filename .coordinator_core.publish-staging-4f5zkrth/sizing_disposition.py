"""Which room a baton's work belongs in, read off the baton's own FKs.

One predicate, two doors. `pickup-assemble brief` emits it today so an EM
picking up a baton is TOLD its sizing disposition instead of auditing for
one; `plan`'s admission gate is the second consumer, and the reason this
lives in its own module rather than inside the pickup assembler — the same
question asked at two seams must not become two implementations that drift.

The axis exists because a continuation's exemption from the sizing wall
keys on a CITATION, never on provenance: `spinoff` never enters the sizing
lobby, and `roadmap-planning` conforms an inbound sizing without stamping
one onto the batons it mints. So a baton that is essentially an idea used
to route an EM straight into `plan` on a route nobody computed.

DR-346 (2026-08-21): the corpus-walk resolution legs this module used to
carry — `resolve_plan_id`, `resolve_plan_by_deliverable`, and the whole
`docs/plans/*.md` / `archive/specs/*/*.md` glob hunt — are RETIRED. The
baton now carries its plan link directly as a stamped `governing_plan`
frontmatter field: a repo-relative POSIX path, checked with the SAME single
stat `_sizing_object_within_root` already used for `sizing_object`, never a
search. A baton whose `origin_plan_id` is populated but whose
`governing_plan` was never stamped reads `unsized` — stranding, not a
defect — PM-ratified 2026-08-21: "retire the walk immediately, stranding
accepted." The deliverable_id-inheritance leg is deleted outright, not
replaced: DR-346 names deliverable_id-resolves-to-plans as the defect
itself, never a feature to preserve.

Cross-repo ask: `cross-repo/inbox/2026-08-20-doe-claude-em-pickup-brief-
should-emit-the-sizing-disposition.md`. Doctrine side (DoE-claude):
`skills/plan/SKILL.md` carve-out 1, `skills/pickup/SKILL.md`; tripwire
`A-BATON-IS-NOT-A-SIZING-ARTIFACT`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

#: Tokens that mean "this record carries no id", on BOTH sides of the join.
#: Same set as `spec_backlink_resolve._real_id`, `deliverable_equivalence.
#: _YAML_NULL_LITERALS`, and `ops/ceremony/renderers._ID_NULL_SENTINELS` —
#: defined locally rather than imported because every one of those modules
#: self-registers a JSON-RPC op on import, and this predicate is read by
#: `plan`'s admission gate before any op registry exists.
_NULL_SENTINELS = frozenset({"null", "~"})


def _sizing_object_within_root(root: Path, sizing_ref: str) -> bool:
    """`True` iff `sizing_ref` is a file, resolves under `root`, and is not
    an absolute path.

    `sizing_object` is a repo-relative FK, but `Path.__truediv__` does not
    enforce that: an absolute right-hand operand REPLACES the left entirely
    (the left operand is discarded outright when the right is absolute),
    and a `..`-laden relative one walks straight past `root`'s boundary.
    Either shape lets a `sizing_object` value naming ANY file that happens
    to exist on disk — not a sizing artifact at all — earn `sized`.
    Rejecting an absolute `sizing_ref` up front, then requiring the resolved
    path to sit under `root`, closes both legs.

    `governing_plan` reuses this exact helper for the same reason: a
    stamped plan link is a repo-relative FK too, and the containment
    property it needs is identical — one stat, root-confined, never a
    search.
    """
    if Path(sizing_ref).is_absolute():
        return False
    candidate = root / sizing_ref
    try:
        resolved = candidate.resolve()
        resolved_root = root.resolve()
    except OSError:
        return False
    if not (resolved == resolved_root or resolved_root in resolved.parents):
        return False
    return candidate.is_file()


def real_id(value: object) -> Optional[str]:
    """`value` stripped, iff it is a non-empty string naming a real id.

    Both legs of this module's join must apply it, and the failure when
    either does not is not cosmetic. A baton whose own `origin_plan_id`
    survived frontmatter parsing as the STRING `"null"` — 80 of the live
    corpus do — must not read as a citation at all: it is an ABSENT
    citation, not a broken one. See DR-344 § R1: absence is information.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped.lower() in _NULL_SENTINELS:
        return None
    return stripped


#: The three values of the axis, in precedence order.
SIZING_DISPOSITION_VALUES: tuple[str, ...] = ("execution", "sized", "unsized")


def cited_plan_fks(fm: dict[str, Any]) -> list[tuple[str, str]]:
    """`(field, plan_id)` pairs the baton cites -- `origin_plan_id` ONLY.

    DR-346 §5 (Correction, C3 2026-08-21): `plan_ids` is a FAN-IN AUDIT
    list, never a plan FK, and reading it as one was the live defect that
    section names -- `2026-08-21-guards-under-the-brightline.md` carried no
    `origin_plan_id` and resolved `execution` SOLELY through a `plan_ids`
    entry, a fabricated citation exactly like the `"null"`-string one
    `real_id`'s own docstring fixes above. `plan_ids` keeps its write side
    (`resolve_lineage`'s `_ordered_unique` and `baton_assemble/apply.py ::
    baton-stamp-carried-ids`, both DR-346-threshold-gated at >=2 distinct
    ids as of this same change) as a pure audit trail; it is never joined
    into a citation here.

    Negative-spec: does NOT read `fm.get("plan_ids")`. A future reintroduction
    of that read is the exact regression `test_plan_ids_is_still_read_as_a_
    citation_dr346` pins against."""
    cited: list[tuple[str, str]] = []
    origin = real_id(fm.get("origin_plan_id"))
    if origin:
        cited.append(("origin_plan_id", origin))
    return cited


def compute_sizing_disposition(
    root: Path, fm: dict[str, Any], self_path: Optional[Path] = None
) -> dict[str, Any]:
    """`{"value", "basis", "warning"}` for one baton's frontmatter.

    Three values, precedence `governing_plan`-then-`sizing_object`:

      `execution` — `governing_plan` is stamped and resolves under `root`
                    (one stat, via `_sizing_object_within_root`). Sized AND
                    planned upstream; the EM resumes and re-litigates
                    neither.
      `sized`     — a `sizing_object` resolves and no `governing_plan` does.
                    `basis` carries the object's path so the sizing lobby
                    conforms it without a lookup.
      `unsized`   — neither resolves. The room is `coordinator:sizing`, not
                    `plan`, whatever the baton's provenance — including a
                    baton that cites `origin_plan_id` but whose
                    `governing_plan` was never stamped. That case is a
                    deliberate STRANDING, not a dangling pointer: DR-346
                    (2026-08-21, PM-ratified) retired the corpus walk that
                    used to resolve `origin_plan_id` by search, and an
                    unstamped link is no longer a claim this module is
                    entitled to call broken — it may resolve once stamped,
                    or it may not; either way this module does not search
                    for it. See `UNSIZED_UNSTAMPED_NEXT_MOVE_PREFIX`.

    Absence and non-resolution are two different failure modes. A
    `governing_plan` that IS stamped but does not exist under `root` is a
    genuine broken link (`warning` set, "dangling" language applies); a
    baton that never had `governing_plan` stamped at all is the stranding
    arm above and gets a DIFFERENT next-move text naming the unstamped
    field, never "dangling" or "does not resolve on disk" — this module no
    longer knows whether the plan exists, only that the pointer was never
    written.

    `self_path` is retained for signature compatibility with existing
    callers; the deliverable_id-inheritance leg that once consulted it is
    deleted (DR-346: deliverable_id resolves to batons, never to plans),
    so it is currently unused here.

    Negative-spec: this emits a FACT, never a gate. It contributes no
    judgment point, blocks no directive, and never enters `gates.coast` —
    admission stays the consuming seam's call. It is also silent on the
    `execution`/`sized` arms beyond the field itself, deliberately: the
    failure mode on that side is an EM re-litigating a baton that WAS sized,
    so saying nothing there is the correct emission, not an omission.

    Negative-spec (DR-346 R1): no fallback, no cache, no index, no registry,
    no "try the stamp then walk the corpus". A `governing_plan`-absent
    baton is unsized, full stop — absence is information.
    """
    governing_plan = real_id(fm.get("governing_plan"))
    if governing_plan:
        if _sizing_object_within_root(root, governing_plan):
            return {
                "value": "execution",
                "basis": f"governing_plan={governing_plan}",
                "warning": None,
            }
        return {
            "value": "unsized",
            "basis": f"governing_plan={governing_plan}",
            "warning": (
                f"Cited but unresolved on disk: governing_plan={governing_plan} — "
                "treated as unsized. A dangling FK is not a sizing artifact."
            ),
        }

    sizing_raw = fm.get("sizing_object")
    sizing_ref = sizing_raw.strip() if isinstance(sizing_raw, str) and sizing_raw.strip() else None
    if sizing_ref and _sizing_object_within_root(root, sizing_ref):
        return {"value": "sized", "basis": f"sizing_object={sizing_ref}", "warning": None}

    dangling = []
    if sizing_ref:
        dangling.append(f"sizing_object={sizing_ref}")
    if dangling:
        joined = ", ".join(dangling)
        return {
            "value": "unsized",
            "basis": joined,
            "warning": (
                f"Cited but unresolved on disk: {joined} — treated as unsized. "
                "A dangling FK is not a sizing artifact."
            ),
        }

    cited = cited_plan_fks(fm)
    if cited:
        joined = ", ".join(f"{field}={plan_id}" for field, plan_id in cited)
        return {
            "value": "unsized",
            "basis": joined,
            "warning": (
                f"The baton carries {joined} but no governing_plan was ever stamped "
                "onto it — an unstamped plan link, not a resolvable citation. "
                "`governing_plan` is the field that would resolve it."
            ),
        }

    return {"value": "unsized", "basis": None, "warning": None}


#: Prepended onto a consumer's `next_move` when the value is `unsized`, and
#: ONLY then — see `compute_sizing_disposition`'s negative-spec for why the
#: other two arms stay silent. Three texts, because the three unsized arms
#: are not the same finding: a baton that cites nothing is an ordinary idea;
#: a baton whose `governing_plan` does not resolve is a broken pointer the
#: reader needs named; a baton that cites `origin_plan_id` with no
#: `governing_plan` stamped is a stranding — DR-346 (2026-08-21) retired the
#: corpus walk that used to resolve the latter by search, and this module is
#: no longer entitled to call it "dangling" or "does not resolve on disk",
#: because it no longer knows whether the plan exists at all.
UNSIZED_NEXT_MOVE_PREFIX = (
    "This baton cites no plan and no sizing object, so it is an unsized ask whatever its "
    "provenance — the room is `coordinator:sizing`, not `plan`. "
)

UNSIZED_DANGLING_NEXT_MOVE_PREFIX = (
    "This baton cites a plan or sizing object that does not resolve on disk, so it is "
    "unsized in fact — the room is `coordinator:sizing`, not `plan`. "
)

UNSIZED_UNSTAMPED_NEXT_MOVE_PREFIX = (
    "This baton carries a plan citation but no `governing_plan` was ever stamped onto "
    "it, so it is unsized as carried — `governing_plan` is the field that would "
    "resolve it, and it was left unset. The room is `coordinator:sizing`, not `plan`. "
)


def unsized_next_move_prefix(verdict: dict[str, Any]) -> str:
    """The `next_move` prefix a verdict earns — empty string on the
    `execution`/`sized` arms, so a consumer can prepend unconditionally
    without re-deciding the axis it was just handed."""
    if verdict.get("value") != "unsized":
        return ""
    warning = verdict.get("warning")
    if warning and "governing_plan was ever stamped" in warning:
        return UNSIZED_UNSTAMPED_NEXT_MOVE_PREFIX
    if warning:
        return UNSIZED_DANGLING_NEXT_MOVE_PREFIX
    return UNSIZED_NEXT_MOVE_PREFIX
