"""
coordinator_core.ops.plan_prep_gate — JSON-RPC "plan.prep_gate" operation.

Purpose: thin RPC wrapper over ``coordinator_core.roadmap.prep_gate``, the
mise-prep authoring bar. This module's only original code is param validation,
worktree-root derivation, and path containment; every predicate and verdict is
computed by the library module, which is pure.

The op exists so that "is this plan safe to fire unattended?" is a MEASUREMENT a
caller reads, not a judgment a caller makes. A driver that has to decide for
itself whether a plan declares enough to run hands-off will decide differently
each time, and the difference is invisible until a wave fires against a plan
whose scope nobody declared.

Wire params:
    plan (str, required) — the plan to gate, repo-relative or absolute. Must
                           resolve inside the main worktree.

Reply fields:
    {"plan": "docs/plans/....md", "verdict": "PREPPED"|"NOT-PREPPED"|"REFUSED",
     "withheld_rows": [row_id, ...], "classes": {SPINE|CENSUS|EXTERNAL_DEPS|
     PRIME_EXIT: {"status", "kind", "detail", "withheld"}}, "message": str,
     "stamp": {"state": "CERTIFIED"|"STALE"|"UNSTAMPED"|"MALFORMED", ...},
     "engine_build": {"engine_sha": str|None, "engine_dirty": None}}

    `engine_build` is the build this verdict was computed BY, not a property of
    the plan. It is reported on every verdict, PREPPED included, because a
    caller that only got it on a refusal would have nothing to compare a later
    refusal against. Without it a DEFECT means either "your plan under-declares"
    or "your engine predates the leg that exempts this" — opposite repairs, and
    a consumer holding a correct plan follows the refusal's prescribed repair
    into fabricating a declaration. Measured: 7 example-game-repo plans refused on
    `ide/` against a mirror predating the `created_roots` exemption, two of
    them already carrying the exact declaration the refusal text prescribed
    (example-game-workbench-repo-00, 2026-09-11). See
    `coordinator_core.engine_version.engine_build`.

    `classes` is the product, not `verdict`. The four classes are fixed in four
    different places — a spine row, a frontmatter key, an `external_gate`, a
    `prime_exit_criterion` — so a caller handed only a boolean has to re-derive
    where to send the author, and derives it differently each time. The corpus's
    own split (216 plans with no spine block against 5 whose spine the reader
    refuses) is legible only per class.

    `stamp` answers the SECOND question a fire-time caller has — is the
    certification on disk still about this document? — with the recomputed body
    sha, never field presence. Reported alongside the bar because both are pure
    reads of the same bytes, and a caller that had to make two calls to learn
    "the plan passes but its stamp is stale" would eventually make one.

Negative-spec:
  - Does NOT write, stamp, or mutate anything. A closed gate is REPORTED here;
    refusing on it is the caller's act, and stamping on it is
    ``plan.stamp_prepped``'s. Making this op the refusal would put an
    authorization decision behind a derived read.
  - Does NOT spawn, and does NOT INVOKE git. The body sha is
    ``primitives.canonical_body_sha``, pure Python and byte-identical to
    `git hash-object` over the plan body; shelling out for it would cost a
    process creation (25.3ms for `git --version` alone) to compute what sha1
    already answers. ``engine_build`` reads the engine's own ``.git`` refs as
    FILES for the same reason — stated as "does not invoke git" rather than
    "does not consult git", because that read is a consultation and the claim
    this op has to keep is the process count.
  - Does NOT sweep the corpus. One plan per call, and the bound is a
    measurement, not a preference — see ``roadmap.prep_gate.gate_plan`` for the
    numbers and for the census surface a corpus-wide question routes to.
  - Does NOT accept a caller-supplied root, and does NOT fall back to the process
    cwd when `repo_root` is absent. An op keyed "common_dir" is always handed
    one; deriving a root from cwd would make the answer depend on where the
    caller happened to stand.
  - Does NOT re-run a `census[].command`, and does NOT check that a plan's cited
    paths resolve. Both are named in the library module's own negative-spec.

Spec backlink: DoE-claude coordinator/docs/wiki/mise-prepped-authoring-bar.md
               .coordinator-local/memo-outbox/sent/mise-prepped-shape-ruling.md § 2
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.engine_version import engine_build
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.roadmap.prep_gate import gate_plan, read_stamp

# Generator-provenance: this op writes nothing.
GENERATES: list = []


def _resolve_plan(raw: str, worktree_root: Path) -> Path:
    """The plan path, resolved against the main worktree and contained by it.

    Containment mirrors ``queue_close``/``plan_status_transition``'s call site
    verbatim (``contained_path(candidate, [allowed_root])``) with the worktree
    root as the sole allowed root. A read op needs it for the same reason a write
    op does: a fat-fingered absolute path otherwise has this op read, and report
    on, a file outside every tree the caller owns.
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = worktree_root / candidate
    if contained_path(candidate, [worktree_root]) is None:
        raise ValueError(f"plan escapes the resolved worktree: {raw!r}")
    if not candidate.is_file():
        raise ValueError(f"no such plan: {raw!r}")
    return candidate


@register_op("plan.prep_gate")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.prep_gate" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.prep_gate requires a resolved repo_root")

    raw_plan = params.get("plan")
    if not isinstance(raw_plan, str) or not raw_plan.strip():
        raise ValueError("plan must be a non-empty string naming one plan file")

    worktree_root = Path(main_worktree_root(repo_root))
    plan_path = _resolve_plan(raw_plan.strip(), worktree_root)

    text = plan_path.read_text(encoding="utf-8", errors="replace")
    report = gate_plan(worktree_root, plan_path, text=text)
    try:
        rel = plan_path.relative_to(worktree_root).as_posix()
    except ValueError:  # pragma: no cover - contained_path already refused this
        rel = plan_path.as_posix()
    return {
        "plan": rel,
        "verdict": report["verdict"],
        "withheld_rows": report["withheld_rows"],
        "classes": report["classes"],
        "message": report["message"],
        "stamp": read_stamp(text),
        "engine_build": engine_build(),
    }
