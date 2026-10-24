"""Shared reader of a warp run's deliverable binding, for the commit-trailer resolver and the
hand-shipped sizing sweep.

`live_run_deliverable_id` answers which deliverable a session's live, sizing-bound run is for;
`run_records` lists every run dir with its submission, declared writes and reconciled state;
`write_reconciled` records the run-local terminal marker the sweep leaves.

Invariants: in-process file I/O only (no spawn, no transcript read), so it is safe on the commit
hot path. Ambiguity returns "" (omit rather than guess).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, record_dir
from coordinator_core.ops.dispatch_emit.commit_request import MalformedCommitRequestError, parse_marker

RECONCILED_FILE = "reconciled.json"
COMMIT_REQUEST_FILE = "commit-request.txt"
SUBMISSION_FILE = "submission.json"

_SCALAR_RE = r"^{key}:[ \t]*(?:\"([^\"\n]*)\"|'([^'\n]*)'|([^\s#'\"][^\s#]*))"


@dataclass(frozen=True)
class RunRecord:
    """One run dir under `scratch/warp`. `submitted_at` is 0.0 and `session_id` "" when
    `submission.json` is absent or unreadable; `sizing_rel` is "" when no record names one."""

    run_id: str
    session_id: str
    submitted_at: float
    sizing_rel: str
    writes: tuple
    reconciled: bool


def _norm(path: str) -> str:
    text = (path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def sizing_scalar(repo_root: Path, sizing_rel: str, key: str) -> str:
    """A top-level scalar of a sizing yaml (`deliverable_id`, `status`) read by line match, "" when
    the file or key is absent. No YAML parse: the commit hot path pays for a line scan only."""
    if not sizing_rel or not key:
        return ""
    try:
        text = (Path(repo_root) / sizing_rel).read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(_SCALAR_RE.format(key=re.escape(key)), text, re.MULTILINE)
    if match is None:
        return ""
    return next((g for g in match.groups() if g is not None), "").strip()


def declared_writes(run_dir: Path) -> tuple:
    """Every chunk path the run's `commit-request.txt` promises its terminal commit, normalized;
    empty when the marker is absent or malformed."""
    try:
        request = parse_marker((run_dir / COMMIT_REQUEST_FILE).read_text(encoding="utf-8"))
    except (OSError, MalformedCommitRequestError):
        return ()
    if request is None:
        return ()
    seen: dict = {}
    for chunk in request.chunks:
        for path in chunk.paths:
            seen.setdefault(_norm(path), None)
    return tuple(seen)


def record_sizing_rel(repo_root: Path, run_id: str) -> str:
    """The normalized `sizing_rel` a run's record-dir `run.json` names, "" when absent or unsafe."""
    try:
        run_json = record_dir(repo_root, run_id) / "run.json"
    except ValueError:
        return ""
    value = _read_json(run_json).get("sizing_rel")
    return _norm(value) if isinstance(value, str) else ""


def run_records(repo_root: Path) -> Iterator[RunRecord]:
    """One `RunRecord` per run dir under `scratch/warp`, in name order."""
    root = Path(repo_root)
    try:
        run_dirs = sorted(p for p in (root / RUN_DIR_ROOT).iterdir() if p.is_dir())
    except OSError:
        return
    for run_dir in run_dirs:
        submission = _read_json(run_dir / SUBMISSION_FILE)
        sizing_rel = submission.get("sizing_rel")
        sizing_rel = _norm(sizing_rel) if isinstance(sizing_rel, str) else ""
        submitted_at = submission.get("submitted_at")
        session_id = submission.get("session_id")
        yield RunRecord(
            run_id=run_dir.name,
            session_id=session_id if isinstance(session_id, str) else "",
            submitted_at=float(submitted_at) if isinstance(submitted_at, (int, float)) else 0.0,
            sizing_rel=sizing_rel or record_sizing_rel(root, run_dir.name),
            writes=declared_writes(run_dir),
            reconciled=(run_dir / RECONCILED_FILE).is_file(),
        )


def live_run_deliverable_id(repo_root: Path, session_id: str, paths: Iterable[str]) -> str:
    """The `deliverable_id` of the one sizing-bound live run `session_id` holds whose declared
    writes include a staged path, else "". Zero, or two-plus distinct ids, return "".

    The path gate keeps an abandoned run's token (live for `run_token.TTL_SECONDS`) from claiming
    every later commit of the session."""
    if not session_id:
        return ""
    staged = {_norm(p) for p in paths if p}
    if not staged:
        return ""
    root = Path(repo_root)
    ids: set = set()
    for record in run_token.live_records(root):
        sizing_rel = record.get("sizing_rel")
        if record.get("session_id") != session_id or not isinstance(sizing_rel, str) or not sizing_rel:
            continue
        run_dir = root / RUN_DIR_ROOT / str(record["run_id"])
        if (run_dir / RECONCILED_FILE).is_file() or staged.isdisjoint(declared_writes(run_dir)):
            continue
        did = sizing_scalar(root, _norm(sizing_rel), "deliverable_id")
        if did:
            ids.add(did)
    return next(iter(ids)) if len(ids) == 1 else ""


