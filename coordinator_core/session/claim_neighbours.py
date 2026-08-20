"""
coordinator_core.session.claim_neighbours — join an artifact's file set to
its live peer claimants.

Plan: docs/plans/2026-08-16-trace-a-claim-back-to-its-session.md, chunk C1.
That plan's Problem section is the full warrant for this module: two EMs on
this box discover they are pointed at the same files by asking over a
broadcast, because the substrate that could answer the question directly
(`claim_index.lookup()`) was never joined to the artifact-claim layer
(`claims.py`). This module is that join, deliberately small and read-only —
it claims nothing, releases nothing, writes nothing to disk.

THE QUESTION: given an artifact path (a plan, sizing, or handoff), which
LIVE peer sessions are pointed at the same files right now?

TWO-TIER FILE-SET RESOLUTION, kept structurally distinguishable from a
neighbour-less answer (the defect this module exists to close — see
``ClaimNeighboursResult`` below):
  1. ``scope:`` in the artifact's own frontmatter (plans and sizings both
     carry one).
  2. A handoff carries no ``scope:`` by design — bridge via the handoff's
     ``deliverable_id`` to the PLAN carrying the same ``deliverable_id``,
     and use that plan's ``scope:``.
  3. Neither resolves -> ``ClaimNeighboursResult.status`` is
     ``UNRESOLVABLE``, never an empty ``neighbours`` list. Conflating "I
     could not tell" with "nobody is there" is exactly what made the
     pre-existing ``holder_evidence.scope_overlap`` stub (transcript-
     derived, ``None`` whenever either side was empty — which is EVERY
     handoff) answer nothing while looking wired. Rebuilding that
     conflation under a new name is the main trap in this module.

COST DISCIPLINE (AC6, live constraint not a preference): the path-set ->
claimant join routes through ``claim_index.lookup()`` ONLY, which is
O(pathspec) by that module's own design (see its docstring) — this module
adds no second O(claims) walk on that path. Peer liveness is O(1) per
candidate via ``liveness.session_live`` (never raw PID probing — the
sanctioned reader only). The one additional read this module performs —
resolving a peer session's OWN claimed artifact, for the report — is a
single pass over the (small, ~200-entry on this box) claim-CLASS
directories (``plan-claims``/``handoff-claims``/``memo-claims``), each
holding a couple of tiny marker files (``session_id``, no touched.txt
walk); this is the same cost CLASS as ``claim_index.rebuild()``'s own
walk over the substrate it depends on, not the excised O(all claims)
git-log mechanism (13.91s on this tree — see ``claim_index``'s module
docstring) this plan's Anti-scope forbids reintroducing. The
deliverable-bridge scan (tier 2 only, one ``docs/plans/*.md`` header read
per plan file, ~15ms measured on this tree's 444 plans) is likewise a
bounded, one-shot read pass, never re-derived per candidate.

ADVISORY / FAIL-SOFT CONTRACT (AC4): this module never raises to its
caller. A degradation anywhere past file-set resolution (a raising
``claim_index.lookup()``, an unreadable claim-class directory, a raising
``liveness.session_live`` for one candidate) is swallowed and narrows the
answer rather than propagating — see ``find_neighbours``'s per-step
try/except. This is what lets C2 fire this at ``claim_artifact`` success
with zero risk of turning an advisory signal into a failed claim; C2 owns
wrapping its OWN call site defensively too (belt-and-braces), but this
module's own contract does not depend on that outer wrap.
"""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter, read_fm_field
from coordinator_core.session import claim_index
from coordinator_core.session import core
from coordinator_core.session import liveness

#: Two-valued status vocabulary for ``ClaimNeighboursResult.status`` — the
#: structural distinction the plan's AC2/C1 require: "I could not resolve a
#: file set for this artifact" (UNRESOLVABLE) must never collapse into
#: "resolved, and nobody is there" (RESOLVED with an empty ``neighbours``
#: list). A caller branches on ``status`` first, never infers UNRESOLVABLE
#: from an empty list.
RESOLVED = "resolved"
UNRESOLVABLE = "unresolvable"

