"""
coordinator_core.roadmap.plan_gate — the two-gate roadmap resolver.

Purpose: answers two questions that the roadmap DAG has always conflated into one,
per baton, from disk alone:

  1. **May PLANNING start on this baton?** — its planning gate. Open when every
     blocker either landed (`coded`) or already carries a review-approved plan.
  2. **May EXECUTION start on this baton?** — its execution gate. Open only when
     every blocker actually landed.

A single `blocked_by` edge used to mean both, so a baton whose blocker had a
ratified plan and no code sat unpickupable — even though nothing about writing
ITS plan needed the blocker's code to exist, only the blocker's *decisions*. The
plan is the thing that carries the decisions. Holding planning until the code
lands serialises a pipeline that does not have to be serial, and the cost is
paid in the one resource a roadmap has least of: elapsed calendar.

The two gates also give the planning side a WAVE STRUCTURE the execution side
already had (`coordinator_core.roadmap.graph.topo_number`): wave 0 is every
baton plannable today, wave N is every baton that becomes plannable once wave
N-1's plans are approved. `plan_waves()` computes that, and it is what lets a
caller thwack a whole roadmap's planning through in ceil(depth) rounds instead
of one baton at a time.

Domain vocabulary: baton, blocker, planning gate, execution gate, planning wave,
blocker disposition, review-approved plan.

Consumed by ``coordinator_core.ops.roadmap_plan_gate`` (the ``roadmap.plan_gate``
op) — this module registers nothing and writes nothing.

Spec backlink: DoE-claude coordinator/skills/plan-blitz/SKILL.md § The two gates;
               coordinator/docs/wiki/coordinator-tripwires/a-planning-gate-is-not-an-execution-gate.md

Budget: pure reads, ZERO spawns, no git. One bounded head-read per candidate
record (``_read_frontmatter_head``, capped at ``_FRONTMATTER_READ_BYTES``), so
cost scales with record COUNT, not with corpus bytes. Measured on claude-klabauter's own
tree (295 live handoffs + 369 plans + 423 sizings) — see
``coordinator_core/roadmap/tests/test_plan_gate.py::test_whole_tree_scan_holds_the_brightline``.

Negative-spec:
  - Does NOT register an op, mutate frontmatter, or write anything. Every
    disposition it computes is derived; nothing is stamped. Stamping a
    `deployment_state` flip remains ``archive_stamp``'s job.
  - Does NOT shell out, and in particular does NOT ask git whether a blocker
    landed. `deployment_state` and the linked plan's `status` are the disk-truth
    this reads; a caller wanting SHA-level proof reads `shipped_in` itself.
  - Does NOT treat an unresolvable blocker id as satisfied. An id naming no
    record on disk yields `unresolved`, closes BOTH gates, and is named in
    `unresolved_blockers`. A gate that fails OPEN on a typo is worse than no
    gate: it authorises exactly the work the edge existed to hold.
  - Does NOT invent a plan link. The four link bases below are tried in a fixed
    order and the one that answered is reported in `plan_link_basis`; no
    title/slug fuzzy match, ever — a plan is linked by an id or a path or not
    at all.
  - Does NOT scan `archive/handoffs/**` for CANDIDATES (an archived baton is not
    plannable) but DOES resolve blockers into it — a blocker that shipped and was
    archived is the single most common satisfied blocker there is, and missing it
    would report every downstream baton as `unresolved`.
  - Does NOT import `coordinator_core.ops.*`. This module is a leaf under
    `coordinator_core.roadmap` for the same reason `spine.py` is: an `ops/`
    module importing it must not pull `ops/__init__`'s registration loop back
    through itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from coordinator_core.dag import _parse_frontmatter

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: A blocker's disposition — what this blocker currently permits downstream.
#: Ordered weakest-to-strongest; `_BLOCKER_RANK` below depends on this order.
BLOCKER_UNRESOLVED = "unresolved"
BLOCKER_UNPLANNED = "unplanned"
BLOCKER_PLAN_DRAFTED = "plan-drafted"
BLOCKER_PLAN_APPROVED = "plan-approved"
BLOCKER_CODED = "coded"

_BLOCKER_RANK = {
    BLOCKER_UNRESOLVED: 0,
    BLOCKER_UNPLANNED: 1,
    BLOCKER_PLAN_DRAFTED: 2,
    BLOCKER_PLAN_APPROVED: 3,
    BLOCKER_CODED: 4,
}

#: Dispositions that open a downstream baton's PLANNING gate. A blocker whose
#: plan cleared review has published its decisions; that is the whole input a
#: dependent plan needs from it.
_PLANNING_SATISFIED = frozenset({BLOCKER_PLAN_APPROVED, BLOCKER_CODED})

#: Dispositions that open a downstream baton's EXECUTION gate. Strictly `coded`
#: — a plan, however well reviewed, is not a merged symbol to call.
_EXECUTION_SATISFIED = frozenset({BLOCKER_CODED})

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
        "sizing_object", "sizing_objects",
        # The plan->execute seam. `handoff_phase: execution` means this baton has
        # been dispositioned and is waiting on /execute-plan, not on planning — read
        # here so a sweep does not re-plan work that already has its marching orders.
        "handoff_phase", "execution_authorized_by", "execution_authorized_at",
        "execution_authorized_sha", "execution_authorized_note",
        # NOT a link basis, and read anyway: `plan` is undeclared in
        # handoff.schema.json, so records carry it freely while `link_plans`
        # reads `governing_plan`. Two names for one edge, one written and the
        # other read. Scanned so `_unlinked_plan_claim` can NAME the record that
        # points at a real plan nothing resolved, instead of leaving it
        # indistinguishable from a baton that has no plan at all.
        "plan",
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
        return value
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
    out: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            first = _skip_leading_comments(fh)
            if first is None or first.strip() != "---":
                return {}
            current_key: Optional[str] = None
            for count, raw in enumerate(fh):
                if count > _FRONTMATTER_MAX_LINES:
                    break
                line = raw.rstrip("\n").rstrip("\r")
                if line.strip() == "---":
                    break
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if line[:1] in (" ", "\t", "-"):
                    # A block-list entry, or an indented continuation of one.
                    if current_key is not None and stripped.startswith("- "):
                        out.setdefault(current_key, []).append(_unquote(stripped[2:]))
                    continue
                key, sep, value = line.partition(":")
                if not sep:
                    current_key = None
                    continue
                key = key.strip()
                if key not in wanted:
                    current_key = None
                    continue
                # Comment-strip BEFORE the shape decision: an inline list with a
                # trailing comment no longer ends in `]` and would be read as a
                # scalar. See `_strip_comment`.
                value = _strip_comment(value)
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
    except OSError:
        return {}
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

    def add(self, rel_path: str, fm: Dict[str, Any]) -> None:
        status = str(fm.get("status") or "").strip().lower()
        self.by_path[rel_path] = {
            "path": rel_path,
            "status": status,
            "title": fm.get("title"),
            "approved": status in PLAN_APPROVED_STATUSES,
            "coded": status in PLAN_CODED_STATUSES,
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
    for path in _iter_record_paths(worktree_root, ("docs", "plans"), recursive=True):
        fm = _scan_fields(path, _PLAN_FIELDS)
        if not fm or not is_plan_record(fm):
            continue
        try:
            rel = path.relative_to(worktree_root).as_posix()
        except ValueError:
            rel = path.as_posix()
        index.add(rel, fm)
    return index


# The order plan links are tried, and the `plan_link_basis` each reports. Fixed,
# strongest-provenance first: an explicit path beats an id, an id beats a shared
# sizing object. `governing_plan` is stamped at mint against THIS baton, so it
# is the only link that cannot be a coincidence of two records citing a third.
_PLAN_LINK_ORDER: Tuple[Tuple[str, str], ...] = (
    ("governing_plan", "governing_plan"),
    ("origin_plan_id", "origin_plan_id"),
    ("plan_ids", "plan_ids"),
    ("deliverable_id", "deliverable_id"),
    ("deliverable_ids", "deliverable_ids"),
    ("sizing_object", "sizing_object"),
    ("sizing_objects", "sizing_objects"),
)


def link_plans(fm: Dict[str, Any], plans: PlanIndex) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Plans linked to a baton, strongest link first, plus the basis that answered.

    Returns `([], None)` when nothing links. Where several plans link, ALL are
    returned (a fan-in baton legitimately carries more than one) and the caller
    reduces them — `_best_plan` takes the most advanced, because a baton with
    one approved plan and one draft has published its decisions.
    """
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
                candidates = plans.by_sizing_object.get(_sizing_key(value), [])
            for rel in candidates:
                record = plans.get(rel)
                if record is not None and rel not in seen:
                    seen.add(rel)
                    hits.append(record)
        if hits:
            return hits, basis
    return [], None


