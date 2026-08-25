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
    2. Resolve the run-report sidecar by reading the ``sidecar_path:`` marker
       out of the finishing agent's OWN transcript
       (``agent_transcript_path``) — see ``_resolve_sidecar_from_transcript``.
       This SUPERSEDES AC14's by-construction derivation at
       ``state/subagent-share/<session_id>/<label>.<agent_id>.md``, which no
       producer ever wrote: ``provision_report._provision`` chooses the name at
       ``PreToolUse``, where ``agent_id`` is structurally absent, so every real
       sidecar is nonce-named and the op could not fire for any dispatch
       (0 of 6,503 measured). AC14 and AC5 are corrected to NOT MET in
       ``f20b551f8272``; the full record, including the two repairs rejected
       before this one, is
       ``state/bug-backlog/2026-08-20-the-mark-op-resolves-a-sidecar-name-noth-526f0eaf2de4.yaml``.
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
    own ``x-bump-note`` already condemned by name (K-005). Step 2 opens ONE
    file, named by the payload; that is not the scan, and reaching for a glob
    when a transcript carries no marker reintroduces exactly what K-005 bans.

    Do NOT re-derive the sidecar path from ``agent_id``, in step 2 or
    anywhere else. The harness's named-teammate id format embeds ``@``, which
    ``provision_report._sanitize_segment`` strips, so an ``agent_id``-shaped
    filename is decided by the harness's id grammar rather than by us and
    fails for every named dispatch. The id is a KEY, never a path segment.

    Do NOT substitute any positional, time-ordered, or nearest-timestamp
    correlation for step 2 (claim-the-oldest-pending, match-by-arrival-order).
    A wrong correlation binds a mark to the WRONG reviewer's findings, which
    is worse than no mark; the transcript is exact because the harness, not
    this op, decided which agent it belongs to. Exact or decline.

    Do NOT mark for a sidecar whose agent has already finished. Nothing is
    backfilled: a mark is derived at the ``SubagentStop`` that carries the
    transcript, so past dispatches stay unmarked — the no-backfill rule
    unchanged in effect, though its reason is now "the event has passed"
    rather than "the name is unresolvable".

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
import re
from pathlib import Path, PurePosixPath
from typing import List, Optional

import yaml

from coordinator_core.commit_ledger import store as ledger_store
from coordinator_core.commit_ledger.resolve_owner import resolve_owner_handoff_id
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op

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


#: The injected marker `enforce-agent-dispatch-mode.py::_compose_sidecar_offer_text`
#: appends verbatim to the child's `tool_input.prompt`, described there as "a
#: machine-readable marker, not prose". Matched against the transcript's RAW
#: line text rather than a decoded prompt string: the value is a sanitized
#: repo-relative path (`_SEGMENT_WHITELIST_RE` admits no whitespace, quote, or
#: backslash), so the terminator set below cannot truncate a legitimate path,
#: and matching raw costs no per-line JSON decode on the hot path.
_SIDECAR_MARKER_RE = re.compile(r'sidecar_path: ([^\s"\\]+)')

#: Bytes of transcript scanned before giving up. The marker lands in the
#: child's FIRST user message; a whole reviewer transcript routinely runs to
#: several hundred KB and none of the rest can contain a marker that was not
#: already seen. Bounds a SubagentStop hook that fires fleet-wide.
_TRANSCRIPT_SCAN_LIMIT_BYTES = 262144


def _resolve_sidecar_from_transcript(transcript_path: str) -> Optional[Path]:
    """Read the provisioned sidecar path out of the finishing agent's OWN
    transcript, or ``None``.

    This replaces an earlier by-construction derivation at
    ``<label>.<agent_id>.md`` that no producer ever wrote — see
    ``state/bug-backlog/2026-08-20-the-mark-op-resolves-a-sidecar-name-noth-526f0eaf2de4.yaml``
    for the measurement (0 of 6,503 sidecars) and for why the two obvious
    repairs were rejected.

    The binding is made by the HARNESS, not reconstructed here, which is what
    makes it exact: ``agent_transcript_path`` arrives in the ``SubagentStop``
    payload and names a file the harness keys to a single ``agent_id``, while
    the sidecar path was injected into that same agent's prompt at
    ``PreToolUse``. Nothing positional, time-ordered, or nearest-match is
    involved, so a mark can never be bound to a different reviewer's findings
    — the two constraints the bug entry records as load-bearing.

    Also why this is NOT the directory scan K-005 forbids: one named file is
    opened, and it is named by the payload rather than searched for.

    ``None`` on an absent/unreadable transcript, no marker, or a marker whose
    value is not a plain repo-relative path (absolute, drive-qualified, or
    carrying a ``..`` segment) — fail-quiet, never a raise.
    """
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(_TRANSCRIPT_SCAN_LIMIT_BYTES)
    except OSError:
        return None

    match = _SIDECAR_MARKER_RE.search(head)
    if match is None:
        return None
    raw = match.group(1)

    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    # A Windows drive-qualified value ("X:/...") is not absolute to
    # PurePosixPath, so it is rejected explicitly rather than joined onto the
    # worktree and silently escaping it.
    if ":" in candidate.parts[0]:
        return None
    return Path(*candidate.parts)


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
    *,
    session_id: str,
    agent_id: str,
    agent_type: str,
    agent_transcript_path: str,
    cwd: str,
    repo_root: str,
) -> None:
    """Blocking body: sidecar read, git resolution, ledger append. Called
    exclusively via ``asyncio.to_thread`` — must not be awaited directly.
    Every branch below returns silently on failure (AC7 fail-quiet); nothing
    here ever raises out to the caller.
    """
    worktree = Path(repo_root)

    sidecar_rel = _resolve_sidecar_from_transcript(agent_transcript_path)
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
    as absent): ``session_id``, ``agent_id``, ``agent_type``,
    ``agent_transcript_path``, ``cwd``.

    ``agent_transcript_path`` is REQUIRED for a mark and its absence is a
    fail-quiet no-op, not an error: a relay that does not forward it (the
    ``SubagentStop`` shim forwarded it only to ``hooks.subagent_zero_tool_use``
    until this op needed it) leaves the op inert rather than marking off a
    guessed path.

    Always returns ``no_advisory()`` — this op never denies, never advises;
    its only observable effect is the ledger append (or its absence).
    """
    session_id = field(params, "session_id")
    agent_id = field(params, "agent_id")
    agent_type = field(params, "agent_type")
    agent_transcript_path = field(params, "agent_transcript_path")
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
        agent_transcript_path=agent_transcript_path,
        cwd=cwd or str(repo_root),
        repo_root=str(repo_root),
    )

    return no_advisory()
