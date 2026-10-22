"""
coordinator_core.roadmap.plan_gate — baton and plan record readers.

Purpose: the shared disk-read layer over `state/handoffs/` batons and
`docs/plans/` plans that the roadmap audit, plan-blitz landing, baton supersede
and sizing-fire code read through. It provides:

  - a narrow frontmatter scanner (`_scan_fields`) over declared baton and plan
    keys;
  - the plan index (`PlanIndex`, `build_plan_index`) and the plan link
    resolution (`link_plans`, `_best_plan`, `_plan_link_ambiguity`);
  - the baton scan (`scan_batons`), which resolves a blocker id into
    `archive/handoffs/**` only when the live tree leaves it unresolved;
  - sizing-citation and sizing-route readers (`_resolve_sizing_citations`,
    `_sizing_route`) and the archived-plan locator (`archived_plan_path`).

Domain vocabulary: baton, blocker, plan link basis, review-approved plan.

This module registers no op and writes nothing.

Budget: pure reads, ZERO spawns. One bounded head-read per record, so cost
scales with record COUNT, not with corpus bytes.

Negative-spec:
  - Does NOT register an op, mutate frontmatter, or write anything.
  - Does NOT shell out, and does NOT ask git whether a blocker landed.
    `deployment_state` and the linked plan's `status` are the disk-truth this
    reads; a caller wanting SHA-level proof reads `shipped_in` itself.
  - Does NOT invent a plan link. The link bases in `_PLAN_LINK_ORDER` are tried
    in a fixed order and the one that answered is returned; no title/slug fuzzy
    match, ever — a plan is linked by an id or a path or not at all.
  - Does NOT scan `archive/handoffs/**` for the live baton list but DOES resolve
    blockers into it — a blocker that shipped and was archived is the most
    common resolved blocker there is.
  - Does NOT import `coordinator_core.ops.*`. This module is a leaf under
    `coordinator_core.roadmap` for the same reason `spine.py` is: an `ops/`
    module importing it must not pull `ops/__init__`'s registration loop back
    through itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# `coordinator_core.dag` is NOT imported at module scope. It is a heavy module
# (subprocess/hashlib/threading pulled in for its git-history machinery this
# module never touches) and this file needs exactly three pure-parsing names
# from it (`_BLOCK_SCALAR_RE`, `_consume_block_scalar`, `_parse_frontmatter`).
# Each is imported inside the one function that uses it (`_scan_fields`,
# `_read_frontmatter_head`) — DR-344's brightline cold-path budget, and the
# same "import inside the predicate that needs it" convention
# `coordinator_core.roadmap.prep_gate`'s own negative-spec already states for
# its spine readers. A bare `import coordinator_core.roadmap.plan_gate` (a
# capability probe, or a CLI's early argument-error exit before any gate ever
# runs) now pays nothing for `dag`'s git machinery.

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Plan `status` values (plan.schema.json) that count as review-approved. The
#: seam is `approved`: `draft`/`reviewed` are pre-ratification, and everything
#: from `approved` rightward has cleared the review gate and cannot un-clear it.
#: `deferred`/`abandoned`/`superseded` are deliberately ABSENT — a shelved plan
#: publishes no decisions a dependent can build on.
PLAN_APPROVED_STATUSES = frozenset({"approved", "executing", "landed", "implemented"})

#: Plan `status` values that mean the work itself landed.
PLAN_CODED_STATUSES = frozenset({"landed", "implemented"})

#: Baton `deployment_state` values (handoff.schema.json) that mean the work
#: landed. `continued` is a succession terminal and `closed` a deliberate stop:
#: both mean this baton will never ship anything further, so a dependent waiting
#: on it waits forever. Treating them as coded is the fail-loud-friendly choice
#: — the alternative is a permanently jammed wave nothing reports on.
BATON_CODED_STATES = frozenset({"shipped", "continued", "closed"})

#: `baton_role` values eligible to be a blitz CANDIDATE. A `record` baton is a
#: filed artifact, never offered for pickup, so it is never planned.
_CANDIDATE_ROLES = frozenset({"work"})

#: `deployment_state` values a candidate may hold. `in_flight` is excluded: it
#: is already being worked, and handing it to a planning wave races its holder.
_CANDIDATE_STATES = frozenset({"ready_to_fire", "awaiting_gate"})

#: Keys the fleet schema defines for a DIFFERENT object, which a reader may write
#: onto a baton expecting it to hold the baton back. They do nothing: candidacy is
#: `baton_role` + `status` + `deployment_state` and consults nothing else, so the
#: record is well-formed frontmatter that changes no verdict — and the NEXT wave's
#: EM reads the field and concludes the question was settled. That is worse than
#: the open defect, because it looks answered.
#:
#: `external_gate` is the measured member: it is a plan SPINE-ROW field
#: (`prep_gate.py :: _external_deps`), and two independent plan-blitz wave EMs
#: recommended the baton-record form to example-game-workbench-repo-b8 as the fix for
#: host-gated batons recycling every wave. The mechanism that does work is
#: `blocked_by: [host:darwin|host:win32|host:linux]` (DoE doctrine 01123ed2a9,
#: tripwire AN-EXTERNAL-GATE-ON-A-BATON-IS-INERT).
#:
#: Membership criterion, so this set grows on evidence rather than on suspicion: a
#: key must be DEFINED by the fleet schema for another object AND plausibly written
#: here in the belief it suppresses. A key that is merely unknown is not a member —
#: `_BATON_FIELDS`' silence about it is correct, and reporting every stray key would
#: bury this one.
#:
#: Third signal, and the one that predicts which candidates earn a slot
#: (example-game-workbench-repo-b8, who found the case): the key was RECOMMENDED — named
#: by agent-facing guidance, a skill body, or a generated verdict as the thing to
#: write. `external_gate` reached them through two independent wave EMs' pull
#: reasons, which is generated guidance converging on one wrong key rather than an
#: author free-associating a plausible name. That is both how the mistake scales and
#: why it survived two reviews, and it is checkable by grep rather than by judgement.
#:
#: `gate_dependency` is the near-miss and stays OUT, deliberately: it is a key about
#: gating that does not suppress planning, which is the exact shape that invites
#: addition. But this module already READS it and reports it under `gated`, with a
#: note redirecting to `plan_blitz_hold_reason` — so listing it here would report a
#: key that is already explained, which is the burying this criterion exists to
#: prevent.
_INERT_ON_A_BATON = frozenset({"external_gate", "pm_approved"})

#: Lines read from the head of each record before the frontmatter block is
#: declared unterminated. A cap, not a budget: claude-klabauter's widest live handoff
#: frontmatter is a small fraction of this, and a record that blows it is
#: malformed rather than large.
_FRONTMATTER_MAX_LINES = 400

#: Frontmatter keys this module ever reads off a BATON. Declared, because
#: `_scan_fields` is a narrow reader — a field absent from this set reads as
#: absent from the record, so adding a field to the logic means adding it here.
#: Pinned against the general parser by
#: `tests/test_plan_gate.py::test_narrow_scan_agrees_with_the_general_parser`.
_BATON_FIELDS = frozenset(
    {
        "title", "kind", "status", "deployment_state", "baton_role",
        "stub_id", "handoff_id", "deliverable_id", "deliverable_ids",
        "roadmap_id", "sprint", "wave", "blocked_by", "blocks",
        # The landing evidence for a terminal baton. This module's own
        # negative-spec points a caller wanting SHA-level proof at `shipped_in`,
        # so it has to be readable through the same scanner everything else is.
        "shipped_in", "shipped_in_kind",
        "governing_plan", "origin_plan_id", "plan_ids",
        # The replan-supersession lineage. `replan_of` is the engine-stamped
        # edge (this record's own id, naming the baton it replaces);
        # `forked_from` is the pre-existing general lineage field every
        # replan baton already carried (the source baton's PATH) before
        # `replan_of` existed — read as the fallback link for a pair minted
        # before this field, so `_w_replanned` can drop the source either way.
        "replan_of", "forked_from",
        "sizing_object", "sizing_objects",
        # The plan->execute seam. `handoff_phase: execution` means this baton has
        # been dispositioned and is waiting on /execute-plan, not on planning — read
        # here so a sweep does not re-plan work that already has its marching orders.
        "handoff_phase", "execution_authorized_by", "execution_authorized_at",
        "execution_authorized_sha", "execution_authorized_note",
        # The plan-blitz hold. FLAT keys, not a nested mapping, because this
        # scanner cannot read one — the constraint the schema's own field
        # descriptions record so nobody tidies them into a map later.
        "plan_blitz_hold_reason", "plan_blitz_hold_cite", "plan_blitz_hold_until",
        # A non-empty value re-offers a pulled baton: the hold is cleared.
        "plan_blitz_hold_cleared_by",
        # Terminal: the baton's work was folded into the baton this names.
        "superseded_by",
        # The OWNER's own declared gate. `deployment_state: awaiting_gate` above is
        # half the statement; this is the half that says what is being waited on.
        "gate_dependency",
    }
)

#: Frontmatter keys this module ever reads off a PLAN. Same contract as above.
#: `kind` and `plan` are read for the sidecar discriminator below, not for any
#: gate: they are how a plan is told apart from the review and coverage-check
#: sidecars that live in the same directory.
_PLAN_FIELDS = frozenset(
    {"title", "status", "plan_id", "deliverable_id", "sizing_object", "kind", "plan"}
)

#: Keys sufficient to dispose of an ARCHIVED baton acting as a blocker. An
#: archived baton in a terminal `deployment_state` needs nothing else; one that
#: is NOT terminal is re-read in full (`_read_baton_fields`) rather than guessed
#: at — see `_resolve_archived`.
_ARCHIVE_PROBE_FIELDS = frozenset(
    {"stub_id", "handoff_id", "deliverable_id", "deployment_state", "baton_role", "title"}
)


# ---------------------------------------------------------------------------
# Record readers
# ---------------------------------------------------------------------------
#
# Why a narrow scanner rather than `coordinator_core.dag._parse_frontmatter`
# everywhere: the general parser costs ~0.26ms per record against ~0.04ms for
# the narrow one, and a whole-tree pass reads ~1600 records. That difference is
# the whole brightline. The narrow scanner is not a second frontmatter parser
# with its own opinions — it reads the same three shapes the general one does
# for the ~20 declared keys above (scalar, inline list, block list), and
# `test_narrow_scan_agrees_with_the_general_parser` reads the entire live corpus
# through BOTH and asserts field-by-field agreement, so a divergence turns the
# suite red rather than quietly changing a gate's verdict.


#: YAML spellings of the null scalar. Read as absence, never as the four-letter
#: string: `origin_plan_id: null` is how the authoring tools spell "this baton
#: has no plan", and carrying it through as text makes it a plan id to look up.
_NULL_SCALARS = frozenset({"null", "~", "Null", "NULL"})


def _strip_comment(value: str) -> str:
    """Drop an unquoted trailing `# ...` comment from one YAML value.

    Quote-aware in the only way that matters here: a value opening with a quote
    is returned untouched (its own quotes delimit it), an unquoted one ends at
    the first ` #`. This is not a nicety — live records carry trailing comments
    on load-bearing fields, including inline LISTS:

        blocked_by: [pcore-03, pcore-09]  # pcore-09 added 2026-07-04: ...

    Left unstripped, that value no longer ends in `]`, so it parses as a scalar
    string and the baton reads as having a single blocker named
    "[pcore-03, pcore-09]" — an id nothing on disk carries. Both real edges
    vanish and the gate reports `unresolved` for an edge set that is perfectly
    well-formed.
    """
    value = value.strip()
    if value[:1] in ("'", '"'):
        # A quoted scalar ends at its own closing quote — and a comment AFTER
        # that quote is still a comment. Returning the whole line was safe only
        # while no scanned field carried the shape; two live records do
        # (`gate_dependency: ""  # both gates discharged 2026-08-29`), and for
        # those the scanner disagreed with the general parser, which is the one
        # thing this scanner may never do.
        quote = value[0]
        index = 1
        while index < len(value):
            char = value[index]
            if char == "\\" and quote == '"':
                index += 2
                continue
            if char == quote:
                if quote == "'" and value[index + 1:index + 2] == "'":
                    index += 2
                    continue
                break
            index += 1
        else:
            # Unterminated quote — not ours to repair, and not ours to truncate.
            return value
        rest = value[index + 1:].lstrip()
        return value[: index + 1] if rest.startswith("#") else value
    if value.startswith("#"):
        return ""
    return value.split(" #", 1)[0].rstrip()


def _unquote(value: str) -> str:
    """One YAML scalar: comment dropped, quotes removed and UNESCAPED, null → ''.

    Unescaping is not cosmetic. YAML escapes an apostrophe inside a single-quoted
    scalar by doubling it, and three live records carry exactly that in
    `execution_authorized_note` — a verbatim PM utterance is the field most likely
    to contain one. Returning the doubled form makes the scanner disagree with the
    general parser, and this module now WRITES that field, so a note round-tripping
    through it would gain an apostrophe on every pass.
    """
    value = _strip_comment(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == "'":
            return inner.replace("''", "'")
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return "" if value in _NULL_SCALARS else value


def _skip_leading_comments(fh) -> Optional[str]:
    """Return the first line that is not blank and not inside a leading HTML
    comment block, or None at EOF.

    Parity with `coordinator_core.dag._parse_frontmatter`, which skips the same
    prologue. Without this, a record whose frontmatter is preceded by a
    `<!-- ... -->` banner reads here as having no frontmatter at all — and a
    baton that reads as having no frontmatter is silently dropped from the
    candidate set, which is a gate failing open by omission.
    """
    for _ in range(_FRONTMATTER_MAX_LINES):
        line = fh.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("<!--"):
            return line
        while "-->" not in stripped:
            nxt = fh.readline()
            if not nxt:
                return None
            stripped = nxt.strip()
    return None


def _scan_fields(path: Path, wanted: frozenset) -> Dict[str, Any]:
    """The `wanted` frontmatter keys of `path`. `{}` on any failure or no block.

    Never raises. An unreadable record and one with no frontmatter both yield
    `{}`, and the CALLER decides what that means: a candidate scan drops it, a
    blocker resolution reports `unresolved`. That asymmetry is deliberate — see
    the module docstring's Negative-spec.
    """
    from coordinator_core.dag import _BLOCK_SCALAR_RE, _consume_block_scalar

    out: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            first = _skip_leading_comments(fh)
            if first is None or first.strip() != "---":
                return {}
            lines: List[str] = []
            for count, raw in enumerate(fh):
                if count > _FRONTMATTER_MAX_LINES:
                    break
                line = raw.rstrip("\n").rstrip("\r")
                if line.strip() == "---":
                    break
                lines.append(line)
    except OSError:
        return {}

    current_key: Optional[str] = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if line[:1] in (" ", "\t", "-"):
            # A block-list entry, or an indented continuation of one.
            if current_key is not None and stripped.startswith("- "):
                out.setdefault(current_key, []).append(_unquote(stripped[2:]))
            i += 1
            continue
        key, sep, value = line.partition(":")
        if not sep:
            current_key = None
            i += 1
            continue
        key = key.strip()
        if key not in wanted:
            if key in _INERT_ON_A_BATON:
                # Reserved out-key, never a record field: this scanner's
                # contract is that an undeclared key reads as ABSENT, and
                # an inert key must stay absent to every reader of `_fm`
                # while still being reportable by the gate.
                out.setdefault("_inert", []).append(key)
            current_key = None
            i += 1
            continue
        # Comment-strip BEFORE the shape decision: an inline list with a
        # trailing comment no longer ends in `]` and would be read as a
        # scalar. See `_strip_comment`.
        value = _strip_comment(value)
        if _BLOCK_SCALAR_RE.match(value):
            # A block-scalar INDICATOR (`|` literal or `>` folded, e.g. `>-`),
            # never a value in itself. Reading it as a plain scalar stored the
            # two-character indicator itself as the field's value — a held
            # baton's `plan_blitz_hold_reason: >-` reported literally as ">-"
            # rather than the folded text on the lines beneath it. Delegate to
            # the general parser's own consumer
            # (`coordinator_core.dag._consume_block_scalar`) so this scanner's
            # reading of a block scalar can never diverge from the oracle
            # `test_narrow_scan_agrees_with_the_general_parser` checks it
            # against.
            current_key = None
            out[key], i = _consume_block_scalar(lines, i + 1, 0)
            continue
        if value == "":
            # A bare `key:` — possibly with a trailing comment — opens a
            # block list; the entries follow on subsequent lines.
            current_key = key
            out.setdefault(key, [])
        elif value.startswith("[") and value.endswith("]"):
            current_key = None
            out[key] = [
                _unquote(item)
                for item in value[1:-1].split(",")
                if _unquote(item)
            ]
        else:
            current_key = None
            out[key] = _unquote(value)
        i += 1
    return out


def _read_baton_fields(path: Path) -> Dict[str, Any]:
    """Every baton field this module reads, narrow-scanned."""
    return _scan_fields(path, _BATON_FIELDS)


def _read_frontmatter_head(path: Path) -> Dict[str, Any]:
    """Full frontmatter via the general parser — the narrow scanner's oracle.

    Not on any hot path. Kept because the agreement test needs a second,
    independent reading of the same bytes, and because a caller debugging a
    disputed gate wants the whole record rather than the declared subset.
    """
    from coordinator_core.dag import _parse_frontmatter

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    parsed = _parse_frontmatter(text)
    return parsed if isinstance(parsed, dict) else {}


def _as_list(value: Any) -> List[str]:
    """Normalise a scalar-or-list frontmatter field to a list of non-empty strings.

    Several of the fields this module joins on are schema'd as
    `anyOf: [string, array, null]` (`plan_ids`, `sizing_objects`,
    `deliverable_ids`), and a hand-authored record routinely writes the scalar
    form where the plural is declared. Reading only the declared shape would
    silently drop the link and report the baton unplanned.
    """
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return []


def _resolve_sizing_citations(
    worktree_root: Path, citations: "List[str]", archive_index: "Dict[str, str]"
) -> "Tuple[List[str], List[str]]":
    """Split `citations` into (resolved, unresolved) against disk.

    `sized` used to be `bool(record["sizing_objects"])` -- frontmatter presence,
    never a disk read. A baton citing a sizing object that is not there therefore
    read `sized: true`, and plan-blitz's args contract says in as many words that
    a sized baton SKIPS the sizing scout. So the wave planned a baton whose size
    nobody had determined, and the blitz-em -- whose job is to interrogate the
    scout's reasoning -- had no reasoning to interrogate. Silent in both
    directions: the gate reported it sized and the wave reported it planned.

    Measured 2026-09-10, wave 0 of run 20260910T000000Z:
    `dlv-deliverable-id-as-a-machine-derived-chai-606ae0` cited
    `state/sizings/2026-08-21-deliverable-id-as-a-machine-derived-chain-identity.yaml`,
    which does not exist; the sizing had been ARCHIVED to
    `archive/sizings/2026-08/` and the citation was never repointed. Its own
    executor found it and said so.

    An archived sizing still counts as sized: the work was done and filed, and
    refusing it would re-scout a baton whose sizing exists. What does not count
    is a citation nothing on disk answers, in either tree.

    The archive index is built at most once per gate read, by the caller, and only
    when some citation missed the live tree -- the same laziness
    `scan_batons`'s archive leg takes, for the same reason.
    """
    resolved: "List[str]" = []
    unresolved: "List[str]" = []
    for citation in citations:
        if not isinstance(citation, str) or not citation.strip():
            continue
        rel = citation.strip()
        if (worktree_root / rel).is_file() or Path(rel).name in archive_index:
            resolved.append(rel)
        else:
            unresolved.append(rel)
    return resolved, unresolved


#: Lines read from a sizing object's head before `route:` is declared absent.
#: `route` is a REQUIRED top-level key of sizing-object.schema.json, so it is
#: near the top of every conforming document; this is a malformed-file cap, not
#: a budget.
_SIZING_MAX_LINES = 80


#: The `route` an XL sizing carries while its exit is still the PM's to pick.
_PM_DECISION_ROUTE = "pm-decision"

#: The only `xl_exit` that resolves `pm-decision` to plannable work. The other
#: three name a different job: `split` and `shape` send the baton back for
#: re-scoping, `roadmap` sends it to an initiative. Accepting one coherent
#: multi-session job is the exit that leaves a plan to write.
_XL_EXIT_RESOLVING_TO_PLAN = "accept_multi_session"


def _sizing_route(worktree_root: Path, resolved: Sequence[str]) -> Optional[str]:
    """The EFFECTIVE `route` of the first resolvable sizing object, or None.

    A bounded head-read, not a YAML parse, for the reason `_scan_fields` is one:
    this runs per candidate and the general parser costs an order of magnitude
    more for a single scalar. `_strip_comment` is reused because the scaffold
    ships `route:` with its whole enum as a trailing comment
    (`route: plan  # dispatch | spec-dispatch | ...`), and reading that comment
    as part of the value makes every sized baton's route unrecognisable.

    Effective, not literal, for `pm-decision` alone: that route means "the PM has
    not picked an XL exit yet", and `xl_exit` is where they record that they have.
    Reading the route literally re-asks a question the same file already answers,
    which is what example-market-data-repo measured on 2026-09-11 — a sizing resolved
    on 2026-08-05 (`xl_exit: accept_multi_session`, `pm_resolution` filled) still
    routing to pm-decision in every wave since, at the PM's expense.

    Only `accept_multi_session` resolves. The other exits name work that is not
    writing this plan, so they stay at the gate, and an unset `xl_exit` stays
    `pm-decision` because null never means accepted (the schema says so).

    None is the answer for an UNSIZED baton, and it is not a finding — the gate
    already reports `unsized` separately, and a scout is what fixes it.
    """
    for citation in resolved:
        path = worktree_root / citation
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                route: Optional[str] = None
                xl_exit: Optional[str] = None
                for count, raw in enumerate(fh):
                    if count > _SIZING_MAX_LINES:
                        break
                    line = raw.rstrip("\n").rstrip("\r")
                    if line[:1].isspace():
                        # A nested key of some other block, not a top-level scalar.
                        continue
                    key, sep, value = line.partition(":")
                    if not sep:
                        continue
                    key = key.strip()
                    if key == "route" and route is None:
                        route = _unquote(_strip_comment(value)) or None
                    elif key == "xl_exit" and xl_exit is None:
                        xl_exit = _unquote(_strip_comment(value)) or None
                    if route is not None and route != _PM_DECISION_ROUTE:
                        return route
                if route == _PM_DECISION_ROUTE and xl_exit == _XL_EXIT_RESOLVING_TO_PLAN:
                    return "plan"
                if route:
                    return route
        except OSError:
            continue
    return None


def _archived_sizing_index(worktree_root: Path) -> "Dict[str, str]":
    """`{basename: relpath}` for every archived sizing. Built lazily, once."""
    index: "Dict[str, str]" = {}
    target = worktree_root / "archive" / "sizings"
    if not target.is_dir():
        return index
    try:
        for path in target.rglob("*.yaml"):
            if path.is_file():
                index.setdefault(path.name, path.relative_to(worktree_root).as_posix())
    except OSError:
        return index
    return index


def archived_plan_path(worktree_root: Path, rel: str) -> Optional[Path]:
    """The `archive/specs/**` file a `docs/plans/<name>.md` path was archived to, or None.

    The archive move keeps the basename and adds a `YYYY-MM/` folder (older moves
    were flat), so the location is predicted, not indexed. Only a `docs/plans/`
    path is eligible; a name filed in several months resolves to the newest.
    """
    parts = rel.replace("\\", "/").split("/")
    if len(parts) < 3 or parts[0] != "docs" or parts[1] != "plans":
        return None
    name = parts[-1]
    base = worktree_root / "archive" / "specs"
    try:
        found = sorted(p for p in base.glob(f"*/{name}") if p.is_file())
        if not found and (base / name).is_file():
            found = [base / name]
    except OSError:
        return None
    return found[-1] if found else None


def _iter_record_paths(root: Path, subdir: Sequence[str], recursive: bool) -> List[Path]:
    """Sorted `*.md` paths under `root/<subdir>`; `[]` if the directory is absent.

    Uses `iterdir`/`rglob` rather than `glob("*.md")` at the top level for the
    reason `roadmap_dag._collect_stub_paths` records: `Path.glob`'s selector
    swallows a `PermissionError` while walking and yields an empty iterator, so
    an unreadable directory becomes indistinguishable from an empty one.
    """
    target = root.joinpath(*subdir)
    if not target.is_dir():
        return []
    try:
        if recursive:
            return sorted(p for p in target.rglob("*.md") if p.is_file())
        return sorted(p for p in target.iterdir() if p.is_file() and p.suffix == ".md")
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Plan index
# ---------------------------------------------------------------------------


class PlanIndex:
    """Every plan on disk, indexed by the four keys a baton can link one with.

    A plan reached by more than one key is the same object each time — the
    index stores paths, and `by_path` is the single store.
    """

    def __init__(self) -> None:
        self.by_path: Dict[str, Dict[str, Any]] = {}
        self.by_plan_id: Dict[str, List[str]] = {}
        self.by_deliverable_id: Dict[str, List[str]] = {}
        self.by_sizing_object: Dict[str, List[str]] = {}
        # Sizing keys cited by more than one baton; set by `roadmap.audit`.
        # A shared sizing object names the roadmap, not any one baton's plan.
        self.shared_sizing: Set[str] = set()
        # Set by `build_plan_index`; `archived()` builds `archive/specs/**` once,
        # on the first link that misses every live plan.
        self.worktree_root: Optional[Path] = None
        self._archived: Optional["PlanIndex"] = None

    def archived(self) -> "PlanIndex":
        """Plans under `archive/specs/**`, built lazily and once. Empty without a root."""
        if self._archived is None:
            idx = PlanIndex()
            root = self.worktree_root
            if root is not None:
                _index_plans_under(idx, root, ("archive", "specs"))
            idx.shared_sizing = self.shared_sizing
            self._archived = idx
        return self._archived

    def add(self, rel_path: str, fm: Dict[str, Any]) -> None:
        status = str(fm.get("status") or "").strip().lower()
        self.by_path[rel_path] = {
            "path": rel_path,
            "status": status,
            "title": fm.get("title"),
            "approved": status in PLAN_APPROVED_STATUSES,
            "coded": status in PLAN_CODED_STATUSES,
            # A plan's OWN `deliverable_id` claim, kept alongside its status so a
            # caller can ask "does THIS deliverable_id have a live plan besides
            # this superseded one" without a second frontmatter read — see
            # `PlanIndex.by_deliverable_id`.
            "deliverable_id": _as_list(fm.get("deliverable_id")),
        }
        for key in _as_list(fm.get("plan_id")):
            self.by_plan_id.setdefault(key, []).append(rel_path)
        for key in _as_list(fm.get("deliverable_id")):
            self.by_deliverable_id.setdefault(key, []).append(rel_path)
        for key in _as_list(fm.get("sizing_object")):
            self.by_sizing_object.setdefault(_sizing_key(key), []).append(rel_path)

    def get(self, rel_path: str) -> Optional[Dict[str, Any]]:
        return self.by_path.get(rel_path)


def _sizing_key(value: str) -> str:
    """Normalise a sizing-object reference to its bare id.

    A baton may cite `state/sizings/foo.yaml` where the plan cites `foo`, or
    the reverse. Joining on the raw strings misses every such pair, and the
    miss reads as "this baton has no plan" — the exact false negative that
    would send an already-planned baton back through a planning wave.
    """
    tail = value.replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in (".yaml", ".yml", ".md", ".json"):
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)]
            break
    return tail.strip()


def is_plan_record(fm: Dict[str, Any]) -> bool:
    """Whether a `docs/plans/` record is a PLAN rather than a sidecar filed beside one.

    `docs/plans/` holds three other record families under `<plan-slug>.<suffix>.md`
    names — staff-eng reviews, plan-coverage checks, docs checks. Both
    discriminators below are positive properties of the sidecars, not of the
    filename: a suffix convention is a naming habit and would silently stop
    working the day somebody files `2026-09-01-review-the-review-gate.md`, which
    is a plan.

      - `kind:` — every sidecar family sets one (`staff-eng-review`, and its
        siblings). `kind: plan` is the ONE admitted value: plan.schema.json
        declares no `kind`, but `coordinator/templates/plans/plan.md.tmpl`
        emits `kind: plan`, so 41 of 283 records in DoE's corpus carry it and
        every one of them is a plan. Reading a bare `kind:` as sidecar-ness
        indexed all 41 as sidecars, which is the second failure this
        docstring's closing paragraph names, fired silently and at scale.
      - `plan:` — a back-pointer AT the plan it reviews. A plan does not point
        at itself.

    Getting this wrong is not cosmetic in either direction. Admitting a sidecar
    lets its `status` answer "is this baton's plan approved?" — a review sidecar
    whose own status is unrelated to the plan's. Excluding a real plan reports
    an already-planned baton as unplanned and feeds it back into a planning
    wave that will write a second plan for work that has one.
    """
    return fm.get("kind") in (None, "", "plan") and not fm.get("plan")


def build_plan_index(worktree_root: Path) -> PlanIndex:
    """Index the PLANS under `docs/plans/**`. Never raises; unreadable files skipped."""
    index = PlanIndex()
    index.worktree_root = worktree_root
    _index_plans_under(index, worktree_root, ("docs", "plans"))
    return index


def _index_plans_under(index: PlanIndex, worktree_root: Path, subdir: Sequence[str]) -> None:
    for path in _iter_record_paths(worktree_root, subdir, recursive=True):
        fm = _scan_fields(path, _PLAN_FIELDS)
        if not fm or not is_plan_record(fm):
            continue
        try:
            rel = path.relative_to(worktree_root).as_posix()
        except ValueError:
            rel = path.as_posix()
        index.add(rel, fm)


# The order plan links are tried, and the `plan_link_basis` each reports. Fixed,
# strongest-provenance first: an explicit path beats an id, an id beats a shared
# sizing object. `deliverable_id` leads: `governing_plan` can name an executed
# PARENT plan of a different deliverable, so it is a fallback accepted only when
# that plan's deliverable does not contradict the baton's.
_PLAN_LINK_ORDER: Tuple[Tuple[str, str], ...] = (
    ("deliverable_id", "deliverable_id"),
    ("deliverable_ids", "deliverable_ids"),
    ("governing_plan", "governing_plan"),
    ("origin_plan_id", "origin_plan_id"),
    ("plan_ids", "plan_ids"),
    ("sizing_object", "sizing_object"),
    ("sizing_objects", "sizing_objects"),
)


def link_plans(fm: Dict[str, Any], plans: PlanIndex) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Live plans first; only when none link, the archived (`archive/specs/`) ones."""
    hits, basis = _link_plans_in(fm, plans)
    if hits or plans.worktree_root is None:
        return hits, basis
    return _link_plans_in(fm, plans.archived())


