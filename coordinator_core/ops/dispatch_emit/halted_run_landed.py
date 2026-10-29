"""``dispatch.record_halted_run_landed``: records a halted run as landed at a verified commit.

Params: ``run_id`` (string, required: the halted run), ``sha`` (string: one commit sha), ``shas``
(list of strings: further commit shas, unioned with ``sha``; at least one of the two is required),
``session_id`` (string, optional uuid: the calling session, used to find the run's review sidecar),
``in_run`` (bool, optional: close the calling run itself as already delivered; admits a ``running``
sibling status or a solo run with no halt marker, and refuses unless every manifest
``review_declared_paths`` entry has no delta between the manifest ``head_sha`` and HEAD and the
worktree; a marker-less run writes nothing and replies the not-applicable close).
Each sha is a lowercase hex commit sha, full or abbreviated to at least 4
characters; an abbreviation must name exactly one commit and the reply and
recorded marker carry the full 40-hex sha. The run's sibling status must read
``halted`` (or already ``landed`` at the same sha) and ``sha`` must be HEAD or an ancestor of HEAD,
merge-reached included. A solo run's marker may also read ``resumed`` when no live run's
ledger names it as ``resumed_from``. The write and the release of ``after`` successors are
``run_close.sibling_release``'s; this op never writes the sibling status file itself. A solo run has
no sibling status: its halt marker (``halted_run``) is read and rewritten to ``landed`` instead, and
the reply names that tracked path as ``recorded``; a sibling run's still-``halted`` marker is flipped
the same way. The op never commits: commit existence is an in-process object read, and ancestry and
abbreviation resolution are at most two batched git spawns; the close legs (``CLOSE_LEGS``) are
queued in a close-job record paid by one detached launch (``_launch``), never inline. The record is written only when the marker names a ``plan`` and no
close-job record exists yet.
Reply: ``{ok, run_id, sha, written, shas?, covers?, siblings?, recorded?, close, blocking_rows}`` or
``{ok: false, error}``; ``blocking_rows`` is the marker's ``blocking`` register-row ids (not paths; the
withheld paths each sha carried are under ``covers``). When ``written``,
``shas`` is every verified full sha (``sha`` plus ``shas`` params, deduplicated) and ``covers`` maps
each to the withheld paths it changed (``{}`` when the run withholds nothing); ``sha`` is the newest.
When the halt marker records withheld paths (``run_artifacts.withheld``), ``sha`` must have changed
every one of them versus its first parent, or several shas must together (each changing at least one),
so recording landed cannot release the withheld-commit guard for a commit that never carried them; a
later follow-up commit that changed them qualifies. A withheld path a revision voided (``_voided_paths``) is
not owed and is echoed under ``voided``; so is a withheld path with no delta (same entry in the marker's
``run_base_sha`` tree and HEAD, working file equal to HEAD's blob; ``_clean_at_head``). The path match is case-insensitive (directory
components too) and a path counts as changed when either side of a rename carries its casefolded
name; ``covers`` keeps the marker's spelling. A set records its newest sha.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

OP_NAME = "dispatch.record_halted_run_landed"

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_SHA_PREFIX = re.compile(r"[0-9a-f]{4,40}")


#: close_background.BACKGROUND_LEGS order; review-stamp precedes plan-reconciliation, which fails
#: "no review stamp minted" without it.
CLOSE_LEGS = (
    "review-stamp", "coded-stamp", "sizing-ship", "plan-reconciliation", "deliverable-cascade", "close-commit",
)
_CLOSE_JOB_WINDOW_MINUTES = 5
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _refuse(error: str) -> dict:
    return {"ok": False, "error": error}


def _launch(root: Path, run_id: str) -> bool:
    """Start the detached close job for *run_id*; False when it did not start."""
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

    engine = Path(__file__).resolve().parents[3]
    script = str(engine / "coordinator_core" / "ops" / "ceremony" / "close_background.py")
    params = json.dumps({"claimed_batons": [], "source_memo": None})
    return spawn_detached(
        str(engine), script, [run_id, "--root", str(root), "--params", params], site_free=True,
    )


def _withheld(marker: dict) -> list:
    """The marker's ``blocking`` register-row ids that are strings."""
    blocking = marker.get("blocking")
    return [b for b in blocking if isinstance(b, str)] if isinstance(blocking, list) else []


