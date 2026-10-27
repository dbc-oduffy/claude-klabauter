"""Doctor layer and recover verb for kept.json blobs missing from the object store.

`sweep` reads every `state/warp/*/kept.json` (chunks.<id>.paths and post_checkpoint) and asks
git once per repo (`cat-file --batch-check`) which listed blobs are absent. `recover` re-creates
them in-process, never touching the worktree: from worktree bytes that hash to the kept sha, else
by replaying every session transcript's Write/Edit/MultiEdit calls on the path from every
historical base (exact raw match only, else lost), then classifies each replayed blob against HEAD
(SUPERSEDED or NOT-LANDED). Method and traps: docs/reference/kept-blob-recovery.md.
`doctor.run_doctor` imports this module lazily to avoid an import cycle. Also registers the
`doctor.recover_kept_blobs` op (callable cross-repo with `repo_root`).

Invariant: worktree bytes are hashed raw (`git hash-object --no-filters` semantics, the domain of
`kept_work.blob_sha`), never through autocrlf filters; `mode` records which hash matched. A kept
path is landed only when the kept sha equals the HEAD blob sha or the raw sha of HEAD's
checked-out bytes; worktree equality with HEAD is never evidence.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

from coordinator_core.git.run import run_git
from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.session import record_homes

if TYPE_CHECKING:
    from coordinator_core.ops.doctor import Layer

LAYER_NAME = "Kept blobs are in the object store"
_DELIMITER_OID = "0" * 39 + "1"
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class MissingBlob:
    run: str
    kind: str  # "chunk:<id>" | "post"
    path: str
    sha: str
    landed: bool
    worktree_holds: bool
    mode: Optional[str] = None  # "raw" | "filtered" when the worktree bytes hash to `sha`
    head_oid: Optional[str] = None  # HEAD's blob oid for `path`; None when HEAD lacks the path


@dataclass
class RecoverResult:
    run: str
    path: str
    sha: str
    outcome: str  # "rescued-worktree" | "rescued-replay" | "lost"
    mode: Optional[str] = None  # "raw" | "filtered" (worktree) | "raw" (replay)
    verdict: Optional[str] = None  # "SUPERSEDED" | "NOT-LANDED" | "CARRIED"; None without a replay base
    commit: Optional[str] = None  # oldest history commit already carrying the kept hunks
    base: Optional[str] = None  # label of the replay base that reproduced the blob
    why: Optional[str] = None


def blob_sha(data: bytes) -> str:
    """Git blob sha1 of `data`, computed in-process."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _run_status(run_dir: Path) -> Optional[str]:
    try:
        return json.loads((run_dir / "halted.json").read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError):
        return None