def _link_plans_in(fm: Dict[str, Any], plans: PlanIndex) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Plans linked to a baton, strongest link first, plus the basis that answered.

    Returns `([], None)` when nothing links. Where several plans link, ALL are
    returned (a fan-in baton legitimately carries more than one) and the caller
    reduces them — `_best_plan` takes the most advanced, because a baton with
    one approved plan and one draft has published its decisions.
    """
    baton_dids = set(_as_list(fm.get("deliverable_id"))) | set(_as_list(fm.get("deliverable_ids")))
    for field, basis in _PLAN_LINK_ORDER:
        raw = _as_list(fm.get(field))
        if not raw:
            continue
        hits: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for value in raw:
            if field == "governing_plan":
                candidates = [value.replace("\\", "/")]
            elif field in ("origin_plan_id", "plan_ids"):
                candidates = plans.by_plan_id.get(value, [])
            elif field in ("deliverable_id", "deliverable_ids"):
                candidates = plans.by_deliverable_id.get(value, [])
            else:
                if _sizing_key(value) in plans.shared_sizing:
                    continue
                candidates = plans.by_sizing_object.get(_sizing_key(value), [])
            for rel in candidates:
                record = plans.get(rel)
                if record is None or rel in seen:
                    continue
                if field == "governing_plan" and baton_dids:
                    plan_dids = set(record.get("deliverable_id") or [])
                    if plan_dids and not (plan_dids & baton_dids):
                        continue
                seen.add(rel)
                hits.append(record)
        if hits:
            return hits, basis
    return [], None


#: Link bases that cannot distinguish THIS baton's plan from a sibling's. A
#: sizing object is cited by every baton minted from one sizing — the normal
#: shape of a roadmap wave — so a hit set resolved on it is "the plans of every
#: baton in this sizing", not "this baton's plans".
_WEAK_PLAN_LINK_BASES = frozenset({"sizing_object", "sizing_objects"})


#: `plan.schema.json`'s `status` enum, in lifecycle-progression order (`closed`
#: is absent from the vendored schema until the re-vendor). Used
#: only to break a `(coded, approved)` tie between two plans — never as a
#: standalone ranking, since the tail (`closed_partial`/`deferred`/
#: `abandoned`/`superseded`) are terminal alternates, not "further along"
#: than `implemented`. An unrecognised or empty status sorts lowest.
_STATUS_LIFECYCLE_ORDER: Tuple[str, ...] = (
    "draft",
    "reviewed",
    "approved",
    "blocked",
    "executing",
    "landed",
    "implemented",
    "closed_partial",
    "deferred",
    "abandoned",
    "closed",
    "superseded",
)


def _status_rank(status: Optional[str]) -> int:
    try:
        return _STATUS_LIFECYCLE_ORDER.index(status or "")
    except ValueError:
        return -1


def _plan_rank_key(plan: Dict[str, Any]) -> Tuple[bool, bool, int]:
    """`_best_plan`'s reduction key: coded > approved > lifecycle progression."""
    return (bool(plan["coded"]), bool(plan["approved"]), _status_rank(plan.get("status")))


