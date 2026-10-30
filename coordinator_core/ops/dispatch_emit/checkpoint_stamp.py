"""Checkpoint-commit parsing and the coded stamp for the rows a checkpoint landed.

``parse_checkpoint`` reads the message ``emit._checkpoint_commit_js`` composes;
``stamp_checkpoint`` flips the rows it names from ``open`` to ``coded`` at the
checkpoint's own sha, in-process (rows named on ``Checkpoint-Gate-Owed:`` or
``Checkpoint-Stop-Rule:`` stay ``open``), and lands one plan-only follow-up commit
carrying ``Checkpoint-Stamp: <sha>``. ``Checkpoint-Not-Landed: run-open`` marks a mid-run
checkpoint and ``Checkpoint-Run-Closed: <base>`` on a later commit closes it for
``rows_landed_at_head``. ``Checkpoint-Run-Halted: <base>`` on a halt commit ends the run for the
publish reader only: ``rows_landed_at_head`` ignores it, so a halted run's rows stay unlanded
(A-HALT-IS-NOT-A-CLOSE). Stdlib-only at module top: callers gate the import on a literal
``Checkpoint-Plan: `` test so a plain commit pays nothing.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Mapping, NamedTuple, Optional

CHECKPOINT_SUBJECT = re.compile(r"^checkpoint\(wave [^)]*\): \d+ rows? — (?P<ids>.+)$")
CHECKPOINT_PLAN_LINE = re.compile(r"^Checkpoint-Plan: (?P<v>\S.*?)\s*$", re.M)
CHECKPOINT_GATE_OWED_LINE = re.compile(r"^Checkpoint-Gate-Owed:[ \t]*(?P<v>.+?)\s*$", re.M)
CHECKPOINT_STOP_RULE_LINE = re.compile(r"^Checkpoint-Stop-Rule:[ \t]*(?P<v>.+?)\s*$", re.M)
# Trap: a stopped row's bytes ARE in the commit that names it here; only its
# landed-ness is withheld, so a refire re-dispatches it.
STOP_RULE_TRAILER = "Checkpoint-Stop-Rule"
STAMP_TRAILER = "Checkpoint-Stamp"
_STAMP_LINE = re.compile(r"^" + STAMP_TRAILER + r":[ \t]*\S", re.M)
# A marked checkpoint is mid-run: outside readers count it only once a newer first-parent
# commit carries RUN_CLOSED_TRAILER naming its base. A line absent means legacy, i.e. landed.
# RUN_HALTED_TRAILER marks a run that ended without landing: the publish reader honours it,
# rows_landed_at_head does not (a halted run's rows must stay unlanded for a refire).
CHECKPOINT_NOT_LANDED_TRAILER = "Checkpoint-Not-Landed"
RUN_CLOSED_TRAILER = "Checkpoint-Run-Closed"
RUN_HALTED_TRAILER = "Checkpoint-Run-Halted"
_NOT_LANDED_LINE = re.compile(r"^" + CHECKPOINT_NOT_LANDED_TRAILER + r":[ \t]*\S", re.M)
_BASE_LINE = re.compile(r"^Checkpoint-Base:[ \t]*(?P<v>\S+)[ \t]*$", re.M)
_REVIEW_BASE_LINE = re.compile(r"^Checkpoint-Review-Base:[ \t]*(?P<v>\S+)[ \t]*$", re.M)
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN_CLOSED_LINE = re.compile(r"^" + RUN_CLOSED_TRAILER + r":[ \t]*(?P<v>[0-9a-f]{40})[ \t]*$", re.M)


class Checkpoint(NamedTuple):
    plan_rel: str
    row_ids: frozenset
    gate_owed: frozenset
    stop_rule: frozenset = frozenset()  # carried, not landed
    base: Optional[str] = None
    review_base: Optional[str] = None
    not_landed: bool = False  # mid-run marker present


def run_closed_bases(message: str) -> frozenset:
    """The 40-hex base of every ``Checkpoint-Run-Closed:`` line in ``message``."""
    return frozenset(m.group("v") for m in _RUN_CLOSED_LINE.finditer(message))


_RUN_HALTED_LINE = re.compile(r"^" + RUN_HALTED_TRAILER + r":[ \t]*(?P<v>[0-9a-f]{40})[ \t]*$", re.M)
_LANDING_TRAILER_LINE = re.compile(r"^(?:Lands-Chunks|Inline-Review):", re.M)
_HALT_SUBJECT_PREFIX = "warp: record halted "
_PLANS_PREFIX = "docs/plans/"


def run_halted_bases(message: str) -> frozenset:
    """The 40-hex base of every ``Checkpoint-Run-Halted:`` line in ``message``."""
    return frozenset(m.group("v") for m in _RUN_HALTED_LINE.finditer(message))


def _trailer_lines(trailer: str, bases) -> list:
    return [f"{trailer}: {b}" for b in dict.fromkeys(b for b in bases if isinstance(b, str) and _FULL_SHA.fullmatch(b))]


_SCRIPT_RUN_BASE = re.compile(r"^\s*let _runBase = '(?P<v>[0-9a-f]{40})';", re.M)


def script_run_base(script_text: str) -> Optional[str]:
    """The 40-hex `Checkpoint-Base` an emitted workflow script stamps on its checkpoints
    (`let _runBase = '<sha>';`); None when the script carries none. An inventory run has no
    manifest or submission record, so the script is the only place its base is recorded."""
    m = _SCRIPT_RUN_BASE.search(script_text or "")
    return m.group("v") if m else None


def run_closed_trailer_lines(bases) -> list:
    """One ``Checkpoint-Run-Closed: <sha>`` line per distinct 40-hex base, input order."""
    return _trailer_lines(RUN_CLOSED_TRAILER, bases)


def run_halted_trailer_lines(bases) -> list:
    """One ``Checkpoint-Run-Halted: <sha>`` line per distinct 40-hex base, input order."""
    return _trailer_lines(RUN_HALTED_TRAILER, bases)


def is_landing_message(message: str) -> bool:
    """True for a run landing: a ``Lands-Chunks:``/``Inline-Review:`` line on a non-checkpoint message."""
    return bool(_LANDING_TRAILER_LINE.search(message)) and parse_checkpoint(message) is None


def terminal_plan_closures(message: str, changed_paths) -> frozenset:
    """The ``Checkpoint-Plan`` paths a landing or halt commit ends a run of; empty unless typed.

    Typed: a landing (``Lands-Chunks:``/``Inline-Review:`` line and not a checkpoint message) or a
    halt (subject starts ``warp: record halted ``). Yields the changed ``docs/plans/*.md`` paths
    plus any ``Halt-Plan:`` value. A checkpoint, stamp or untyped commit touching a plan yields
    nothing."""
    # Function-local: the module top stays stdlib-only (test_module_top_imports_stdlib_only).
    from coordinator_core.session import record_homes

    # Repo-relative and "/"-separated, the form `changed_paths` are normalised to below.
    mise_prefix = record_homes.home_dir("", "mise-inventory").replace("\\", "/") + "/"
    lines = message.splitlines()
    subject = lines[0].strip() if lines else ""
    halt = subject.startswith(_HALT_SUBJECT_PREFIX)
    landing = is_landing_message(message)
    if not (halt or landing):
        return frozenset()
    out = {
        p for p in (str(c).replace("\\", "/") for c in changed_paths)
        if (p.startswith(_PLANS_PREFIX) and p.endswith(".md"))
        or (p.startswith(mise_prefix) and p.endswith(".spine.md"))
    }
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_TRAILER

    for m in re.finditer(r"^" + re.escape(HALT_PLAN_TRAILER) + r":[ \t]*(?P<v>\S+)[ \t]*$", message, re.M):
        out.add(m.group("v").replace("\\", "/"))
    return frozenset(out)


def _ids(text: str) -> frozenset:
    return frozenset(i.strip() for i in re.split(r"[,\s]+", text) if i.strip())


def parse_checkpoint(message: str) -> Optional[Checkpoint]:
    """The plan, named row ids, gate-owed and stop-rule ids of a checkpoint message; None when
    the subject is not a checkpoint's or the ``Checkpoint-Plan:`` line is absent."""
    subject = CHECKPOINT_SUBJECT.match((message.splitlines() or [""])[0].strip())
    plan = CHECKPOINT_PLAN_LINE.search(message)
    if not subject or not plan:
        return None
    owed = CHECKPOINT_GATE_OWED_LINE.search(message)
    stopped = CHECKPOINT_STOP_RULE_LINE.search(message)
    base = _BASE_LINE.search(message)
    review = _REVIEW_BASE_LINE.search(message)
    return Checkpoint(
        plan.group("v").replace("\\", "/"),
        _ids(subject.group("ids")),
        _ids(owed.group("v")) if owed else frozenset(),
        _ids(stopped.group("v")) if stopped else frozenset(),
        base.group("v") if base else None,
        review.group("v") if review else None,
        bool(_NOT_LANDED_LINE.search(message)),
    )


def is_stamp_commit(message: str) -> bool:
    """True when the message carries a ``Checkpoint-Stamp:`` trailer."""
    return bool(_STAMP_LINE.search(message))


class CheckpointWalk(NamedTuple):
    landed: dict  # {checkpoint sha: frozenset(row ids)}, ids minus that checkpoint's gate-owed and stop-rule
    commits_read: int
    capped: bool  # the cap ended the walk with wanted ids still unfound


_WALK_META_CACHE: dict = {}
_WALK_META_CACHE_MAX = 4096


def _walk_commit_meta(common_dir: Path, sha: str) -> Optional[dict]:
    """``commit_meta`` for a first-parent walk from HEAD, memoized by ``(common_dir, sha)`` (a
    commit's content is its sha; an absent commit is never cached). Callers must not mutate the
    returned dict: one prep-gate call walks the same history once per predecessor plan. Trap: ``commit_meta`` probes every pack
    index (twice) before it tries a loose object, and the recent commits a walk reads are loose, so
    a cold 500-commit walk costs ~450ms on a many-pack repo; a loose read costs ~0.04ms. A
    commit absent loose falls through to ``commit_meta`` unchanged."""
    key = (str(common_dir), sha)
    cached = _WALK_META_CACHE.get(key)
    if cached is not None:
        return cached
    from coordinator_core.git.commit_walk import commit_meta, parse_commit
    from coordinator_core.git.git_objects import _read_loose_object

    obj = _read_loose_object(common_dir, sha)
    meta = parse_commit(obj[1]) if obj is not None and obj[0] == "commit" else commit_meta(common_dir, sha)
    if meta is not None:
        if len(_WALK_META_CACHE) >= _WALK_META_CACHE_MAX:
            _WALK_META_CACHE.pop(next(iter(_WALK_META_CACHE)))
        _WALK_META_CACHE[key] = meta
    return meta


_PROCESS_HALF_SUFFIX = "(process files)"


def is_process_half(subject: str) -> bool:
    """True for the ``(process files)`` child of a split checkpoint. The stamp writer and
    ``emit.resume_landed_state`` share this rule, so the sha a row is stamped with and the sha the
    gate reads for it cannot drift."""
    return subject.strip().endswith(_PROCESS_HALF_SUFFIX)


def without_process_half(subject: str) -> str:
    """``subject`` minus a trailing ``(process files)``, so the last row id parses cleanly."""
    s = subject.strip()
    return s[: -len(_PROCESS_HALF_SUFFIX)].rstrip() if is_process_half(s) else s


_CREATED_LINE = re.compile(r"^created:[ \t]*[\"']?(\d{4})-(\d{2})-(\d{2})", re.M)
_DAY_SECONDS = 86400


def plan_created_floor(plan_text: str) -> Optional[int]:
    """Epoch below which no checkpoint of the plan can exist: its ``created:`` day at
    00:00 UTC minus one day of timezone slack; None when absent or unparseable."""
    head = plan_text.split("\n---", 1)[0] if plan_text.startswith("---") else ""
    m = _CREATED_LINE.search(head)
    if not m:
        return None
    try:
        from datetime import datetime, timezone

        day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(day.timestamp()) - _DAY_SECONDS


def checkpoint_rows_landed(
    worktree_root: Path,
    plan_rel: str,
    start_sha: str,
    wanted: frozenset,
    *,
    floor_epoch: Optional[int] = None,
    cap: int = 500,
    honour_open_runs: bool = False,
) -> CheckpointWalk:
    """Which ``wanted`` rows did same-plan checkpoints land, at which sha, newest first.

    First-parent walk from ``start_sha``, in-process. Stamp commits, other plans'
    checkpoints and process halves are stepped over; a same-plan checkpoint records
    ``(row ids - gate-owed - stop-rule) & remaining`` at its own sha, so the newest landing wins.
    Ends when ``wanted`` is exhausted, below ``floor_epoch``, at ``cap`` commits, or at
    an unreadable or root commit. ``Checkpoint-Base:`` is not a stop.

    ``honour_open_runs`` is for readers outside the run: the walk collects each commit's
    ``Checkpoint-Run-Closed:`` bases as it passes, and a ``Checkpoint-Not-Landed`` checkpoint
    counts only when its base or review base is already collected (the close is newer, so it is
    seen first); otherwise it is stepped over and its rows stay wanted. Run-internal readers
    (resume, refire, re-mint) leave it False.
    """
    empty = CheckpointWalk({}, 0, False)
    if not wanted or not plan_rel or not isinstance(start_sha, str) or not _FULL_SHA.fullmatch(start_sha):
        return empty
    from coordinator_core.git.git_dir import resolve_git_common_dir

    common_dir = resolve_git_common_dir(Path(worktree_root))
    remaining = set(wanted)
    landed: dict = {}
    read = 0
    closed: set = set()
    sha: Optional[str] = start_sha
    while remaining and sha is not None and read < cap:
        meta = _walk_commit_meta(common_dir, sha)
        if meta is None:
            break
        epoch = meta.get("committer_epoch")
        if floor_epoch is not None and epoch is not None and epoch < floor_epoch:
            break
        read += 1
        message = meta.get("message") or ""
        if honour_open_runs:
            closed |= run_closed_bases(message)
        cp = None if is_stamp_commit(message) else parse_checkpoint(message)
        if cp is not None and cp.plan_rel == plan_rel:
            subject = (message.splitlines() or [""])[0].strip()
            ids = (cp.row_ids - cp.gate_owed - cp.stop_rule) & remaining
            if honour_open_runs and cp.not_landed and not ({cp.base, cp.review_base} & closed):
                ids = frozenset()
            if ids and not is_process_half(subject):
                landed[sha] = frozenset(ids)
                remaining -= ids
        parents = meta.get("parents") or []
        sha = parents[0] if parents else None
    return CheckpointWalk(landed, read, bool(remaining) and sha is not None and read >= cap)


_HEAD_WALK_CAP = 500


def rows_landed_at_head(repo_root: Path, plan_rel: str, wanted: frozenset) -> frozenset:
    """The ``wanted`` rows of ``plan_rel`` named by a same-plan checkpoint in HEAD's first-parent
    history, minus gate-owed and stop-rule ids. In-process; empty on an unborn HEAD. The walk
    reads at most ``_HEAD_WALK_CAP`` commits; a checkpoint older than that is not seen, so its
    row stays held (fails closed). A ``Checkpoint-Not-Landed`` checkpoint lands its rows only
    once a newer commit carries ``Checkpoint-Run-Closed:`` for its base."""
    if not wanted or not plan_rel:
        return frozenset()
    from coordinator_core.git.git_state import head_sha

    head = head_sha(repo_root)
    if not head:
        return frozenset()
    floor = None
    try:
        floor = plan_created_floor((Path(repo_root) / plan_rel).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        pass
    walk = checkpoint_rows_landed(
        repo_root, plan_rel, head, frozenset(wanted), floor_epoch=floor, cap=_HEAD_WALK_CAP,
        honour_open_runs=True,
    )
    return frozenset().union(*walk.landed.values())


def rows_delivered_at_head(repo_root: Path, plan_rel: str, row_writes: Mapping[str, tuple]) -> frozenset:
    """The rows of ``plan_rel`` whose every ``writes`` path differs at HEAD from the commit that
    introduced the plan (a path absent there counts as differing): delivery evidence for a
    hand-landing or ``record_halted_run_landed`` commit, which carries no checkpoint trailer.

    ``row_writes`` is ``{row id: writes paths}``; a row with no writes is never delivered.
    In-process, one tree pass for all rows. Fails closed (empty) on an unborn HEAD, a plan
    absent or untracked at HEAD, or an introduction commit older than ``_HEAD_WALK_CAP``.
    Trap: a peer's edit to a row's only writes path reads as delivery; a row with several
    writes needs all of them changed."""
    wanted = {
        rid: tuple(str(p).replace("\\", "/") for p in paths if isinstance(p, str) and p)
        for rid, paths in row_writes.items()
    }
    wanted = {rid: paths for rid, paths in wanted.items() if paths}
    if not wanted or not plan_rel:
        return frozenset()
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.rollback_check import ABSENT, _values_along

    head = head_sha(repo_root)
    if not head:
        return frozenset()
    floor = None
    try:
        floor = plan_created_floor((Path(repo_root) / plan_rel).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        pass
    common_dir = resolve_git_common_dir(Path(repo_root))
    ancestors: list = []
    sha: Optional[str] = head
    closed = False
    while sha is not None and len(ancestors) < _HEAD_WALK_CAP:
        meta = _walk_commit_meta(common_dir, sha)
        if meta is None:
            return frozenset()
        epoch = meta.get("committer_epoch")
        if floor is not None and epoch is not None and epoch < floor:
            closed = True
            break
        ancestors.append((len(ancestors) + 1, sha))
        parents = meta.get("parents") or []
        sha = parents[0] if parents else None
        closed = sha is None
    paths = list(dict.fromkeys([plan_rel, *(p for ps in wanted.values() for p in ps)]))
    series = _values_along(common_dir, ancestors, paths)
    plan_series = [v for _s, v in series[plan_rel]]
    if not plan_series or plan_series[0] is ABSENT or plan_series[0] is None:
        return frozenset()
    base_idx = len(plan_series) - 1
    for i, value in enumerate(plan_series):
        if value is None:
            return frozenset()
        if value is ABSENT:
            base_idx = i - 1
            break
    else:
        if not closed:
            return frozenset()

    def _changed(path: str) -> bool:
        at_head, at_base = series[path][0][1], series[path][base_idx][1]
        return at_head is not ABSENT and at_head is not None and at_head != at_base

    return frozenset(rid for rid, ps in wanted.items() if all(_changed(p) for p in ps))


def inventory_family_landed(
    repo_root: Path,
    inventory_dir_rel: str,
    family_base: str,
    item_rows: Mapping[str, tuple],
    stop_sha: Optional[str],
) -> dict:
    """``{(source plan rel, chunk id): checkpoint sha}`` for the chunks that checkpoints of one
    inventory family landed, newest checkpoint winning.

    One in-process first-parent walk from HEAD, ended at ``stop_sha`` or ``_HEAD_WALK_CAP``. A
    checkpoint counts when its ``Checkpoint-Plan`` is a minted spine
    ``<inventory_dir_rel>/<family_base>[-tN][-pN].spine.md``; ``<item>.<chunk>`` maps through
    ``item_rows[item][0]``, a bare ``<item>`` through ``item_rows[item]``, and an item absent from
    ``item_rows`` is skipped. Gate-owed and stop-rule ids, stamp commits and ``(process files)``
    halves land nothing. Trap: item ids repeat across inventories, so the family regex anchors
    on ``family_base``.
    """
    if not item_rows or not family_base:
        return {}
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha

    head = head_sha(repo_root)
    if not head:
        return {}
    spine = re.compile(
        re.escape(f"{inventory_dir_rel.replace(chr(92), '/').rstrip('/')}/{family_base}")
        + r"(?:-t\d+)?(?:-p\d+)?\.spine\.md"
    )
    common_dir = resolve_git_common_dir(Path(repo_root))
    landed: dict = {}
    read = 0
    sha: Optional[str] = head
    while sha is not None and read < _HEAD_WALK_CAP:
        if stop_sha and sha == stop_sha:
            break
        meta = _walk_commit_meta(common_dir, sha)
        if meta is None:
            break
        read += 1
        message = meta.get("message") or ""
        cp = None if is_stamp_commit(message) else parse_checkpoint(message)
        if cp is not None and spine.fullmatch(cp.plan_rel):
            if not is_process_half((message.splitlines() or [""])[0]):
                for ident in cp.row_ids - cp.gate_owed - cp.stop_rule:
                    item, dot, chunk = ident.partition(".")
                    target = item_rows.get(item)
                    if target is None:
                        continue
                    key = (target[0], chunk) if dot else (target[0], target[1])
                    landed.setdefault(key, sha)
        parents = meta.get("parents") or []
        sha = parents[0] if parents else None
    return landed


def _plan_state(worktree_root: Path, rel: str, sha: str, data: bytes) -> str:
    """``clean`` (tracked at ``sha``, blob identical to ``data``), ``untracked`` (absent at
    ``sha``) or ``dirty`` (differs, or the tree cannot answer)."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit

    common_dir = resolve_git_common_dir(worktree_root)
    blob = _blob_at_commit(common_dir, sha, rel) if _walk_commit_meta(common_dir, sha) else None
    if blob is None:
        return "dirty"
    if blob is ABSENT:
        return "untracked"
    if hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest() == blob:
        return "clean"
    from coordinator_core.git.content_hash import content_matches_index_sha

    return "clean" if content_matches_index_sha(worktree_root, rel, blob) is True else "dirty"


def stamp_checkpoint(
    worktree_root: Path, message: str, sha: str, *, session_id: Optional[str] = None
) -> dict:
    """Stamp the checkpoint's open rows ``coded`` at ``sha``, minus gate-owed and stop-rule rows.

    Returns ``{}`` when nothing flips; else ``{"rows_coded": {plan: [ids]}, "stamp_sha"}``
    (``stamp_sha`` None with ``stamp_skipped`` when the plan stays stamped on disk only: an
    untracked/ignored plan, or a tracked one whose bytes differ from ``sha`` beforehand);
    or ``{"stamp_error"}`` with every plan restored. Never raises. After a landed stamp commit the
    claims on its plan paths are released under ``session_id`` (skipped when it is falsy).
    """
    worktree_root = Path(worktree_root)
    originals: dict = {}
    try:
        cp = parse_checkpoint(message)
        if cp is None:
            return {}
        from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source
        from coordinator_core.ops.dispatch_emit.plan_row_coding import flip_rows_coded, source_rows_by_plan
        from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel
        from coordinator_core.session.claimed_write import replace_text

        ids = sorted(cp.row_ids - cp.gate_owed - cp.stop_rule)
        by_plan = source_rows_by_plan(worktree_root, cp.plan_rel, ids)
        rows_coded: dict = {}
        state: dict = {}
        for plan_rel in sorted(by_plan):
            original = read_rel(worktree_root, plan_rel)
            if original is None:
                continue
            updated, flipped = flip_rows_coded(original, set(by_plan[plan_rel]), sha)
            if not flipped:
                continue
            invalid = check_plan_tasks_source(updated)
            if invalid is not None and check_plan_tasks_source(original) is None:
                raise ValueError(f"{plan_rel}: stamped spine fails schema: {invalid}")
            raw = (worktree_root / plan_rel).read_bytes()
            state[plan_rel] = _plan_state(worktree_root, plan_rel, sha, raw)
            replace_text(worktree_root / plan_rel, updated)
            originals[plan_rel] = original
            rows_coded[plan_rel] = flipped
        if not rows_coded:
            return {}

        commit_rels = [p for p in sorted(rows_coded) if state[p] == "clean"]
        out: dict = {"rows_coded": rows_coded, "stamp_sha": None}
        skipped = {state[p] for p in rows_coded} - {"clean"}
        if skipped:
            out["stamp_skipped"] = "plan_dirty" if "dirty" in skipped else "plan_untracked"
        if not commit_rels:
            return out

        from functools import partial

        from coordinator_core.git.commit import CommitRefused, commit_paths, hash_worktree_blobs_via_spawn
        from coordinator_core.git.commit_trailers import apply_missing_trailers
        from coordinator_core.git.ref_cas_retry import REF_CAS_ATTEMPTS, is_ref_moved

        n = sum(len(rows_coded[p]) for p in commit_rels)
        body = f"mark {n} rows coded ({sha[:7]})\n\n{STAMP_TRAILER}: {sha}"
        body = apply_missing_trailers(body, worktree_root, commit_rels, session_id_override=session_id)
        # Trap: unlike terminal_commit and land_run this loop does not refuse when the peer commit
        # touched ``commit_rels``; ``ref_cas_retry.retry_ref_moved`` would add that check.
        for attempt in range(REF_CAS_ATTEMPTS):
            try:
                outcome = commit_paths(
                    worktree_root, commit_rels, body, prefer_deliberate_stage=True,
                    blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree_root),
                )
                break
            except CommitRefused as exc:
                if not is_ref_moved(exc) or attempt == REF_CAS_ATTEMPTS - 1:
                    raise
        out["stamp_sha"] = outcome.sha
        if outcome.sha:
            from coordinator_core.session.scope import release_committed_claims_or_retain

            release_committed_claims_or_retain(worktree_root, commit_rels, session_id, "checkpoint_stamp")
        return out
    except Exception as exc:  # noqa: BLE001 -- the checkpoint commit stands; the stamp is surfaced
        try:
            from coordinator_core.session.claimed_write import replace_text

            for rel, text in originals.items():
                replace_text(worktree_root / rel, text)
        except Exception:  # noqa: BLE001
            pass
        return {"stamp_error": repr(exc)}
