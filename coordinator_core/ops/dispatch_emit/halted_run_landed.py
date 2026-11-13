"""``dispatch.record_halted_run_landed``: records a halted run as landed at a verified commit.

Params: ``run_id`` (string, required: the halted run), ``sha`` (string: one commit sha), ``shas``
(list of strings: further commit shas, unioned with ``sha``; at least one of the two is required),
``met_by`` (object, optional: ``{blocking row id: sha}``; each sha an ancestor-or-equal of HEAD and equal to or after the
halt commit, else the op refuses naming the row; recorded on the landed marker and echoed in the reply),
a marker whose ``blocking`` rows lack a ``met_by`` credit refuses ``halt reason stands``, and a marker whose
reason starts ``completion gate:`` refuses unless the run-outcome ``inline_review`` leaves ``completion_blockers`` empty,
``session_id`` (string, optional uuid: the calling session, used to find the run's review sidecar),
``in_run`` (bool, optional: close the calling run itself as already delivered; admits a ``running``
sibling status or a solo run with no halt marker, and refuses unless every manifest
``review_declared_paths`` entry has no delta between the manifest ``head_sha`` and HEAD and the
worktree; a marker-less run writes nothing and replies the not-applicable close),
``landed_under`` (string, optional run id: admits a run with no sibling status or halt record whose
work landed under that other run; the named run's marker must read ``landed`` at a sha matching
``sha``/``shas``, else the "never ran" refusal stands; writes nothing and replies the not-applicable close),
``foreign`` (list of path strings, optional: owed withheld paths holding only a concurrent run's hunks; voided
unless the run's own ``kept.json`` fingerprints the path, which refuses).
Each sha is a lowercase hex commit sha, full or abbreviated to at least 4
characters; an abbreviation must name exactly one commit and the reply and
recorded marker carry the full 40-hex sha. The run's sibling status must read
``halted`` (or already ``landed`` at the same sha; or ``running`` with no live run token, a launcher
gone before its close) and ``sha`` must be HEAD or an ancestor of HEAD,
merge-reached included. A solo run's marker may also read ``resumed`` when no live run's
ledger names it as ``resumed_from``. The write and the release of ``after`` successors are
``run_close.sibling_release``'s; this op never writes the sibling status file itself. A solo run has
no sibling status: its halt marker (``halted_run``) is read and rewritten to ``landed`` instead, and
the reply names that tracked path as ``recorded``; a sibling run's still-``halted`` marker is flipped
the same way. The records the flip writes (``halted.json``, ``run-stage.json``, under ``state/``) are
committed in the same op as one scoped commit naming the run; the reply carries ``records_commit`` (its
sha), or ``records_commit_error`` when the commit refused (the landed write stands). That commit also
carries ``Checkpoint-Run-Closed:`` for the run's own checkpoint bases (``landing_integrity.closing_bases``: manifest,
recorded base, halt marker ``run_base_sha``, resumed chain), whether or not the marker names a plan; only after the
sha was verified an ancestor of HEAD. No write, no commit. Commit existence is an in-process object read, and ancestry and
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
#: Param, reply key and halt-marker key: ``{blocking row id: full sha of the commit at or after the halt that met it}``.
MET_BY_FIELD = "met_by"


#: Close detail for a landing recorded on a run that never halted (stalled or staged): no marker exists to flip.
_NEVER_HALTED = "no halt record: the run never halted, so there is no marker to flip; landing {sha} verified and stands, nothing more to close"

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


def _met_by_rows(marker: dict) -> list:
    """Register-row ids ``met_by`` may name: ``_withheld(marker)`` plus a ledger-check halt's ``blocking_items`` / ``items`` ids."""
    rows = _withheld(marker)
    if marker.get("halted") == "ledger-check":
        for key in ("blocking_items", "items"):
            seq = marker.get(key)
            if isinstance(seq, list):
                rows.extend(i["id"] for i in seq if isinstance(i, dict) and isinstance(i.get("id"), str))
    return rows


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

    return _review_record_in(root, session_id, ask_contract.read_run_outcome(root, run_id))


def _landed_without_verdict(root: Path, run_id: str, session_id: Optional[str]) -> bool:
    """True when no review record resolves and the run outcome carries no ``delivery.verdict``; one outcome read."""
    from coordinator_core.ops.dispatch_emit import ask_contract

    outcome = ask_contract.read_run_outcome(root, run_id)
    if _review_record_in(root, session_id, outcome) is not None:
        return False
    inline_review = outcome.get("inline_review") if outcome else None
    delivery = inline_review.get("delivery") if isinstance(inline_review, dict) else None
    return not (isinstance(delivery, dict) and delivery.get("verdict"))


def _review_record_in(root: Path, session_id: Optional[str], outcome: Optional[dict]) -> Optional[str]:
    """``_review_record`` over an already-read run *outcome*."""
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