#: Frontmatter keys that carry a plan PATH but are not link bases. `plan:` is the
#: one that matters: it is undeclared in handoff.schema.json and therefore
#: undeclared-but-tolerated, so records carry it freely while `link_plans` reads
#: `governing_plan`. Two names for one edge, one written and the other read.
_UNDECLARED_PLAN_PATH_KEYS: Tuple[str, ...] = ("plan",)


def _unlinked_plan_claim(fm: Dict[str, Any], worktree_root: Path) -> Optional[Dict[str, str]]:
    """A baton that NAMES a plan on disk which no link basis resolved.

    Reported, never linked. The distinction it restores is the one that costs
    sessions: `needs_plan: true` means "a blitz has work to do here", and a
    baton whose plan link merely failed to resolve is indistinguishable from one
    that genuinely has no plan. So the record is re-planned by every sweep
    forever, beside an approved plan for the same work, and any execution record
    attaches to nothing. Measured once in example-retrieval-repo against a PM-authorized
    plan; the cost is silent and unbounded in time.

    NOT promoted to a link basis, deliberately. `plan:` is undeclared, and a
    resolver that read it would bless an undeclared field as an edge and remove
    the pressure to correct the record. The repair is to write `governing_plan`
    (or the `deliverable_id` the plan already carries) onto the baton — which
    this report names, so nobody has to discover it from a wave that planned
    work twice.
    """
    for key in _UNDECLARED_PLAN_PATH_KEYS:
        value = fm.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        rel = value.strip().replace("\\", "/")
        if not rel.endswith(".md"):
            continue
        if not (worktree_root / rel).is_file():
            continue
        return {
            "field": key,
            "path": rel,
            "repair": (
                f"baton names {rel} in `{key}:`, which is not a link basis — "
                "write `governing_plan:` (or the plan's own `deliverable_id:`) onto the baton"
            ),
        }
    return None