def _halted(root: Path, run_id: str) -> bool:
    """A run whose record carries a `halted` marker writes nothing more; its token stays fresh for
    the TTL and its declaration contests no live write. Its declared writes stay held against
    another run's carry until it resumes or ages out (`halted_run_write_claims`). A `resumed`
    marker is live again."""
    from coordinator_core.ops.dispatch_emit import halted_run

    try:
        marker = halted_run._read(record_dir(root, run_id))
    except ValueError:
        return False
    return marker is not None and marker.get("status") == halted_run.STATUS_HALTED


def _manifest_declared(run_id: str, root: Path) -> tuple:
    """`(paths, prefixes, unreadable)` a run's `manifest.json` (`rows[].writes`, `doc_writes`)
    and `commit-request.txt` (chunk paths and prefixes) declare, normalized. `unreadable` is True
    when a file exists but does not parse; an absent file is not unreadable. A manifest that fails
    mid-parse keeps the paths gathered before the failure."""
    paths: list = []
    prefixes: list = []
    unreadable = False
    try:
        text = (record_dir(root, run_id) / "manifest.json").read_text(encoding="utf-8")
    except (OSError, ValueError):
        text = None
    if text is not None:
        try:
            data = json.loads(text)
            rows = data["rows"]
            for row in rows:
                paths.extend(w for w in row["writes"] if isinstance(w, str))
            paths.extend(w for w in (data.get("doc_writes") or ()) if isinstance(w, str))
        except (ValueError, KeyError, TypeError):
            unreadable = True
    try:
        request = parse_marker((root / RUN_DIR_ROOT / run_id / COMMIT_REQUEST_FILE).read_text(encoding="utf-8"))
    except OSError:
        request = None
    except MalformedCommitRequestError:
        request = None
        unreadable = True
    if request is not None:
        for chunk in request.chunks:
            paths.extend(chunk.paths)
            prefixes.extend(chunk.prefixes)
    return (
        tuple(p for p in (_norm(x) for x in paths) if p),
        tuple(p.rstrip("/") for p in (_norm(x) for x in prefixes) if p.rstrip("/")),
        unreadable,
    )


def _withheld_paths(root: Path, run_id: str) -> set:
    """Normalized paths a run recorded in `withhold.jsonl`: it promised not to carry them."""
    from coordinator_core.ops.dispatch_emit import run_withhold

    return {n for n in (_norm(p) for p in run_withhold.read([record_dir(root, run_id)])) if n}


def _live_peer_run(
    root: Path, record: dict, exclude_session: str, cwd: Optional[str], minted_before: Optional[tuple] = None,
) -> bool:
    """True when a held `record` is a live peer's run: fresh (claimed records included), a
    non-empty `session_id` other than `exclude_session` that is `touch_record.session_live`, not
    halted, and (when given) strictly senior to `minted_before=(minted_at, run_id)`."""
    from coordinator_core.session import touch_record

    sid = record.get("session_id")
    run_id = record["run_id"]
    if not isinstance(sid, str) or not sid or sid == exclude_session or not run_token._fresh(record):
        return False
    if minted_before is not None and (record["minted_at"], run_id) >= tuple(minted_before):
        return False
    return touch_record.session_live(sid, cwd) and not _halted(root, run_id)


def live_record_dir_owners(
    repo_root: Path, paths: Iterable[str], *, exclude_session: str, cwd: Optional[str] = None,
) -> dict:
    """`{path: sorted peer session ids}` for each of `paths` under a run's record dir
    (`RECORD_DIR_ROOT/<run_id>/...`) whose run is held by a live peer (`_live_peer_run`).

    Paths outside a record dir, runs held by `exclude_session`, and unheld runs are absent.
    One token-dir listing, no spawn."""
    root = Path(repo_root)
    prefix = RECORD_DIR_ROOT + "/"
    by_run: dict = {}
    for path in (_norm(p) for p in paths if p):
        if path.startswith(prefix):
            run_id = path[len(prefix):].split("/", 1)[0]
            if run_id:
                by_run.setdefault(run_id, []).append(path)
    owners: dict = {}
    if not by_run:
        return owners
    for record in run_token.held_records(root):
        run_id = record["run_id"]
        if run_id in by_run and _live_peer_run(root, record, exclude_session, cwd):
            for path in by_run[run_id]:
                owners.setdefault(path, set()).add(record["session_id"])
    return {p: sorted(s) for p, s in owners.items()}