def _review_record(root: Path, run_id: str, session_id: Optional[str]) -> Optional[str]:
    """Repo-relative posix path of the run's review sidecar, or None when none resolves.

    Candidates, first existing file wins: ``inline_review.integration.sidecar`` (relative, posix, no
    ``..``, under *root*), then ``inline_review.integration_stem`` (a bare stem) at
    ``.coordinator-local/subagent-share/<session_id>/<stem>.md``. The stem carries no session, and
    *session_id* is the session calling this op, which need not be the one whose wave review wrote the
    sidecar; a mismatch resolves None, and the caller settles ``no-review-outcome``. That is the honest
    outcome, not a bug. One json read, at most two stats.
    """
    from coordinator_core.ops.dispatch_emit import ask_contract

    outcome = ask_contract.read_run_outcome(root, run_id)
    inline_review = outcome.get("inline_review") if outcome else None
    if not isinstance(inline_review, dict):
        return None
    candidates: list[Path] = []
    integration = inline_review.get("integration")
    sidecar = integration.get("sidecar") if isinstance(integration, dict) else None
    if isinstance(sidecar, str) and sidecar and "\\" not in sidecar:
        parts = sidecar.split("/")
        if not sidecar.startswith("/") and ".." not in parts and not re.match(r"[A-Za-z]:", sidecar):
            candidates.append(root / sidecar)
    stem = inline_review.get("integration_stem")
    if (
        session_id and isinstance(stem, str) and stem and stem != ".." and "/" not in stem and "\\" not in stem
    ):
        candidates.append(root / ".coordinator-local" / "subagent-share" / session_id / f"{stem}.md")
    for path in candidates:
        try:
            rel = path.resolve().relative_to(root.resolve())
            if path.is_file():
                return rel.as_posix()
        except (OSError, ValueError):
            continue
    return None


def _queue_close(root: Path, run_id: str, sha: str, marker: Optional[dict], session_id: Optional[str]) -> dict:
    """The reply's ``close`` and ``withheld`` for a run just recorded landed at *sha*.

    Writes the close-job record and launches it once; a failed write or launch reports
    ``launched: false`` and never fails the op. With a resolvable review record the review-stamp leg
    is queued and handed it; otherwise that leg is settled ``review skipped: no-review-outcome``.
    """
    from datetime import datetime, timedelta

    from coordinator_core.ops.dispatch_emit import run_close

    marker = marker if isinstance(marker, dict) else {}
    withheld = _withheld(marker)
    plan = marker.get("plan")
    if not isinstance(plan, str) or not plan:
        return {"close": {"status": "not-applicable", "detail": "halt record names no plan"}, "blocking_rows": withheld}
    base = marker.get("run_base_sha")
    deadline = datetime.now().astimezone() + timedelta(minutes=_CLOSE_JOB_WINDOW_MINUTES)
    try:
        record = _review_record(root, run_id, session_id)
        if record is not None:
            queued, settled, inputs = CLOSE_LEGS, None, {"review_record": record}
        else:
            queued = tuple(leg for leg in CLOSE_LEGS if leg != "review-stamp")
            settled = {"review-stamp": run_close.review_skipped("no-review-outcome")}
            inputs = None
        job = run_close.write_close_job(
            root, run_id, queued, deadline,
            session_id=session_id,
            plan_rel=plan,
            terminal_sha=sha,
            run_base_sha=base if isinstance(base, str) and _FULL_SHA.fullmatch(base) else None,
            settled=settled,
            inputs=inputs,
        )
    except Exception as exc:  # noqa: BLE001 -- the landed write stands; the close reports itself
        return {
            "close": {"job": None, "launched": False, "queued": list(CLOSE_LEGS), "error": f"{type(exc).__name__}: {exc}"},
            "blocking_rows": withheld,
        }
    launch_error = None
    try:
        launched = bool(_launch(root, run_id))
    except Exception as exc:  # noqa: BLE001 -- the job record stands; the reply reports launched: false
        launched = False
        launch_error = f"{type(exc).__name__}: {exc}"
    close = {"job": job, "launched": launched, "queued": list(queued)}
    if launch_error is not None:
        close["error"] = launch_error
    return {"close": close, "blocking_rows": withheld}