def _closed_trailers(root: Path, run_id: str, record_dir: Path) -> str:
    """A blank line then one ``Checkpoint-Run-Closed:`` line per own checkpoint base; ``""`` when none resolve. In-process reads."""
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_trailer_lines
    from coordinator_core.ops.dispatch_emit.landing_integrity import closing_bases

    marker = halted_run._read(record_dir) or {}
    lines = run_closed_trailer_lines(closing_bases(root, run_id, {"run_base_sha": marker.get("run_base_sha")}))
    return "\n\n" + "\n".join(lines) if lines else ""


def _commit_records(root: Path, run_id: str, record_dir: Path, sha: str, verified: bool = False) -> dict:
    """Commit the landed flip's record files (halted.json, run-stage.json) plus every other changed or untracked path in the run's record dir (prompts/, resume.json) in one scoped commit naming *run_id*.

    ``verified`` (the sha was checked an ancestor of HEAD) adds ``Checkpoint-Run-Closed:`` trailers for the run's bases.

    Returns ``{"records_commit": sha}``; ``{}`` when nothing changed or the record dir is untracked
    scratch; ``{"records_commit_error": ...}`` on a refusal, which never fails the landed write.
    """
    from coordinator_core.git.commit import CommitRefused, NothingToCommit, commit_paths
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE
    from coordinator_core.ops.dispatch_emit.run_artifacts import record_dir_residue

    try:
        fixed = [
            (record_dir / name).relative_to(root).as_posix()
            for name in (halted_run.HALTED_FILE, RUN_STAGE_FILE)
            if (record_dir / name).is_file()
        ]
    except ValueError:
        return {}
    if not fixed or not fixed[0].startswith("state/"):
        return {}
    rels = sorted(set(fixed) | set(record_dir_residue(root, run_id)))
    try:
        outcome = commit_paths(
            root, rels,
            f"dispatch.record_halted_run_landed: {run_id} landed at {sha[:12]}"
            + (_closed_trailers(root, run_id, record_dir) if verified else ""),
        )
    except NothingToCommit:
        return {}
    except (CommitRefused, OSError) as exc:
        return {"records_commit_error": f"{type(exc).__name__}: {exc}"}
    return {"records_commit": outcome.sha}


