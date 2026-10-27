"""Review stamps and fingerprints for dispatch.terminal_commit: recorded reviews, reviewer-applied fixes, post-checkpoint edits."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence, Optional
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
) -> dict:
    """``{path: sha256}`` the run's expectation for the commit's candidate paths; one batched sink
    read. A session event older than ``run_started_at`` is another run's touch and is no expectation
    of this run: the path falls through to the review hash, else carries no entry. Per path the newer of the session's last recorded write and the run's newest review
    (``reviewed`` -> sha256 of the bytes it read, ``reviewed_at`` epoch seconds) supplies the hash;
    an exact tie goes to the session write. When the review wave applied fixes
    (``fixes_integrated_at``: epoch the wave's integration was recorded), a reviewed path whose
    bytes were last written no later than that moment, after the newest session write, is declared
    at those bytes; a later change keeps the older expectation and refuses. A path the wave's own
    record lists (``wave_paths``) counts as reviewed when no stored hash names it. A path whose
    newest session event postdates the review (``fixes_integrated_at`` when set, else
    ``reviewed_at``) and whose file mtime is no earlier than that event is declared at its current
    bytes: the run left them after review; a peer rewrite after that write is not detected. Candidates are DONE-chunk and run-owned paths; a live
    peer's claim on either already refused the call upstream, so no contest check runs here.
    ``review_events`` (``{path: (sha256, epoch)}``, ``_review_write_events``) is a review fix-pass
    agent's own newest write; it supplies the hash for a path whose session event is older or absent,
    so the run's own fix-pass write is the expectation and a later foreign edit still refuses.
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
    return out


def _review_write_events(worktree_root: Path, review_writes: Optional[dict]) -> dict:
    """``{path: (sha256, epoch)}`` of the newest hashed write event, in the sink of the agent
    ``review_writes`` names for that path, per path of ``review_writes``; one sink walk per
    distinct agent. A path whose agent's newest write is hashless, or whose sink is unreadable,
    has no entry."""
    if not review_writes:
        return {}
    from coordinator_core.session import core, touch_record

    hub = core.sessions_dir(str(worktree_root))
    if not hub:
        return {}
    by_agent: dict = {}
    for path, agent_id in review_writes.items():
        by_agent.setdefault(agent_id, set()).add(path)
    out: dict = {}
    for agent_id, paths in by_agent.items():
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
            continue
        out.update({p: v for p, v in newest.items() if v[0]})
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
    listed = inline_review.get("wave_sidecar_paths")
    for w in listed if isinstance(listed, list) else []:
        if isinstance(w, str) and w:
            done_at, fixed = _reviewer_applied_fixes(worktree_root / w)
            if done_at is not None:
                wave_stamps.append(done_at)
                paths.update(fixed)
    if stamps:
        at, newest = max(stamps, key=lambda s: s[0])
        paths.update(_wave_slice_files(newest))
    elif wave_stamps:
        at = max(wave_stamps)
    else:
        return None, frozenset()
    return at, frozenset(paths)


_VERIFIED_AT_RE = re.compile(r"^findings_ledger:.*?\bverified_at:\s*['\"]?([0-9T:.\-]+Z?)", re.MULTILINE)

_LEDGER_BLOCK_RE = re.compile(r"^## Findings Ledger\b.*?^```json\s*\n(.*?)\n```", re.MULTILINE | re.DOTALL)


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
    block = _LEDGER_BLOCK_RE.search(text)
    try:
        rows = json.loads(block.group(1)) if block else []
    except ValueError:
        rows = []
    fixed = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("verified") is not True:
            continue
        if row.get("status") in ("suspended", "rejected") or not (row.get("before") or row.get("after")):
            continue
        f = row.get("file")
        if isinstance(f, str) and f:
            fixed.add(f.replace("\\", "/"))
    return done_at, frozenset(fixed)


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


def _stage_returns(inline_review: dict) -> Optional[dict]:
    """The run record's stage-return fields out of the digest's
    ``inline_review``, or ``None`` when the digest predates them (no ``prep``
    block). Maps the digest's names onto the record keys ``review_stamp.mint``
    reads."""
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
    """Repo-relative paths the review's test-runner or reverify record lists as ``edited`` or
    ``created``: files the run's review changed, including ones an earlier checkpoint of this run
    already committed and this fire's chunks no longer declare. Absolute and ``..`` paths are skipped."""
    found: set = set()
    if isinstance(inline_review, dict):
        for key in ("tests", "reverify"):
            block = inline_review.get(key)
            if not isinstance(block, dict):
                continue
            for field in ("edited", "created"):
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


def _sole_run_review_writes(
    worktree_root: Path, session_id: Optional[str], run_id: Optional[str], inline_review: object
) -> frozenset:
    """Normalised in-worktree paths the session's review agents wrote while this run is the only
    one the session holds; a review write outside every declared path lands or halts instead of
    staying untracked."""
    from coordinator_core.ops.dispatch_emit import review_edit_source, review_edits
    from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

    try:
        raw = review_edit_source.sole_run_attested_writes(
            worktree_root, session_id, run_id, _tc._review_since(worktree_root, run_id, inline_review)
        )
    except OSError:  # only an unreadable store attributes nothing; any other error is a bug and propagates
        return frozenset()
    normed = (review_edits.norm(p) for p in raw)
    return frozenset(p for p in normed if not review_edits.out_of_worktree(p))


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