def _close_landed(root: Path, run_id: str, sha: str, session_id: Optional[str]) -> dict:
    """Flip a still-``halted`` or ``resumed`` marker to landed, then queue the close unless a job record exists."""
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, run_close

    out: dict = {}
    try:
        record_dir = ask_contract.record_dir(root, run_id)
    except ValueError:
        return {"close": {"status": "not-applicable", "detail": "no halt record"}, "blocking_rows": []}
    marker = halted_run._read(record_dir)
    if marker is not None and marker.get("status") in (halted_run.STATUS_HALTED, halted_run.STATUS_RESUMED):
        if halted_run.mark_landed(record_dir, sha):
            out["recorded"] = (record_dir / halted_run.HALTED_FILE).relative_to(root).as_posix()
    if marker is None:
        return {**out, "close": {"status": "not-applicable", "detail": "no halt record"}, "blocking_rows": []}
    if run_close.resolve_close_job_stem(root, run_id) is not None:
        return {**out, "close": {"status": "not-applicable", "detail": "close job already recorded"}, "blocking_rows": _withheld(marker)}
    return {**out, **_queue_close(root, run_id, sha, marker, session_id)}


def _live_successor(root: Path, run_id: str) -> Optional[str]:
    """The id of a live run whose ledger resumes *run_id*, else None; scans both run record roots once, reading one ledger per run dir (cost grows with the run-dir count)."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl, run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT

    for base in (RECORD_DIR_ROOT, RUN_DIR_ROOT):
        try:
            candidates = sorted(p for p in (root / base).iterdir() if p.is_dir() and p.name != run_id)
        except OSError:
            continue
        for cand in candidates:
            ledger = rl.read_run(cand)
            if (
                ledger and ledger.get(rl.RESUMED_FROM_FIELD) == run_id
                and run_token.is_live(root, cand.name) and not _terminal(cand)
            ):
                return cand.name
    return None


def _terminal(cand: Path) -> bool:
    """True when the run dir records the run landed or halted: its token's TTL is not liveness then."""
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE

    marker = halted_run._read(cand)
    if marker is not None and marker.get("status") == halted_run.STATUS_LANDED:
        return True
    try:
        stage = json.loads((cand / RUN_STAGE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(stage, dict) and stage.get("stage") in ("halted", "landed")


def _casefolded_entries(load_dir, root_tree: str, path: str) -> Optional[frozenset]:
    """Every tree entry under *root_tree* whose path casefolds to *path*, as ``{(exact_path, mode, oid)}``.

    *load_dir* maps a tree sha to its parsed entries (None when unreadable). Directory components
    match casefolded too; the exact spelling is in the key so a case-only rename, which keeps the
    blob oid, still differs. None when any tree on the way is unreadable.
    """
    parts = path.casefold().split("/")
    frontier = [("", root_tree)]
    found: set = set()
    for i, part in enumerate(parts):
        nxt = []
        for prefix, tree_sha in frontier:
            entries = load_dir(tree_sha)
            if entries is None:
                return None
            for name, (mode, oid) in entries.items():
                if name.casefold() != part:
                    continue
                full = f"{prefix}/{name}" if prefix else name
                if i == len(parts) - 1:
                    found.add((full, mode, oid))
                elif mode == 0o40000:
                    nxt.append((full, oid))
        frontier = nxt
    return frozenset(found)


_VOID_WORDING = re.compile(r"\bvoid\b|never added|remove from this run's diff", re.IGNORECASE)
_CLOSED_ROW = ("voided", "wont_do")


def _voided_paths(root: Path, marker: Optional[dict], withheld: list) -> set:
    """The *withheld* paths a revision voided, so the landing record does not owe them.

    A path is voided when every plan row declaring it is closed ``voided``/``wont_do``, or when a live
    revision-origin register row of the marker's sizing carries void wording and names the path's file
    name or, for a hyphenated or underscored directory, its parent directory. An unreadable plan or
    sizing voids nothing, so the landing check stays strict.

    Trap: void wording and the name need only co-occur in one revision row's text, and a hyphenated or
    underscored parent directory name voids every withheld file under it, so a revision that merely
    mentions such a directory beside "void" releases all of its files.
    """
    if not isinstance(marker, dict):
        return set()
    voided: set = set()
    plan = marker.get("plan")
    if isinstance(plan, str) and plan:
        try:
            from coordinator_core.frontmatter.body_blocks import LocateStatus
            from coordinator_core.ops.plan_tasks_render import load_rows

            loaded = load_rows((root / plan).read_text(encoding="utf-8"))
            rows = [r for r in loaded.rows if isinstance(r, dict)] if loaded.status is LocateStatus.LOCATED else []
        except Exception:  # noqa: BLE001 -- an unreadable plan voids nothing
            rows = []
        for p in withheld:
            declaring = []
            for r in rows:
                raw = r.get("writes") or []
                if p.casefold() in {str(w).replace("\\", "/").casefold() for w in ([raw] if isinstance(raw, str) else raw)}:
                    declaring.append(r.get("disposition"))
            if declaring and all(d in _CLOSED_ROW for d in declaring):
                voided.add(p)
    sizing = marker.get("sizing_rel")
    if isinstance(sizing, str) and sizing:
        try:
            import yaml

            from coordinator_core.ops import requirement_register as rr
            from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

            reg = (yaml.safe_load((root / sizing).read_text(encoding="utf-8")) or {}).get("requirement_register") or {}
            texts = [
                str(r.get(rr.ROW_TEXT) or "").casefold() for r in reg.get("rows") or []
                if isinstance(r, dict)
                and rl._origin(r.get(rr.ROW_ANCHOR))[0] == rl.ORIGIN_REVISION
                and r.get(rr.ROW_STATUS) not in (rr.STATUS_DEFERRED, rr.STATUS_WAIVED)
                and _VOID_WORDING.search(str(r.get(rr.ROW_TEXT) or ""))
            ]
        except Exception:  # noqa: BLE001 -- an unreadable sizing voids nothing
            texts = []
        for p in withheld:
            parts = p.casefold().split("/")
            names = [parts[-1]] + ([parts[-2]] if len(parts) > 1 and re.search(r"[-_]", parts[-2]) else [])
            if any(re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w-])", t) for t in texts for n in names):
                voided.add(p)
    return voided


def _clean_at_head(root: Path, common, load_dir, paths: list, base: Optional[str]) -> set:
    """The *paths* with no delta: identical in the run's base tree and HEAD, and the working-tree file hashes to HEAD's blob (or is absent from both).

    In-process reads only. No recorded *base*, an unreadable tree, a symlink or a case-variant tree
    entry leaves the path owed.
    """
    import hashlib

    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_state import head_sha

    head = head_sha(root)
    meta = commit_meta(common, head) if head else None
    base_meta = commit_meta(common, base) if base else None
    if not meta or not meta.get("tree") or not base_meta or not base_meta.get("tree"):
        return set()
    clean: set = set()
    for p in paths:
        entries = _casefolded_entries(load_dir, meta["tree"], p)
        if entries is None or len(entries) > 1 or entries != _casefolded_entries(load_dir, base_meta["tree"], p):
            continue
        target = root / p
        try:
            if not entries:
                if not target.exists() and not target.is_symlink():
                    clean.add(p)
                continue
            (exact, mode, oid), = entries
            if exact != p or mode & 0o170000 != 0o100000 or not target.is_file() or target.is_symlink():
                continue
            data = target.read_bytes()
            if hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == oid:
                clean.add(p)
        except OSError:
            continue
    return clean


def _carries_withheld(root: Path, run_id: str, shas: list) -> tuple[Optional[str], dict, list]:
    """``(None, covers, voided)`` when *shas* together changed every owed path the run's halt marker withholds, else ``(refusal, {}, [])``.

    Each sha is compared with its first parent; the union of changed withheld paths must cover the
    withheld list less the paths a revision voided (``voided``, ``_voided_paths``), and every sha must
    change at least one owed path. ``covers`` maps each sha to the owed paths it changed (empty for a
    run withholding nothing or with no marker, which passes). An unreadable commit or tree refuses.
    """
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.git_state import _parse_tree_entries
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run

    try:
        record_dir = ask_contract.record_dir(root, run_id)
        withheld = sorted(halted_run.halted_withheld_strict(record_dir))
    except halted_run.UnreadableHaltRecord as exc:
        return f"{exc.reason}: halt marker unreadable; the run's withheld paths are unknown", {}, []
    except ValueError:
        return None, {}, []
    if not withheld:
        return None, {}, []
    voided = _voided_paths(root, halted_run._read(record_dir), withheld)
    voided_list = [p for p in withheld if p in voided]
    withheld = [p for p in withheld if p not in voided]
    if not withheld:
        return None, {}, voided_list
    common = resolve_git_common_dir(root)
    dirs: dict = {}

    def _dir(tree_sha: str):
        if tree_sha not in dirs:
            got = read_object(common, tree_sha)
            dirs[tree_sha] = _parse_tree_entries(got[1]) if got and got[0] == "tree" else None
        return dirs[tree_sha]

    covered: set = set()
    covers: dict = {}
    for sha in shas:
        meta = commit_meta(common, sha)
        parents = meta.get("parents") if meta else None
        parent_meta = commit_meta(common, parents[0]) if parents else None
        if meta is None or not meta.get("tree") or (parents and (parent_meta is None or not parent_meta.get("tree"))):
            return f"{sha} or its parent is unreadable; retry", {}, []
        changed = set()
        for p in withheld:
            after = _casefolded_entries(_dir, meta["tree"], p)
            before = _casefolded_entries(_dir, parent_meta["tree"], p) if parent_meta else frozenset()
            if after is None or before is None:
                return f"{sha} tree is unreadable; retry", {}, []
            if after != before:
                changed.add(p)
        if len(shas) > 1 and not changed:
            return f"{sha} changed none of the run's withheld paths; drop it from shas", {}, []
        covered |= changed
        covers[sha] = [p for p in withheld if p in changed]
    unchanged = [p for p in withheld if p not in covered]
    if unchanged:
        clean = _clean_at_head(root, common, _dir, unchanged, halted_run.halted_run_base(record_dir))
        voided_list += [p for p in unchanged if p in clean]
        unchanged = [p for p in unchanged if p not in clean]
    if unchanged:
        more = f" (+{len(unchanged) - 1} more)" if len(unchanged) > 1 else ""
        who = shas[0] if len(shas) == 1 else "shas together"
        return (
            f"{who} did not change withheld {unchanged[0]}{more}; "
            "pass the commit(s) that landed the run's withheld paths"
        ), {}, []
    return None, covers, voided_list


def _verify(root: Path, run_id: str, shas: list) -> tuple[Optional[str], Optional[str], dict]:
    """``(newest_full_sha, None, echo)`` when *shas* resolve, are reachable and cover the withheld paths, else ``(None, refusal, {})``.

    ``echo`` is ``{"shas": every verified full sha, "covers": {sha: withheld paths it changed}}``.
    """
    from coordinator_core import git_ancestry
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha

    common = resolve_git_common_dir(root)
    for sha in shas:
        if _FULL_SHA.fullmatch(sha) and commit_meta(common, sha) is None:
            return None, f"{sha} is not a commit in this repo; pass the landing commit's full sha", {}
    head = head_sha(root)
    if not head:
        return None, "HEAD is unreadable; retry", {}
    abbreviated = [s for s in shas if not _FULL_SHA.fullmatch(s)]
    resolved, problem = git_ancestry.resolve_commit_prefixes(abbreviated, cwd=str(root))
    if problem:
        return None, problem, {}
    full = list(dict.fromkeys(resolved.get(s, s) for s in shas))
    read_ok, reached = git_ancestry.ancestors_of(full, head, cwd=str(root))
    if not read_ok:
        return None, "ancestry of the sha could not be read; retry", {}
    for sha in full:
        if sha not in reached:
            return None, f"{sha} is not an ancestor of HEAD; pass the commit that landed the work", {}
    problem, covers, voided = _carries_withheld(root, run_id, full)
    if problem:
        return None, problem, {}
    echo = {"shas": full, "covers": covers, **({"voided": voided} if voided else {})}
    if len(full) == 1:
        return full[0], None, echo
    newest = full[0]
    for sha in full[1:]:
        if _newer(common, sha, newest):
            newest = sha
    return newest, None, echo


_TIE_WALK_CAP = 256


def _newer(common, a: str, b: str) -> bool:
    """True when commit *a* is newer than *b*: a later committer epoch, or on a tie *a* descends from *b*.

    The tie walk is an in-process parent walk pruned below *b*'s epoch and capped at ``_TIE_WALK_CAP`` commits;
    a walk that hits the cap, or finds no descent, answers False, so an equal-epoch pair deeper than the cap
    keeps the caller's earlier pick.
    """
    from coordinator_core.git.commit_walk import commit_meta

    meta_a, meta_b = commit_meta(common, a) or {}, commit_meta(common, b) or {}
    epoch_a, epoch_b = meta_a.get("committer_epoch") or 0, meta_b.get("committer_epoch") or 0
    if epoch_a != epoch_b:
        return epoch_a > epoch_b
    seen, stack = {a}, list(meta_a.get("parents") or [])
    while stack and len(seen) < _TIE_WALK_CAP:
        sha = stack.pop()
        if sha == b:
            return True
        if sha in seen:
            continue
        seen.add(sha)
        meta = commit_meta(common, sha) or {}
        if (meta.get("committer_epoch") or 0) >= epoch_b:
            stack.extend(meta.get("parents") or [])
    return False


def _shas_param(params: dict) -> tuple[list, Optional[str]]:
    """``(shas, None)`` from ``sha`` and/or ``shas`` params, else ``([], refusal)``."""
    shas: list = []
    if params.get("sha") is not None:
        shas.append(params["sha"])
    extra = params.get("shas")
    if extra is not None:
        if not isinstance(extra, list):
            return [], f"{OP_NAME} requires shas to be a list"
        shas.extend(extra)
    if not shas or any(not isinstance(s, str) or not _SHA_PREFIX.fullmatch(s) for s in shas):
        return [], f"{OP_NAME} requires a lowercase hex sha of 4 to 40 characters"
    return list(dict.fromkeys(shas)), None


def _in_run_unclean(root: Path, run_id: str) -> Optional[str]:
    """None when every ``review_declared_paths`` entry of the run manifest has no delta between the manifest's ``head_sha`` and HEAD and the worktree; else the refusal.

    An unreadable manifest, a missing ``head_sha`` or an empty write set refuses: nothing proves the run delivered nothing.
    """
    import json as _json

    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.git_state import _parse_tree_entries
    from coordinator_core.ops.dispatch_emit import ask_contract

    try:
        doc = _json.loads((ask_contract.record_dir(root, run_id) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"{run_id} has no readable run manifest; in_run cannot prove the write set is clean"
    base = doc.get("head_sha") if isinstance(doc, dict) else None
    raw = doc.get("review_declared_paths") if isinstance(doc, dict) else None
    paths = [p.replace("\\", "/") for p in raw if isinstance(p, str) and p] if isinstance(raw, list) else []
    if not isinstance(base, str) or not _FULL_SHA.fullmatch(base) or not paths:
        return f"{run_id} manifest names no head_sha or no review_declared_paths; in_run cannot prove the write set is clean"
    common = resolve_git_common_dir(root)
    dirs: dict = {}

    def _dir(tree_sha: str):
        if tree_sha not in dirs:
            got = read_object(common, tree_sha)
            dirs[tree_sha] = _parse_tree_entries(got[1]) if got and got[0] == "tree" else None
        return dirs[tree_sha]

    clean = _clean_at_head(root, common, _dir, paths, base)
    dirty = [p for p in paths if p not in clean]
    if dirty:
        more = f" (+{len(dirty) - 1} more)" if len(dirty) > 1 else ""
        return f"{run_id} write set is not clean at HEAD: {dirty[0]}{more} differs from the run base; the work is not already delivered"
    return None


def _record_solo(
    root: Path, run_id: str, shas: list, session_id: Optional[str] = None, in_run: bool = False,
) -> dict:
    """Solo run (no sibling status): record landed on its halt marker; the caller commits ``recorded``.

    ``in_run`` admits a marker-less run: it verifies the sha and the clean write set, writes nothing and
    replies ``_close_landed``'s not-applicable close.
    """
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run

    try:
        record_dir = ask_contract.record_dir(root, run_id)
    except ValueError as exc:
        return _refuse(str(exc))
    try:
        marker = halted_run.read_strict_run(record_dir)
    except halted_run.UnreadableHaltRecord as exc:
        return _refuse(f"{exc.reason}: halt marker unreadable; {run_id} is not recorded landed")
    if marker is None and in_run:
        sha, problem, echo = _verify(root, run_id, shas)
        problem = problem or _in_run_unclean(root, run_id)
        if problem:
            return _refuse(problem)
        return {"ok": True, "run_id": run_id, "sha": sha, "written": True, **echo, **_close_landed(root, run_id, sha, session_id)}
    if marker is None:
        return _refuse(f"{run_id} has no sibling status or halt record; it never ran")
    status = marker.get("status")
    if status == halted_run.STATUS_LANDED:
        landed = marker.get("sha")
        if isinstance(landed, str) and any(landed.startswith(s) for s in shas):
            return {
                "ok": True, "run_id": run_id, "sha": landed, "written": False,
                **_close_landed(root, run_id, landed, session_id),
            }
        return _refuse(f"{run_id} already landed at {marker.get('sha')}; nothing to record")
    if status == halted_run.STATUS_RESUMED:
        successor = _live_successor(root, run_id)
        if successor:
            return _refuse(f"{run_id} is resumed and its successor {successor} is still live; wait for it or stop it")
    elif status != halted_run.STATUS_HALTED:
        return _refuse(f"{run_id} is {status}; only a halted or resumed run is recorded landed")
    sha, problem, echo = _verify(root, run_id, shas)
    if problem:
        return _refuse(problem)
    if not halted_run.mark_landed(record_dir, sha):
        return _refuse(f"{run_id} halt marker changed under the write; retry")
    recorded = (record_dir / halted_run.HALTED_FILE).relative_to(root).as_posix()
    return {
        "ok": True, "run_id": run_id, "sha": sha, "written": True, **echo, "recorded": recorded,
        **_queue_close(root, run_id, sha, marker, session_id),
    }


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.record_halted_run_landed": verify the sha, then close the run as landed."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
    from coordinator_core.ops.dispatch_emit import run_close

    if repo_root is None:
        return _refuse(f"{OP_NAME} requires repo_root")
    params = params if isinstance(params, dict) else {}
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id or not safe_id(run_id):
        return _refuse(f"{OP_NAME} requires a safe string run_id")
    shas, problem = _shas_param(params)
    if problem:
        return _refuse(problem)
    in_run = params.get("in_run")
    if in_run is not None and not isinstance(in_run, bool):
        return _refuse(f"{OP_NAME} requires in_run to be a boolean")

    sid = params.get("session_id")
    session_id = sid if isinstance(sid, str) and _UUID.fullmatch(sid) else None

    root = main_worktree_root(Path(repo_root))
    try:
        prior = json.loads((root / contract.status_rel(run_id)).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _record_solo(root, run_id, shas, session_id, bool(in_run))
    except (OSError, ValueError):
        return _refuse(f"{run_id} sibling status is unreadable")
    status = prior.get("status") if isinstance(prior, dict) else None
    if status == contract.STATUS_LANDED:
        landed = prior.get("sha")
        if isinstance(landed, str) and any(landed.startswith(s) for s in shas):
            return {
                "ok": True, "run_id": run_id, "sha": landed, "written": False,
                **_close_landed(root, run_id, landed, session_id),
            }
        return _refuse(f"{run_id} already landed at {prior.get('sha')}; nothing to record")
    if status != contract.STATUS_HALTED and not (in_run and status == contract.STATUS_RUNNING):
        return _refuse(f"{run_id} is {status}, not halted; only a halted run is recorded landed")

    sha, problem, echo = _verify(root, run_id, shas)
    if problem:
        return _refuse(problem)
    if in_run:
        problem = _in_run_unclean(root, run_id)
        if problem:
            return _refuse(problem)
    reply = run_close.sibling_release(root, run_id, sha)
    out: dict = {"ok": True, "run_id": run_id, "sha": sha, "written": True, **echo}
    if reply is not None:
        out["siblings"] = reply
    out.update(_close_landed(root, run_id, sha, session_id))
    return out