#: Repo-relative directories the artifact-claim layer's basenames resolve
#: against (mirrors ``claims.py``'s own path templates — ``claim_plan``'s
#: ``docs/plans/{slug}.md`` and the handoff-transition seam's
#: ``state/handoffs/{basename}``; verified against this repo's live
#: ``plan-claims``/``handoff-claims`` directory contents, see this module's
#: run-report sidecar).
_PLAN_DIR = "docs/plans"
_HANDOFF_DIR = "state/handoffs"

#: The two artifact-claim classes this module resolves a repo-relative path
#: for, mapping ``<sessions-dir>/<class>-claims/<basename>/`` entries back to
#: a real file. The two classes format their basename DIFFERENTLY (verified
#: against this repo's live claim-class directory contents, see this
#: module's run-report sidecar): ``plan-claims``' basename is a bare SLUG
#: with no extension (``claims.claim_plan``'s one-arg wrapper joins it as
#: ``docs/plans/{slug}.md``), while ``handoff-claims``' basename already
#: carries its own ``.md`` (the handoff-transition seam joins it as
#: ``state/handoffs/{basename}`` verbatim). ``memo`` is deliberately
#: excluded — this module's own file-set resolution never reads a memo (out
#: of this plan's scope), and memo basenames do not resolve to a
#: within-this-repo path the way plan/handoff basenames do.
_CLAIM_CLASS_TO_PATH_FMT = {
    "plan": lambda basename: f"{_PLAN_DIR}/{basename}.md",
    "handoff": lambda basename: f"{_HANDOFF_DIR}/{basename}",
}

#: Bare-local scope entry pattern shared (as a private re-derivation, not an
#: upward import — this module sits below ``pickup_assemble``, the same
#: layering reason ``holder_evidence._parse_scope_entry`` gives for its own
#: private copy) with ``pickup_assemble.holder_evidence``'s scope-entry
#: split: a `<repo-id>: <path>` entry is a CROSS-REPO scope reference and is
#: skipped here — ``claim_index`` only ever resolves THIS repo's claimants
#: (see this plan's Out of scope: cross-machine peers are not answered by
#: this module, and pretending otherwise would be a silent wrong answer).
_SCOPE_ENTRY_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.+)$")

#: Bytes read from the head of each ``docs/plans/*.md`` file while scanning
#: for a ``deliverable_id:`` match (tier 2, the handoff bridge). Generous
#: relative to this repo's plan frontmatter blocks (typically well under 1
#: KB) so the closing ``---`` is reliably inside the read window; a plan
#: whose frontmatter genuinely exceeds this is skipped by the scan (falls
#: through to the next candidate) rather than mis-resolved.
_PLAN_HEAD_SCAN_BYTES = 8192