def _best_plan(hits: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The most advanced of several linked plans: coded > approved > anything."""
    if not hits:
        return None
    return max(hits, key=lambda p: (bool(p["coded"]), bool(p["approved"])))


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


# ---------------------------------------------------------------------------
# Blocker disposition
# ---------------------------------------------------------------------------


def blocker_disposition(
    blocker_id: str,
    batons_by_id: Dict[str, Dict[str, Any]],
    plans: PlanIndex,
) -> Dict[str, Any]:
    """What `blocker_id` currently permits downstream.

    An id that resolves to no record is `unresolved` — never `unplanned`, and
    never quietly satisfied. The two are not the same finding: `unplanned` is a
    real baton awaiting work, `unresolved` is an edge pointing at nothing, which
    is an authoring defect somebody has to fix before the wave means anything.
    """
    record = batons_by_id.get(blocker_id)
    if record is None:
        return {
            "blocker": blocker_id,
            "disposition": BLOCKER_UNRESOLVED,
            "path": None,
            "plan": None,
            "reason": "no baton on disk carries this stub_id, handoff_id, or filename",
        }

    if record["deployment_state"] in BATON_CODED_STATES:
        return {
            "blocker": blocker_id,
            "disposition": BLOCKER_CODED,
            "path": record["path"],
            "plan": None,
            "reason": f"deployment_state: {record['deployment_state']}",
        }

    hits, basis = link_plans(record["_fm"], plans)
    plan = _best_plan(hits)
    if plan is None:
        return {
            "blocker": blocker_id,
            "disposition": BLOCKER_UNPLANNED,
            "path": record["path"],
            "plan": None,
            "reason": "no plan links to this baton",
        }

    if plan["coded"]:
        disposition = BLOCKER_CODED
    elif plan["approved"]:
        disposition = BLOCKER_PLAN_APPROVED
    else:
        disposition = BLOCKER_PLAN_DRAFTED

    return {
        "blocker": blocker_id,
        "disposition": disposition,
        "path": record["path"],
        "plan": {"path": plan["path"], "status": plan["status"], "link_basis": basis},
        "reason": f"plan {plan['path']} is status: {plan['status'] or '(unset)'}",
    }


def gates_for(
    record: Dict[str, Any],
    batons_by_id: Dict[str, Dict[str, Any]],
    plans: PlanIndex,
    memo: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Both gates for one baton, each with the blockers that hold it shut.

    A gate with an empty `blocking` list is open. `blocking` is ordered weakest
    blocker first, so a caller reporting only the head names the blocker that is
    furthest from clearing — the one worth acting on.

    `memo` caches dispositions across batons. A popular blocker is named by
    every one of its dependents, and its disposition is the same each time; the
    wave pass and the report pass then ask for the same set again. Without the
    cache this module recomputed the identical `link_plans` walk thousands of
    times, which measured as more than the entire disk scan.
    """
    cache = memo if memo is not None else {}
    dispositions = []
    for blocker in record["blocked_by"]:
        if blocker not in cache:
            cache[blocker] = blocker_disposition(blocker, batons_by_id, plans)
        dispositions.append(cache[blocker])
    dispositions = list(dispositions)
    dispositions.sort(key=lambda d: _BLOCKER_RANK[d["disposition"]])

    planning_blocking = [
        d for d in dispositions if d["disposition"] not in _PLANNING_SATISFIED
    ]
    execution_blocking = [
        d for d in dispositions if d["disposition"] not in _EXECUTION_SATISFIED
    ]
    return {
        "blockers": dispositions,
        "planning_gate": {"open": not planning_blocking, "blocking": planning_blocking},
        "execution_gate": {"open": not execution_blocking, "blocking": execution_blocking},
    }


# ---------------------------------------------------------------------------
# Planning waves
# ---------------------------------------------------------------------------


def plan_waves(
    records: Sequence[Dict[str, Any]],
    batons_by_id: Dict[str, Dict[str, Any]],
    plans: PlanIndex,
    gates: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Optional[int]], List[List[str]], List[List[str]]]:
    """Assign each candidate baton a planning wave.

    Wave 0 is every candidate whose planning gate is open TODAY. Wave N is every
    candidate whose only remaining planning blockers are candidates at waves
    < N — i.e. it becomes plannable once those waves' plans clear review. That
    is the whole scheduling claim this module makes, and it is why a roadmap of
    depth D plans in D rounds rather than D landings.

    A blocker that is neither satisfied nor itself a candidate (an `in_flight`
    baton, a `record`, an `unresolved` id) is UNSCHEDULABLE from here: nothing
    this pass does will clear it, so its dependents get wave `None` rather than
    a wave the caller would act on. Same for anything in a cycle.

    Returns `(wave_by_id, waves, cycles)` — `waves[i]` lists the ids at wave i;
    `cycles` lists each unresolved strongly-connected group, so a caller can
    name the cycle instead of reporting a silently empty tail.

    `gates` is the per-baton gate map the caller has already computed, keyed by
    baton id. Passing it is not an optimisation detail — it is what guarantees
    the wave a baton is assigned and the gate the report shows it were derived
    from ONE evaluation. Recomputing here could disagree with the report if the
    tree moved mid-pass, and a wave that disagrees with its own gate is worse
    than either answer alone.
    """
    # Wave membership is the set that NEEDS a plan, not the set that is eligible
    # for one. A baton whose plan already cleared review is not work this blitz
    # has to do: it stays a satisfied BLOCKER for its dependents (via
    # `_PLANNING_SATISFIED`) but is never itself scheduled. Including it put 18
    # already-planned batons into DoE-claude's wave 0 on the first live run —
    # a blitz firing that wave would have re-planned every one of them, which is
    # the exact failure `_sizing_key`'s docstring warns about, arriving by a
    # different route.
    candidates = {r["id"]: r for r in records if r["candidate"] and r["needs_plan"]}
    gates = gates if gates is not None else {}
    memo: Dict[str, Dict[str, Any]] = {}

    # Resolve each candidate's unsatisfied planning blockers ONCE, mapped onto
    # candidate ids where possible. A blocker resolving to a non-candidate is
    # kept as an unschedulable marker rather than dropped — dropping it would
    # promote its dependent into a wave whose gate never opens.
    pending: Dict[str, List[Optional[str]]] = {}
    for ident, record in candidates.items():
        gate = gates.get(ident) or gates_for(record, batons_by_id, plans, memo)
        holds: List[Optional[str]] = []
        for blocked in gate["planning_gate"]["blocking"]:
            blocker_record = batons_by_id.get(blocked["blocker"])
            if blocker_record is not None and blocker_record["id"] in candidates:
                holds.append(blocker_record["id"])
            else:
                holds.append(None)  # unschedulable in this pass
        pending[ident] = holds

    wave_by_id: Dict[str, Optional[int]] = {}
    unschedulable = {i for i, holds in pending.items() if None in holds}
    for ident in unschedulable:
        wave_by_id[ident] = None

    # Longest-path relaxation over the candidate-only subgraph, iterated to a
    # fixed point. Kahn's algorithm would do this in one sweep, but it also
    # needs an in-degree table keyed the same way twice; the corpus is small
    # (candidate counts are tens, not thousands) and a fixed-point loop bounded
    # by len(candidates) cannot outrun the DAG's own depth. Anything still
    # unassigned when the loop stops is in a cycle, which is the report we want.
    settled: Dict[str, int] = {}
    remaining = {i for i in candidates if i not in unschedulable}
    for _ in range(len(candidates) + 1):
        if not remaining:
            break
        progressed = False
        for ident in sorted(remaining):
            holds = [h for h in pending[ident] if h is not None]
            if any(h in unschedulable for h in holds):
                wave_by_id[ident] = None
                unschedulable.add(ident)
                remaining.discard(ident)
                progressed = True
                break
            if all(h in settled for h in holds):
                settled[ident] = (max((settled[h] for h in holds), default=-1)) + 1
                remaining.discard(ident)
                progressed = True
                break
        if not progressed:
            break

    cycles = _cycle_groups(remaining, pending)
    for ident in remaining:
        wave_by_id[ident] = None
    wave_by_id.update(settled)

    depth = max(settled.values(), default=-1) + 1
    waves: List[List[str]] = [[] for _ in range(depth)]
    for ident, wave in sorted(settled.items()):
        waves[wave].append(ident)

    return wave_by_id, waves, cycles