def _kept_entries(repo: Path) -> List[tuple]:
    """(run, kind, path, sha, landed) for every blob named by a kept.json."""
    items: List[tuple] = []
    for kept in sorted(Path(record_homes.home_dir(str(repo), "warp")).glob("*/kept.json")):
        try:
            doc = json.loads(kept.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        run = kept.parent.name
        landed = _run_status(kept.parent) == "landed"
        for cid, entry in (doc.get("chunks") or {}).items():
            for path, sha in ((entry or {}).get("paths") or {}).items():
                items.append((run, f"chunk:{cid}", path, sha, landed))
        for path, sha in (doc.get("post_checkpoint") or {}).items():
            items.append((run, "post", path, sha, landed))
    return items


def sweep(repo_root: Path) -> List[MissingBlob]:
    """Every kept blob absent from `repo_root`'s object store; one git spawn for the repo."""
    repo = Path(repo_root)
    items = [i for i in _kept_entries(repo) if isinstance(i[3], str) and i[3]]
    if not items:
        return []
    proc = run_git(["-C", str(repo), "cat-file", "--batch-check"],
                   input="".join(i[3] + "\n" for i in items).encode(), binary=True)
    lines = proc.stdout_bytes.decode("utf-8", "replace").splitlines()
    if len(lines) != len(items):
        return []
    absent = [i for i, line in zip(items, lines) if line.endswith("missing")]
    if not absent:
        return []
    filtered, head, head_oids = _filtered_and_head(repo, [i[2] for i in absent])
    missing: List[MissingBlob] = []
    for run, kind, path, sha, landed in absent:
        if sha in head.get(path, ()):
            continue
        wt = repo / path
        mode: Optional[str] = None
        try:
            if wt.is_file():
                if blob_sha(wt.read_bytes()) == sha:
                    mode = "raw"
                elif filtered.get(path) == sha:
                    mode = "filtered"
        except OSError:
            pass
        missing.append(MissingBlob(run, kind, path, sha, landed, mode is not None, mode, head_oids.get(path)))
    return missing


def _filtered_and_head(repo: Path, paths: List[str]) -> tuple:
    """({path: filtered worktree blob sha}, {path: shas that mean HEAD holds the kept content},
    {path: HEAD blob oid}).

    Per path the HEAD set is its blob sha plus the raw sha of its checked-out bytes (smudge/eol
    filters applied); three spawns for all paths."""
    unique = sorted(set(paths))
    filtered: Dict[str, str] = {}
    existing = [p for p in unique if (repo / p).is_file()]
    if existing:
        out = run_git(["-C", str(repo), "hash-object", "--stdin-paths"],
                      input="".join(p + "\n" for p in existing).encode(), binary=True)
        shas = out.stdout_bytes.decode("utf-8", "replace").splitlines()
        if out.returncode == 0 and len(shas) == len(existing):
            filtered = dict(zip(existing, shas))
    head: Dict[str, set] = {}
    head_oids: Dict[str, str] = {}
    out = run_git(["-C", str(repo), "cat-file", "--batch-check"],
                  input="".join(f"HEAD:{p}\n" for p in unique).encode(), binary=True)
    lines = out.stdout_bytes.decode("utf-8", "replace").splitlines()
    if len(lines) == len(unique):
        for p, line in zip(unique, lines):
            if not line.endswith("missing"):
                head[p] = {line.split(" ", 1)[0]}
                head_oids[p] = line.split(" ", 1)[0]
    present = [p for p in unique if p in head]
    if present:
        # The size in a --filters header is not the converted length, so a never-resolving oid
        # after each request delimits the content.
        stop = b"%s missing\n" % _DELIMITER_OID.encode()
        req = "".join(f"HEAD:{p} {p}\n{_DELIMITER_OID}\n" for p in present).encode()
        chunks = run_git(["-C", str(repo), "cat-file", "--batch", "--filters"],
                         input=req, binary=True).stdout_bytes.split(b"\n" + stop)
        if len(chunks) == len(present) + 1:
            for p, chunk in zip(present, chunks):
                nl = chunk.find(b"\n")
                if nl >= 0 and chunk[:nl].split(b" ")[1:2] == [b"blob"]:
                    head[p].add(blob_sha(chunk[nl + 1 :]))
    return filtered, head, head_oids


def kept_blobs_layer(repo_root: Path) -> "Layer":
    """broken with one finding per run that lost blobs; ok when every kept blob persists."""
    from coordinator_core.ops.doctor import Finding, Layer

    missing = sweep(repo_root)
    if not missing:
        return Layer(LAYER_NAME, "ok", [])
    per_run: Dict[str, List[MissingBlob]] = {}
    for m in missing:
        per_run.setdefault(m.run, []).append(m)
    findings: List[Finding] = []
    for run, blobs in sorted(per_run.items()):
        rescuable = sum(1 for b in blobs if b.worktree_holds)
        state = "landed" if blobs[0].landed else "NOT LANDED"
        first = blobs[0]
        findings.append(
            Finding(
                "broken",
                f"{run} ({state}): {len(blobs)} kept blob(s) absent from the object store, "
                f"{rescuable} still in the worktree (e.g. {first.path} {first.sha[:10]}). "
                f"Recover: python -m coordinator_core.invoke doctor.recover_kept_blobs "
                f"'{json.dumps({'repo_root': str(repo_root)})}'",
            )
        )
    return Layer(LAYER_NAME, "broken", findings)


def _commit_history(bases: list) -> list:
    """Commit bases as (commit10, since, data), oldest first; `empty` and `base_sha:` bases excluded."""
    rows = [
        (b.label.split(":", 1)[0], b.since or _EPOCH, b.data)
        for b in bases
        if b.label != "empty" and not b.label.startswith("base_sha:")
    ]
    return sorted(rows, key=lambda r: r[1])


def _classify_hit(hit, m: MissingBlob, bases: list, common) -> tuple:
    """(verdict, commit, why) of a replayed blob against HEAD; verdict None when HEAD is unreadable."""
    from coordinator_core.git import git_objects
    from coordinator_core.ops.kept_blob_classify import classify

    head: Optional[bytes] = None
    if m.head_oid:
        obj = git_objects.read_object(common, m.head_oid)
        if obj is None or obj[0] != "blob":
            return None, None, "HEAD blob unreadable"
        head = obj[1]
    v = classify(hit.base.data, hit.data, head, _commit_history(bases))
    return v.verdict, v.commit, v.why


def recover(
    repo_root: Path,
    base_sha: Optional[str] = None,
    transcript_roots: Optional[Iterable[Path]] = None,
    scan: Optional[dict] = None,
) -> List[RecoverResult]:
    """Re-create each absent kept blob in the object store; the worktree is never modified.

    Order per blob: worktree bytes hashing to the kept sha, else the exact replay of every
    transcript's Write/Edit/MultiEdit calls on the path from a historical base. A replayed blob is
    classified against HEAD; a blob no replay reproduces is lost and no replayed bytes are written.

    Bulk remediation verb under the DR-344 bulk-transfer carve-out, granted by G-EM ruling (a)
    (group-em-44, PM-delegated, 2026-10-25): the all-transcript scan is not held to the 500ms bar.
    Run it once per incident, explicitly; it is never reachable from a hook,
    the sweep or the doctor default. Transcripts stream one at a time. `scan`, when given, receives
    {roots, transcripts, window} of what was scanned.
    """
    from coordinator_core.git import git_dir, git_objects
    from coordinator_core.ops.kept_blob_replay import history_bases, replay_exact
    from coordinator_core.ops.kept_blob_transcripts import collect_edits, default_transcript_root

    repo = Path(repo_root)
    if scan is not None:
        scan.update(roots=0, transcripts=0, window=None)
    absent = sweep(repo)
    if not absent:
        return []
    roots = list(transcript_roots) if transcript_roots is not None else [default_transcript_root()]
    rels = {m.path.replace("\\", "/") for m in absent}
    edits = collect_edits(repo, rels, roots, scan)
    bases = history_bases(repo, rels, base_sha)
    common = git_dir.resolve_git_common_dir(repo)

    results: List[RecoverResult] = []
    hits: list = []
    filtered_idx: List[int] = []
    for m in absent:
        rel = m.path.replace("\\", "/")
        hit = replay_exact(bases[rel], edits[rel], m.sha)
        outcome, mode = "lost", None
        if m.worktree_holds and m.mode == "raw":
            try:
                data = (repo / m.path).read_bytes()
            except OSError:
                data = b""
            if blob_sha(data) == m.sha:
                git_objects.write_object(common, b"blob", data)
                outcome, mode = "rescued-worktree", "raw"
        elif m.worktree_holds and m.mode == "filtered":
            filtered_idx.append(len(results))
        if outcome == "lost" and hit is not None and m.mode != "filtered":
            git_objects.write_object(common, b"blob", hit.data)
            outcome, mode = "rescued-replay", "raw"
        results.append(RecoverResult(m.run, m.path, m.sha, outcome, mode))
        hits.append(hit)

    if filtered_idx:
        paths = [absent[i].path for i in filtered_idx]
        out = run_git(["-C", str(repo), "hash-object", "-w", "--stdin-paths"],
                      input="".join(p + "\n" for p in paths).encode(), binary=True)
        shas = out.stdout_bytes.decode("utf-8", "replace").splitlines()
        stored = out.returncode == 0 and len(shas) == len(paths)
        for i, sha in zip(filtered_idx, shas if stored else [None] * len(paths)):
            r = results[i]
            if sha == r.sha:
                r.outcome, r.mode = "rescued-worktree", "filtered"
            elif hits[i] is not None:
                git_objects.write_object(common, b"blob", hits[i].data)
                r.outcome, r.mode = "rescued-replay", "raw"

    for m, r, hit in zip(absent, results, hits):
        if hit is None:
            continue
        r.base = hit.base.label
        r.verdict, r.commit, r.why = _classify_hit(hit, m, bases[m.path.replace("\\", "/")], common)
    for r, hit in zip(results, hits):
        if hit is None and r.outcome == "lost":
            r.why = "no replay base"
    return results


@register_op("doctor.recover_kept_blobs")
def _recover_op(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Handler body for `doctor.recover_kept_blobs`: params `repo_root` (else the envelope's), `base_sha`.

    Bulk remediation under the DR-344 bulk-transfer carve-out, granted by G-EM ruling (a)
    (group-em-44, PM-delegated, 2026-10-25); the 500ms bar does not govern it;
    explicit invocation only. The result's `scanned` = {roots, transcripts, window}.
    """
    raw = params.get("repo_root") if isinstance(params, dict) else None
    if raw is not None and not isinstance(raw, str):
        raise CallerFacingValidationError("doctor.recover_kept_blobs repo_root must be a string path.")
    target = Path(raw) if raw else repo_root
    if target is None or not (Path(target) / ".git").exists():
        raise CallerFacingValidationError("doctor.recover_kept_blobs needs repo_root: a git repository root.")
    base = params.get("base_sha")
    if base is not None and not isinstance(base, str):
        raise CallerFacingValidationError("doctor.recover_kept_blobs base_sha must be a string.")
    scan: dict = {}
    results = recover(Path(target), base_sha=base, scan=scan)
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
        if r.verdict:
            key = f"verdict:{r.verdict}"
            counts[key] = counts.get(key, 0) + 1
    return {
        "repo_root": str(target),
        "counts": counts,
        "scanned": scan,
        "results": [
            {
                "run": r.run, "path": r.path, "sha": r.sha, "outcome": r.outcome, "mode": r.mode,
                "verdict": r.verdict, "commit": r.commit, "base": r.base, "why": r.why,
            }
            for r in results
        ],
    }
