"""coordinator_core.tests._baton_dag_oracle — independent pointer-resolution oracle.

Purpose: a from-scratch (does NOT import coordinator_core.dag) reverse-indexer over a
handoff corpus's raw frontmatter, used as a DIFFERENTIAL ORACLE against
coordinator_core.dag's engine implementation of the same pointer-resolution job (C6
pointer-normalization seam). Two independent implementations of "who points at this
baton" agreeing is much stronger evidence than either one's own unit tests.

Relocated (2026-07-26) from a throwaway location
(DoE-claude/state/audits/2026-07-26-baton-dag.py) into this permanent test surface —
the throwaway copy was slated to evaporate; this module IS the copy now, extended with
a `fields` parameter so a caller can score predecessor-family and origin_handoff-family
pointers separately (the throwaway script summed both into one children set, which
would have masked a kind-specific regression).

Normalizes the six observed value encodings (bare filename, state/-relative path,
archive/-relative path, quoted variants of either, the literal string "none", and
YAML null) AND the id-suffixed field convention (predecessor_id / origin_handoff_id
naming a handoff_id rather than a path) down to a single basename key — independently
of coordinator_core.dag.resolve_target / build_handoff_id_index, which do the same job
via a different code path (tiered filesystem resolution vs. this module's pure
string/dict normalization).

DR-242 (`docs/decisions/DR-242-successor-named-child-is-not-evidence-of-succ.md`,
Claude-klabauter) added `claimed_or_shipped` / `claimed_or_shipped_at_path`: a
succession sweep may not treat a successor-named child's existence, alone, as
evidence the named parent was superseded — it must independently verify the
parent was, at some point, claimed or reached a terminal `deployment_state`.
ADD-ONLY per the shared-surface note below: no existing function's signature or
semantics changed.

Negative-spec:
  - Does NOT touch disk beyond reading the corpus's own .md files — no filesystem
    existence probing, no git-history tier. A ref naming a file that doesn't exist on
    disk still normalizes to a basename here; coordinator_core.dag would (correctly)
    fail to resolve it. Callers comparing the two must account for this asymmetry
    (see test_c6_pointer_normalization.py's cross-check, which only asserts agreement
    on batons actually present in the corpus).
  - Does NOT apply the terminal-status / archive-residency exclusion that
    coordinator_core.archival.reverse_membership layers on top of
    coordinator_core.dag.referenced_by — this oracle is a pointer-resolution-only
    cross-check, not a full has-live-children behavioural clone.
  - `claimed_or_shipped` does NOT read any child-referencing field (predecessor,
    predecessor_id, origin_handoff, origin_handoff_id) — it inspects only the
    candidate parent's own frontmatter, by construction, so it cannot be used to
    launder a successor-named-child check into looking like this predicate.

Consumer note (CORRECTED 2026-07-29 — verify before trusting a claim like this).
This header previously read: "this module is SHARED with DoE-claude (relocated
there by DoE's 2026-07-26-push-side-write-discipline plan's C6, commit
`f2fefa23`) — any change here is producer-side with a live consumer." Both facts
were wrong, and checked at the point of use they fall over immediately:

  - `claimed_or_shipped` is defined NOWHERE in the DoE-claude clone; the only
    matches there are a `state/cockpit-emission.json` blob and two archived
    memos that merely mention the name. No code consumes this module outside
    claude-klabauter.
  - `f2fefa23` is a claude-klabauter commit ("C6: one pointer-normalization seam
    for the six on-disk encodings"), not a DoE one. The header credited claude-klabauter's
    own commit to DoE and asserted a relocation that never happened.

The cost was real, not theoretical: a 2026-07-29 fix to `_frontmatter` was
scoped as a cross-repo producer-side change and a notice memo drafted for a
consumer that does not exist. Re-verify before reinstating any shared-surface
claim here.

The REAL consumers are all in-repo, and they are what make this module's
correctness load-bearing rather than test-only. `claimed_or_shipped_at_path`
is imported by five PRODUCTION gates:
  - `coordinator_core/archive_stamp.py`
  - `coordinator_core/baton_assemble/apply.py`
  - `coordinator_core/ops/handoff_transition.py`
  - `coordinator_core/ops/handoff_archive_transition.py`
  - `coordinator/bin/handoff-archive-transition.py`
So despite living under a `tests`-named package, a bug here refuses real
operations on real handoffs — keep changes conservative and keep the
differential-oracle independence (see `_frontmatter`'s own docstring for why
that means NOT importing production's parser to fix a divergence from it).
`claimed_or_shipped` / `claimed_or_shipped_at_path` signatures stay additive-only.
"""
from __future__ import annotations

import glob
import os
import re
from collections import defaultdict
from typing import Dict, Iterable, List, Set, Tuple

from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

