"""
coordinator_core.session.session_facts — the session fact facade.

Roadmap `fact-layer-core`, stub `fl-core-01` chunk C2
(docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md). Builds against
docs/decisions/DR-319-session-fact-facade-shape-and-failure-posture.md, which is
BINDING here, not advisory — this module's return shape is DR-319's contract verbatim,
checked by this package's own contract test rather than by prose review.

SHAPE — a plain Python module, NOT a registered op (DR-319 § (a)). The engine is
warm-on-demand with cold spawn-per-call as the fallback (CLAUDE.md § What this repo
is); a registered op costs one engine invocation per fact read, a leaf-module import
costs zero. `fl-core-02` lifts four more facts onto whatever shape this decision picks,
multiplying the choice by five — a registered op multiplies a real per-call cost by
five before any measurement is taken, a leaf module multiplies zero by five. Same
precedent shape as `coordinator_core/composition_budget.py`: "dependency-free leaf"
there means free of the ENGINE (no dispatch, no op registration, no subprocess to check
a budget) — it does not mean this module may not import the producer whose fact it
fronts, which is the entire point of a facade. This module imports exactly one
intra-repo producer (`branch_resolution.session_commit_count_attributed`) and nothing
from `coordinator_core.ipc` or the op registry.

SERVES EXACTLY ONE FACT FOR NOW: the attributed session commit magnitude. The other
four DoE close-gate facts (`fl-core-02`) are explicitly NOT lifted here — anti-scope,
this chunk only.

RETURN-SHAPE CONTRACT (DR-319, binding — verbatim, not summarized):
    Discriminator key `"degraded"` (bool), present on every returned record.

    Computed (`degraded: False`):
        {"degraded": False, "value": <fact payload>, "source": "<producer string>",
         "collision": <bool | None>}
    `collision` is ALWAYS present on a computed record — `None` when the fact has no
    peer-mutable surface, a bool when it does. Never omitted, never coerced to `False`
    (R-11): an omitted key declares nothing, which is the absent-vs-clean conflation
    R-11 forbids; `None` is the fact's own declaration that no collision mode exists.

    Degraded (`degraded: True`):
        {"degraded": True, "evidence": "<free prose>", "source": "<producer string>"}
    No `"value"` key on a degraded record — a caller reading `.get("value")` on a
    degraded record gets `None` by absence, never a fabricated zero.

    NO verdict/recommendation/disposition/action key anywhere (AC8, DR-306's
    detect/decide split) — this facade emits evidence and collision state, never a
    decision replacing an EM judgment.

WHY `collision: None` FOR THIS FACT (R-11's peer-mutability test, not fact identity):
`session_commit_count_attributed` scopes strictly to commits carrying THIS session's own
`Session-Id: <sid>` trailer. A peer session cannot write a commit under a foreign sid —
sid assignment is per-session — so no peer mutation can change what this fact reports
for a given sid, even though the underlying `git log` surface (the whole branch history)
is itself shared. This is R-11's Fact-1 precedent (session-scoped, single-writer): the
fact has no peer-mutable surface, so it declares `collision: None` rather than an
always-`False` field that would read as "checked, clean."

KNOWN ATTRIBUTION LIMIT — DECLARED, NOT SILENTLY INHERITED (the Staff Engineer F5, R-11): the
backing producer's `--grep=Session-Id: <sid>` only sees commits carrying the trailer.
A commit made via plumbing (`git commit-tree`, bypassing the porcelain `commit` path)
loses the trailer, so a computed `value: 0` is reachable BOTH for a session that made
no commits AND for a session that committed real work entirely through plumbing. This
facade cannot recover that distinction — no signal exists to tell the two cases apart —
so it is declared here, at the served fact's own seam, rather than left implicit.  A
caller that needs to rule this out must independently check for un-trailered commits;
this fact's `value: 0` does not do so on its own.

ANTI-SCOPE (this chunk only, not a standing rule): does not build a second session
store (`coordinator_core/session/shape.py` already is one; this module reads through
existing producers, never a store of its own — AC10). Does not lift the other four
close-gate facts (`fl-core-02`). Does not converge the 21 hand-rolled dirty-probe
implementations (a sibling roadmap-seed's scope, not this one's).

Spec backlink: docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md § C2
Contract:      docs/decisions/DR-319-session-fact-facade-shape-and-failure-posture.md
Authoritative: state/roadmap/fact-layer-core/COORDINATOR-RESOLUTIONS.md (R-09, R-10, R-11)
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.ceremony.branch_resolution import (
    session_commit_count_attributed,
)

#: Literal, grep-able backing-source string (DR-319 § (b)) — a reader can grep this
#: straight back to the producing function, not a category token.
_SOURCE_SESSION_MAGNITUDE_ATTRIBUTED = (
    "coordinator_core/ops/ceremony/branch_resolution.py::session_commit_count_attributed"
)


def session_magnitude_attributed(worktree_root: Path, sid: str) -> dict:
    """Serve the attributed session commit magnitude: the count of commits on
    `worktree_root`'s branch carrying a `Session-Id: <sid>` trailer for THIS session,
    via `git log --grep=Session-Id: <sid>` — never `git rev-list --count
    head_at_start..HEAD` (AC4's trap: the positional count credits a session with its
    peers' commits on a shared branch, per R-09).

    Returns DR-319's return-shape contract (module docstring, "RETURN-SHAPE CONTRACT"):
      - computed:  {"degraded": False, "value": <int>, "source": <str>, "collision": None}
      - degraded:  {"degraded": True, "evidence": <str>, "source": <str>}

    `collision` is always `None` for this fact — see module docstring, "WHY
    `collision: None` FOR THIS FACT".

    KNOWN LIMIT: `value: 0` is reachable both for a genuinely zero-commit session and
    for a session that committed only via plumbing (loses the `Session-Id:` trailer) —
    see module docstring, "KNOWN ATTRIBUTION LIMIT". This facade does not (cannot)
    disambiguate the two; it serves the trailer-attributed count, declared as such.
    """
    record = session_commit_count_attributed(worktree_root, sid)
    if record["degraded"]:
        return {
            "degraded": True,
            "evidence": record["evidence"],
            "source": _SOURCE_SESSION_MAGNITUDE_ATTRIBUTED,
        }
    return {
        "degraded": False,
        "value": record["value"],
        "source": _SOURCE_SESSION_MAGNITUDE_ATTRIBUTED,
        "collision": None,
    }