def _close_landed(root: Path, run_id: str, sha: str, session_id: Optional[str], verified: bool = False) -> dict:
    """Flip a still-``halted`` or ``resumed`` marker to landed and commit its records, then queue the close unless a job record exists."""
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, run_close

    without_verdict = _landed_without_verdict(root, run_id, session_id)
    out: dict = {"landed_without_verdict": without_verdict}
    try:
        record_dir = ask_contract.record_dir(root, run_id)
    except ValueError:
        return {**out, "close": {"status": "not-applicable", "detail": "no halt record"}, "blocking_rows": []}
    marker = halted_run._read(record_dir)
    if marker is not None and marker.get("status") in (halted_run.STATUS_HALTED, halted_run.STATUS_RESUMED):
        if halted_run.mark_landed(record_dir, sha, without_verdict=without_verdict):
            out["recorded"] = (record_dir / halted_run.HALTED_FILE).relative_to(root).as_posix()
            out.update(_commit_records(root, run_id, record_dir, sha, verified))
    if marker is None:
        return {**out, "close": {"status": "not-applicable", "detail": _NEVER_HALTED.format(sha=sha[:12])}, "blocking_rows": []}
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
    """True when the run dir records the run landed, closed (abandoned) or halted, or never left `minted`: its token's TTL is not liveness then."""
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE

    marker = halted_run._read(cand)
    if marker is not None and marker.get("status") in (halted_run.STATUS_LANDED, halted_run.STATUS_CLOSED):
        return True
    if halted_run._is_shell(cand):
        return True
    try:
        stage = json.loads((cand / RUN_STAGE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(stage, dict) and stage.get("stage") in ("halted", "landed")


def _launcher_gone(root: Path, run_id: str) -> bool:
    """True when no launcher can still be driving *run_id*: its token is not live, or its run dir records a terminal stage.

    Trap: a halted or abandoned run keeps an unclaimed in-TTL token (nothing redeems it), so the
    token alone reads such a run as live; the run dir's own terminal record settles it.
    """
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, run_token

    if not run_token.is_live(root, run_id):
        return True
    try:
        record_dir = ask_contract.record_dir(root, run_id)
    except ValueError:
        return False
    return _terminal(record_dir) and not halted_run._is_shell(record_dir)


_REPO_QUALIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]+:(?=[^/\\])")


def _tree_path(path: str) -> str:
    """*path* without a leading ``<repo>:`` qualifier (a withheld entry may name its repo); a one-letter drive prefix is kept.

    The qualifier is only stripped here; ``_own_tree_path`` decides whether it names the run's repo.
    """
    return _REPO_QUALIFIER.sub("", path, count=1)


def _own_tree_path(root: Path, path: str, _seen: Optional[dict] = None) -> Optional[str]:
    """The run-repo tree path of *path*, or None when its ``<repo>:`` qualifier names another repo.

    A qualifier is the run's own when it matches the root's directory name or the machine registry
    (``repos.<name>``) resolves it to *root*. Anything else is a foreign repo's path and stays owed.
    """
    if not _REPO_QUALIFIER.match(path):
        return path
    qualifier = path.split(":", 1)[0]
    seen = _seen if _seen is not None else {}
    if qualifier not in seen:
        own = qualifier.casefold() == root.name.casefold()
        if not own:
            try:
                import os

                from coordinator_core.machine_resolver import registry_get

                reg = registry_get("repos." + qualifier.casefold().replace("-", "_"))
                own = bool(reg) and os.path.normcase(os.path.realpath(reg)) == os.path.normcase(os.path.realpath(str(root)))
            except Exception:  # noqa: BLE001 -- an unresolvable qualifier is foreign
                own = False
        seen[qualifier] = own
    return _tree_path(path) if seen[qualifier] else None


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
    """The *paths* with no delta: identical in the run's base tree and HEAD, and the working-tree file hashes to HEAD's blob (or is absent from both, or present untracked and git-ignored).

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
    seen: dict = {}
    matcher = None
    for p in paths:
        tp = _own_tree_path(root, p, seen)
        if tp is None:
            continue
        entries = _casefolded_entries(load_dir, meta["tree"], tp)
        if entries is None or len(entries) > 1 or entries != _casefolded_entries(load_dir, base_meta["tree"], tp):
            continue
        target = root / tp
        try:
            if not entries:
                if not target.exists() and not target.is_symlink():
                    clean.add(p)
                elif target.is_file() and not target.is_symlink():
                    from coordinator_core.git.ignore_match import IgnoreMatcher

                    matcher = matcher or IgnoreMatcher(root)
                    if matcher.ignored(tp, False):
                        clean.add(p)
                continue
            (exact, mode, oid), = entries
            if exact != tp or mode & 0o170000 != 0o100000 or not target.is_file() or target.is_symlink():
                continue
            data = target.read_bytes()
            # core.autocrlf checks out CRLF over an LF blob; git status calls that clean, so do we.
            for body in (data, data.replace(b"\r\n", b"\n")) if b"\r\n" in data else (data,):
                if hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest() == oid:
                    clean.add(p)
                    break
        except OSError:
            continue
    return clean


_UNTOUCHED_WORDING = re.compile(
    r"needed no change|no change (?:was )?(?:needed|made)|\bunchanged\b|\buntouched\b|not authored here"
    r"|not (?:edited|modified) (?:here|by this)",
    re.IGNORECASE,
)


def _note_blocks(text: str) -> list:
    """The delivery note split into blank-line paragraphs and, inside them, bullets with their continuation lines."""
    blocks: list = []
    for para in re.split(r"\n\s*\n", text):
        current: list = []
        for line in para.splitlines():
            if re.match(r"\s*(?:[-*]|\d+\.)\s", line) and current:
                blocks.append(" ".join(current))
                current = []
            current.append(line.strip())
        if current:
            blocks.append(" ".join(current))
    return blocks


def _unauthored_paths(root: Path, run_id: str, record_dir: Path, paths: list) -> set:
    """The unchanged *paths* the run never authored a hunk on, so the landing record does not owe them.

    Excused when the run's current bytes equal its ``write_baseline`` print (it left the path as it
    found it, the delta is foreign), or when the run's delivery note carries one bullet naming the
    path and saying it was left unchanged. Refused either way when the path is in the baseline,
    differs from it, and equals a blob ``kept.json`` fingerprints for the run (its own uncommitted
    hunks). Reads only; any unreadable evidence excuses nothing.

    Trap: the note wording is free text, so one bullet that names the path and also says "unchanged"
    of something else excuses the path.
    """
    from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit.ask_contract import delivery_note_locate

    try:
        base = hha._baseline(root, run_id) or {}
        kept = kept_work.kept_fingerprints(record_dir)
        try:
            blocks = _note_blocks((root / delivery_note_locate(root, run_id)).read_text(encoding="utf-8"))
        except OSError:
            blocks = []
        out: set = set()
        for p in paths:
            cur = kept_work.fingerprint(root, p)
            if p in base and cur != base[p] and cur is not None and cur in kept.get(p, frozenset()):
                continue
            if p in base and cur == base[p]:
                out.add(p)
                continue
            name = p.rsplit("/", 1)[-1].casefold()
            if any(
                (p.casefold() in b.casefold() or name in b.casefold()) and _UNTOUCHED_WORDING.search(b)
                for b in blocks
            ):
                out.add(p)
        return out
    except Exception:  # noqa: BLE001 -- unreadable evidence excuses nothing
        return set()


def _foreign_problem(root: Path, record_dir: Path, owed: list, foreign: frozenset) -> tuple[Optional[str], list]:
    """``(refusal, claimed)``: the *owed* paths the caller declared *foreign*, or the refusal for one the run's own ``kept.json`` fingerprints.

    A foreign path holds only a concurrent run's hunks. The match is casefolded on the tree path (a
    ``<repo>:`` qualifier stripped); a *foreign* name that is not owed is ignored.
    """
    from coordinator_core.ops.dispatch_emit import kept_work

    claimed = [p for p in owed if _tree_path(p).replace("\\", "/").casefold() in foreign]
    if not claimed:
        return None, []
    try:
        kept = {k.casefold() for k in kept_work.kept_fingerprints(record_dir)}
    except Exception:  # noqa: BLE001 -- an unreadable kept record cannot clear a path as foreign
        return f"kept record unreadable; cannot clear {claimed[0]} as foreign", []
    for p in claimed:
        if _tree_path(p).replace("\\", "/").casefold() in kept:
            return f"{p} is fingerprinted in the run's own kept record; it is not foreign", []
    return None, claimed


def _carries_withheld(
    root: Path, run_id: str, shas: list, foreign: frozenset = frozenset(),
) -> tuple[Optional[str], dict, list]:
    """``(None, covers, voided)`` when *shas* together changed every owed path the run's halt marker withholds, else ``(refusal, {}, [])``.

    Each sha is compared with its first parent; the union of changed withheld paths must cover the
    withheld list less the paths a revision voided (``voided``, ``_voided_paths``), and every sha must
    change at least one owed path. ``covers`` maps each sha to the owed paths it changed (empty for a
    run withholding nothing or with no marker, which passes). An unreadable commit or tree refuses.
    *foreign* (casefolded tree paths the caller declares hold only a peer's hunks) voids those owed
    paths last, and refuses when the run's own ``kept.json`` fingerprints one.
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
    seen: dict = {}
    for sha in shas:
        meta = commit_meta(common, sha)
        parents = meta.get("parents") if meta else None
        parent_meta = commit_meta(common, parents[0]) if parents else None
        if meta is None or not meta.get("tree") or (parents and (parent_meta is None or not parent_meta.get("tree"))):
            return f"{sha} or its parent is unreadable; retry", {}, []
        changed = set()
        for p in withheld:
            tp = _own_tree_path(root, p, seen)
            if tp is None:
                continue
            after = _casefolded_entries(_dir, meta["tree"], tp)
            before = _casefolded_entries(_dir, parent_meta["tree"], tp) if parent_meta else frozenset()
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
        unauthored = _unauthored_paths(root, run_id, record_dir, unchanged)
        voided_list += [p for p in unchanged if p in unauthored]
        unchanged = [p for p in unchanged if p not in unauthored]
    if unchanged:
        # A path the EM withheld by `workflow.revise withhold_paths` is left out on purpose: owing it
        # makes a hand-landing that skips a peer's hunks in a shared file unrecordable.
        from coordinator_core.ops.dispatch_emit import run_withhold

        em_held = run_withhold.read([record_dir])
        voided_list += [p for p in unchanged if p in em_held]
        unchanged = [p for p in unchanged if p not in em_held]
    if unchanged and foreign:
        problem, claimed = _foreign_problem(root, record_dir, unchanged, foreign)
        if problem:
            return problem, {}, []
        voided_list += claimed
        unchanged = [p for p in unchanged if p not in claimed]
    if unchanged:
        more = f" (+{len(unchanged) - 1} more)" if len(unchanged) > 1 else ""
        who = shas[0] if len(shas) == 1 else "shas together"
        return (
            f"{who} did not change withheld {unchanged[0]}{more}; "
            "pass the commit(s) that landed the run's withheld paths"
        ), {}, []
    return None, covers, voided_list


class _ParentBaseline:
    """The ``head`` the closure detector diffs landed text against: the landed path's text in the sha's first parent."""

    def __init__(self, texts: dict) -> None:
        self._texts = texts

    def head_text(self, path: str) -> Optional[str]:
        return self._texts.get(path)


def _tree_py_delta(load_dir, before: Optional[str], after: str, prefix: str = "") -> list:
    """``(path, after_oid, before_oid)`` of each ``.py`` blob that differs between two trees; equal subtrees are skipped unread."""
    new = load_dir(after) or {}
    old = (load_dir(before) if before else None) or {}
    out: list = []
    for name, (mode, oid) in new.items():
        prior = old.get(name)
        if prior is not None and prior[1] == oid:
            continue
        full = f"{prefix}{name}"
        if mode == 0o40000:
            out += _tree_py_delta(load_dir, prior[1] if prior and prior[0] == 0o40000 else None, oid, full + "/")
        elif name.endswith(".py") and mode & 0o170000 == 0o100000:
            out.append((full, oid, prior[1] if prior and prior[0] & 0o170000 == 0o100000 else None))
    return out


def _withheld_import_problem(root: Path, run_id: str, shas: list, covers: dict) -> Optional[str]:
    """Refusal when a landed ``.py`` imports a name only a still-withheld path binds, else None.

    The still-withheld set is the run's withheld paths no verified sha changed, less the revision-voided
    ones. The landed text is each sha's own blob, judged against its first parent's, because HEAD often
    is the sha and the detector only judges lines the landing adds. Zero spawns; nothing runs when
    nothing is still withheld.

    Trap: a withheld path with no delta (clean at HEAD) binds nothing the detector cannot already see at HEAD.
    """
    from coordinator_core.authoring_leaks import import_closure
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.git_state import _parse_tree_entries
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run

    try:
        record_dir = ask_contract.record_dir(root, run_id)
        withheld = sorted(halted_run.halted_withheld_strict(record_dir))
    except (ValueError, halted_run.UnreadableHaltRecord):
        return None
    covered = {p for paths in covers.values() for p in paths}
    owed = [p for p in withheld if p not in covered]
    if not owed:
        return None
    owed = [p for p in owed if p not in _voided_paths(root, halted_run._read(record_dir), owed)]
    seen: dict = {}
    tree_paths = [tp for tp in (_own_tree_path(root, p, seen) for p in owed) if tp]
    if not tree_paths:
        return None
    common = resolve_git_common_dir(root)
    dirs: dict = {}

    def _dir(tree_sha: str):
        if tree_sha not in dirs:
            got = read_object(common, tree_sha)
            dirs[tree_sha] = _parse_tree_entries(got[1]) if got and got[0] == "tree" else None
        return dirs[tree_sha]

    def _text(oid: Optional[str]) -> Optional[str]:
        got = read_object(common, oid) if oid else None
        if not got or got[0] != "blob":
            return None
        try:
            return got[1].decode("utf-8")
        except UnicodeDecodeError:
            return None

    for sha in shas:
        meta = commit_meta(common, sha)
        parents = meta.get("parents") if meta else None
        parent_meta = commit_meta(common, parents[0]) if parents else None
        if not meta or not meta.get("tree"):
            continue
        delta = _tree_py_delta(_dir, parent_meta.get("tree") if parent_meta else None, meta["tree"])
        landed = {path: _text(after) for path, after, _ in delta}
        if not landed:
            continue
        found = import_closure.detect(
            root, sorted(landed), _ParentBaseline({path: _text(before) for path, _, before in delta}),
            landed_text=landed.get,
        )
        for f in found:
            hit = next((p for p in tree_paths if f"; {p} is " in f.detail), None)
            if hit:
                return f"{f.path} (landed in {sha[:12]}) {f.detail}; withheld {hit} is not landed; land it or drop the import"
    return None


def _halt_commit(root: Path, record_dir: Path) -> Optional[str]:
    """The commit that first added the run's ``halted.json`` (the halt commit), else None; one git spawn."""
    from coordinator_core import git_ancestry
    from coordinator_core.ops.dispatch_emit import halted_run

    rel = (record_dir / halted_run.HALTED_FILE).relative_to(root).as_posix()
    result = git_ancestry.run_git(["log", "--diff-filter=A", "--format=%H", "--", rel], cwd=str(root))
    lines = result.stdout.split() if result.returncode == 0 else []
    return lines[-1] if lines and _FULL_SHA.fullmatch(lines[-1]) else None


def _met_by_problem(root: Path, run_id: str, met_by: dict, reached: frozenset) -> Optional[str]:
    """Refusal naming the first ``met_by`` row whose sha is not a blocking row's commit at or after the halt, else None.

    *met_by* holds full shas already proven ancestors of HEAD (*reached*). At-or-after the halt is
    equal to the halt commit or not an ancestor of it: one batched ``rev-list`` for all rows.
    """
    from coordinator_core import git_ancestry
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run

    try:
        record_dir = ask_contract.record_dir(root, run_id)
    except ValueError as exc:
        return str(exc)
    marker = halted_run._read(record_dir)
    if marker is None:
        return f"met_by needs {run_id}'s halt record; none is readable"
    blocking = _met_by_rows(marker)
    for row, sha in met_by.items():
        if row not in blocking:
            return f"met_by row {row} is not a blocking row of {run_id}"
        if sha not in reached:
            return f"met_by row {row}: {sha} is not an ancestor of HEAD; pass the commit that met the row"
    halt = _halt_commit(root, record_dir)
    if halt is None:
        return f"{run_id} halt commit could not be found; met_by cannot be placed after it"
    read_ok, before = git_ancestry.ancestors_of(list(met_by.values()), halt, cwd=str(root))
    if not read_ok:
        return "ancestry of the met_by shas could not be read; retry"
    for row, sha in met_by.items():
        if sha != halt and sha in before:
            return f"met_by row {row}: {sha} is before the halt commit {halt}; pass a commit at or after the halt"
    return None


_COMPLETION_GATE_PREFIX = "completion gate:"


def _standing_halt_problem(root: Path, run_id: str, credited: dict) -> Optional[str]:
    """Refusal while the run's halt marker states a reason no evidence discharges, else None.

    Blocking rows (``_withheld``) owe a ``met_by`` credit, from this call (*credited*) or already
    stamped on the marker. A marker whose ``reason``/``halted`` starts ``completion gate:`` also owes a
    review record whose ``completion_blockers`` (halt marker excluded) is empty. A marker-less run
    (``in_run``, ``landed_under``, never halted) has nothing standing.
    """
    from coordinator_core.ops.dispatch_emit import ask_contract, completion_gate, halted_run

    try:
        marker = halted_run._read(ask_contract.record_dir(root, run_id))
    except ValueError:
        return None
    if marker is None:
        return None
    stamped = marker.get(MET_BY_FIELD)
    have = set(credited) | (set(stamped) if isinstance(stamped, dict) else set())
    uncredited = [b for b in _withheld(marker) if b not in have]
    if uncredited:
        return f"halt reason stands: blocking rows {', '.join(uncredited)} carry no met_by credit"
    if any(
        isinstance(marker.get(k), str) and marker[k].lstrip().startswith(_COMPLETION_GATE_PREFIX)
        for k in (halted_run.HALT_REASON_FIELD, "halted")
    ):
        outcome = ask_contract.read_run_outcome(root, run_id)
        review = outcome.get("inline_review") if isinstance(outcome, dict) else None
        blockers = completion_gate.completion_blockers(inline_review=review, halt_marker=None)
        if blockers:
            return f"halt reason stands: completion gate unmet ({'; '.join(blockers)})"
    return None


def _verify(
    root: Path, run_id: str, shas: list, met_by: Optional[dict] = None, foreign: frozenset = frozenset(),
) -> tuple[Optional[str], Optional[str], dict]:
    """``(newest_full_sha, None, echo)`` when *shas* resolve, are reachable and cover the withheld paths, else ``(None, refusal, {})``.

    ``echo`` is ``{"shas": every verified full sha, "covers": {sha: withheld paths it changed}}``,
    plus ``"met_by": {row: full sha}`` when *met_by* is given. *met_by* shas join the same
    prefix-resolve and HEAD-ancestry reads as *shas*.
    """
    from coordinator_core import git_ancestry
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha

    met_by = met_by or {}
    common = resolve_git_common_dir(root)
    for sha in [*shas, *met_by.values()]:
        if _FULL_SHA.fullmatch(sha) and commit_meta(common, sha) is None:
            return None, f"{sha} is not a commit in this repo; pass the landing commit's full sha", {}
    head = head_sha(root)
    if not head:
        return None, "HEAD is unreadable; retry", {}
    abbreviated = [s for s in [*shas, *met_by.values()] if not _FULL_SHA.fullmatch(s)]
    resolved, problem = git_ancestry.resolve_commit_prefixes(abbreviated, cwd=str(root))
    if problem:
        return None, problem, {}
    full = list(dict.fromkeys(resolved.get(s, s) for s in shas))
    full_met = {row: resolved.get(s, s) for row, s in met_by.items()}
    read_ok, reached = git_ancestry.ancestors_of(list(dict.fromkeys([*full, *full_met.values()])), head, cwd=str(root))
    if not read_ok:
        return None, "ancestry of the sha could not be read; retry", {}
    for sha in full:
        if sha not in reached:
            return None, f"{sha} is not an ancestor of HEAD; pass the commit that landed the work", {}
    if full_met:
        problem = _met_by_problem(root, run_id, full_met, reached)
        if problem:
            return None, problem, {}
    problem, covers, voided = _carries_withheld(root, run_id, full, foreign)
    if problem:
        return None, problem, {}
    problem = _withheld_import_problem(root, run_id, full, covers)
    if problem:
        return None, problem, {}
    echo = {
        "shas": full, "covers": covers, **({"voided": voided} if voided else {}),
        **({MET_BY_FIELD: full_met} if full_met else {}),
    }
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


def _met_by_param(params: dict) -> tuple[dict, Optional[str]]:
    """``({row_id: sha}, None)`` from the optional ``met_by`` param, else ``({}, refusal)``."""
    raw = params.get(MET_BY_FIELD)
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, f"{OP_NAME} requires met_by to be an object of row id to sha"
    for row, sha in raw.items():
        if not isinstance(row, str) or not row:
            return {}, f"{OP_NAME} requires every met_by key to be a non-empty row id"
        if not isinstance(sha, str) or not _SHA_PREFIX.fullmatch(sha):
            return {}, f"{OP_NAME} requires met_by row {row} to be a lowercase hex sha of 4 to 40 characters"
    return dict(raw), None


def _foreign_param(params: dict) -> tuple[frozenset, Optional[str]]:
    """``(casefolded forward-slash paths, None)`` from the optional ``foreign`` list param, else ``(frozenset(), refusal)``."""
    raw = params.get("foreign")
    if raw is None:
        return frozenset(), None
    if not isinstance(raw, list) or any(not isinstance(p, str) or not p for p in raw):
        return frozenset(), f"{OP_NAME} requires foreign to be a list of non-empty path strings"
    return frozenset(_tree_path(p).replace("\\", "/").casefold() for p in raw), None


def _stamp_met_by(root: Path, run_id: str, met_by: dict) -> None:
    """Record *met_by* on the run's halt marker; ``mark_landed`` carries it into the landed marker."""
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run

    record_dir = ask_contract.record_dir(root, run_id)
    marker = halted_run._read(record_dir)
    if marker is not None:
        halted_run._write(record_dir, {**marker, MET_BY_FIELD: met_by})


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


# Bounds the first-parent walk of _derive_landing_sha: a declared path untouched in recent history must refuse, not walk the whole repo.
_DERIVE_WALK_DEPTH = 512


def _derive_landing_sha(root: Path, run_id: str) -> tuple[Optional[str], Optional[str]]:
    """``(sha, None)``: the newest non-merge commit on HEAD's first-parent chain that changed a manifest ``review_declared_paths`` entry, else ``(None, refusal)``.

    ``_in_run_unclean`` must pass first. A merge is skipped (it diffs against its first parent with the
    merged side's changes, so it would name the merge, not the landing). In-process object reads only;
    the walk stops at the first hit or after ``_DERIVE_WALK_DEPTH`` commits.
    """
    import json as _json

    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.git_state import _parse_tree_entries, head_sha
    from coordinator_core.ops.dispatch_emit import ask_contract

    problem = _in_run_unclean(root, run_id)
    if problem:
        return None, problem
    try:
        doc = _json.loads((ask_contract.record_dir(root, run_id) / "manifest.json").read_text(encoding="utf-8"))
        declared = [p for p in doc["review_declared_paths"] if isinstance(p, str) and p]
    except (OSError, ValueError, KeyError, TypeError):
        return None, f"{run_id} manifest is unreadable or lacks review_declared_paths; no landing commit to name"
    seen: dict = {}
    tree_paths = [
        tp for tp in (_own_tree_path(root, p.replace("\\", "/"), seen) for p in declared)
        if tp is not None
    ]
    if not tree_paths:
        return None, f"{run_id} declares no path in this repo; no landing commit to name"
    common = resolve_git_common_dir(root)
    dirs: dict = {}

    def _dir(tree_sha: str):
        if tree_sha not in dirs:
            got = read_object(common, tree_sha)
            dirs[tree_sha] = _parse_tree_entries(got[1]) if got and got[0] == "tree" else None
        return dirs[tree_sha]

    def _at(tree: Optional[str], tp: str):
        return frozenset() if tree is None else _casefolded_entries(_dir, tree, tp)

    sha = head_sha(root)
    for _ in range(_DERIVE_WALK_DEPTH):
        meta = commit_meta(common, sha) if sha else None
        if not meta or not meta.get("tree"):
            break
        parents = meta.get("parents") or []
        if len(parents) <= 1:
            parent_meta = commit_meta(common, parents[0]) if parents else None
            if parents and not parent_meta:
                break
            parent_tree = parent_meta.get("tree") if parent_meta else None
            if any(_at(meta["tree"], tp) != _at(parent_tree, tp) for tp in tree_paths):
                return sha, None
        if not parents:
            break
        sha = parents[0]
    return None, f"{run_id} write set is clean but no commit within {_DERIVE_WALK_DEPTH} first-parent commits of HEAD changed it; pass sha"


def _landed_sha(root: Path, other: str, shas: list) -> Optional[str]:
    """The sha *other* landed at when its halt marker or sibling status reads landed at a sha matching *shas*, else None."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract

    if not safe_id(other):
        return None
    landed = None
    try:
        marker = halted_run.read_strict_run(ask_contract.record_dir(root, other))
        if marker is not None and marker.get("status") == halted_run.STATUS_LANDED:
            landed = marker.get("sha")
    except (ValueError, halted_run.UnreadableHaltRecord):
        pass
    if landed is None:
        try:
            prior = json.loads((root / contract.status_rel(other)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(prior, dict) and prior.get("status") == contract.STATUS_LANDED:
            landed = prior.get("sha")
    if isinstance(landed, str) and any(landed.startswith(s) for s in shas):
        return landed
    return None


def _record_solo(
    root: Path, run_id: str, shas: list, session_id: Optional[str] = None, in_run: bool = False,
    met_by: Optional[dict] = None, landed_under: Optional[str] = None, foreign: frozenset = frozenset(),
) -> dict:
    """Solo run (no sibling status): record landed on its halt marker; the caller commits ``recorded``.

    ``in_run`` admits a marker-less run: it verifies the sha and the clean write set, writes nothing and
    replies ``_close_landed``'s not-applicable close. ``landed_under`` admits a marker-less run whose
    work the named run already landed: no git read, the named run's landed sha is the credit.
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
        sha, problem, echo = _verify(root, run_id, shas, met_by, foreign)
        problem = problem or _in_run_unclean(root, run_id)
        if problem:
            return _refuse(problem)
        return {"ok": True, "run_id": run_id, "sha": sha, "written": True, **echo, **_close_landed(root, run_id, sha, session_id)}
    if marker is None and landed_under:
        landed = _landed_sha(root, landed_under, shas)
        if landed:
            return {
                "ok": True, "run_id": run_id, "sha": landed, "written": False, "landed_under": landed_under,
                **_close_landed(root, run_id, landed, session_id),
            }
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
    sha, problem, echo = _verify(root, run_id, shas, met_by, foreign)
    problem = problem or _standing_halt_problem(root, run_id, echo.get(MET_BY_FIELD, {}))
    if problem:
        return _refuse(problem)
    if met_by:
        _stamp_met_by(root, run_id, echo[MET_BY_FIELD])
    without_verdict = _landed_without_verdict(root, run_id, session_id)
    if not halted_run.mark_landed(record_dir, sha, without_verdict=without_verdict):
        return _refuse(f"{run_id} halt marker changed under the write; retry")
    recorded = (record_dir / halted_run.HALTED_FILE).relative_to(root).as_posix()
    return {
        "ok": True, "run_id": run_id, "sha": sha, "written": True, **echo, "recorded": recorded,
        "landed_without_verdict": without_verdict,
        **_commit_records(root, run_id, record_dir, sha, True),
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
    derive = params.get("in_run") is True and params.get("sha") is None and params.get("shas") is None
    shas, problem = ([], None) if derive else _shas_param(params)
    if problem:
        return _refuse(problem)
    met_by, problem = _met_by_param(params)
    if problem:
        return _refuse(problem)
    foreign, problem = _foreign_param(params)
    if problem:
        return _refuse(problem)
    in_run = params.get("in_run")
    if in_run is not None and not isinstance(in_run, bool):
        return _refuse(f"{OP_NAME} requires in_run to be a boolean")

    landed_under = params.get("landed_under")
    if landed_under is not None and (not isinstance(landed_under, str) or not landed_under):
        return _refuse(f"{OP_NAME} requires landed_under to be a run id string")

    sid = params.get("session_id")
    session_id = sid if isinstance(sid, str) and _UUID.fullmatch(sid) else None

    root = main_worktree_root(Path(repo_root))
    if derive:
        derived, problem = _derive_landing_sha(root, run_id)
        if problem:
            return _refuse(problem)
        shas = [derived]
    try:
        prior = json.loads((root / contract.status_rel(run_id)).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _record_solo(root, run_id, shas, session_id, bool(in_run), met_by, landed_under, foreign)
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
    admitted = status == contract.STATUS_HALTED or (
        status == contract.STATUS_RUNNING and (in_run or _launcher_gone(Path(root), run_id))
    )
    if not admitted:
        return _refuse(f"{run_id} is {status}, not halted; only a halted run is recorded landed")

    sha, problem, echo = _verify(root, run_id, shas, met_by, foreign)
    problem = problem or _standing_halt_problem(root, run_id, echo.get(MET_BY_FIELD, {}))
    if problem:
        return _refuse(problem)
    if in_run:
        problem = _in_run_unclean(root, run_id)
        if problem:
            return _refuse(problem)
    if met_by:
        _stamp_met_by(root, run_id, echo[MET_BY_FIELD])
    reply = run_close.sibling_release(root, run_id, sha)
    out: dict = {"ok": True, "run_id": run_id, "sha": sha, "written": True, **echo}
    if reply is not None:
        out["siblings"] = reply
    out.update(_close_landed(root, run_id, sha, session_id, True))
    return out