@dataclasses.dataclass(frozen=True)
class Neighbour:
    """One live peer session pointed at the same files as the caller's
    artifact.

    ``artifact_path`` is the peer's OWN currently-claimed plan or handoff
    (repo-relative), resolved by scanning the claim-class directories for an
    entry whose recorded ``session_id`` matches this neighbour — ``None``
    when the peer holds no plan/handoff artifact claim at all (e.g. an
    ad-hoc session that only touched files without claiming a baton). A
    ``None`` here is not a failure; it is an honest "we can see the file
    overlap but not what artifact they're working from."
    """

    session_id: str
    overlapping_paths: List[str]
    artifact_path: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class PathSetNeighboursResult:
    """The bare-path-set join's full answer — the public seam a caller that
    already has a file set (no artifact to resolve one from) uses, and the
    same join ``find_neighbours`` itself is expressed in terms of once IT
    has resolved an artifact down to a file set. One code path, two entry
    points: this dataclass and ``find_neighbours_for_paths`` below are that
    path; ``ClaimNeighboursResult``/``find_neighbours`` stay the
    artifact-shaped entry point, unchanged.

    Distinguishes THREE per-path outcomes — the same structural discipline
    ``ClaimNeighboursResult`` applies at the whole-answer level, applied here
    per path, because a bare path set has no single ``scope:`` to be
    RESOLVED/UNRESOLVABLE about:
      - a path with >=1 live claimant folds into ``neighbours`` (grouped by
        owner session, per usual);
      - a path the claim-index lookup answered but found no LIVE claimant
        for lands in ``no_claimant_paths`` — "checked, found nobody", never
        conflated with the next case;
      - a path ``claim_index.lookup()`` could not answer at all
        (``claim_index.UNANSWERABLE``) lands in ``unanswerable_paths`` —
        "could not tell". Collapsing this into "no claimant" is exactly the
        defect this whole plan exists to remove (see module docstring); a
        refactor is exactly where it creeps back in, so a caller must keep
        branching on which of these three buckets a path landed in.

    ``lookup_raised`` is set only when ``claim_index.lookup()`` itself
    raised — in that case every requested path lands in
    ``unanswerable_paths`` and neither of the other two buckets is
    populated. ``abort_cause`` is set when the lookup call itself
    succeeded but flagged an incomplete walk (a subset of paths went
    ``UNANSWERABLE`` for a stated reason). The two are mutually exclusive
    by construction — a raising call never also returns a partial answer.
    """

    neighbours: List["Neighbour"]
    no_claimant_paths: List[str]
    unanswerable_paths: List[str]
    lookup_raised: Optional[str] = None
    abort_cause: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ClaimNeighboursResult:
    """The whole answer to "which live peers are pointed at this artifact's
    files" — status-first, per this module's docstring.

    ``status`` is ``RESOLVED`` or ``UNRESOLVABLE`` — check this BEFORE
    reading ``neighbours``. ``UNRESOLVABLE`` means the artifact's file set
    itself could not be determined (no ``scope:``, and either no
    ``deliverable_id`` to bridge from or no plan carries a matching one);
    ``neighbours`` is always ``[]`` on this status and must not be read as
    "no neighbours" — ``reason`` names why. ``RESOLVED`` means a file set
    (possibly empty, e.g. an artifact with an explicit ``scope: []``) was
    determined; ``neighbours`` is the genuine answer, and an empty list on
    this status IS "nobody is there" (or, when ``reason`` is set, "the
    lookup itself degraded" — see below).

    ``reason`` is set in two distinct situations, both non-``None`` only
    when something is worth telling a caller about:
      - ``status == UNRESOLVABLE``: always set, names why resolution failed.
      - ``status == RESOLVED`` with ``neighbours == []``: set ONLY when the
        empty list is itself a degradation (e.g. ``claim_index.lookup()``
        raised) rather than a genuine zero-overlap answer — AC4's fail-soft
        contract narrows the answer instead of raising, and this field is
        how that narrowing stays visible rather than silently indistinguishable
        from "checked, found nobody."
    """

    status: str
    file_set: List[str]
    neighbours: List[Neighbour]
    reason: Optional[str] = None

    @property
    def resolvable(self) -> bool:
        return self.status == RESOLVED


def _parse_scope_entry(entry: str) -> tuple:
    match = _SCOPE_ENTRY_RE.match(entry.strip())
    if match is None:
        return None, entry.strip()
    return match.group(1), match.group(2).strip()


