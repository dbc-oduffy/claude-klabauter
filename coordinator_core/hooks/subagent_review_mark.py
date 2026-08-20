"""
coordinator_core.hooks.subagent_review_mark — SubagentStop mark-derivation op.

Purpose: derive a commit-ledger review mark (``commit_ledger.store.mark_reviewed``)
from a FINISHING REVIEWER's own ``reviewed_range`` findings, at ``SubagentStop``.
Modelled directly on ``hooks.subagent_zero_tool_use`` (same event, same
shim→engine→durable-write shape, same fail-quiet posture on a missing/unreadable
artifact) — that module's docstring is the sanctioned precedent for everything
this op does on this event: fail-quiet on an unreadable artifact, a plain append
as the product, and emitting nothing to stdout or stderr (on ``SubagentStop``,
stdout reaches the SUBAGENT's own context, not the EM's, and any stderr marks
the hook itself failed even at exit 0 — see that module's docstring for the
full rationale, not re-derived here).

ONE inheritance that is NOT total, unlike everything above: ``subagent_zero_tool_use``
writes its store INSIDE ``.git/`` and declares ``GENERATES: []`` for it. This op
writes into the commit ledger (``coordinator_core.commit_ledger.store``, also a
``.git/``-internal store) via a MARK, a different provenance class from a
bookkeeping counter — ``GENERATES: []`` is carried forward here on the SAME
reasoning (the ledger sits under ``<git_common_dir>/coordinator-sessions/
.commit-ledger/``, never a tracked artifact), not by unexamined copy.

Spec backlink: state/dispatch-briefs/2026-08-20-the-refusal-dies-and-the-mark-falls-out/C4.md

Steps (see the dispatch brief for the full staff-eng-review-cited rationale
behind each):
    1. Gate on the finishing agent being a REVIEWER — ``agent_type``'s bare
       form (stripped of any ``coordinator:``/``agent:`` namespace prefix,
       mirroring ``review_trail_write._bare_reviewer_hint``'s own stripping
       convention) must be a member of ``review_trail_write._DELEGATE_REVIEWERS``
       — the SAME closed reviewer vocabulary ``review_trail.write`` already
       enforces for the ``reviewer`` field, reused rather than re-derived.
    2. Resolve the run-report sidecar at
       ``state/subagent-share/<session_id>/<label>.<agent_id>.md`` — BY
       CONSTRUCTION (AC14), never a directory scan (see
       ``coordinator_core.subagent_sandbox.provision_report._provision``'s
       ``derived_key`` branch, ported there from
       ``coordinator_core.dispatch.provision``'s sibling branch by this same
       chunk — the naming fix this op depends on).
    3. Read ``reviewed_range`` (a YAML LIST) off the sidecar frontmatter.
    4. Resolve ``handoff_id`` via
       ``coordinator_core.commit_ledger.resolve_owner.resolve_owner_handoff_id``,
       passing the FINISHING agent's ``agent_id`` as ``committer_id`` — its
       own two-hop (agent → dispatching EM session → held baton) resolution,
       not re-derived here. ``None`` (standalone, no held baton) is fail-quiet,
       not a raise.
    5. Resolve ``reviewed_range`` to a commit set with ONE ``git rev-list``
       call carrying every declared range in a single argv (AC8 — a per-range
       call is a per-item git spawn the amplification gate
       (``coordinator_core.tests.test_no_unbatched_per_item_git_spawn``) is
       watching).
    6. CONTAINMENT BOUND (AC13): intersect that resolved set with the union of
       this session's own ``verdict: "pending"``, ``scope_kind: "diff"``
       review-trail records' ``sha_range`` — the ``--range``
       ``freeze-review-diff.py``'s ``_open_pending_trail_record`` persisted at
       dispatch time (kept alive by C3's out-of-scope carve-out for that
       function and the frozen ``.diff``/``.head.sha`` artifacts). A
       ``reviewed_range`` that exceeds every range this session actually froze
       is a defect in the self-report, not a valid superset claim.
    7. ``commit_ledger.store.mark_reviewed(handoff_id, shas, reviewer, cwd,
       agent_id=..., sidecar_path=...)`` over the bound-intersected set.

Fail-quiet (AC7) on every absent/unresolvable input: non-reviewer agent, no
resolvable sidecar, no/empty ``reviewed_range``, unresolvable ``handoff_id``
(including the standalone-no-baton case), an empty resolved/bound-intersected
commit set, or any git/file failure. Writes nothing, raises nothing — a hook
that raises on a subagent's exit is worse than a missing mark.

THE PM CONSTRAINT IS THE DESIGN, not a note on it: nothing here asks the agent
for anything. ``reviewed_range`` is already written as findings by the
reviewing subagent itself. That said, ``run-report.schema.json``'s
``reviewed_range`` field is ``WRITE-OWNER: written by the reviewing subagent
ONLY, never by the EM``, and no mechanism enforces that today (an EM-pre-created
sidecar under a real dispatch still passes) — this op inherits that known,
unenforced gap from DR-321 rather than closing it.

ZERO NEW SPAWNS FLEET-WIDE (AC8) is measured at the SHIM-side registration
site, not this op in isolation (see C5) — this op's own git spawns (one for
step 5, one per this-session pending-diff record for step 6's containment
bound, each bounded by this session's own freeze count, never per-commit) are
in scope for that measurement but are not themselves the AC8 violation class
the amplification gate polices (per-item-over-a-growing-set spawns are).

Negative-spec:
    Do NOT glob ``state/subagent-share/<session_id>/<label>-*.md`` to find a
    sidecar — the exact directory-scan shape ``review-trail.schema.json``'s
    own ``x-bump-note`` already condemned by name (K-005). The sidecar path
    is resolved by construction (step 2) or not at all.

    Do NOT mark for a pre-existing, nonce-named sidecar. Marks only work for
    agents dispatched AFTER the ``provision_report.py`` naming fix in this
    same chunk lands — every pre-existing nonce-named sidecar stays
    permanently unresolvable. Consistent with the no-backfill rule; not a
    defect to "fix" by adding a scan.

    Do NOT batch the containment-bound ranges (step 6) into the SAME
    ``git rev-list`` call as ``reviewed_range`` (step 5), and do NOT batch
    multiple containment ranges into one call either: ``git rev-list``'s
    exclusions are GLOBAL across the whole argv, not per-argument-pair — two
    adjacent ranges batched together can silently narrow the resulting set
    (the same caveat ``review_trail_write.py``'s own per-range git spawns
    document). Each containment range gets its own call.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional

import yaml

from coordinator_core.commit_ledger import store as ledger_store
from coordinator_core.commit_ledger.resolve_owner import resolve_owner_handoff_id
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op
from coordinator_core.subagent_sandbox.provision_report import _sanitize_segment

#: Written into .git/coordinator-sessions/.commit-ledger/<handoff_id>.jsonl —
#: inside .git/, never a tracked artifact. Same reasoning as
#: subagent_zero_tool_use's GENERATES: [] (see module docstring above), not
#: an unexamined carry-over.
GENERATES: list = []


def _bare_type(value: str) -> str:
    """Strip a ``coordinator:``/``agent:``-shaped namespace prefix, mirroring
    ``review_trail_write._bare_reviewer_hint``'s own stripping convention —
    the reviewer vocabulary is spelled bare (``code-reviewer``, ``staff-eng``,
    ...), never namespaced."""
    _prefix, sep, bare = value.rpartition(":")
    return bare if sep else value


def _is_reviewer(agent_type: str) -> bool:
    """True iff ``agent_type``'s bare form is a DELEGATE reviewer per
    ``review_trail_write``'s own closed vocabulary — reused, not re-derived
    (see module docstring step 1). Local import: avoids a module-init-order
    cycle with ``coordinator_core.ops`` (which imports ``coordinator_core.hooks``
    as one of its own eager modules), mirroring
    ``commit_ledger.resolve_owner``'s existing local-import convention for
    the identical reason.
    """
    if not agent_type:
        return False
    from coordinator_core.ops.review_trail_write import _DELEGATE_REVIEWERS

    return _bare_type(agent_type) in _DELEGATE_REVIEWERS


def _resolve_sidecar_path(session_id: str, agent_type: str, agent_id: str) -> Optional[Path]:
    """``state/subagent-share/<session_id>/<label>.<agent_id>.md`` — by
    construction (AC14), never a directory scan. Mirrors
    ``provision_report._provision``'s ``derived_key`` branch exactly (see
    that module, this same chunk): a malformed ``agent_id`` (does not survive
    ``_sanitize_segment`` unchanged) resolves to no sidecar rather than
    guessing.
    """
    sanitized_session_id = _sanitize_segment(session_id)
    sanitized_label = _sanitize_segment(agent_type)
    if sanitized_session_id is None or sanitized_label is None:
        return None
    if _sanitize_segment(agent_id) != agent_id:
        return None
    derived_key = f"{sanitized_label}.{agent_id}"
    sanitized_key = _sanitize_segment(derived_key)
    if sanitized_key is None:
        return None
    return Path("state") / "subagent-share" / sanitized_session_id / f"{sanitized_key}.md"


def _read_reviewed_range(sidecar_abs_path: Path) -> Optional[List[str]]:
    """Read the ``reviewed_range`` YAML-list frontmatter field, or ``None``
    on an unreadable file, absent frontmatter fence, absent field, or a field
    that is not a list of non-empty strings."""
    try:
        raw = sidecar_abs_path.read_text(encoding="utf-8")
    except OSError:
        return None
    split = split_frontmatter(raw.replace("\r\n", "\n"))
    if split is None:
        return None
    try:
        fm = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    values = fm.get("reviewed_range")
    if not isinstance(values, list) or not values:
        return None
    ranges = [v for v in values if isinstance(v, str) and v]
    return ranges or None


def _git_rev_list(args: List[str], cwd: str) -> Optional[List[str]]:
    """``git rev-list <args>`` -> resolved SHA list, or ``None`` on any
    resolution failure. Reuses ``review_trail_write._git_runner`` for the
    Windows-safe (CREATE_NO_WINDOW + stdin=DEVNULL) subprocess invocation
    shape rather than re-deriving it — local import for the same
    module-init-order reason as ``_is_reviewer``.
    """
    from coordinator_core.ops.review_trail_write import _git_runner

    rc, out, _err = _git_runner(["git", "rev-list", *args], cwd)
    if rc != 0:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def _own_pending_diff_ranges(session_id: str, worktree: Path) -> List[str]:
    """Every ``verdict: "pending"``, ``scope_kind: "diff"`` review-trail
    record's ``sha_range`` this session itself wrote — the AC13 containment
    bound's source (see module docstring step 6). ``session_id`` in a
    review-trail record is the TRUNCATED 8-char form
    (``review_trail_write``'s own filename-derivation convention); compared
    here against the same truncation of the SubagentStop payload's
    ``session_id``. Fails closed to ``[]`` on any read error — an
    unenumerable bound admits nothing rather than everything.
    """
    trail_dir = worktree / "state" / "review-trail"
    try:
        candidates = sorted(trail_dir.glob("*.json"))
    except OSError:
        return []
    import json as _json

    short_sid = session_id[:8]
    ranges: List[str] = []
    for candidate in candidates:
        try:
            data = _json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("session_id") != short_sid:
            continue
        if data.get("verdict") != "pending" or data.get("scope_kind") != "diff":
            continue
        sha_range = data.get("sha_range")
        if isinstance(sha_range, str) and sha_range:
            ranges.append(sha_range)
    return ranges


def _resolve_and_mark_sync(
    *, session_id: str, agent_id: str, agent_type: str, cwd: str, repo_root: str
) -> None:
    """Blocking body: sidecar read, git resolution, ledger append. Called
    exclusively via ``asyncio.to_thread`` — must not be awaited directly.
    Every branch below returns silently on failure (AC7 fail-quiet); nothing
    here ever raises out to the caller.
    """
    worktree = Path(repo_root)

    sidecar_rel = _resolve_sidecar_path(session_id, agent_type, agent_id)
    if sidecar_rel is None:
        return
    sidecar_abs = worktree / sidecar_rel
    reviewed_range = _read_reviewed_range(sidecar_abs)
    if not reviewed_range:
        return

    try:
        handoff_id, _degraded = resolve_owner_handoff_id(agent_id, worktree)
    except ValueError:
        return
    if not handoff_id:
        # Standalone (no held baton) is a legitimate, fail-quiet outcome
        # (AC7) — never a raise, never a mark.
        return

    reviewed_shas = _git_rev_list(list(reviewed_range), str(worktree))
    if not reviewed_shas:
        return

    bound_shas: set = set()
    for rng in _own_pending_diff_ranges(session_id, worktree):
        resolved = _git_rev_list([rng], str(worktree))
        if resolved:
            bound_shas.update(resolved)
    if not bound_shas:
        return

    admitted = [sha for sha in reviewed_shas if sha in bound_shas]
    if not admitted:
        return

    ledger_store.mark_reviewed(
        handoff_id,
        admitted,
        agent_type,
        cwd=str(worktree),
        agent_id=agent_id,
        sidecar_path=sidecar_rel.as_posix(),
    )


@register_op("hooks.subagent_review_mark")
async def _handler(params: dict, repo_root=None) -> dict:
    """SubagentStop write op: derive + append a commit-ledger review mark
    from a finishing reviewer's own ``reviewed_range`` findings.

    Inputs (flat scalar, extracted via ``_payload.field()``; ``""`` treated
    as absent): ``session_id``, ``agent_id``, ``agent_type``, ``cwd``.

    Always returns ``no_advisory()`` — this op never denies, never advises;
    its only observable effect is the ledger append (or its absence).
    """
    session_id = field(params, "session_id")
    agent_id = field(params, "agent_id")
    agent_type = field(params, "agent_type")
    cwd = field(params, "cwd")

    if not repo_root or not session_id or not agent_id or not agent_type:
        return no_advisory()

    if not _is_reviewer(agent_type):
        return no_advisory()

    await asyncio.to_thread(
        _resolve_and_mark_sync,
        session_id=session_id,
        agent_id=agent_id,
        agent_type=agent_type,
        cwd=cwd or str(repo_root),
        repo_root=str(repo_root),
    )

    return no_advisory()
