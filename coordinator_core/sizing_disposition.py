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

_PLAN_DELIVERABLE_RE = re.compile(r"""^deliverable_id:\s*["']?([^"'\s]+)""", re.M)

#: Tokens that mean "this record carries no id", on BOTH sides of the join.
#: Same set as `spec_backlink_resolve._real_id`, `deliverable_equivalence.
#: _YAML_NULL_LITERALS`, and `ops/ceremony/renderers._ID_NULL_SENTINELS` —
#: defined locally rather than imported because every one of those modules
#: self-registers a JSON-RPC op on import, and this predicate is read by
#: `plan`'s admission gate before any op registry exists.
_NULL_SENTINELS = frozenset({"null", "~"})


def _real_id(value: object) -> Optional[str]:
    """`value` stripped, iff it is a non-empty string naming a real id.

    Both legs of this module's join must apply it, and the failure when
    either does not is not cosmetic. The scanned globs contain a plan whose
    frontmatter reads `plan_id: null  # stamped by a real /plan run`, and
    `_PLAN_ID_RE` captures the literal token `null` off it. A baton whose
    own `origin_plan_id` survived frontmatter parsing as the STRING
    `"null"` — 80 of the live corpus do — then matched that plan by string
    equality and was stamped `execution`: "sized upstream, re-litigate
    nothing", citing a stale unrelated draft as the authorization. A
    fabricated citation is strictly worse than the `unsized` this now
    returns, because the EM acts on it. See DR-344 § R1: absence is
    information.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or stripped.lower() in _NULL_SENTINELS:
        return None
    return stripped

#: The three values of the axis, in precedence order.
SIZING_DISPOSITION_VALUES: tuple[str, ...] = ("execution", "sized", "unsized")


def _plan_frontmatter_head(candidate: Path) -> Optional[str]:
    """The leading bytes of a plan record, or `None` if unreadable."""
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")[:_PLAN_ID_SCAN_BYTES]
    except OSError:
        return None


def _resolve_plan_by(
    root: Path,
    pattern_re: "re.Pattern[str]",
    wanted: str,
    self_path: Optional[Path] = None,
) -> Optional[str]:
    """Repo-relative path of the first plan record whose `pattern_re` capture
    equals `wanted`, or `None`.

    `self_path` excludes the inbound artifact from its own resolution. A
    record is never its own upstream plan — see `compute_sizing_disposition`'s
    `self_path` parameter for why that is a correctness property and not a
    caller's convenience."""
    resolved_self = None
    if self_path is not None:
        try:
            resolved_self = Path(self_path).resolve()
        except OSError:
            resolved_self = None
    for pattern in PLAN_ID_SEARCH_GLOBS:
        for candidate in sorted(root.glob(pattern)):
            if resolved_self is not None:
                try:
                    if candidate.resolve() == resolved_self:
                        continue
                except OSError:
                    pass
            head = _plan_frontmatter_head(candidate)
            if head is None:
                continue
            match = pattern_re.search(head)
            if match and _real_id(match.group(1)) == wanted:
                return candidate.relative_to(root).as_posix()
    return None


def resolve_plan_id(root: Path, plan_id: str) -> Optional[str]:
    """Repo-relative path of the plan record declaring `plan_id: <plan_id>`,
    or `None` when no record on disk claims it.

    A `pln-`-prefixed id is a cross-entity ref, not a path (see
    `handoff.schema.json :: origin_plan_id`), so resolution is a frontmatter
    hunt rather than a stat.

    Negative-spec: resolves the FK and stops. Does not read the plan's
    `status`, does not judge whether it is live, abandoned, or superseded,
    and never opens the plan body. A stale pointer still means this baton
    was planned upstream, which is the only question this axis asks — plan
    liveness is a separate read with a separate owner.
    """
    return _resolve_plan_by(root, _PLAN_ID_RE, plan_id)


def resolve_plan_by_deliverable(
    root: Path, deliverable_id: str, self_path: Optional[Path] = None
) -> Optional[str]:
    """Repo-relative path of a plan record carrying the SAME
    `deliverable_id`, or `None`.

    This is the execution leg that matters in practice, and the one an
    FK-name reading misses. `origin_plan_id` is **fork-spawn** provenance —
    its own schema description says "the plan under execution when this fork
    was spawned" — so an ordinary `/handoff` fired mid-`execute-plan` is not
    a fork and carries neither plan FK. Measured on this corpus at the time
    this landed: 1 of 150 live batons carried a plan FK; 36 more inherit a
    plan's `deliverable_id`. Without this leg every one of those 36 computes
    `unsized` and the EM is sent to the sizing lobby for work planned days
    earlier — the precise failure the axis exists to prevent, arriving
    through the field meant to prevent it.

    Why INHERITANCE and not mere presence: `deliverable_id` is minted once
    at the earliest artifact and carried verbatim downstream, but a
    `spinoff` mints its OWN fresh `dlv-<slug>-<6hex>`, so a baton having the
    key proves nothing. A plan on disk carrying the SAME key is the
    discriminant — a self-minted id matches no plan and correctly stays
    uncited.
    """
    return _resolve_plan_by(root, _PLAN_DELIVERABLE_RE, deliverable_id, self_path=self_path)


def cited_plan_fks(fm: dict[str, Any]) -> list[tuple[str, str]]:
    """`(field, plan_id)` pairs the baton cites, `origin_plan_id` before
    `plan_ids`. The singular is the single-plan case's field and the plural
    its fan-in sibling; neither supersedes the other, and a record may carry
    either, both, or neither."""
    cited: list[tuple[str, str]] = []
    origin = _real_id(fm.get("origin_plan_id"))
    if origin:
        cited.append(("origin_plan_id", origin))
    plural = fm.get("plan_ids")
    if isinstance(plural, list):
        for entry in plural:
            real = _real_id(entry)
            if real:
                cited.append(("plan_ids", real))
    return cited


def compute_sizing_disposition(
    root: Path, fm: dict[str, Any], self_path: Optional[Path] = None
) -> dict[str, Any]:
    """`{"value", "basis", "warning"}` for one baton's frontmatter.

    Three values, precedence plan-FK-then-`sizing_object`:

      `execution` — a plan FK resolves, OR the baton inherits a plan's
                    `deliverable_id`. Sized AND planned upstream; the EM
                    resumes and re-litigates neither. The inheritance leg is
                    the one that carries in practice — see
                    `resolve_plan_by_deliverable` for why an FK-name reading
                    misses the ordinary mid-execution continuation.
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

    `self_path` is the inbound artifact's own path, excluded from the
    inheritance scan. A record is never its own upstream plan, and omitting
    this is not cosmetic: a PLAN carries a `deliverable_id` and lives in the
    scanned globs, so it matches ITSELF and reads `execution` — "already
    planned", citing itself as the evidence. That silently waves through the
    exact wall condition `plan` exists to catch, an unsized plan. Batons do
    not live in the scanned globs, so a caller passing one may omit it; a
    caller passing a plan must not.

    A `deliverable_id` that matches NO plan is not a citation and does not
    reach the `warning` arm: a spinoff minting its own fresh key is the
    ordinary case, not a dangling pointer. Only `origin_plan_id`/`plan_ids`/
    `sizing_object` — fields whose whole purpose is to point elsewhere — earn
    a warning when they fail to resolve.

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

    deliverable_id = _real_id(fm.get("deliverable_id"))
    if deliverable_id:
        inherited = resolve_plan_by_deliverable(root, deliverable_id, self_path=self_path)
        if inherited:
            return {
                "value": "execution",
                "basis": f"deliverable_id={deliverable_id} -> {inherited} (inherited)",
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