def _local_paths_from_scope(scope: Iterable) -> List[str]:
    """Filter a parsed ``scope:`` YAML list down to bare-local (this-repo)
    path entries, in original order, de-duplicated. Non-string entries
    (malformed frontmatter) are skipped rather than raising — this module
    degrades, it does not gate."""
    paths: List[str] = []
    seen: set = set()
    for entry in scope:
        if not isinstance(entry, str):
            continue
        repo_id, path = _parse_scope_entry(entry)
        if repo_id is not None:
            continue
        path = path.rstrip("/")
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _read_frontmatter_dict(path: Path, max_bytes: Optional[int] = None) -> Optional[dict]:
    """Read ``path``, split its frontmatter, and full-YAML-parse it into a
    dict. Returns ``None`` on any I/O failure, missing/unparseable
    frontmatter, or a frontmatter block that does not parse to a mapping —
    never raises."""
    try:
        if max_bytes is None:
            text = path.read_text(encoding="utf-8")
        else:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read(max_bytes)
    except OSError:
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    try:
        fm = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def _resolve_repo_root(cwd: Optional[str]) -> str:
    """Best-effort repo root for joining ``_PLAN_DIR`` (tier 2 only — tier 1
    never needs this). Prefers ``core.git_root(cwd)``; falls back to ``cwd``
    itself when git resolution fails (e.g. a synthetic test fixture with no
    real ``.git``, the same convention ``claim_index``'s own tests use by
    passing a bare ``tmp_path``), then to ``"."``."""
    root = core.git_root(cwd)
    if root:
        return root
    return cwd or "."


def _bridge_deliverable_to_plan_scope(
    deliverable_id: str, *, cwd: Optional[str] = None
) -> Optional[List[str]]:
    """Tier 2: find the plan carrying ``deliverable_id`` and return its
    ``scope:``, filtered to bare-local paths. ``None`` when no such plan
    exists (the caller renders this as UNRESOLVABLE, never empty-neighbours)
    OR the matching plan itself carries no parseable ``scope:`` list."""
    repo_root = Path(_resolve_repo_root(cwd))
    plans_dir = repo_root / _PLAN_DIR
    try:
        entries = sorted(os.scandir(plans_dir), key=lambda e: e.name)
    except OSError:
        return None

    from coordinator_core.ops.deliverable_equivalence import canonicalize, load_equivalence_map

    equivalence_map = load_equivalence_map(repo_root)
    canonical_deliverable_id = canonicalize(deliverable_id, equivalence_map)

    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        try:
            with open(entry.path, "r", encoding="utf-8") as fh:
                head = fh.read(_PLAN_HEAD_SCAN_BYTES)
        except OSError:
            continue
        split = split_frontmatter(head)
        if split is None:
            continue
        candidate = read_fm_field(split.fm_text, "deliverable_id")
        if candidate is None:
            continue
        candidate = candidate.strip("'\"")
        # Join key canonicalized (C6b/AC11): a plan whose deliverable_id forked
        # from the handoff's still bridges, rather than rendering UNRESOLVABLE.
        if canonicalize(candidate, equivalence_map) != canonical_deliverable_id:
            continue
        try:
            fm = yaml.safe_load(split.fm_text)
        except yaml.YAMLError:
            return None
        if not isinstance(fm, dict):
            return None
        scope = fm.get("scope")
        if not isinstance(scope, list):
            return None
        return _local_paths_from_scope(scope)

    return None


def _resolve_file_set(
    artifact_path: str, *, cwd: Optional[str] = None
) -> tuple:
    """Returns ``(file_set, reason)``. ``file_set is None`` means
    UNRESOLVABLE (``reason`` names why); otherwise ``file_set`` is the
    (possibly empty) list of bare-local repo-relative paths and ``reason``
    is ``None``."""
    fm = _read_frontmatter_dict(Path(artifact_path))
    if fm is None:
        return None, f"could not read or parse frontmatter for {artifact_path}"

    scope = fm.get("scope")
    if isinstance(scope, list):
        # Tier 1 — present (possibly empty) scope: is authoritative, even
        # when empty. An artifact declaring `scope: []` HAS a resolved file
        # set; it is just empty — RESOLVED, not UNRESOLVABLE.
        return _local_paths_from_scope(scope), None

    deliverable_id = fm.get("deliverable_id")
    if not deliverable_id or not isinstance(deliverable_id, str):
        return None, (
            f"{artifact_path} carries neither a scope: list nor a "
            "deliverable_id to bridge from"
        )

    plan_scope = _bridge_deliverable_to_plan_scope(deliverable_id, cwd=cwd)
    if plan_scope is None:
        return None, (
            f"no plan carries deliverable_id {deliverable_id!r} to bridge "
            f"{artifact_path}'s file set from"
        )
    return plan_scope, None


