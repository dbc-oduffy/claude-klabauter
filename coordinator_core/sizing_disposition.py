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

Cross-repo ask: `cross-repo/inbox/2026-08-20-doe-claude-em-pickup-brief-
should-emit-the-sizing-disposition.md`. Doctrine side (DoE-claude):
`skills/plan/SKILL.md` carve-out 1, `skills/pickup/SKILL.md`; tripwire
`A-BATON-IS-NOT-A-SIZING-ARTIFACT`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

#: Directories whose `*.md` records may declare a `plan_id:` — the search
#: space `resolve_plan_id` walks. Live plans first (the common hit);
#: archived specs second, because an execution baton outlives its plan's
#: archival and must still read as `execution` after the move.
PLAN_ID_SEARCH_GLOBS: tuple[str, ...] = (
    "docs/plans/*.md",
    "archive/specs/*/*.md",
)

#: Bytes of each candidate read while hunting a `plan_id:`. Frontmatter is a
#: leading block, so the whole corpus is never paid for.
_PLAN_ID_SCAN_BYTES = 4096

_PLAN_ID_RE = re.compile(r"""^plan_id:\s*["']?([^"'\s]+)""", re.M)

#: The three values of the axis, in precedence order.
SIZING_DISPOSITION_VALUES: tuple[str, ...] = ("execution", "sized", "unsized")


def resolve_plan_id(root: Path, plan_id: str) -> Optional[str]:
    """Repo-relative path of the plan record declaring `plan_id: <plan_id>`,
    or `None` when no record on disk claims it.

    A `pln-`-prefixed id is a cross-entity ref, not a path (see
    `handoff.schema.json :: origin_plan_id`), so resolution is a frontmatter
    hunt rather than a stat. Only ever reached when a baton actually cites a
    plan FK — which is exactly the population the scan buys an `execution`
    verdict for. A baton citing nothing pays nothing.

    Negative-spec: resolves the FK and stops. Does not read the plan's
    `status`, does not judge whether it is live, abandoned, or superseded,
    and never opens the plan body. A stale pointer still means this baton
    was planned upstream, which is the only question this axis asks — plan
    liveness is a separate read with a separate owner.
    """
    for pattern in PLAN_ID_SEARCH_GLOBS:
        for candidate in sorted(root.glob(pattern)):
            try:
                head = candidate.read_text(encoding="utf-8", errors="replace")[:_PLAN_ID_SCAN_BYTES]
            except OSError:
                continue
            match = _PLAN_ID_RE.search(head)
            if match and match.group(1) == plan_id:
                return candidate.relative_to(root).as_posix()
    return None


def cited_plan_fks(fm: dict[str, Any]) -> list[tuple[str, str]]:
    """`(field, plan_id)` pairs the baton cites, `origin_plan_id` before
    `plan_ids`. The singular is the single-plan case's field and the plural
    its fan-in sibling; neither supersedes the other, and a record may carry
    either, both, or neither."""
    cited: list[tuple[str, str]] = []
    origin = fm.get("origin_plan_id")
    if isinstance(origin, str) and origin.strip():
        cited.append(("origin_plan_id", origin.strip()))
    plural = fm.get("plan_ids")
    if isinstance(plural, list):
        for entry in plural:
            if isinstance(entry, str) and entry.strip():
                cited.append(("plan_ids", entry.strip()))
    return cited


def compute_sizing_disposition(root: Path, fm: dict[str, Any]) -> dict[str, Any]:
    """`{"value", "basis", "warning"}` for one baton's frontmatter.

    Three values, precedence plan-FK-then-`sizing_object`:

      `execution` — a plan FK resolves. Sized AND planned upstream; the EM
                    resumes and re-litigates neither.
      `sized`     — a `sizing_object` resolves and no plan FK does. `basis`
                    carries the object's path so the sizing lobby conforms
                    it without a lookup.
      `unsized`   — neither resolves. The room is `coordinator:sizing`, not
                    `plan`, whatever the baton's provenance.

    Absence and non-resolution are two different failure modes, and the
    prose-only wall this replaces could catch neither. A cited-but-dangling
    FK is `unsized` PLUS a named `warning`, with `basis` still recording the
    dangling citation — "we looked, and this is what did not resolve" is the
    diagnostic a reader needs; silently passing to `execution` on an id that
    nothing on disk declares is the bug.

    Negative-spec: this emits a FACT, never a gate. It contributes no
    judgment point, blocks no directive, and never enters `gates.coast` —
    admission stays the consuming seam's call. It is also silent on the
    `execution`/`sized` arms beyond the field itself, deliberately: the
    failure mode on that side is an EM re-litigating a baton that WAS sized,
    so saying nothing there is the correct emission, not an omission.
    """
    cited = cited_plan_fks(fm)
    for field, plan_id in cited:
        resolved = resolve_plan_id(root, plan_id)
        if resolved:
            return {
                "value": "execution",
                "basis": f"{field}={plan_id} -> {resolved}",
                "warning": None,
            }

    sizing_raw = fm.get("sizing_object")
    sizing_ref = sizing_raw.strip() if isinstance(sizing_raw, str) and sizing_raw.strip() else None
    if sizing_ref and (root / sizing_ref).is_file():
        return {"value": "sized", "basis": f"sizing_object={sizing_ref}", "warning": None}

    dangling = [f"{field}={plan_id}" for field, plan_id in cited]
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
    return {"value": "unsized", "basis": None, "warning": None}


#: Prepended onto a consumer's `next_move` when the value is `unsized`, and
#: ONLY then — see `compute_sizing_disposition`'s negative-spec for why the
#: other two arms stay silent. Two texts, because the two unsized arms are
#: not the same finding: a baton that cites nothing is an ordinary idea, and
#: a baton whose citation does not resolve is a broken pointer the reader
#: needs named. Telling the second "you cite no plan" would be false.
UNSIZED_NEXT_MOVE_PREFIX = (
    "This baton cites no plan and no sizing object, so it is an unsized ask whatever its "
    "provenance — the room is `coordinator:sizing`, not `plan`. "
)

UNSIZED_DANGLING_NEXT_MOVE_PREFIX = (
    "This baton cites a plan or sizing object that does not resolve on disk, so it is "
    "unsized in fact — the room is `coordinator:sizing`, not `plan`. "
)


def unsized_next_move_prefix(verdict: dict[str, Any]) -> str:
    """The `next_move` prefix a verdict earns — empty string on the
    `execution`/`sized` arms, so a consumer can prepend unconditionally
    without re-deciding the axis it was just handed."""
    if verdict.get("value") != "unsized":
        return ""
    if verdict.get("warning"):
        return UNSIZED_DANGLING_NEXT_MOVE_PREFIX
    return UNSIZED_NEXT_MOVE_PREFIX
