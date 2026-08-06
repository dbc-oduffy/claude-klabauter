"""
coordinator_core.session.claimed_plan — resolve the plan THIS session has
claimed, so a downstream artifact (``/handoff``) can discover a parent
``deliverable_id`` from context instead of minting a fresh one.

Two-tier resolution, in order:

  (a) ``<git-common-dir>/coordinator-sessions/<sid>/session-shape.json``'s
      ``plan.path`` field — the fast path, written best-effort by
      ``claims.claim_plan``'s ``session_shape_set`` call at claim time.
  (b) Fallback: scan ``<git-common-dir>/coordinator-sessions/plan-claims/*/
      session_id`` for a directory whose ``session_id`` file matches THIS
      session. The claim directory NAME is the plan slug (``claims.claim_plan``
      rejects any path-shaped slug before it ever reaches ``claim_artifact``,
      so the directory name is always a bare slug), so the plan path is
      ``docs/plans/<dirname>.md``.

Tier (b) exists, and is load-bearing (not polish), because the tier (a) write
is DOCUMENTED best-effort: ``claim_plan`` wraps the ``session_shape_set`` call
in a bare ``try/except Exception: pass`` (non-fatal by design — a shape-write
failure must never break the plan claim itself) and skips the write entirely
whenever ``core.resolve_session_id`` returns falsy. The claim directory
itself — written unconditionally by ``claim_artifact("plan", ...)`` before
the shape write is even attempted — is therefore the more durable record.

Spec backlink: docs/plans/2026-08-01-deliverable-id-carry-onto-executing-handoff.md § C1a
(DR-207 DD#1: mint a stable ``deliverable_id`` once at the earliest artifact and carry it
verbatim; ``/handoff`` derives from the session's active/predecessor plan and mints only
when no parent id is discoverable from context — this module is that discovery step.)

Negative-spec:
    - Do NOT re-derive the ``plan-claims/`` path convention (a literal
      ``"plan-claims"`` join, or a hand-rolled ``<common_dir>/coordinator-sessions/
      plan-claims"``) — import and reuse
      ``coordinator_core.ops.fleet._common.plan_claim_dir`` instead. A second
      hand-rolled copy of that convention is exactly the class of bug
      ``plan_claim_dir``'s own docstring warns against.
    - Do NOT hoist that ``plan_claim_dir`` import to module scope. Anything
      under ``coordinator_core.ops`` triggers that package's eager import of
      all ~161 op modules, four of which import back into this one
      (``handoff_normalize``, ``handoff_author_fork``, ``handoff_correct_body``,
      ``queue_scaffold_baton``). At module scope the cycle leaves this module
      partially initialized mid-sweep and those four ops silently fail to
      register — the failure is a warning on stderr, not an exception, so it
      is easy to ship. Keep the import inside the tier-(b) branch.
    - Do NOT raise when no plan is claimed — that is an ordinary, legitimate
      state (a handoff genuinely unattached to a plan), not an error. Every
      failure edge (unresolvable session id, unresolvable git root, missing/
      malformed ``session-shape.json``, missing/unreadable claim dirs) falls
      through to returning ``None``.
"""

from __future__ import annotations

import json
from pathlib import Path
from coordinator_core.session import core, shape

# ``plan_claim_dir`` is imported lazily inside the tier-(b) branch below, NOT at
# module scope. ``coordinator_core.ops`` eagerly imports all ~161 op modules on
# first touch, four of which reach back here — a module-scope import of anything
# under ``coordinator_core.ops`` makes this module partially-initialized during
# that sweep and silently DEREGISTERS those four ops. See the negative-spec.


def resolve_claimed_plan_path(cwd: str | Path | None = None) -> str | None:
    """Repo-relative path of the plan this session has claimed, or ``None``.

    Resolves THIS session's id via ``core.resolve_session_id`` (the canonical
    4-tier chain — no second precedence ladder is added here), then tries
    tier (a) ``session-shape.json`` before falling back to tier (b) the
    ``plan-claims/`` directory scan (see module docstring for why tier (b)
    is necessary, not merely defensive).

    Returns ``None`` — never raises — whenever no plan is claimed, the
    session id is unresolvable, or the repo's git-common-dir is
    unresolvable (e.g. not inside a git repository).
    """
    cwd_str = str(cwd) if cwd is not None else None

    sid = core.resolve_session_id(cwd_str)
    if not sid:
        return None

    common = core.sessions_dir(cwd_str)
    if not common:
        return None
    common_dir = Path(common).parent

    # ---- Tier (a): session-shape.json plan.path ----
    raw = shape.session_shape_read(sid, cwd_str)
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        plan_field = parsed.get("plan")
        if isinstance(plan_field, dict):
            path = plan_field.get("path")
            if isinstance(path, str) and path:
                return path

    # ---- Tier (b): scan plan-claims/*/session_id for this session's claim ----
    # Imported HERE, not at module scope — see the note beside this module's
    # imports. ``plan_claim_dir`` returns ``<...>/plan-claims/<plan_path.stem>``;
    # a throwaway stem's ``.parent`` recovers the ``plan-claims/`` base dir
    # without re-deriving that convention by hand.
    from coordinator_core.ops.fleet._common import plan_claim_dir

    plan_claims_dir = plan_claim_dir(common_dir, Path("placeholder")).parent
    if not plan_claims_dir.is_dir():
        return None
    try:
        entries = sorted(plan_claims_dir.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            held_sid = (entry / "session_id").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if held_sid == sid:
            return f"docs/plans/{entry.name}.md"

    return None