#: Default link fields: predecessor-family + origin_handoff-family, id-suffixed
#: aliases included. A caller wanting to score one family in isolation (to match
#: coordinator_core.dag's edge_kinds={'predecessor'} vs {'origin_handoff'} opt-in
#: split) passes an explicit `fields` tuple instead.
ALL_LINK_FIELDS: Tuple[str, ...] = (
    "predecessor", "predecessor_id",
    "origin_handoff", "origin_handoff_id",
)

PREDECESSOR_LINK_FIELDS: Tuple[str, ...] = ("predecessor", "predecessor_id")
ORIGIN_HANDOFF_LINK_FIELDS: Tuple[str, ...] = ("origin_handoff", "origin_handoff_id")


def _frontmatter(path: str) -> str:
    """Return a document's raw frontmatter text, or "" when it has none.

    Tolerates a LEADING PREAMBLE (blank lines and/or HTML comments) before
    the opening `---`, because handoffs carrying one are a supported shape:
    the frontmatter-preservation parity suite exercises it explicitly
    (`_PREAMBLE_FIXTURE` in frontmatter/tests/test_parity_handoff_ops.py),
    and production's own `frontmatter.primitives.split_frontmatter` parses
    it. This function previously required `text.startswith("---")` and
    returned "" otherwise, so a preamble-carrying handoff read as having NO
    frontmatter at all.

    That mattered beyond this module: `claimed_or_shipped_at_path` is
    imported by five PRODUCTION gates (archive_stamp, baton_assemble.apply,
    ops/handoff_transition, ops/handoff_archive_transition,
    bin/handoff-archive-transition.py). With empty frontmatter it can read
    no `status`/`deployment_state`, so DR-242's check concluded "never
    claimed or shipped" and REFUSED the operation on a handoff that had in
    fact been claimed — a false refusal with a message stating the opposite
    of the truth. Fail direction was safe (refuse, never permit), and no
    file in the live corpus carries a preamble today, so this was latent
    rather than firing.

    Independence is deliberate and preserved: this oracle does NOT import
    `split_frontmatter` (or anything from `coordinator_core.dag`) to do
    this. Its entire value is being a from-scratch second implementation —
    borrowing production's parser to fix a divergence FROM production would
    collapse the differential into a tautology. The preamble skip below is
    this module's own string handling.
    """
    text = open(path, encoding="utf-8", errors="replace").read()

    # Skip a leading preamble of blank lines and HTML comments to find the
    # opening delimiter. Anything else before it means "no frontmatter" —
    # deliberately narrow, so a stray `---` deeper in a body is never
    # mistaken for an opening fence.
    body = text
    while True:
        stripped = body.lstrip()
        if stripped.startswith("<!--"):
            end = stripped.find("-->")
            if end == -1:
                return ""
            body = stripped[end + 3:]
            continue
        body = stripped
        break

    if not body.startswith("---"):
        return ""
    return body.split("\n---", 1)[0][4:]


def _field(fm: str, key: str) -> str:
    m = re.search(r"^%s:[ \t]*(.*)$" % re.escape(key), fm, re.M)
    if not m:
        return ""
    v = m.group(1).strip().strip("\"'")
    return "" if v in ("none", "null", "~", "") else v


def collect_corpus_paths(root: str) -> List[str]:
    """Enumerate state/handoffs/*.md + archive/handoffs/**/*.md under root."""
    live = sorted(glob.glob(os.path.join(root, "state/handoffs/*.md")))
    archived = sorted(glob.glob(os.path.join(root, "archive/handoffs/**/*.md"), recursive=True))
    return live + archived


#: `deployment_state` values that are terminal — a baton that ever reached one of
#: these was, at some point, disposed of by design, not merely referenced by a
#: later file's naming convention.
#:
#: DERIVED from the SSOT, not hand-listed. This constant previously read
#: ("shipped", "continued", "closed") — handoff.schema.json's post-DR-084-P4
#: enum tail — and so answered False for `abandoned`, which
#: `lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT` has recognized since the
#: P4 narrow was reverted at 9d00b459 (2026-07-23), five days before this
#: constant was first written. The schema is the WRITE vocabulary: `abandoned`
#: can no longer be written. This predicate asks a READ question — was this
#: record EVER terminally disposed of — over live AND archived corpora,
#: including the consumer repos whose on-disk frontmatter still carries the old
#: token, which is exactly the axis the SSOT's dual-vocabulary read tolerance
#: exists for. Same bug shape, and same fix, as `superseded`'s restoration on
#: the `status` axis above: without it a legitimate supersede against an
#: abandoned parent is refused with a message asserting the opposite of the
#: truth.
#:
#: Importing the SSOT does NOT weaken this module's differential independence.
#: `lifecycle_constants` is a leaf constants module (it imports nothing from
#: coordinator_core), i.e. shared DATA — not a second copy of the predicate or
#: parser under test. The independence that matters here, and is preserved, is
#: that this module derives its own answer with its own frontmatter handling
#: (see `_frontmatter`'s docstring) rather than delegating to production's.
#: Retained here (unchanged) even though `claimed_or_shipped` moved out below —
#: this tuple is SSOT-derived data, not a copy of the predicate, and this
#: module's own differential-oracle role never depended on it.
_TERMINAL_DEPLOYMENT_STATES: Tuple[str, ...] = tuple(sorted(HANDOFF_TERMINAL_DEPLOYMENT))


