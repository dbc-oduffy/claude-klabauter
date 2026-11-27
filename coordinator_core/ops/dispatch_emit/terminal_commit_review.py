"""Review stamps and fingerprints for dispatch.terminal_commit: recorded reviews, reviewer-applied fixes, post-checkpoint edits."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Sequence
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.ask_contract import RESUMED_RUN_ID_KEY
from coordinator_core.ops.dispatch_emit.commit_request import CommitRequest
from coordinator_core.ops.dispatch_emit.plan_row_coding import (
    yaml_load as _yaml_load,
    source_rows_by_plan as _source_rows_by_plan,
    REVIEW_WALK_CAP as _REVIEW_WALK_CAP,
)
from coordinator_core.ops.dispatch_emit.register_gate import (
    RESOLUTION_RUN_ID as _RESOLUTION_RUN_ID,
    run_started_at as _run_started_at,
)
from coordinator_core.ops.dispatch_emit.worktree_snapshot import Snapshot as _Snapshot

from coordinator_core.ops.dispatch_emit.terminal_commit_checkpoints import _open_row_ids


def _delivery_not_rostered(inline_review: dict) -> bool:
    """True when the review carries no delivery verdict because the roster names no
    delivery-verifier: no objection, unlike any other absent verdict."""
    from coordinator_core.ops.review_mint.roster import DELIVERY_NOT_ROSTERED

    return (
        not isinstance(inline_review.get("delivery"), dict)
        and inline_review.get("delivery_not_run") == DELIVERY_NOT_ROSTERED
    )


def _review_only_coding(
    worktree_root: Path, terminal_sha: str, request: CommitRequest, inline_review: dict,
) -> tuple:
    """``({sha: {plan: {row id}}}, [row ids with no delta])`` for a review-only run.

    A review row is coded only when the marker recorded it ``open`` at emit, it is still
    ``open`` on disk, the inline review and judge passed, and some commit in
    ``run_base..terminal_sha`` changes a blob in its writes; it closes at the newest such
    commit. An open row no commit touched is reported, never closed. The walk is
    in-process (zero spawns) along first parents; a run base it never reaches (past the
    walk cap, on a merged-in branch, rewritten away) attributes nothing, and every
    still-open reviewed row then lands in the no-delta list.
    """
    if (
        not request.review_open_rows
        or not request.review_run_base
        or _reviewed_files_if_passed(inline_review) is None
    ):
        return {}, []
    criterion = inline_review.get("criterion")
    if isinstance(criterion, dict) and criterion.get("status") == "not_met":
        return {}, []
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import _blob_at_commit

    common_dir = resolve_git_common_dir(worktree_root)
    base = request.review_run_base
    delta: list = []  # newest first: (sha, parent)
    sha = terminal_sha
    reached = False
    from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

    for _ in range(_tc._REVIEW_WALK_CAP):
        if sha.startswith(base):
            reached = True
            break
        meta = commit_meta(common_dir, sha)
        if meta is None or not meta.get("parents"):
            break
        delta.append((sha, meta["parents"][0]))
        sha = meta["parents"][0]
    if not reached:
        delta = []

    open_now: dict = {}
    coded: dict = {}
    no_delta: list = []
    wanted = set(request.review_open_rows)
    for chunk in request.chunks:
        if chunk.id not in wanted:
            continue
        by_plan = _source_rows_by_plan(worktree_root, request.plan_path, [chunk.id])
        live = {
            plan_rel: ids for plan_rel, ids in by_plan.items()
            if ids & open_now.setdefault(plan_rel, _open_row_ids(worktree_root, plan_rel))
        }
        if not live:
            continue
        hit = next(
            (
                csha for csha, parent in delta
                if any(
                    _blob_at_commit(common_dir, csha, p) != _blob_at_commit(common_dir, parent, p)
                    for p in chunk.paths
                )
            ),
            None,
        )
        if hit is None:
            no_delta.append(chunk.id)
            continue
        for plan_rel, ids in live.items():
            coded.setdefault(hit, {}).setdefault(plan_rel, set()).update(ids)
    return coded, no_delta


def _recorded_fingerprints(
    worktree_root: Path, session_id: str, candidates: Sequence[str], all_paths: Sequence[str],
    deleted_paths: Sequence[str], *, excluded: set,
    reviewed: Optional[dict] = None, reviewed_at: Optional[float] = None,
    fixes_integrated_at: Optional[float] = None, wave_paths: frozenset = frozenset(),
    review_events: Optional[dict] = None, run_started_at: Optional[float] = None,
    run_agent_events: Optional[dict] = None,
) -> dict:
    """``{path: sha256}`` the run's expectation for the commit's candidate paths; one batched sink
    read. A session event older than ``run_started_at`` is another run's touch and is no expectation
    of this run: the path falls through to the review hash, else carries no entry. Per path the newer of the session's last recorded write and the run's newest review
    (``reviewed`` -> sha256 of the bytes it read, ``reviewed_at`` epoch seconds) supplies the hash;
    an exact tie goes to the session write. When the review wave applied fixes
    (``fixes_integrated_at``: epoch the wave's integration was recorded), a reviewed path whose
    bytes were last written no later than that moment, after the newest session write, is declared
    at those bytes; a later change keeps the older expectation and refuses, unless the wave's
    ledger rows (``wave_paths.fix_rows``) show the later bytes carry the applied fix. A path the wave's own
    record lists (``wave_paths``) counts as reviewed when no stored hash names it. A path whose
    newest session event postdates the review (``fixes_integrated_at`` when set, else
    ``reviewed_at``) and whose file mtime is no earlier than that event is declared at its current
    bytes: the run left them after review; a peer rewrite after that write is not detected. Candidates are DONE-chunk and run-owned paths; a live
    peer's claim on either already refused the call upstream, so no contest check runs here.
    ``review_events`` (``{path: (sha256, epoch)}``, ``_review_write_events``) is a review fix-pass
    agent's own newest write; it supplies the hash for a path whose session event is older or absent,
    so the run's own fix-pass write is the expectation and a later foreign edit still refuses. An
    event older than ``run_started_at`` is a pre-refire review's write and is no expectation.
    ``run_agent_events`` (``_run_agent_write_events``) is the run's own newest hashed agent write of
    any type, a revise-leg executor's included: for a path that already carries an expectation it
    replaces it when strictly newer than the session event, the review and the review fix-pass write,
    so a later write by the run's own legs is the expectation and a foreign one still refuses.
    A hashless newest session event, or an unrecorded path outside the review, carries no entry."""
    from coordinator_core.session import touch_record

    committed = set(all_paths) - set(deleted_paths)
    wanted = [p for p in dict.fromkeys(candidates) if p in committed and p not in excluded]
    if not wanted:
        return {}
    seen = touch_record.last_seen_events(session_id, wanted, worktree_root)
    if run_started_at is not None:
        seen = {p: (None if e is not None and e[1] < run_started_at else e) for p, e in seen.items()}
    reviewed = reviewed if reviewed_at is not None and isinstance(reviewed, dict) else {}
    if run_started_at is not None and review_events:
        review_events = {p: e for p, e in review_events.items() if e[1] >= run_started_at}
    out: dict = {}
    for p in wanted:
        event = seen.get(p)
        review_hash = reviewed.get(p)
        if (review_hash or p in wave_paths) and fixes_integrated_at is not None and (event is None or fixes_integrated_at > event[1]):
            target = worktree_root / p
            try:
                settled = target.stat().st_mtime <= fixes_integrated_at
            except OSError:
                settled = False
            if not settled:
                rows = getattr(wave_paths, "fix_rows", {}).get(p)
                settled = bool(rows) and _carries_applied_fix(target, rows)
            post_fix = touch_record.compute_content_hash(target) if settled else None
            if post_fix:
                out[p] = post_fix
                continue
        anchor = fixes_integrated_at if fixes_integrated_at is not None else reviewed_at
        if event is not None and anchor is not None and event[1] > anchor:
            target = worktree_root / p
            try:
                written_after = target.stat().st_mtime >= event[1]
            except OSError:
                written_after = False
            current = touch_record.compute_content_hash(target) if written_after else None
            if current:
                out[p] = current
                continue
        if review_hash and (event is None or reviewed_at > event[1]):
            out[p] = review_hash
        elif event is not None and event[0]:
            out[p] = event[0]
        agent_event = (review_events or {}).get(p)
        if agent_event is not None and agent_event[0] and (event is None or agent_event[1] > event[1]):
            out[p] = agent_event[0]
        run_event = (run_agent_events or {}).get(p)
        if run_event is not None and p in out:
            older = (event[1] if event else None, reviewed_at, agent_event[1] if agent_event else None)
            if run_event[1] > max((t for t in older if t is not None), default=float("-inf")):
                out[p] = run_event[0]
    return out


REVIEW_RECORD_AGENT = "review-record"


def _reverify_finished_at(worktree_root: Path, inline_review: object) -> Optional[float]:
    """Epoch the re-verify step finished: the newest mtime of the files its record names
    (``sidecar_path``, ``record``); None when the record names none that resolve."""
    block = inline_review.get("reverify") if isinstance(inline_review, dict) else None
    if not isinstance(block, dict):
        return None
    stamps: list = []
    for key in ("sidecar_path", "record"):
        rel = block.get(key)
        if isinstance(rel, str) and rel:
            try:
                stamps.append((worktree_root / rel).stat().st_mtime)
            except OSError:
                continue
    return max(stamps) if stamps else None


def _review_write_events(
    worktree_root: Path, review_writes: Optional[dict], inline_review: object = None
) -> dict:
    """``{path: (sha256, epoch)}`` of the newest hashed write event, in the sink of the agent
    ``review_writes`` names for that path, per path of ``review_writes``; one sink walk per
    distinct agent. A path whose agent's newest write is hashless, or whose sink is unreadable,
    has no entry. A path recorded by the re-verify record (``REVIEW_RECORD_AGENT``, which has no
    sink) is declared at its current bytes, stamped with the file's mtime. When the record carries
    ``edited_sha256`` ({path: sha256}) for the path, those bytes must equal the recorded hash and
    the mtime is not consulted; otherwise the mtime must be no later than the re-verify step's
    finish. A later change has no entry and keeps refusing."""
    if not review_writes:
        return {}
    from coordinator_core.session import core, touch_record

    out: dict = {}
    recorded = {p for p, a in review_writes.items() if a == REVIEW_RECORD_AGENT}
    finished = _reverify_finished_at(worktree_root, inline_review) if recorded else None
    block = inline_review.get("reverify") if isinstance(inline_review, dict) else None
    hashes = block.get("edited_sha256") if isinstance(block, dict) else None
    hashes = hashes if isinstance(hashes, dict) else {}
    for p in recorded:
        recorded_hash = hashes.get(p)
        if not isinstance(recorded_hash, str) or not recorded_hash:
            recorded_hash = None
        if recorded_hash is None and finished is None:
            continue
        target = worktree_root / p
        try:
            mtime = target.stat().st_mtime
        except OSError:
            continue
        if recorded_hash is None and mtime > finished:
            continue
        digest = touch_record.compute_content_hash(target)
        if digest and (recorded_hash is None or digest == recorded_hash):
            out[p] = (digest, mtime)
    hub = core.sessions_dir(str(worktree_root))
    if not hub:
        return out
    by_agent: dict = {}
    for path, agent_id in review_writes.items():
        if agent_id != REVIEW_RECORD_AGENT:
            by_agent.setdefault(agent_id, set()).add(path)
    for agent_id, paths in by_agent.items():
        out.update(_agent_newest_hashed_writes(hub, agent_id, paths))
    return out


def _agent_newest_hashed_writes(hub: str, agent_id: str, paths: set) -> dict:
    """``{path: (sha256, epoch)}`` of one agent sink's newest write per path of ``paths``; a path
    whose newest write is hashless, or an unreadable sink, has no entry."""
    from coordinator_core.session import touch_record

    newest: dict = {}
    try:
        for member in touch_record.discover_family(touch_record.sink_path(Path(hub) / ".agents" / agent_id)):
            for line in touch_record.iter_complete_lines(member.read_bytes()):
                try:
                    ev = touch_record.decode_line(line)
                except touch_record.MalformedRecordLine:
                    continue
                if (
                    ev.verb == touch_record.VERB_TOUCH and ev.kind != touch_record.KIND_READ
                    and ev.path in paths and ev.timestamp >= newest.get(ev.path, (None, float("-inf")))[1]
                ):
                    newest[ev.path] = (ev.content_hash, ev.timestamp)
    except OSError:
        return {}
    return {p: v for p, v in newest.items() if v[0]}


def _run_agent_write_events(
    worktree_root: Path, session_id: Optional[str], since: Optional[float], paths: Sequence[str]
) -> dict:
    """``{path: (sha256, epoch)}`` of the newest hashed write to ``paths`` since ``since`` by any
    agent the session dispatched (a revise-leg executor included, which ``_review_write_events``
    never sees), judged on the agent's own touch record. Agents whose owner is not ``session_id``
    are skipped. Empty when the session, ``since``, the hub or the dispatch ledger is unreadable."""
    wanted = set(paths)
    if not session_id or since is None or not wanted:
        return {}
    from coordinator_core.session import core, touch_record

    hub = core.sessions_dir(str(worktree_root))
    if not hub:
        return {}
    try:
        ledger = (Path(hub) / session_id / "dispatched-agents.txt").read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict = {}
    for agent_id in dict.fromkeys(row.split("\t", 1)[0] for row in ledger.splitlines() if row.strip()):
        owner, conclusive = touch_record.agent_owner_sid(str(Path(hub) / ".agents" / agent_id))
        if not conclusive or owner != session_id:
            continue
        for p, v in _agent_newest_hashed_writes(hub, agent_id, wanted).items():
            if v[1] >= since and v[1] >= out.get(p, (None, float("-inf")))[1]:
                out[p] = v
    unrecorded = wanted - out.keys()
    if unrecorded:
        out.update(_unrecorded_executor_writes(worktree_root, ledger, since, unrecorded))
    return out


def _unrecorded_executor_writes(worktree_root: Path, ledger: str, since: float, paths: set) -> dict:
    """``{path: (sha256, mtime)}`` of ``paths`` last written since ``since`` while the run had
    dispatched an executor, for a path no agent sink recorded a hashed write to. Trap: agent sinks
    can be absent, so the file's own mtime stands as the write time; a write after the re-verify
    finish still refuses in ``_less_run_agent_edits``."""
    from coordinator_core.session import touch_record

    def spawned_executor(row: str) -> bool:
        cols = row.split("\t")
        try:
            return len(cols) >= 4 and "executor" in cols[2] and float(cols[3]) >= since
        except ValueError:
            return False

    if not any(spawned_executor(r) for r in ledger.splitlines()):
        return {}
    out: dict = {}
    for p in paths:
        target = worktree_root / p
        try:
            mtime = target.stat().st_mtime
        except OSError:
            continue
        digest = touch_record.compute_content_hash(target)
        if digest and mtime >= since:
            out[p] = (digest, mtime)
    return out


def _fixes_integrated_at(worktree_root: Path, inline_review: object) -> tuple:
    """``(epoch, reviewed_paths)``: when the review wave's integration was recorded (its
    sidecar's mtime) and that sidecar's ``prep.slice_files``; ``(None, frozenset())`` when the
    wave applied no fixes or its sidecar does not resolve. The sidecar's mtime is the cutoff, so a
    later rewrite of that sidecar moves it forward; ``integration.sidecar`` is tried before the
    share-dir glob, which scans every session's directory."""
    if not isinstance(inline_review, dict):
        return None, frozenset()
    fixes = inline_review.get("fixes")
    if not (isinstance(fixes, int) and not isinstance(fixes, bool) and fixes > 0):
        return None, frozenset()
    integration = inline_review.get("integration")
    sidecar = integration.get("sidecar") if isinstance(integration, dict) else None
    candidates: list = []
    if isinstance(sidecar, str) and sidecar:
        candidates.append(worktree_root / sidecar)
    stem = inline_review.get("integration_stem")
    if isinstance(stem, str) and stem and "/" not in stem and "\\" not in stem:
        share = worktree_root / ".coordinator-local" / "subagent-share"
        try:
            candidates.extend(share.glob(f"*/{stem}.md"))
        except OSError:
            pass
    # A bookkeeping stem's record may carry no integration file: the reviewer's own sidecar then
    # records which paths its applied fixes touched (its ledger rows) and when it finished
    # (its recorded ``verified_at``, not the file's mtime, which any later touch moves).
    stamps: list = []
    for c in candidates:
        try:
            stamps.append((c.stat().st_mtime, c))
        except OSError:
            continue
    paths: set = set()
    wave_stamps: list = []
    fix_rows: dict = {}
    listed = inline_review.get("wave_sidecar_paths")
    for w in listed if isinstance(listed, list) else []:
        if isinstance(w, str) and w:
            done_at, fixed = _reviewer_applied_fixes(worktree_root / w)
            if done_at is not None:
                wave_stamps.append(done_at)
                paths.update(fixed)
                for f, rows in _reviewer_fix_rows(worktree_root / w).items():
                    fix_rows.setdefault(f, []).extend(rows)
    # The cutoff is the integration record's mtime, else the reviewer's stamp; the applier can write
    # after either, so the ledger rows let a later write that carries the fix stand as delivery.
    if stamps:
        at, newest = max(stamps, key=lambda s: s[0])
        paths.update(_wave_slice_files(newest))
    elif wave_stamps:
        at = max(wave_stamps)
    else:
        return None, frozenset()
    wave = _WavePaths(paths)
    wave.fix_rows = fix_rows
    return at, wave


_VERIFIED_AT_RE = re.compile(r"^findings_ledger:.*?\bverified_at:\s*['\"]?([0-9T:.\-]+Z?)", re.MULTILINE)

_LEDGER_BLOCK_RE = re.compile(r"^## Findings Ledger\b.*?^```json\s*\n(.*?)\n```", re.MULTILINE | re.DOTALL)


# A whole-second ``verified_at`` truncates the finish: a fix written later in that same second
# carries a larger mtime, so the stamp covers the second it names.
_WHOLE_SECOND_SPAN = 1.0 - 1e-6


def _reviewer_applied_fixes(sidecar: Path) -> tuple:
    """``(completed_epoch, fixed_paths)`` from a review-wave reviewer's sidecar: the epoch of its
    frontmatter ``findings_ledger.verified_at`` and the ``file`` of each row of its
    ``## Findings Ledger`` block that is verified, not suspended or rejected, and changed text
    (a non-empty ``before`` or ``after``). A path merely named elsewhere in the sidecar is not
    fixed. ``(None, frozenset())`` when unreadable or unstamped."""
    try:
        text = sidecar.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, frozenset()
    stamp = _VERIFIED_AT_RE.search(text.split("\n---", 2)[0] if text.startswith("---") else "")
    done_at = _epoch_of(stamp.group(1) if stamp else None)
    if done_at is None:
        return None, frozenset()
    if "." not in stamp.group(1):
        done_at += _WHOLE_SECOND_SPAN
    return done_at, frozenset(f for f, _ in _applied_fix_rows(text))


def _ledger_rows(text: str) -> list:
    """The dict rows of the sidecar's ``## Findings Ledger`` block; empty when absent or malformed."""
    block = _LEDGER_BLOCK_RE.search(text)
    try:
        rows = json.loads(block.group(1)) if block else []
    except ValueError:
        rows = []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _slash_path(path: str) -> str:
    """``path`` with forward slashes and no leading ``./``: the key form a pin and a ledger row share."""
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _applied_fix_rows(text: str, unverified_ok: bool = False) -> list:
    """``[(file, row)]`` for each verified ledger row whose status is ``applied`` (a row with no
    status is applied) and that changed text; with ``unverified_ok`` a row carrying no ``verified``
    key counts too (an explicit ``verified: false`` never does). A ``handed-to-em``, ``open``,
    rejected or suspended row is not an applied fix."""
    out = []
    for row in _ledger_rows(text):
        verified = row.get("verified")
        if not (verified is True or (unverified_ok and verified is None)) or (row.get("status") or "applied") != "applied":
            continue
        if not (row.get("before") or row.get("after")):
            continue
        f = row.get("file")
        if isinstance(f, str) and f:
            out.append((_slash_path(f), row))
    return out


def _applied_row_paths(text: str) -> list:
    """The ``file`` of each verified ``applied`` ledger row, whether or not it records its
    ``before``/``after`` text: the reviewer rewrote that file after judging it."""
    out = []
    for row in _ledger_rows(text):
        f = row.get("file")
        if row.get("verified") is True and (row.get("status") or "applied") == "applied" and isinstance(f, str) and f:
            out.append(_slash_path(f))
    return out


_DISPOSED_STATUSES = frozenset({"suspended", "rejected", "em-rejected", "em_rejected"})
_VERDICT_RE = re.compile(r"^verdict:[ \t]*['\"]?([A-Za-z_][A-Za-z_ \-]*)", re.MULTILINE)
_BODY_VERDICT_RE = re.compile(
    r"^[ \t#*_\-]*verdict[ \t*_]*[:—–\-][ \t*_`'\"]*([A-Za-z_][A-Za-z_ \-]*)", re.MULTILINE | re.IGNORECASE
)
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$", re.MULTILINE | re.DOTALL)
REQUIRES_CHANGES = "REQUIRES_CHANGES"
BLOCKED = "BLOCKED"


def _sidecar_verdict(text: str) -> Optional[str]:
    """Frontmatter ``verdict``, else the first body ``Verdict:`` line; None when neither."""
    head = text.split("\n---", 2)[0] if text.startswith("---") else ""
    found = _VERDICT_RE.search(head) or _BODY_VERDICT_RE.search(_FENCE_RE.sub("", text))
    return found.group(1) if found else None


def _row_fix(row: dict) -> tuple:
    b, a = row.get("before"), row.get("after")
    return (b if isinstance(b, str) else "", a if isinstance(a, str) else "")


def review_applied_in_tree(
    worktree_root: Path, inline_review: object, within: Optional[Sequence[str]] = None,
) -> Optional[tuple]:
    """``(applied, owed)`` derived from the landed tree, never from the reviewer's own count.

    ``applied`` counts the wave's verified ``applied`` ledger rows whose change is present in the
    file now (``_carries_applied_fix``). ``owed`` lists ``"<sidecar>:<id>"`` for every other row not
    suspended or rejected, but only from a sidecar whose frontmatter ``verdict`` is
    ``REQUIRES_CHANGES``: those findings are outstanding and the verdict stands. A row marked
    ``out_of_ask`` is a peer's finding and is skipped unless its file is one of ``within`` (the
    paths this commit carries). ``None`` when the run names no wave sidecar or one has no readable
    ledger: the reviewer's count is then the only evidence."""
    listed = inline_review.get("wave_sidecar_paths") if isinstance(inline_review, dict) else None
    if not isinstance(listed, list) or not listed:
        return None
    own = {_slash_path(str(p)) for p in within} if within is not None else set()
    applied, owed = 0, []
    for w in listed:
        if not (isinstance(w, str) and w):
            continue
        try:
            text = (worktree_root / w).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        rows = _ledger_rows(text)
        if not rows and _LEDGER_BLOCK_RE.search(text) is None:
            return None
        standing = _claimed_standing(_sidecar_verdict(text))
        for i, row in enumerate(rows):
            status = row.get("status") or "applied"
            f = row.get("file")
            peer = row.get("out_of_ask") is True and not (isinstance(f, str) and _slash_path(f) in own)
            if status in _DISPOSED_STATUSES or peer:
                continue
            carried = (
                status == "applied" and row.get("verified") is True and isinstance(f, str) and bool(f)
                and (row.get("before") or row.get("after"))
                and _carries_applied_fix(worktree_root / f.replace("\\", "/"), [_row_fix(row)])
            )
            if carried:
                applied += 1
            elif standing:
                owed.append(f"{w}:{row.get('id') or f'row-{i + 1}'}")
    return applied, owed


def _claimed_standing(verdict: object) -> bool:
    """True for a ``REQUIRES_CHANGES`` or ``BLOCKED`` verdict, however spaced or cased."""
    if not isinstance(verdict, str):
        return False
    tokens = [t for t in re.split(r"[\s_\-]+", verdict.strip().upper()) if t]
    return tokens[:2] == ["REQUIRES", "CHANGES"] or tokens[:1] == [BLOCKED]


def _unverifiable_standing(worktree_root: Path, inline_review: object) -> list:
    """Names of ``REQUIRES_CHANGES`` verdicts the ledger walk cannot resolve: a verdict claimed in
    ``wave_reviews`` with no sidecar behind it, a claimed verdict whose sidecar is unreadable, and a
    readable standing sidecar with no ledger rows. Each one is unresolved, never a pass."""
    if not isinstance(inline_review, dict):
        return []
    claims: dict = {}
    out: list = []
    reviews = inline_review.get("wave_reviews")
    for i, w in enumerate(reviews if isinstance(reviews, list) else []):
        if not isinstance(w, dict) or not _claimed_standing(w.get("verdict")):
            continue
        path = w.get("sidecar_path")
        if isinstance(path, str) and path:
            claims[path] = True
        else:
            out.append(f"wave_reviews[{i}]:no-sidecar")
    listed = inline_review.get("wave_sidecar_paths")
    paths = [w for w in (listed if isinstance(listed, list) else []) if isinstance(w, str) and w]
    for w in dict.fromkeys([*paths, *claims]):
        try:
            text = (worktree_root / w).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            if claims.get(w):
                out.append(f"{w}:sidecar-unreadable")
            continue
        stated = _sidecar_verdict(text)
        if stated is not None:
            standing = _claimed_standing(stated)
        else:
            standing = bool(claims.get(w))
        if standing and not _ledger_rows(text):
            out.append(f"{w}:no-ledger-rows")
    return out


_RUN_KEY_MAX_PIECES = 12  # hyphen-separated prefix pieces tried as a run-key suffix
_PREP_SIDECAR_TAIL = re.compile(r"^(?P<prefix>.+)-[0-9a-f]{12}-prep\.review-prep\.md$")


def _read_prep_pins(worktree_root: Path, rel: str) -> tuple:
    """``(judged_sha256 map, frontmatter)`` of one prep sidecar; ``({}, {})`` when unreadable."""
    from coordinator_core.frontmatter.primitives import split_frontmatter
    from coordinator_core.ops.review_prep import VERDICT_JUDGED_KEY

    try:
        split = split_frontmatter((worktree_root / rel).read_text(encoding="utf-8"))
        fm = _yaml_load(split.fm_text) if split else None
    except Exception:  # noqa: BLE001 -- an unreadable prep sidecar pins nothing
        return {}, {}
    if not isinstance(fm, dict):
        return {}, {}
    got = fm.get(VERDICT_JUDGED_KEY)
    pins = {k.replace("\\", "/"): v for k, v in got.items() if isinstance(k, str) and isinstance(v, str) and v} if isinstance(got, dict) else {}
    return pins, fm


def _latest_pass_prep(worktree_root: Path, rel: str) -> Optional[str]:
    """The run's newest ``review.prep`` sidecar when it is not the one ``rel`` names, else ``None``.

    The run-only pointer (``review_prep._write_pointers``) is rewritten by every pass, so it names
    the latest prep even once the base-keyed names are ``ambiguous``. The run key is a suffix of the
    sidecar's own prefix (``manifest-<run>`` on the ask route, ``<plan stem>-<run>`` on the plan route)."""
    from coordinator_core.ops import review_prep

    parts = PurePosixPath(rel.replace("\\", "/"))
    tail = _PREP_SIDECAR_TAIL.match(parts.name)
    if tail is None:
        return None
    pieces = tail.group("prefix").split("-")
    for i in range(min(len(pieces), _RUN_KEY_MAX_PIECES)):
        pointer = review_prep._read_pointer(worktree_root / parts.parent / review_prep._pointer_name("-".join(pieces[i:]), None))
        named = pointer.get("sidecar") if pointer else None
        if isinstance(named, str) and named and PurePosixPath(named).name == named:
            if named == parts.name or not (worktree_root / parts.parent / named).is_file():
                return None
            return (parts.parent / named).as_posix()
    return None


def _prep_judged_shas(worktree_root: Path, inline_review: object) -> dict:
    """``judged_sha256`` of the ``review.prep`` sidecar the run's ``inline_review`` names
    (``prep_sidecar``): the sha of every slice file as the reviewers read it. Empty when unnamed,
    unreadable, or written before the prep recorded pins. A later pass of the same run re-pins the
    files it reviewed: its pin replaces the named prep's for those paths, so unchanged bytes of a
    re-review stay pinned to the pass that judged them, and a path only an earlier pass judged keeps
    its earlier pin."""
    rel = inline_review.get("prep_sidecar") if isinstance(inline_review, dict) else None
    if not (isinstance(rel, str) and rel):
        return {}
    pins, _ = _read_prep_pins(worktree_root, rel)
    latest = _latest_pass_prep(worktree_root, rel)
    if latest is not None:
        later, fm = _read_prep_pins(worktree_root, latest)
        if fm.get("agent_type") == "engine:prep-freeze":
            pins = {**pins, **later}
    return pins


def verdict_judged_shas(worktree_root: Path, inline_review: object) -> dict:
    """``{path: [sha256, ...]}`` the review wave pinned: the prep's ``judged_sha256`` of every slice
    file (the producer: ``review_prep.slice_pins``), less the files a wave sidecar's applied ledger
    rows rewrote, plus each sidecar's own frontmatter ``judged_sha256`` (a reviewer's post-fix bytes).
    Empty when no wave sidecar is readable or nothing pins, so an unpinned verdict is never refused."""
    from coordinator_core.frontmatter.primitives import split_frontmatter
    from coordinator_core.ops.review_prep import VERDICT_JUDGED_KEY

    listed = inline_review.get("wave_sidecar_paths") if isinstance(inline_review, dict) else None
    out: dict = {}
    fixed: set = set()
    read_any = False
    for w in listed if isinstance(listed, list) else []:
        if not (isinstance(w, str) and w):
            continue
        try:
            text = (worktree_root / w).read_text(encoding="utf-8")
            split = split_frontmatter(text)
            fm = _yaml_load(split.fm_text) if split else None
        except Exception:  # noqa: BLE001 -- an unreadable or malformed sidecar pins nothing
            continue
        read_any = True
        applied = {_worktree_relative(worktree_root, f) for f in _applied_row_paths(text)}
        fixed.update(applied)
        textless = applied - {_worktree_relative(worktree_root, f) for f, _ in _applied_fix_rows(text)}
        own = fm.get(VERDICT_JUDGED_KEY) if isinstance(fm, dict) else None
        for p, sha in (own.items() if isinstance(own, dict) else ()):
            key = _worktree_relative(worktree_root, p) if isinstance(p, str) else None
            if key and key not in textless and isinstance(sha, str) and sha:
                out.setdefault(key, []).append(sha)
    if read_any:
        for p, sha in _prep_judged_shas(worktree_root, inline_review).items():
            if p not in fixed:
                out.setdefault(p, []).append(sha)
    return out


def _worktree_relative(worktree_root: Path, path: str) -> str:
    """``path`` as a forward-slash worktree-relative key; an absolute path under the root is
    relativised, any other path is only slash-normalised."""
    norm = _slash_path(path)
    root = str(worktree_root).replace("\\", "/").rstrip("/")
    if norm.lower().startswith(root.lower() + "/"):
        return norm[len(root) + 1:]
    return norm


def _reverify_clean(inline_review: object) -> bool:
    """The run's re-verify record passed, or every failure it lists pre-existed or is foreign:
    nothing red is the reviewer's own."""
    block = inline_review.get("reverify") if isinstance(inline_review, dict) else None
    if not isinstance(block, dict) or str(block.get("reverify_error") or "").strip():
        return False
    if block.get("status_after") == "pass":
        return True
    failing = block.get("failing")
    if block.get("status_after") not in ("fail", None) or not isinstance(failing, list) or not failing:
        return False
    known = {*(block.get("pre_existing") or ()), *(block.get("foreign") or ())}
    return all(f in known for f in failing) and not block.get("unattributed")


def _reviewer_agent_ids(worktree_root: Path, inline_review: object) -> list:
    """The ``review_receipt.agent_id`` of each wave sidecar: the reviewer agents whose touch sinks
    record what they wrote."""
    from coordinator_core.frontmatter.primitives import split_frontmatter

    listed = inline_review.get("wave_sidecar_paths") if isinstance(inline_review, dict) else None
    out: list = []
    for w in listed if isinstance(listed, list) else []:
        if not (isinstance(w, str) and w):
            continue
        try:
            split = split_frontmatter((worktree_root / w).read_text(encoding="utf-8"))
            fm = _yaml_load(split.fm_text) if split else None
        except Exception:  # noqa: BLE001 -- an unreadable sidecar names no reviewer
            continue
        receipt = fm.get("review_receipt") if isinstance(fm, dict) else None
        agent = receipt.get("agent_id") if isinstance(receipt, dict) else None
        if isinstance(agent, str) and agent and "/" not in agent and "\\" not in agent:
            out.append(agent)
    return out


def _less_reviewer_edits(worktree_root: Path, inline_review: object, stale: list) -> list:
    """``stale`` less each path whose current bytes equal the reviewer's newest recorded write to it,
    when the re-verify is clean: the reviewer's post-edit bytes are the reviewed product. A later or
    foreign edit differs from that hash and stays stale."""
    if not stale or not _reverify_clean(inline_review):
        return stale
    agents = _reviewer_agent_ids(worktree_root, inline_review)
    if not agents:
        return stale
    from coordinator_core.session import core, touch_record

    hub = core.sessions_dir(str(worktree_root))
    if not hub:
        return stale
    wanted = set(stale)
    written: dict = {}
    for agent in agents:
        for p, (sha, at) in _agent_newest_hashed_writes(hub, agent, wanted).items():
            if at >= written.get(p, (None, float("-inf")))[1]:
                written[p] = (sha, at)
    return [
        p for p in stale
        if p not in written or touch_record.compute_content_hash(worktree_root / p) != written[p][0]
    ]


def _less_reverify_edits(worktree_root: Path, inline_review: object, stale: list) -> list:
    """``stale`` less each path the re-verify record names in ``edited`` whose current bytes are the
    ones that step recorded (``_review_write_events``: ``edited_sha256`` when carried, else an mtime
    no later than its finish), when the re-verify is clean. A later or foreign edit stays stale."""
    block = inline_review.get("reverify") if isinstance(inline_review, dict) else None
    edited = block.get("edited") if isinstance(block, dict) else None
    if not stale or not isinstance(edited, list) or not _reverify_clean(inline_review):
        return stale
    named = {p.replace("\\", "/") for p in edited if isinstance(p, str) and p}
    claimed = {p: REVIEW_RECORD_AGENT for p in stale if p in named}
    if not claimed:
        return stale
    judged = _review_write_events(worktree_root, claimed, inline_review)
    return [p for p in stale if p not in judged]


def run_agent_pin_events(
    worktree_root: Path, session_id: Optional[str], run_id: Optional[str], inline_review: object,
) -> Callable[[Sequence[str]], dict]:
    """A ``paths -> {path: (sha256, epoch)}`` reader of the run's own dispatched agents' newest hashed
    writes (``_run_agent_write_events``: a revise-leg executor included, which the review-writer map
    never sees). Lazy: the sinks are opened only when a pin already mismatches."""

    def read(paths: Sequence[str]) -> dict:
        read_run, read_session = _resumed_review_source(worktree_root, inline_review, run_id, session_id)
        return _run_agent_write_events(
            worktree_root, read_session, _review_since(worktree_root, read_run, inline_review), list(paths)
        )

    return read


def _less_run_agent_edits(
    worktree_root: Path, inline_review: object, stale: list, run_agent_events: Optional[Callable[[Sequence[str]], dict]],
) -> list:
    """``stale`` less each path whose current bytes equal the newest hashed write of an agent this run
    dispatched, when the re-verify is clean and that write is no later than the re-verify step's
    finish (``_reverify_finished_at``; unknown finish keeps it stale): a fix-leg edit the re-verify
    judged is the reviewed product, as the commit's blob-drift gate already treats it. A later or
    peer write, or an agent write after the re-verify, stays stale."""
    if not stale or run_agent_events is None or not _reverify_clean(inline_review):
        return stale
    from coordinator_core.session import touch_record

    finished = _reverify_finished_at(worktree_root, inline_review)
    if finished is None:
        return stale
    events = run_agent_events(stale)
    return [
        p for p in stale
        if p not in events or events[p][1] > finished
        or touch_record.compute_content_hash(worktree_root / p) != events[p][0]
    ]


def _less_applied_fix_edits(worktree_root: Path, inline_review: object, stale: list) -> list:
    """``stale`` less each path whose current bytes carry every change the wave's verified applied
    ledger rows recorded for it (``_carries_applied_fix``): the run's own finding-application wrote
    those bytes after the pin was taken. A foreign rewrite that lacks the fix stays stale."""
    listed = inline_review.get("wave_sidecar_paths") if isinstance(inline_review, dict) else None
    if not stale or not isinstance(listed, list):
        return stale
    rows: dict = {}
    lenient = _reverify_clean(inline_review)
    for w in listed:
        if isinstance(w, str) and w:
            for f, pairs in _reviewer_fix_rows(worktree_root / w, lenient).items():
                rows.setdefault(_worktree_relative(worktree_root, f), []).extend(pairs)
    return [p for p in stale if p not in rows or not _carries_applied_fix(worktree_root / p, rows[p])]


def verdict_pin_mismatches(
    worktree_root: Path, inline_review: object, within: Optional[Sequence[str]] = None,
    run_agent_events: Optional[Callable[[Sequence[str]], dict]] = None,
) -> list:
    """Sorted paths (of ``within``, else every pinned path) whose bytes match no sha the verdict
    pinned, less a path the reviewer itself last wrote, the clean re-verify recorded as its own
    edit, or (given ``run_agent_events``) a run-dispatched agent last wrote: the files that owe
    re-review. Empty when the verdict pins nothing."""
    from coordinator_core.ops.review_prep import judged_mismatches

    judged = verdict_judged_shas(worktree_root, inline_review)
    stale = judged_mismatches(worktree_root, judged, list(judged) if within is None else [q for q in (str(p).replace("\\", "/") for p in within) if q in judged])
    stale = _less_applied_fix_edits(worktree_root, inline_review, stale)
    stale = _less_reverify_edits(worktree_root, inline_review, _less_reviewer_edits(worktree_root, inline_review, stale))
    return _less_run_agent_edits(worktree_root, inline_review, stale, run_agent_events)


def verdict_pin_refusal(mismatched: Sequence[str]) -> str:
    """The refusal text for ``mismatched`` paths: one fact, then the files owed."""
    names = ", ".join(mismatched[:8]) + (" ..." if len(mismatched) > 8 else "")
    return f"review verdict judged other bytes than the tree carries for {len(mismatched)} file(s); re-review only: {names}"


def _real_pin_mismatches(
    worktree_root: Path, inline_review: object, within: Optional[Sequence[str]],
    run_agent_events: Optional[Callable[[Sequence[str]], dict]] = None,
) -> list:
    """``verdict_pin_mismatches`` less the comment-only edits the land arm tolerates: a path whose
    pre-review snapshot proves the drift comment-only is no different reviewed product."""
    stale = verdict_pin_mismatches(worktree_root, inline_review, within, run_agent_events)
    if not stale or not isinstance(inline_review, dict):
        return stale
    from coordinator_core.ops.dispatch_emit.comment_only_drift import tolerated_drift
    from coordinator_core.session import touch_record

    judged = verdict_judged_shas(worktree_root, inline_review)
    pending = [
        {"path": p, "basis": "content-sha256", "expected": judged[p][0],
         "actual": touch_record.compute_content_hash(worktree_root / p)}
        for p in stale
        if judged.get(p) and len(judged[p]) == 1
    ]
    if not pending:
        return stale
    _, real = tolerated_drift(
        worktree_root, inline_review.get("prep_sidecar"), pending,
        untrusted_paths=list(within) if within is not None else [],
    )
    covered = {d["path"] for d in pending}
    return [p for p in stale if p in real or p not in covered]


def _passing_agent_types(worktree_root: Path, inline_review: dict) -> set:
    """``agent_type`` of each of the call's ``wave_reviews`` whose verdict, the sidecar's own stated
    verdict first, is present and not standing: a later pass that supersedes a recorded standing one."""
    out: set = set()
    reviews = inline_review.get("wave_reviews")
    for w in reviews if isinstance(reviews, list) else []:
        if not isinstance(w, dict) or not isinstance(w.get("agent_type"), str):
            continue
        verdict = w.get("verdict")
        path = w.get("sidecar_path")
        if isinstance(path, str) and path:
            try:
                stated = _sidecar_verdict((worktree_root / path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                stated = None
            verdict = stated if stated is not None else verdict
        if isinstance(verdict, str) and verdict.strip() and not _claimed_standing(verdict):
            out.add(w["agent_type"])
    return out


def _with_recorded_standing(worktree_root: Path, inline_review: object, run_id: Optional[str]) -> object:
    """``inline_review`` plus the standing (``REQUIRES_CHANGES``/``BLOCKED``) reviews this run, and the
    halted run it resumes, recorded in their own ``run-outcome.json``: a recomposed commit leg whose
    review names none of them still owes their findings. A recorded sidecar the call already lists,
    or whose ``agent_type`` the call re-reviewed with a passing verdict, is not added. One read per
    run id; ``inline_review`` itself when nothing is added."""
    if not isinstance(inline_review, dict):
        return inline_review
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_outcome

    ids = [r for r in dict.fromkeys([run_id, inline_review.get(RESUMED_RUN_ID_KEY)]) if isinstance(r, str) and r]
    listed = inline_review.get("wave_sidecar_paths")
    paths = [p for p in (listed if isinstance(listed, list) else []) if isinstance(p, str) and p]
    superseded = None
    added: list = []
    for rid in ids:
        recorded = read_run_outcome(worktree_root, rid)
        recorded_review = recorded.get("inline_review") if isinstance(recorded, dict) else None
        reviews = recorded_review.get("wave_reviews") if isinstance(recorded_review, dict) else None
        for w in reviews if isinstance(reviews, list) else []:
            path = w.get("sidecar_path") if isinstance(w, dict) else None
            if not (isinstance(path, str) and path) or path in paths or not _claimed_standing(w.get("verdict")):
                continue
            if superseded is None:
                superseded = _passing_agent_types(worktree_root, inline_review)
            if w.get("agent_type") in superseded:
                continue
            paths.append(path)
            added.append(w)
    if not added:
        return inline_review
    reviews = inline_review.get("wave_reviews")
    return {
        **inline_review,
        "wave_sidecar_paths": paths,
        "wave_reviews": [*(reviews if isinstance(reviews, list) else []), *added],
    }


def review_owed_findings(
    worktree_root: Path, inline_review: object, within: Optional[Sequence[str]] = None,
    run_id: Optional[str] = None,
) -> Optional[str]:
    """The owed-findings reason of ``review_owed_refusal`` alone, never the pin-staleness arm."""
    standing = _with_recorded_standing(worktree_root, inline_review, run_id)
    derived = review_applied_in_tree(worktree_root, standing, within)
    owed = list(derived[1]) if derived is not None else []
    owed.extend(_unverifiable_standing(worktree_root, standing))
    if not owed:
        return None
    applied = derived[0] if derived is not None else 0
    return (
        f"review verdict {REQUIRES_CHANGES} or {BLOCKED} stands with {len(owed)} finding(s) not in the landed tree "
        f"({applied} applied): {', '.join(owed[:8])}{' ...' if len(owed) > 8 else ''}"
    )


def review_owed_refusal(
    worktree_root: Path, inline_review: object, within: Optional[Sequence[str]] = None,
    run_agent_events: Optional[Callable[[Sequence[str]], dict]] = None,
    run_id: Optional[str] = None,
) -> Optional[str]:
    """The refusal reason when a ``REQUIRES_CHANGES`` verdict is unresolved, else ``None``: a
    reviewer's findings not all in the landed tree, or a standing verdict no ledger can resolve
    (``_unverifiable_standing``), whether the call's ``inline_review`` names it or the run's own
    record does (``_with_recorded_standing``). Names what is owed. With nothing owed, a verdict whose
    pinned ``judged_sha256`` no longer matches the tree refuses, naming only the changed files (of
    ``within`` when given: the paths this commit carries)."""
    owed = review_owed_findings(worktree_root, inline_review, within, run_id)
    if owed is not None:
        return owed
    stale = _real_pin_mismatches(worktree_root, inline_review, within, run_agent_events)
    return verdict_pin_refusal(stale) if stale else None


class _WavePaths(frozenset):
    """The wave's fixed paths; ``fix_rows`` maps a ledger-named path to its ``(before, after)`` pairs,
    the applier's expected change, which a write after the reviewer's ``verified_at`` must show."""

    fix_rows: dict


def _reviewer_fix_rows(sidecar: Path, unverified_ok: bool = False) -> dict:
    """``{path: [(before, after), ...]}`` of the applied ledger rows of a reviewer's sidecar."""
    try:
        text = sidecar.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    out: dict = {}
    for f, row in _applied_fix_rows(text, unverified_ok):
        b, a = row.get("before"), row.get("after")
        out.setdefault(f, []).append((b if isinstance(b, str) else "", a if isinstance(a, str) else ""))
    return out


def _carries_applied_fix(target: Path, rows: Sequence) -> bool:
    """True when ``target`` shows every row's change: its ``after`` text present, or its ``before``
    text absent for a pure removal. A foreign rewrite lacking the fix does not qualify."""
    try:
        body = target.read_bytes().decode("utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return False
    norm = [(b.replace("\r\n", "\n"), a.replace("\r\n", "\n")) for b, a in rows]
    for i, (before, after) in enumerate(norm):
        if after:
            if after not in body and not any(after in later for later, _ in norm[i + 1:]):
                return False
        elif before in body:
            return False
    return bool(rows)


def _wave_slice_files(sidecar: Path) -> frozenset:
    """``prep.slice_files`` of the wave's on-disk integration record: the paths the wave reviewed.
    A run whose prep was a test-runner sidecar stores no ``reviewed_sha256``; this is then the
    only record of which paths the wave's fixes may have touched. Empty when unreadable."""
    import yaml

    from coordinator_core.frontmatter.primitives import split_frontmatter

    try:
        split = split_frontmatter(sidecar.read_text(encoding="utf-8"))
        fm = _yaml_load(split.fm_text) if split else None
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return frozenset()
    prep = fm.get("prep") if isinstance(fm, dict) else None
    files = prep.get("slice_files") if isinstance(prep, dict) else None
    return frozenset(f for f in files if isinstance(f, str)) if isinstance(files, list) else frozenset()


def _epoch_of(stamp: object) -> Optional[float]:
    """Epoch seconds of an ISO-8601 ``Z`` stamp, ``None`` when absent or unparseable."""
    if not isinstance(stamp, str):
        return None
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def _withhold_reasons(worktree_root: Path, artifacts, record_dir_rel: Optional[str]) -> dict:
    """``{path: reason}`` the EM withheld via ``workflow.revise withhold_paths``, read from this
    run's record dir and the superseded predecessor dirs ``artifacts`` already carries (a
    predecessor's entries are read first, so this run's own win). Empty with no bound sizing:
    ``superseded_runs`` then yields no predecessor, so only the run's own dir is read."""
    from coordinator_core.ops.dispatch_emit import run_withhold

    dirs = list(artifacts.record_dirs) if artifacts is not None else []
    if record_dir_rel and record_dir_rel not in dirs:
        dirs.insert(0, record_dir_rel)
    return run_withhold.read(worktree_root / d for d in reversed(dirs))


_RESOLUTION_SHA = re.compile(r"^[0-9a-f]{7,64}$")


def _reconcile_prep(
    worktree_root: Path, session_id: Optional[str], run_id: Optional[str],
    request: CommitRequest, inline_review: object,
) -> tuple:
    """Replace the relayed ``inline_review["prep"]`` with the reply ``review.prep`` stored on disk
    for this run, in place, and point ``prep_sidecar`` at it.

    Disk truth the engine wrote wins over a review agent's copy of it, which may be trimmed
    (``product_files`` null, ``slice_files`` empty) and then reads as a zero-file diff. Does nothing
    when no base resolves or no single stored reply matches; the relay then stands.

    Returns ``(reviewed_sha256, recorded_at_epoch, matched)`` of the stored reply -- the bytes the
    run's newest review read -- or ``({}, None, False)`` when there is no usable reply or timestamp.
    ``matched`` is True only when a stored reply replaced the relayed prep.
    """
    none: tuple = ({}, None, False)
    if not isinstance(inline_review, dict) or not session_id or not run_id:
        return none
    prep = inline_review.get("prep")
    base = prep.get("run_base_sha") if isinstance(prep, dict) else None
    if not (isinstance(base, str) and _RESOLUTION_SHA.match(base)):
        from coordinator_core.ops.dispatch_emit.ask_fanout_contract import recorded_run_base

        base = recorded_run_base(worktree_root, run_id)
    if not base:
        return none
    from coordinator_core.ops import review_prep

    reply = review_prep.stored_reply_for_run(
        worktree_root, session_id, base, run_key=run_id,
        within=[p for c in request.chunks for p in c.paths], covering=True,
    )
    if reply is None:
        return none
    inline_review["prep"] = {
        "run_base_sha": reply["run_base_sha"],
        "product_files": reply["product_files"],
        "foreign_claims": reply["foreign_claims"],
        "slice_files": [f for s in reply["slices"] or [] if isinstance(s, dict) for f in s.get("files") or []],
    }
    inline_review["prep_sidecar"] = reply["sidecar_path"]
    reviewed = reply.get("reviewed_sha256")
    return (reviewed if isinstance(reviewed, dict) else {}), _epoch_of(reply.get("recorded_at")), True


def _write_resolution(worktree_root: Path, sizing_rel: str, sha: str, run_id: str) -> dict:
    """Write ``resolution: {sha, run_id}`` beside the sizing's ``status: shipped``, in place and
    schema-checked. ``{}`` on a write, ``{"resolution_error": reason}`` otherwise; never raises --
    the product commit stands."""
    if not _RESOLUTION_SHA.match(sha or "") or not _RESOLUTION_RUN_ID.match(run_id or ""):
        return {"resolution_error": f"unrecordable resolution sha={sha!r} run_id={run_id!r}"}
    import yaml

    from coordinator_core.frontmatter.primitives import write_fm_nested_field
    from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter
    from coordinator_core.locked_write import MutateAbort, locked_rmw

    schema_path = Path(__file__).parent.parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"

    def mutate(old: str) -> str:
        new = write_fm_nested_field(old, "resolution", f'  sha: "{sha}"\n  run_id: "{run_id}"\n')
        try:
            errors = validate_frontmatter(_yaml_load(new) or {}, schema_path)
        except Exception as exc:  # noqa: BLE001 -- surfaced as a refusal
            raise MutateAbort(f"resolution write refused: {exc}")
        if errors:
            raise MutateAbort(f"resolution write refused: {format_validation_errors(errors)}")
        return new

    try:
        locked_rmw(worktree_root / sizing_rel, mutate, repo_root=worktree_root)
    except MutateAbort as exc:
        return {"resolution_error": str(exc.args[0]) if exc.args else "resolution write refused"}
    except Exception as exc:  # noqa: BLE001 -- lock timeout, I/O: refuse, keep the commit
        return {"resolution_error": f"resolution write refused: {exc!r}"}
    return {}


def _stage_returns(inline_review: dict, worktree_root: Optional[Path] = None) -> Optional[dict]:
    """The run record's stage-return fields out of the digest's
    ``inline_review``, or ``None`` when the digest predates them (no ``prep``
    block). Maps the digest's names onto the record keys ``review_stamp.mint``
    reads. With ``worktree_root``, ``fixes_applied`` is the count of ledger fixes present in the
    tree (``review_applied_in_tree``) and ``review_owed`` lists the findings a standing
    ``REQUIRES_CHANGES`` verdict still owes; the reviewer's own count is the fallback only."""
    if not isinstance(inline_review.get("prep"), dict):
        return None
    out: dict = {k: inline_review.get(k) for k in ("prep", "delivery", "tests", "criterion")}
    not_run = inline_review.get("delivery_not_run")
    if isinstance(not_run, str) and not_run:
        out["delivery_not_run"] = not_run
    wave_not_run = inline_review.get("wave_not_run")
    if isinstance(wave_not_run, list) and wave_not_run:
        out["wave_not_run"] = wave_not_run
    fixes = inline_review.get("fixes")
    if isinstance(fixes, int) and not isinstance(fixes, bool):
        out["fixes_applied"] = fixes
    derived = review_applied_in_tree(worktree_root, inline_review) if worktree_root is not None else None
    if derived is not None:
        out["fixes_applied"] = derived[0]
        if derived[1]:
            out["review_owed"] = derived[1]
    integration = inline_review.get("integration")
    if isinstance(integration, dict):
        out["integration_sidecar"] = integration.get("sidecar")
        out["unresolved"] = integration.get("unresolved") or []
        out["confinement_violations"] = integration.get("confinement_violations") or 0
    return out


def _mint_review_stamp(
    worktree_root: Path, plan_rel: Optional[str], sha: str, record_abs: Path, record: dict,
    tree: Optional[str] = None,
) -> dict:
    """Mint the plan's ``review_stamp`` against the commit this op just
    landed. A refusal is reported, never raised: the product commit stands."""
    if not plan_rel:
        return {}
    guarded = contained_path(worktree_root / plan_rel, [worktree_root])
    if guarded is None:
        return {}
    from coordinator_core.ops.review_stamp import MintRefusal, mint

    try:
        mint(
            guarded, worktree_root, build_test_path=None, resolved=(sha, record_abs, record),
            criterion_open_ok=True, terminal_tree_sha=tree,
        )
    except MintRefusal as exc:
        return {"review_stamp": "refused", "review_stamp_refusal": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- surfaced; product commit stands
        return {"review_stamp": "refused", "review_stamp_refusal": repr(exc)}
    return {"review_stamp": "minted"}


def _review_since(
    worktree_root: Path, run_id: Optional[str], inline_review: object
) -> Optional[float]:
    """Epoch seconds the run began: its ``submission.json`` ``submitted_at``, else the committer
    time of its base commit (the review prep's ``run_base_sha``, else the recorded run base), read
    in-process. ``None`` when neither resolves."""
    started = _run_started_at(worktree_root, run_id)
    if started is not None:
        return started
    prep = inline_review.get("prep") if isinstance(inline_review, dict) else None
    base = prep.get("run_base_sha") if isinstance(prep, dict) else None
    if not (isinstance(base, str) and _RESOLUTION_SHA.match(base)) and run_id and _RESOLUTION_RUN_ID.match(run_id):
        from coordinator_core.ops.dispatch_emit.ask_fanout_contract import recorded_run_base

        base = recorded_run_base(worktree_root, run_id)
    if not (isinstance(base, str) and _RESOLUTION_SHA.match(base)):
        return None
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir

    meta = commit_meta(resolve_git_common_dir(worktree_root), base)
    epoch = (meta or {}).get("committer_epoch")
    return float(epoch) if isinstance(epoch, int) and not isinstance(epoch, bool) else None


def _resumed_review_source(
    worktree_root: Path, inline_review: object, run_id: Optional[str], session_id: Optional[str]
) -> tuple:
    """``(run_id, session_id)`` whose review writes this landing reads: the halted run named by
    ``inline_review[RESUMED_RUN_ID_KEY]`` with its recorded emitter, when that id is safe and its
    record dir exists; else the caller's own pair."""
    from coordinator_core.ops._path_guard import safe_id

    resumed = inline_review.get(RESUMED_RUN_ID_KEY) if isinstance(inline_review, dict) else None
    if not (isinstance(resumed, str) and resumed and safe_id(resumed)):
        return run_id, session_id
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        if not record_dir(worktree_root, resumed).is_dir():
            return run_id, session_id
    except ValueError:
        return run_id, session_id
    from coordinator_core.ops.dispatch_emit.halted_run import recorded_emitter

    return resumed, recorded_emitter(worktree_root, resumed) or session_id


def _post_checkpoint_named(
    *reason_maps, art_landable=None, sibling_withheld=(), left_out=(), held=(),
) -> set:
    """Every path a landing already names as withheld: the ``reason_maps`` keys, ``sibling_withheld``,
    ``left_out``, ``held`` and ``art_landable.withheld``. The one builder for both gate sites. Trap:
    ``sibling_withheld`` exists only after the pre-commit sibling filter, so the early site passes none."""
    named: set = set()
    for m in reason_maps:
        named.update(m)
    named.update(sibling_withheld)
    named.update(left_out)
    named.update(held)
    if art_landable is not None:
        named.update(art_landable.withheld)
    return named


def _unclaimed_post_checkpoint(snapshot, recorded_pc: dict, *, landing: set, named: set) -> list:
    """Sorted recorded post-checkpoint paths still dirty in ``snapshot`` and neither in ``landing`` nor
    ``named``."""
    return sorted(
        p for p in snapshot.changed_among(sorted(recorded_pc)) if p not in landing and p not in named
    )


def _recorded_post_checkpoint(
    worktree_root: Path, ask_run_dir: Optional[Path], run_id: Optional[str], inline_review: object
) -> dict:
    """``{path: record dir name}`` of the ``post_checkpoint`` hunks this run's own kept.json and the
    resumed run's (``RESUMED_RUN_ID_KEY``) hold: hunks a halt recorded that this landing must carry
    or withhold. File reads only; an unreadable or unsafe record contributes nothing."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    dirs: list = []
    if ask_run_dir is not None:
        dirs.append(ask_run_dir)
    elif isinstance(run_id, str) and run_id:
        try:
            dirs.append(record_dir(worktree_root, run_id))
        except ValueError:
            pass
    resumed = inline_review.get(RESUMED_RUN_ID_KEY) if isinstance(inline_review, dict) else None
    if isinstance(resumed, str) and resumed and safe_id(resumed):
        try:
            dirs.append(record_dir(worktree_root, resumed))
        except ValueError:
            pass
    found: dict = {}
    for rdir in dirs:
        for rel in kept_work.read(rdir)[kept_work.POST_CHECKPOINT_KEY]:
            norm = kept_work._norm(rel)
            if norm is not None:
                found.setdefault(norm, rdir.name)
    return found


def _review_recorded_edits(inline_review: object) -> frozenset:
    """Repo-relative paths the review's test-runner or reverify record lists as ``edited``,
    ``created`` or ``outside_declared``: files the run's review changed, including ones an earlier checkpoint of this run
    already committed and this fire's chunks no longer declare. Absolute and ``..`` paths are skipped."""
    found: set = set()
    if isinstance(inline_review, dict):
        for key in ("tests", "reverify"):
            block = inline_review.get(key)
            if not isinstance(block, dict):
                continue
            # `outside_declared` is reverify's list of tracked files the review wrote outside the manifest;
            # omitting it drops those fixes before `review_edits.partition` can land or surface them.
            for field in ("edited", "created", "outside_declared"):
                listed = block.get(field)
                if not isinstance(listed, list):
                    continue
                for p in listed:
                    if not isinstance(p, str) or not p:
                        continue
                    n = p.replace("\\", "/")
                    if n.startswith("/") or n[1:2] == ":" or ".." in n.split("/"):
                        continue
                    found.add(n)
    return frozenset(found)


def _review_edit_verdict(
    worktree_root: Path, writes: Optional[dict], snapshot: Optional[_Snapshot],
    session_id: Optional[str], run_id: Optional[str], committed: set,
    sibling_claims: Optional[dict] = None,
    own_files: Optional[frozenset] = None,
):
    """``review_edits.partition`` over the review stage's writes against the pre-commit snapshot.
    Without a snapshot the dirt cannot be read, so the verdict is unanswerable. ``sibling_claims``
    ({path: run id} an unlanded sibling run claims) joins the live siblings' declared paths.
    ``own_files`` bounds residue: a write outside it belongs to another run of the session and is
    neither landed nor halted."""
    from coordinator_core.ops.dispatch_emit import review_edits

    if writes is not None and own_files is not None:
        writes = {k: v for k, v in writes.items() if review_edits.norm(k) in own_files}

    if writes is None or snapshot is None:
        return review_edits.partition(
            None, changed=set(), ignored=(), committed=set(), sibling_declared={},
            contested=set(), present=lambda _p: False,
        )
    paths = [review_edits.norm(k) for k in writes]
    inside = [p for p in paths if not review_edits.out_of_worktree(p)]
    candidates = [p for p in inside if p in snapshot.changed and p not in committed]
    contested: set = set()
    if candidates and session_id:
        from coordinator_core.session import scope as session_scope

        found = session_scope.contested_by_live_peers(candidates, session_id, cwd=str(worktree_root))
        contested = set(found) if found else set()
    return review_edits.partition(
        writes,
        changed=snapshot.changed,
        ignored=snapshot.ignored_among(inside),
        committed=committed,
        sibling_declared={**(sibling_claims or {}), **review_edits.live_sibling_declared(worktree_root, run_id)},
        contested=contested,
        present=lambda p: (worktree_root / p).is_file(),
    )


def _reverted_paths(params: dict, inline_review: object) -> frozenset:
    """Repo-relative paths the run's test-runner or review recorded as reverted: the string
    entries of ``reverted`` under ``params.targeted_tests``, ``inline_review.tests`` and
    ``inline_review.integration``. Malformed input contributes nothing and never raises.
    A reverted path absent from the worktree is a revert, never a declared deletion."""
    holders: list = [params.get("targeted_tests")]
    if isinstance(inline_review, dict):
        holders += [inline_review.get("tests"), inline_review.get("integration")]
    found: set = set()
    for holder in holders:
        listed = holder.get("reverted") if isinstance(holder, dict) else None
        if isinstance(listed, list):
            found.update(p.replace("\\", "/") for p in listed if isinstance(p, str) and p)
    return frozenset(found)


def _reviewed_files_if_passed(inline_review: dict) -> Optional[set]:
    """The files the review wave covered (``prep.slice_files``), or ``None`` when the
    review did not pass: delivery verdict PASS, no unresolved finding, no confinement
    violation. Absent evidence is not a pass."""
    prep = inline_review.get("prep")
    delivery = inline_review.get("delivery")
    integration = inline_review.get("integration")
    unrostered = _delivery_not_rostered(inline_review)
    if not (isinstance(prep, dict) and (unrostered or isinstance(delivery, dict)) and isinstance(integration, dict)):
        return None
    files = prep.get("slice_files")
    if (
        (not unrostered and delivery.get("verdict") != "PASS")
        or integration.get("unresolved")
        or integration.get("confinement_violations")
        or not isinstance(files, list)
    ):
        return None
    return {f for f in files if isinstance(f, str)}


def _clean_review_files(inline_review: dict) -> Optional[set]:
    """``prep.slice_files`` when the review has no unresolved finding and no confinement
    violation, else ``None``. Unlike ``_reviewed_files_if_passed`` the delivery verdict is not read."""
    prep = inline_review.get("prep")
    integration = inline_review.get("integration")
    if not (isinstance(prep, dict) and isinstance(integration, dict)):
        return None
    files = prep.get("slice_files")
    if integration.get("unresolved") or integration.get("confinement_violations") or not isinstance(files, list):
        return None
    return {f for f in files if isinstance(f, str)}
