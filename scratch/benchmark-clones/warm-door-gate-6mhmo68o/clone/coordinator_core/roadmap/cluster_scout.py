"""
coordinator_core.roadmap.cluster_scout — pure dispatch planner for
``dispatch-cluster-scout`` (coordinator:roadmap-planning step 2, Phase 1).

Purpose: given a set of reconciliation.md clusters, decide which get a web
scout dispatched, which get a PM-gated deep-research directive, and which are
skipped outright — WITHOUT ever computing the two inputs that drive that
decision. Both ``depth_disposition`` (the PM's step-1.5.0 disposition) and
``excluded_clusters`` (example-retrieval-repo's UE-probe resolution) arrive from the
caller; this module only consumes them (plan C9 body, "Two inputs the op
MUST NEVER COMPUTE").

Fragment-resolution mechanism (mirrors C8/``pm_gate_signal.py`` and
``review_mint/op.py::load_fragment``'s shape, but NOT its live sibling-clone
read): the DoE-authored cluster-scout-brief fragment is vendored as an
inlined copy under ``coordinator_core/roadmap/fragments/`` and loaded from
disk here — no cross-repo path is resolved at dispatch time. The vendored
copy is pinned byte-identical against
``coordinator/contract/cluster-scout-brief-fragment.json`` in DoE-claude by
``tests/test_cluster_scout_dispatch.py``; DoE's own
``coordinator/tests/test_supplied_fragments.py`` pins the same file on
their side, including the inlined preamble against its live snippet
(``coordinator/snippets/internet-research-scout.md``), so an edited snippet
cannot leave this repo dispatching a stale brief silently (AC17).

Composition rule (fragment ``brief.composition``, never varied per-cluster):
``preamble_text``, one blank line, then the cluster's own scope text taken
verbatim from reconciliation.md. No EM commentary, no per-cluster prompt
authoring (fragment ``never`` rule ``no-brief-authoring``).

Dispatch decision per cluster, in order:
  1. ``excluded_clusters`` membership -> skip, never substitute a web scout
     (fragment ``never`` rule ``no-substitute-scout``). The UE-internal-API
     probe that produces this set is example-retrieval-repo's capability, deliberately
     not this module's job (fragment ``excluded_clusters.note``).
  2. ``measurement_derived`` -> skip. A cluster whose research-corpus file is
     EM-authored ground truth (a prior spike, direct inspection, or probe
     output) is REPLACED by that file outright; scouting over it launders
     measurement into search results (fragment ``never`` rule
     ``no-scout-over-measurement``). This module never touches disk to
     determine that; the caller — the party that already knows whether it
     hand-authored the corpus file — states it per cluster.
  3. resolved ``depth_disposition`` for the cluster:
       - ``"deep-research"`` -> emit a directive, dispatch nothing. ``/research``
         is PM-gated and never EM-auto-invoked (fragment ``never`` rule
         ``no-research-pipeline``); self-escalating here would take a PM gate
         this module does not hold.
       - ``"solo-scout"`` -> compose the brief and mark for dispatch.
       - anything else -> a caller defect; raised loud, never guessed.

``depth_disposition`` may be a single value applied uniformly, or a mapping
of ``cluster_id -> value`` for the fragment's ``"mixed"`` case (per-cluster
disposition). A caller passing the literal string ``"mixed"`` as a uniform
value is itself a defect — ``"mixed"`` only ever describes the PM's
disposition object shape (a mapping), never a per-cluster value — and is
rejected loud rather than silently treated as ``"solo-scout"``.

``plan_cluster_dispatch`` enforces the fragment's ``max_concurrent`` cap
(``dispatch.max_concurrent``, 8) over the clusters actually marked for
dispatch in one call; a caller wanting more clusters scouted batches across
multiple calls rather than this module raising the cap.

Negative-spec:
  - Does NOT invoke ``/research``, the Deep Research pipeline, or spawn any
    agent — this module computes a plan, it dispatches nothing itself.
  - Does NOT resolve the UE-internal-API probe or any other exclusion
    logic — ``excluded_clusters`` is consumed verbatim, never recomputed.
  - Does NOT consult the PM's step-1.5.0 process to derive
    ``depth_disposition`` — consumed verbatim, never recomputed.
  - Does NOT vary the composed brief preamble per cluster, and does NOT
    accept EM commentary as part of the composition.
  - Does NOT read or write ``state/roadmap/<run-id>/research-corpus/`` —
    the fragment's ``output.path_template`` and ``min_bytes`` floor apply to
    the scout's own returned artifact, downstream of this module's plan.

Spec backlink: docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-spine-split.md § C9
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union

_FRAGMENT_RELPATH = Path(__file__).resolve().parent / "fragments" / "cluster-scout-brief-fragment.json"

_ACTION_DISPATCH = "dispatch"
_ACTION_DEEP_RESEARCH_DIRECTIVE = "deep-research-directive"
_ACTION_SKIP_EXCLUDED = "skip-excluded"
_ACTION_SKIP_MEASUREMENT = "skip-measurement"

_VALID_DISPOSITIONS = ("solo-scout", "deep-research")


class ClusterScoutFragmentError(ValueError):
    """Raised when the vendored fragment is missing or malformed."""


class ClusterDispatchInputError(ValueError):
    """Raised when a caller-supplied ``depth_disposition`` value is not one
    of the fragment's per-cluster dispositions (``"solo-scout"`` or
    ``"deep-research"``) -- e.g. the literal ``"mixed"`` passed as a uniform
    value, or an unrecognised string. Never guessed or coerced."""


class ClusterDispatchCapExceededError(ValueError):
    """Raised when the clusters marked for dispatch in one call exceed the
    fragment's ``dispatch.max_concurrent`` cap. The caller batches across
    multiple calls; this module never silently truncates the dispatch set."""


@dataclass(frozen=True)
class Cluster:
    """One reconciliation.md cluster carrying a KEEP or MERGE-target verdict.

    ``scope_text`` is the cluster's own scope text, taken verbatim from
    reconciliation.md (fragment ``brief.appended``). ``measurement_derived``
    is a caller-stated fact, never computed here (see module docstring).
    """

    id: str
    scope_text: str
    measurement_derived: bool = False


@dataclass(frozen=True)
class ClusterDispatchDecision:
    """One cluster's resolved outcome: what to do, why, and (only for
    ``dispatch``) the composed brief text."""

    cluster_id: str
    action: str
    reason: str
    brief: Optional[str] = None


def load_fragment(fragment_path: Optional[Path] = None) -> dict:
    """Load the vendored cluster-scout-brief fragment from disk and return
    the parsed dict.

    Reads the local, byte-identity-pinned copy under
    ``coordinator_core/roadmap/fragments/`` -- never a live cross-repo path
    resolved at op runtime (plan C9 body; module docstring). Raises
    ``ClusterScoutFragmentError`` on a missing or malformed file, never a
    silently empty/fabricated fragment.
    """
    path = fragment_path or _FRAGMENT_RELPATH
    if not path.is_file():
        raise ClusterScoutFragmentError(
            f"cluster-scout-brief fragment not found at {path}"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ClusterScoutFragmentError(
            f"cluster-scout-brief fragment at {path} is not valid JSON: {exc}"
        ) from exc


def _resolve_disposition(
    cluster_id: str,
    depth_disposition: Union[str, Mapping[str, str]],
) -> str:
    if isinstance(depth_disposition, Mapping):
        if cluster_id not in depth_disposition:
            raise ClusterDispatchInputError(
                f"depth_disposition is a mapping but names no entry for "
                f"cluster {cluster_id!r} -- the PM's step-1.5.0 disposition "
                "must resolve every cluster, never leave one unstated."
            )
        value = depth_disposition[cluster_id]
    else:
        value = depth_disposition
    if value not in _VALID_DISPOSITIONS:
        raise ClusterDispatchInputError(
            f"depth_disposition {value!r} for cluster {cluster_id!r} is not "
            f"one of {_VALID_DISPOSITIONS!r} -- the fragment's \"mixed\" "
            "value describes the disposition OBJECT shape (a per-cluster "
            "mapping), never a literal per-cluster value; this module never "
            "guesses at an unrecognised disposition."
        )
    return value


def compose_brief(preamble_text: str, scope_text: str) -> str:
    """Compose exactly ``preamble_text``, a blank line, then ``scope_text``.

    No EM commentary, no per-cluster prompt authoring (fragment ``brief``
    ``composition`` field; ``never`` rule ``no-brief-authoring``).
    """
    return f"{preamble_text}\n\n{scope_text}"


def plan_cluster_dispatch(
    clusters: Sequence[Cluster],
    depth_disposition: Union[str, Mapping[str, str]],
    excluded_clusters: Sequence[str],
    fragment: Optional[dict] = None,
) -> list[ClusterDispatchDecision]:
    """Compute one dispatch decision per cluster.

    ``depth_disposition`` and ``excluded_clusters`` are consumed verbatim as
    caller-supplied inputs -- this function computes neither (module
    docstring, "Two inputs the op MUST NEVER COMPUTE"; fragment
    ``excluded_clusters.never_substitute``).

    Raises ``ClusterDispatchCapExceededError`` if the clusters marked for
    dispatch in this single call exceed the fragment's
    ``dispatch.max_concurrent`` cap.
    """
    fragment = fragment if fragment is not None else load_fragment()
    preamble_text = fragment["brief"]["preamble_text"]
    max_concurrent = fragment["dispatch"]["max_concurrent"]
    excluded = set(excluded_clusters)

    decisions: list[ClusterDispatchDecision] = []
    dispatch_ids: list[str] = []

    for cluster in clusters:
        if cluster.id in excluded:
            decisions.append(
                ClusterDispatchDecision(
                    cluster_id=cluster.id,
                    action=_ACTION_SKIP_EXCLUDED,
                    reason=(
                        "cluster is in excluded_clusters -- never substitute "
                        "a web scout for an already-resolved exclusion "
                        "(no-substitute-scout)"
                    ),
                )
            )
            continue

        if cluster.measurement_derived:
            decisions.append(
                ClusterDispatchDecision(
                    cluster_id=cluster.id,
                    action=_ACTION_SKIP_MEASUREMENT,
                    reason=(
                        "cluster's research-corpus file is EM-authored "
                        "measurement ground truth -- it replaces the scout "
                        "outright (no-scout-over-measurement)"
                    ),
                )
            )
            continue

        disposition = _resolve_disposition(cluster.id, depth_disposition)

        if disposition == "deep-research":
            decisions.append(
                ClusterDispatchDecision(
                    cluster_id=cluster.id,
                    action=_ACTION_DEEP_RESEARCH_DIRECTIVE,
                    reason=(
                        "depth_disposition is deep-research -- /research is "
                        "PM-gated and never EM-auto-invoked; emit a "
                        "directive naming the cluster, dispatch nothing "
                        "(no-research-pipeline)"
                    ),
                )
            )
            continue

        brief = compose_brief(preamble_text, cluster.scope_text)
        decisions.append(
            ClusterDispatchDecision(
                cluster_id=cluster.id,
                action=_ACTION_DISPATCH,
                reason="depth_disposition is solo-scout",
                brief=brief,
            )
        )
        dispatch_ids.append(cluster.id)

    if len(dispatch_ids) > max_concurrent:
        raise ClusterDispatchCapExceededError(
            f"{len(dispatch_ids)} clusters marked for dispatch in one call "
            f"exceeds the fragment's max_concurrent cap of {max_concurrent} "
            f"({dispatch_ids!r}) -- batch across multiple calls instead."
        )

    return decisions