def live_run_write_claims(
    repo_root: Path,
    paths: Iterable[str],
    *,
    exclude_session: str,
    cwd: Optional[str] = None,
    minted_before: Optional[tuple] = None,
    exclude_run: Optional[str] = None,
) -> Optional[dict]:
    """`{path: sorted peer session ids}` for each of `paths` a live peer run declares it writes.

    A peer run is a held token (`run_token.held_records`, listed once) that is fresh, whose
    non-empty `session_id` differs from `exclude_session` and is `touch_record.session_live`.
    `exclude_run` skips that run id before any other test. `minted_before=(minted_at, run_id)`
    keeps only strictly senior runs. A path matches a declared path or sits under a declared
    prefix, unless the run recorded it with `workflow.revise withhold_paths`. Returns None (contest unknown) when a live peer's declaration is unparseable and
    yields no usable paths; {} when nothing is declared."""
    root = Path(repo_root)
    wanted = {_norm(p) for p in paths if p}
    wanted.discard("")
    claims: dict = {}
    if not wanted:
        return claims
    for record in run_token.held_records(root):
        sid = record.get("session_id")
        run_id = record["run_id"]
        if exclude_run is not None and run_id == exclude_run:
            continue
        if not _live_peer_run(root, record, exclude_session, cwd, minted_before):
            continue
        declared, prefixes, unreadable = _manifest_declared(run_id, root)
        if unreadable and not declared and not prefixes:
            logging.getLogger(__name__).warning("live run %s: unreadable write declaration", run_id)
            return None
        declared_set = set(declared)
        withheld = _withheld_paths(root, run_id)
        for path in wanted - withheld:
            if path in declared_set or any(path.startswith(pre + "/") or path == pre for pre in prefixes):
                claims.setdefault(path, set()).add(sid)
    return {p: sorted(s) for p, s in claims.items()}


def halted_run_write_claims(
    repo_root: Path, paths: Iterable[str], *, exclude_run: Optional[str], now: Optional[float] = None,
) -> Optional[dict]:
    """`{path: sorted halted run ids}` for each of `paths` a halted, unresumed run's halt record
    names as withheld: a halted run that withheld nothing on a path holds nothing there.

    Only record dirs whose run-id stamp is within `run_token.TTL_SECONDS` of `now` are read; a name
    with no stamp, or an older one, costs no IO. `exclude_run` is skipped. No session filter."""
    import time

    from coordinator_core.ops.dispatch_emit import stopped_runs

    root = Path(repo_root)
    wanted = {_norm(p) for p in paths if p}
    wanted.discard("")
    claims: dict = {}
    if not wanted:
        return claims
    moment = time.time() if now is None else now
    try:
        with os.scandir(root / RECORD_DIR_ROOT) as entries:
            names = sorted(e.name for e in entries if e.is_dir())
    except OSError:
        return claims
    for run_id in names:
        if exclude_run is not None and run_id == exclude_run:
            continue
        stamp = stopped_runs._stamp_epoch(run_id)
        if stamp is None or moment - stamp > run_token.TTL_SECONDS:
            continue
        if not _halted(root, run_id):
            continue
        for path in wanted & halted_withheld_paths(root, run_id):
            claims.setdefault(path, set()).add(run_id)
    return {p: sorted(s) for p, s in claims.items()}


def halted_withheld_paths(root: Path, run_id: str) -> set:
    """Normalized paths a halted run's marker records as withheld (hunks it left in the tree);
    empty when the marker has none, is absent, or the run id is invalid."""
    from coordinator_core.ops.dispatch_emit import halted_run

    try:
        withheld = halted_run.halted_withheld(record_dir(root, run_id))
    except ValueError:
        return set()
    return {n for n in (_norm(p) for p in withheld) if n}


def write_reconciled(run_dir: Path, sizing_rel: str, deliverable_id: str, shas: Iterable[str]) -> Path:
    """Write the run-local terminal marker naming the commits that shipped the run by hand."""
    from coordinator_core.atomic_replace import atomic_write_bytes

    target = Path(run_dir) / RECONCILED_FILE
    body = {"sizing_rel": _norm(sizing_rel), "deliverable_id": deliverable_id, "shas": list(shas)}
    atomic_write_bytes(target, json.dumps(body, sort_keys=True).encode("utf-8"), preserve_mode=False)
    return target