# ---------------------------------------------------------------------------
# DR-242 predicate — RELOCATED (2026-08-06) to coordinator_core.archival
# ---------------------------------------------------------------------------
#
# `claimed_or_shipped` / `claimed_or_shipped_at_path` used to be DEFINED here
# and imported by six production modules despite living under a `tests`-named
# package (a bare install-manifest exclusion of `coordinator_core/tests/`
# would have broken all six at import time). They never participated in this
# module's actual job — the C6 pointer-resolution differential-oracle
# comparison against `coordinator_core.dag` (`build_children_index`, exercised
# by `test_c6_pointer_normalization.py`) — because `claimed_or_shipped` reads
# only a candidate parent's OWN frontmatter and never a child-referencing
# field, so it was never compared against a second implementation anywhere.
# This module's independence claim (see `_frontmatter`'s docstring) is about
# NOT delegating frontmatter PARSING to production's `split_frontmatter` for
# that pointer-resolution comparison — a claim this re-export does not touch,
# since `_frontmatter`/`_field` immediately above stay exactly as they were,
# still used by `build_children_index` below. `coordinator_core.archival`
# carries its OWN separate, deliberately-duplicated copy of `_frontmatter`/
# `_field` for `claimed_or_shipped_at_path`'s use (see that module's DR-242
# section header) — two independently-maintained copies serving two unrelated
# consumers, not drift.
#
# Re-exported here (not merely deleted) so `test_baton_dag_oracle_claimed_or_
# shipped.py`'s existing imports keep working unchanged.
from coordinator_core.archival import (  # noqa: E402  (re-export, not a relocation of use)
    claimed_or_shipped,
    claimed_or_shipped_at_path,
)


def build_children_index(
    root: str,
    fields: Iterable[str] = ALL_LINK_FIELDS,
) -> Tuple[List[str], Dict[str, Set[str]]]:
    """Return (live_paths, children) — children[basename] = set of referencing basenames.

    Args:
        root:   repo root containing state/handoffs/ and archive/handoffs/.
        fields: frontmatter keys to treat as parent-pointer edges. Values whose
                basename does not end in '.md' are treated as a handoff_id and
                resolved via a `handoff_id` -> basename index built from the same
                corpus scan (mirrors dag.build_handoff_id_index's id_index, but
                keyed to basename rather than absolute path — sufficient for a
                same-corpus differential check).

    Returns:
        live_paths: sorted state/handoffs/*.md paths (the "live" set a caller
                    iterates to build a report — mirrors the throwaway script's
                    original `main()` shape).
        children:   basename -> set of referencing basenames, for the given fields.
    """
    paths = collect_corpus_paths(root)
    fms = {p: _frontmatter(p) for p in paths}
    by_id = {
        _field(fm, "handoff_id"): os.path.basename(p)
        for p, fm in fms.items() if _field(fm, "handoff_id")
    }

    corpus_basenames = {os.path.basename(p) for p in paths}

    children: Dict[str, Set[str]] = defaultdict(set)
    for p, fm in fms.items():
        for key in fields:
            v = _field(fm, key)
            if not v:
                continue
            b = os.path.basename(v)
            if not b.endswith(".md"):
                b = by_id.get(b, b)
            # A pointer that explicitly names a NON-baton family resolves to that
            # file, not to a same-basename baton. Collapsing straight to the
            # basename made `predecessor: docs/problems/<name>.md` on a handoff
            # of the same `<name>.md` record the handoff as its own child — a
            # spurious self-edge, and the disagreement that surfaced this. The
            # module's existing negative-spec anticipated only the
            # nonexistent-target case, not same-basename-different-family.
            # Deliberately still basename-keyed for a pointer that names no
            # directory at all, or names a baton family: that bare-ref
            # resolution is what this oracle exists to check independently, and
            # `dag.resolve_target` reaches the same answer by a different route
            # (explicit path first, basename probing only as stale-path
            # recovery) rather than by sharing this code.
            pointer_dir = os.path.dirname(str(v).replace("\\", "/")).strip("/")
            if pointer_dir and b in corpus_basenames:
                names_baton_family = pointer_dir.startswith(
                    ("state/handoffs", "archive/handoffs")
                )
                if not names_baton_family:
                    continue
            children[b].add(os.path.basename(p))

    live_paths = sorted(glob.glob(os.path.join(root, "state/handoffs/*.md")))
    return live_paths, children