def _tied_top_hits(hits: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every hit sharing the maximum `_plan_rank_key`, or `[]` if `hits` is empty."""
    if not hits:
        return []
    best_key = max(_plan_rank_key(p) for p in hits)
    return [p for p in hits if _plan_rank_key(p) == best_key]


def _best_plan(
    hits: Sequence[Dict[str, Any]], basis: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The most advanced of several linked plans: coded > approved > lifecycle.

    The reduction presupposes the hit set is the baton's OWN plans — true for
    `governing_plan`/`origin_plan_id`/`plan_ids`/`deliverable_id`, where a
    fan-in baton legitimately carries several. Under a weak basis it is false:
    `max()` then reads "whichever sibling is furthest along" and hands the
    baton a disposition it did not earn. Reported against example-cockpit-repo's pvcs-01..04 (2026-09-12), where four batons
    sharing one sizing object let pvcs-04's approved plan open pvcs-04's own
    planning gate through pvcs-01, whose real plan was `reviewed`.

    So a multi-hit weak-basis set is DECLINED, not reduced: no linked plan, and
    `_plan_link_ambiguity` names the candidates. A gate that fails OPEN on a
    mis-resolution authorises exactly the work the edge existed to hold; an
    honest "I cannot tell which of these is yours" is a closed gate.

    A `(coded, approved)` tie is broken by `_STATUS_LIFECYCLE_ORDER`, never by
    the incidental order `hits` arrives in (which tracked alphabetical path
    order under the old `max()` call). If the lifecycle order also ties —
    both hits at the exact same `status` — that is an exact tie: declined the
    same way a weak-basis multi-hit is, and named by `_plan_link_ambiguity`.
    """
    if not hits:
        return None
    if len(hits) > 1 and basis in _WEAK_PLAN_LINK_BASES:
        return None
    top = _tied_top_hits(hits)
    if len(top) > 1:
        return None
    return top[0]


def _plan_link_ambiguity(
    hits: Sequence[Dict[str, Any]], basis: Optional[str]
) -> Optional[Dict[str, Any]]:
    """The candidate set `_best_plan` declined to reduce, or None.

    Present-as-null in the report for `unlinked_plan_claim`'s reason: a baton
    whose plan link resolved to several siblings' plans is indistinguishable in
    the output from one that has no plan at all, and the repair differs —
    write `governing_plan:` (or the plan's own `deliverable_id:`) onto the
    baton, rather than write a plan.

    Fires on two distinct declines: a weak-basis multi-hit (every candidate
    named), and a strong-basis exact tie (only the tied top named — a
    strong-basis set can legitimately carry a less-advanced plan alongside
    the tied pair, and that one is not ambiguous, just outranked).
    """
    if len(hits) < 2:
        return None
    if basis in _WEAK_PLAN_LINK_BASES:
        paths = sorted(p["path"] for p in hits)
        return {
            "basis": basis,
            "paths": paths,
            "repair": (
                f"{len(paths)} plans link on `{basis}` alone, which every baton in a "
                "sizing shares — cannot tell which is this baton's. Write "
                "`governing_plan:` (or the plan's own `deliverable_id:`) onto the baton"
            ),
        }
    top = _tied_top_hits(hits)
    if len(top) < 2:
        return None
    paths = sorted(p["path"] for p in top)
    return {
        "basis": basis,
        "paths": paths,
        "repair": (
            f"{len(paths)} plans link on `{basis}` and tie exactly on lifecycle "
            "progression — cannot tell which is more advanced. Advance or "
            "disambiguate one of them"
        ),
    }


# ---------------------------------------------------------------------------
# Baton index
# ---------------------------------------------------------------------------


def _baton_ids(fm: Dict[str, Any], path: Path) -> List[str]:
    """Every id a `blocked_by` edge may legitimately name this baton by.

    handoff.schema.json's `blocked_by` admits stub ids OR handoff ids ("the
    mutating path already matches blocker_id in (stub_id, handoff_id)"), so the
    resolver must index both, plus the filename stem an author reaches for when
    neither is at hand.
    """
    ids: List[str] = []
    for field in ("stub_id", "handoff_id", "deliverable_id"):
        ids.extend(_as_list(fm.get(field)))
    ids.append(path.stem)
    out: List[str] = []
    for value in ids:
        if value and value not in out:
            out.append(value)
    return out


def scan_batons(
    worktree_root: Path,
    include_archived: bool = True,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Read every LIVE baton, and only the archived ones a blocker edge names.

    Returns `(by_id, records)` — `by_id` maps every id a blocker edge may name a
    baton by (see `_baton_ids`) onto that baton's record; `records` is the
    de-duplicated list in scan order, live batons first.

    The archive is resolved LAZILY and only for ids the live scan left
    unresolved. This is a cost decision with a correctness consequence, so both
    halves are stated: claude-klabauter's archive holds ~3x more handoffs than its live
    tree, and parsing all of them cost more than the whole rest of this module
    put together, for an answer that on a healthy tree is "nothing was missing".
    The correctness half is that the archive is still scanned in FULL whenever
    anything IS missing — the shortcut is skipping a scan whose result is
    already known to be empty, never accepting an unresolved edge to save time.

    `include_archived=False` disables the second leg entirely, which is a
    testing and diagnostic affordance: it makes "how many edges depend on the
    archive?" an observable number rather than an assumption.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    records: List[Dict[str, Any]] = []

    for path in _iter_record_paths(worktree_root, ("state", "handoffs"), recursive=False):
        fm = _read_baton_fields(path)
        if not fm:
            continue
        record = _baton_record(_rel(path, worktree_root), path, fm, live=True)
        records.append(record)
        for ident in record["ids"]:
            # First writer wins: a live baton indexed before an archived
            # namesake keeps the id. An id collision across the live/archive
            # seam is the supersede-and-archive shape, and the LIVE record
            # is the one a downstream edge means.
            by_id.setdefault(ident, record)

    if not include_archived:
        return by_id, records

    pending = {
        blocker
        for record in records
        for blocker in record["blocked_by"]
        if blocker not in by_id
    }
    if pending:
        _resolve_archived(worktree_root, pending, by_id, records)

    return by_id, records


def _rel(path: Path, worktree_root: Path) -> str:
    try:
        return path.relative_to(worktree_root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve_archived(
    worktree_root: Path,
    pending: Set[str],
    by_id: Dict[str, Dict[str, Any]],
    records: List[Dict[str, Any]],
) -> None:
    """Probe `archive/handoffs/**` for the ids in `pending`, cheapest read first.

    Two-stage by design. Stage one narrow-scans the handful of fields that
    identify a record and say whether it is terminal — enough to dispose of the
    overwhelming majority of archived blockers, which shipped. Stage two re-reads
    ONLY a matched record that turned out non-terminal, in full, because such a
    record's disposition depends on its plan links and guessing at them is the
    one thing this module must not do.

    Scans the whole archive rather than stopping at the last match: an early
    exit would make the result depend on directory order, and a second id
    appearing later would resolve or not depending on how the first one sorted.
    """
    for path in _iter_record_paths(worktree_root, ("archive", "handoffs"), recursive=True):
        probe = _scan_fields(path, _ARCHIVE_PROBE_FIELDS)
        if not probe:
            continue
        ids = _baton_ids(probe, path)
        if not any(ident in pending for ident in ids):
            continue
        state = str(probe.get("deployment_state") or "").strip()
        fm = probe if state in BATON_CODED_STATES else _read_baton_fields(path)
        record = _baton_record(_rel(path, worktree_root), path, fm or probe, live=False)
        records.append(record)
        for ident in record["ids"]:
            by_id.setdefault(ident, record)


def _baton_record(rel: str, path: Path, fm: Dict[str, Any], live: bool) -> Dict[str, Any]:
    deployment_state = str(fm.get("deployment_state") or "").strip()
    status = str(fm.get("status") or "").strip()
    role = str(fm.get("baton_role") or "work").strip() or "work"
    return {
        "path": rel,
        "ids": _baton_ids(fm, path),
        "id": _baton_ids(fm, path)[0],
        "title": fm.get("title"),
        "kind": fm.get("kind"),
        "roadmap_id": fm.get("roadmap_id"),
        "stub_id": fm.get("stub_id"),
        "sprint": fm.get("sprint"),
        "wave": fm.get("wave"),
        "status": status,
        "deployment_state": deployment_state,
        "baton_role": role,
        "blocked_by": _as_list(fm.get("blocked_by")),
        "blocks": _as_list(fm.get("blocks")),
        "sizing_objects": _as_list(fm.get("sizing_object")) + _as_list(fm.get("sizing_objects")),
        "live": live,
        "candidate": (
            live
            and role in _CANDIDATE_ROLES
            and status != "claimed"
            and deployment_state in _CANDIDATE_STATES
        ),
        "_fm": fm,
    }