def _sid_to_artifact_map(
    sessions_base: str, wanted_sids: FrozenSet[str]
) -> Dict[str, str]:
    """One pass over the claim-class directories (``plan-claims``,
    ``handoff-claims``), reading each entry's ``session_id`` marker file,
    building ``{sid: repo-relative artifact path}`` for every ``sid`` in
    ``wanted_sids``. Bounded by the claim-class corpus size (a couple
    hundred small marker-file reads on this box, ~4ms measured) — NOT a
    walk of ``touched.txt``/the git log, and never re-derived per
    candidate. Any per-entry I/O failure is skipped, not raised; a
    directory-enumeration failure for one class degrades that class only."""
    result: Dict[str, str] = {}
    if not wanted_sids:
        return result
    remaining = set(wanted_sids)
    for class_, path_fmt in _CLAIM_CLASS_TO_PATH_FMT.items():
        if not remaining:
            break
        claims_dir = Path(sessions_base) / f"{class_}-claims"
        try:
            entries = os.scandir(claims_dir)
        except OSError:
            continue
        try:
            for entry in entries:
                if not remaining:
                    break
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                try:
                    with open(
                        os.path.join(entry.path, "session_id"),
                        "r",
                        encoding="utf-8",
                    ) as fh:
                        held_sid = fh.readline().strip()
                except OSError:
                    continue
                if held_sid not in remaining:
                    continue
                result[held_sid] = path_fmt(entry.name)
                remaining.discard(held_sid)
        finally:
            try:
                entries.close()
            except AttributeError:
                pass
    return result


def find_neighbours_for_paths(
    file_set: Iterable[str],
    *,
    caller_sid: Optional[str] = None,
    cwd: Optional[str] = None,
    sessions_dir: Optional[str] = None,
) -> PathSetNeighboursResult:
    """THE bare-path-set join: look ``file_set`` up via
    ``claim_index.lookup()``, fold claimants to owner sessions, drop the
    caller's own session and every non-live claimant, and attach each
    surviving peer's own claimed artifact — the same steps
    ``find_neighbours`` performs after it resolves an artifact down to a
    file set, factored out here as the one code path both entry points
    share.

    ``caller_sid``: excluded from the neighbour set, never treated as a
    peer of itself. ``sessions_dir`` / ``cwd``: same override contract as
    ``claim_index.lookup()`` — when ``sessions_dir`` is omitted, the
    peer-artifact step falls back to ``core.sessions_dir(cwd)`` (advisory;
    a failure there just leaves peer artifacts unresolved, never raises).

    Never raises (AC4, same fail-soft contract as ``find_neighbours``): a
    raising ``claim_index.lookup()`` degrades to every path landing in
    ``unanswerable_paths`` with ``lookup_raised`` set; a raising
    ``liveness.session_live`` for one candidate skips that candidate only;
    a raising peer-artifact resolution degrades to an unresolved artifact
    map, never a raise.
    """
    paths = list(file_set)

    try:
        lookup = claim_index.lookup(paths, sessions_dir=sessions_dir, cwd=cwd)
    except Exception as exc:
        return PathSetNeighboursResult(
            neighbours=[],
            no_claimant_paths=[],
            unanswerable_paths=list(paths),
            lookup_raised=f"claim_index lookup failed: {exc!r}",
        )

    no_claimant_paths: List[str] = []
    unanswerable_paths: List[str] = []
    peer_paths: Dict[str, set] = {}
    abort_cause = None

    for path in paths:
        claimants = lookup.get(path, [])
        if claim_index.UNANSWERABLE in claimants:
            unanswerable_paths.append(path)
            cause = getattr(lookup, "abort_cause", None)
            if cause and abort_cause is None:
                abort_cause = cause
            continue
        found_live = False
        for sid in claimants:
            if caller_sid and sid == caller_sid:
                continue
            try:
                is_live = liveness.session_live(sid, cwd)
            except Exception:
                # AC4/AC5: a raising liveness check for one candidate must
                # not surface a dead-or-unknown claimant as a neighbour —
                # skip this candidate rather than propagate.
                continue
            if not is_live:
                continue
            found_live = True
            peer_paths.setdefault(sid, set()).add(path)
        if not found_live:
            no_claimant_paths.append(path)

    artifact_map: Dict[str, str] = {}
    if peer_paths:
        try:
            base = sessions_dir or core.sessions_dir(cwd)
        except Exception:
            base = ""
        if base:
            try:
                artifact_map = _sid_to_artifact_map(
                    base, frozenset(peer_paths.keys())
                )
            except Exception:
                artifact_map = {}

    neighbours = [
        Neighbour(
            session_id=sid,
            overlapping_paths=sorted(paths_),
            artifact_path=artifact_map.get(sid),
        )
        for sid, paths_ in sorted(peer_paths.items())
    ]

    return PathSetNeighboursResult(
        neighbours=neighbours,
        no_claimant_paths=no_claimant_paths,
        unanswerable_paths=unanswerable_paths,
        lookup_raised=None,
        abort_cause=abort_cause,
    )