def _cycle_groups(
    remaining: Iterable[str], pending: Dict[str, List[Optional[str]]]
) -> List[List[str]]:
    """Group the unsettled ids into connected components of their hold edges.

    Not a strict SCC decomposition — an undirected component is enough to NAME
    the cycle for a human, and naming it is the whole job here. Reporting "a
    cycle exists" without its members leaves the author to find it by hand,
    which is the failure `roadmap.spine._assert_acyclic` already refuses to
    repeat.
    """
    remaining = set(remaining)
    groups: List[List[str]] = []
    seen: Set[str] = set()
    for start in sorted(remaining):
        if start in seen:
            continue
        stack = [start]
        component: Set[str] = set()
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            seen.add(node)
            for held in pending.get(node, []):
                if held in remaining and held not in component:
                    stack.append(held)
            for other in remaining:
                if node in [h for h in pending.get(other, []) if h] and other not in component:
                    stack.append(other)
        groups.append(sorted(component))
    return groups


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def assemble_plan_gate(
    worktree_root: Path,
    subject: Optional[str] = None,
    roadmap_id: Optional[str] = None,
    targets: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """The whole two-gate reading of a repo, ready to serialise.

    Three narrowing knobs, and the distinction between them is load-bearing:

      - `subject` narrows only the REPORT, to one baton. Gates and waves are
        still computed over everything, because a baton's gates are a function
        of the whole corpus.
      - `roadmap_id` narrows the CANDIDATE set to one roadmap.
      - `targets` narrows the candidate set to explicitly named batons — the
        skill's targeted mode, where an EM or the PM picks the work rather than
        sweeping. A non-target stays fully available as a BLOCKER; only its own
        eligibility is withdrawn.

    Under every one of them the SCAN stays whole-tree. Narrowing what you are
    asking about must never narrow what the answer is computed from — that is
    how a gate reports open because the thing holding it shut was filtered out
    of the question.

    The default candidate set is every baton that `needs_plan`: no linked plan,
    or one that has not cleared review. A baton whose plan is already approved
    is not work a blitz has to do, so it never enters a wave — it stays a
    satisfied blocker for its dependents.
    """
    plans = build_plan_index(worktree_root)
    batons_by_id, records = scan_batons(worktree_root)

    # `needs_plan` — does a blitz have work to do on this baton? Annotated here
    # rather than in `_baton_record` because it needs the plan index, which is
    # a property of the corpus, not of the record.
    for record in records:
        own = _best_plan(link_plans(record["_fm"], plans)[0])
        record["own_plan"] = own
        record["unlinked_plan_claim"] = (
            None if own else _unlinked_plan_claim(record["_fm"], worktree_root)
        )
        # Two ways a baton stops needing planning work, and the second is not
        # optional: an S-lane baton stamped `handoff_phase: execution` carries a
        # parked spec and a four-field authorization, so it is waiting on
        # /execute-plan. Its plan stays at `draft` by design — the S lane never
        # takes the approval — so keying `needs_plan` on plan status ALONE would
        # re-plan it on every later sweep, forever.
        authorized = str(record["_fm"].get("handoff_phase") or "").strip() == "execution"
        record["execution_authorized"] = authorized
        record["needs_plan"] = not (own and own["approved"]) and not authorized

    if roadmap_id is not None:
        for record in records:
            if record["candidate"] and record["roadmap_id"] != roadmap_id:
                record["candidate"] = False

    if targets:
        # Targeted mode: the caller named the batons. Everything else stops being
        # a candidate — which also stops it being a wave member, while leaving it
        # fully available as a BLOCKER. Narrowing the target set must never
        # narrow what the gates are computed against.
        wanted = set(targets)
        for record in records:
            if record["candidate"] and not (wanted & set(record["ids"]) or record["path"] in wanted):
                record["candidate"] = False

    # One gate evaluation per baton, shared by the wave pass and the report —
    # see plan_waves' `gates` parameter for why this is a correctness property
    # and not just a saved read.
    memo: Dict[str, Dict[str, Any]] = {}
    subjects = [
        record
        for record in records
        if record["candidate"]
        or (subject is not None and (subject in record["ids"] or subject == record["path"]))
    ]
    gate_by_id = {
        record["id"]: gates_for(record, batons_by_id, plans, memo) for record in subjects
    }

    wave_by_id, waves, cycles = plan_waves(records, batons_by_id, plans, gate_by_id)

    reported: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, str]] = []
    for record in subjects:
        if subject is not None and subject not in record["ids"] and subject != record["path"]:
            continue
        gate = gate_by_id[record["id"]]
        hits, basis = link_plans(record["_fm"], plans)
        own_plan = record["own_plan"]
        for blocked in gate["blockers"]:
            if blocked["disposition"] == BLOCKER_UNRESOLVED:
                unresolved.append({"baton": record["id"], "blocker": blocked["blocker"]})
        reported.append(
            {
                "id": record["id"],
                "path": record["path"],
                "title": record["title"],
                "kind": record["kind"],
                "roadmap_id": record["roadmap_id"],
                "stub_id": record["stub_id"],
                "deployment_state": record["deployment_state"],
                "status": record["status"],
                "blocked_by": record["blocked_by"],
                "blockers": gate["blockers"],
                "planning_gate": gate["planning_gate"],
                "execution_gate": gate["execution_gate"],
                "plan": (
                    {"path": own_plan["path"], "status": own_plan["status"], "link_basis": basis}
                    if own_plan
                    else None
                ),
                "needs_plan": record["needs_plan"],
                # Present-as-null, never absent: an omitted key and "no claim" would
                # be one value, and this field exists to make a silent case loud.
                "unlinked_plan_claim": record["unlinked_plan_claim"],
                "execution_authorized": record["execution_authorized"],
                "sized": bool(record["sizing_objects"]),
                "sizing_objects": record["sizing_objects"],
                "planning_wave": wave_by_id.get(record["id"]),
                "candidate": record["candidate"],
            }
        )

    # Counts are over the WHOLE candidate set, never over the filtered report — same rule
    # `waves` follows. A `subject` filter that narrowed these would put "candidates: 276"
    # beside "planning_open: 0" and mean "0 of the 1 shown", which reads as "none of the 276".
    # A count whose denominator moves with a display filter is a count nobody can act on.
    candidate_records = [r for r in subjects if r["candidate"]]
    counts = {
        "candidates": sum(1 for r in records if r["candidate"]),
        "planning_open": sum(
            1 for r in candidate_records if gate_by_id[r["id"]]["planning_gate"]["open"]
        ),
        "execution_open": sum(
            1 for r in candidate_records if gate_by_id[r["id"]]["execution_gate"]["open"]
        ),
        "unsized": sum(1 for r in candidate_records if not r["sizing_objects"]),
        "unplanned": sum(1 for r in candidate_records if r["own_plan"] is None),
        "needs_plan": sum(1 for r in candidate_records if r["needs_plan"]),
        # Scoped to batons that NEED a plan: one that already has an approved
        # plan has no wave because it needs none, which is not the same finding
        # as a baton this pass cannot schedule. Conflating them reported 18
        # healthy batons as unschedulable on DoE-claude's first live run.
        "unschedulable": sum(
            1
            for r in candidate_records
            if r["needs_plan"] and wave_by_id.get(r["id"]) is None
        ),
    }

    # `counts.unschedulable` says HOW MANY this pass cannot schedule and never
    # WHICH, so a driver reading it can neither act on them nor tell a held
    # baton from one that quietly vanished. Every other list this report returns
    # names its subjects; this one did not, and the gap is what let a baton the
    # PM had taken personally cost a scout slot in wave after wave -- the driver
    # had no way to see it was being held rather than skipped.
    #
    # Named here rather than left to the caller because the blocker mapping is
    # already computed above (`pending`) and re-deriving it caller-side is a
    # second answer to a settled question. Scoped identically to the count it
    # explains, so the two can never disagree.
    # The wave-assigner's own candidate set, rebuilt here: a blocker counts as
    # unmappable exactly when it is not a baton this pass could have scheduled,
    # which is the same test `_assign_waves` applies when it records a `None`
    # hold. Same predicate, so the explanation cannot drift from the exclusion.
    schedulable_ids = {r["id"] for r in candidate_records if r["needs_plan"]}
    unschedulable_rows = [
        {
            "id": r["id"],
            "title": r.get("title"),
            "path": r.get("path"),
            # The blockers holding it that this pass could not map onto a
            # candidate. A blocker naming no baton is exactly how a record says
            # "something outside this repo holds me" -- a PM decision, a
            # licensing call, an external dependency -- and it is the reason the
            # row is here rather than in a wave.
            # Two ways a blocker holds a row out of every wave, and the row is
            # useless to a driver unless BOTH are named. Directly: the blocker
            # maps to no schedulable baton, which is exactly how a record says
            # something outside this repo holds it -- a PM decision, a licensing
            # call, an external dependency. Transitively: the blocker IS a
            # schedulable candidate but is itself unscheduled this pass, so
            # waiting on it never ends either.
            #
            # Reporting only the direct case returned `held_by: []` for a row
            # that genuinely could not be scheduled, which is the same
            # count-with-no-subject defect this field exists to fix, one level
            # down. An empty list here must mean "nothing holds it", never
            # "something holds it and this report cannot say what".
            "held_by": [
                b["blocker"]
                for b in gate_by_id[r["id"]]["planning_gate"]["blocking"]
                if batons_by_id.get(b["blocker"]) is None
                or batons_by_id[b["blocker"]]["id"] not in schedulable_ids
                or wave_by_id.get(batons_by_id[b["blocker"]]["id"]) is None
            ],
        }
        for r in candidate_records
        if r["needs_plan"] and wave_by_id.get(r["id"]) is None
    ]

    return {
        "batons": reported,
        "waves": waves,
        "cycles": cycles,
        "unresolved_blockers": unresolved,
        "unschedulable": unschedulable_rows,
        "counts": counts,
        "scanned": {"batons": len(records), "plans": len(plans.by_path)},
    }