def find_neighbours(
    artifact_path: str,
    *,
    caller_sid: Optional[str] = None,
    cwd: Optional[str] = None,
    sessions_dir: Optional[str] = None,
) -> ClaimNeighboursResult:
    """THE join: resolve ``artifact_path``'s file set, look it up via
    ``claim_index.lookup()``, drop the caller's own session and every
    non-live claimant, and return the surviving neighbours.

    ``caller_sid``: the calling session's own id, excluded from the
    neighbour set. Resolved via ``core.resolve_session_id(cwd)`` when
    omitted — pass it explicitly when the caller already has it, to avoid a
    redundant resolution.

    ``sessions_dir`` / ``cwd``: same override contract as
    ``claim_index.lookup()`` — pass ``sessions_dir`` explicitly in tests to
    point at a synthetic fixture instead of a real repo.

    Never raises (AC4). A failure resolving the file set yields
    ``UNRESOLVABLE`` (not a raise); a failure anywhere in the
    claim-lookup/liveness/peer-artifact steps AFTER a successful file-set
    resolution degrades to ``RESOLVED`` with a narrowed (possibly empty)
    ``neighbours`` list and a non-``None`` ``reason`` — see
    ``ClaimNeighboursResult``'s docstring.
    """
    file_set, reason = _resolve_file_set(artifact_path, cwd=cwd)
    if file_set is None:
        return ClaimNeighboursResult(
            status=UNRESOLVABLE, file_set=[], neighbours=[], reason=reason
        )
    if not file_set:
        return ClaimNeighboursResult(
            status=RESOLVED, file_set=[], neighbours=[], reason=None
        )

    my_sid = caller_sid
    if not my_sid:
        try:
            my_sid = core.resolve_session_id(cwd)
        except Exception:
            my_sid = ""

    joined = find_neighbours_for_paths(
        file_set, caller_sid=my_sid, cwd=cwd, sessions_dir=sessions_dir
    )

    # find_neighbours predates the three-bucket per-path split: it never
    # surfaced "no live claimant" or "per-path unanswerable" as a `reason` —
    # only a wholesale `claim_index.lookup()` raise did. Preserve that
    # contract exactly; `joined.abort_cause` (a partial-walk degradation)
    # stays internal to the bare-path-set seam, not this one.
    return ClaimNeighboursResult(
        status=RESOLVED,
        file_set=file_set,
        neighbours=joined.neighbours,
        reason=joined.lookup_raised,
    )
