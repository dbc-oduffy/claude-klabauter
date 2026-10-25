"""Doctor layer and recover verb for kept.json blobs missing from the object store.

`sweep` reads every `state/warp/*/kept.json` (chunks.<id>.paths and post_checkpoint) and asks
git once per repo (`cat-file --batch-check`) which listed blobs are absent. `recover` re-creates
them in-process, never touching the worktree: from worktree bytes that hash to the kept sha, else
by replaying the run's executor Write/Edit tool calls from its workflow transcripts.
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
import os
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op

if TYPE_CHECKING:
    from coordinator_core.ops.doctor import Layer

LAYER_NAME = "Kept blobs are in the object store"
_DELIMITER_OID = "0" * 39 + "1"
_NO_WINDOW =getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class MissingBlob:
    run: str
    kind: str  # "chunk:<id>" | "post"
    path: str
    sha: str
    landed: bool
    worktree_holds: bool
    mode: Optional[str] = None  # "raw" | "filtered" when the worktree bytes hash to `sha`


@dataclass
class RecoverResult:
    run: str
    path: str
    sha: str
    outcome: str  # "rescued-worktree" | "rescued-replay" | "lost"
    mode: Optional[str] = None  # "raw" | "filtered" (worktree) | "raw" (replay)


def _git(repo: Path, args: List[str], data: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data,
        capture_output=True,
        creationflags=_NO_WINDOW,
    )


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
    for kept in sorted((repo / "state" / "warp").glob("*/kept.json")):
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
    proc = _git(repo, ["cat-file", "--batch-check"], "".join(i[3] + "\n" for i in items).encode())
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    if len(lines) != len(items):
        return []
    absent = [i for i, line in zip(items, lines) if line.endswith("missing")]
    if not absent:
        return []
    filtered, head = _filtered_and_head(repo, [i[2] for i in absent])
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
        missing.append(MissingBlob(run, kind, path, sha, landed, mode is not None, mode))
    return missing


def _filtered_and_head(repo: Path, paths: List[str]) -> tuple:
    """({path: filtered worktree blob sha}, {path: shas that mean HEAD holds the kept content}).

    Per path the HEAD set is its blob sha plus the raw sha of its checked-out bytes (smudge/eol
    filters applied); three spawns for all paths."""
    unique = sorted(set(paths))
    filtered: Dict[str, str] = {}
    existing = [p for p in unique if (repo / p).is_file()]
    if existing:
        out = _git(repo, ["hash-object", "--stdin-paths"], "".join(p + "\n" for p in existing).encode())
        shas = out.stdout.decode("utf-8", "replace").splitlines()
        if out.returncode == 0 and len(shas) == len(existing):
            filtered = dict(zip(existing, shas))
    head: Dict[str, set] = {}
    out = _git(repo, ["cat-file", "--batch-check"], "".join(f"HEAD:{p}\n" for p in unique).encode())
    lines = out.stdout.decode("utf-8", "replace").splitlines()
    if len(lines) == len(unique):
        for p, line in zip(unique, lines):
            if not line.endswith("missing"):
                head[p] = {line.split(" ", 1)[0]}
    present = [p for p in unique if p in head]
    if present:
        # The size in a --filters header is not the converted length, so a never-resolving oid
        # after each request delimits the content.
        stop = b"%s missing\n" % _DELIMITER_OID.encode()
        req = "".join(f"HEAD:{p} {p}\n{_DELIMITER_OID}\n" for p in present).encode()
        chunks = _git(repo, ["cat-file", "--batch", "--filters"], req).stdout.split(b"\n" + stop)
        if len(chunks) == len(present) + 1:
            for p, chunk in zip(present, chunks):
                nl = chunk.find(b"\n")
                if nl >= 0 and chunk[:nl].split(b" ")[1:2] == [b"blob"]:
                    head[p].add(blob_sha(chunk[nl + 1 :]))
    return filtered, head


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


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def _path_matches(tool_path: str, rel: str) -> bool:
    t = _norm(tool_path)
    return t == rel or t.endswith("/" + rel)


def _tool_calls(transcript: Path) -> Iterable[dict]:
    try:
        text = transcript.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        if rec.get("type") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield {"ts": rec.get("timestamp") or "", "name": block.get("name"), "input": block.get("input") or {}}


def default_transcript_roots() -> List[Path]:
    base = Path(os.path.expanduser("~")) / ".claude" / "projects"
    return list(base.glob("*/*/subagents/workflows")) if base.is_dir() else []


def _run_transcripts(run: str, roots: Iterable[Path]) -> List[Path]:
    needle = run.encode()
    found: List[Path] = []
    for root in roots:
        for f in Path(root).rglob("*.jsonl"):
            try:
                if needle in f.read_bytes():
                    found.append(f)
            except OSError:
                continue
    return found


def _replay(calls: List[dict], rel: str, base: Optional[bytes]) -> Optional[bytes]:
    cur = base
    for call in calls:
        inp = call["input"]
        if not _path_matches(str(inp.get("file_path", "")), rel):
            continue
        if call["name"] == "Write" and isinstance(inp.get("content"), str):
            cur = inp["content"].encode("utf-8")
        elif call["name"] == "Edit" and cur is not None:
            old, new = inp.get("old_string"), inp.get("new_string")
            try:
                text = cur.decode("utf-8")
            except UnicodeDecodeError:
                return None
            if not isinstance(old, str) or not isinstance(new, str) or old not in text:
                return None
            text = text.replace(old, new) if inp.get("replace_all") else text.replace(old, new, 1)
            cur = text.encode("utf-8")
    return cur


def _write_loose(repo: Path, sha: str, data: bytes) -> bool:
    """Write the blob as a loose object; True when the store now holds `sha`."""
    proc = _git(repo, ["rev-parse", "--git-common-dir"])
    if proc.returncode != 0:
        return False
    gitdir = Path(proc.stdout.decode().strip())
    if not gitdir.is_absolute():
        gitdir = repo / gitdir
    dest = gitdir / "objects" / sha[:2] / sha[2:]
    if dest.exists():
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"tmp_obj_{os.getpid()}")
    tmp.write_bytes(zlib.compress(b"blob %d\0" % len(data) + data))
    os.replace(tmp, dest)
    return True


def _base_candidates(repo: Path, rel: str, base_sha: Optional[str]) -> List[Optional[bytes]]:
    cands: List[Optional[bytes]] = []
    refs = ([base_sha] if base_sha else []) + ["HEAD"]
    for ref in refs:
        proc = _git(repo, ["cat-file", "blob", f"{ref}:{rel}"])
        if proc.returncode == 0:
            cands.append(proc.stdout)
    cands.append(None)  # new file: first Write defines the content
    return cands


def recover(
    repo_root: Path,
    base_sha: Optional[str] = None,
    transcript_roots: Optional[Iterable[Path]] = None,
) -> List[RecoverResult]:
    """Re-create each absent kept blob in the object store; the worktree is never modified.

    Order per blob: worktree bytes hashing to the kept sha, else a replay of the run's Write/Edit
    calls onto the base blob accepted only when its blob sha equals the kept sha.
    """
    repo = Path(repo_root)
    roots = list(transcript_roots) if transcript_roots is not None else default_transcript_roots()
    results: List[RecoverResult] = []
    calls_by_run: Dict[str, List[dict]] = {}
    for m in sweep(repo):
        outcome = "lost"
        mode = None
        if m.worktree_holds:
            try:
                if m.mode == "raw":
                    ok = _write_loose(repo, m.sha, (repo / m.path).read_bytes())
                else:
                    ok = _git(repo, ["hash-object", "-w", "--", m.path]).stdout.decode().strip() == m.sha
                if ok:
                    outcome, mode = "rescued-worktree", m.mode
            except OSError:
                pass
        if outcome == "lost":
            if m.run not in calls_by_run:
                calls = [c for f in _run_transcripts(m.run, roots) for c in _tool_calls(f)]
                calls_by_run[m.run] = sorted(calls, key=lambda c: c["ts"])
            for base in _base_candidates(repo, m.path, base_sha):
                data = _replay(calls_by_run[m.run], _norm(m.path), base)
                if data is not None and blob_sha(data) == m.sha and _write_loose(repo, m.sha, data):
                    outcome, mode = "rescued-replay", "raw"
                    break
        results.append(RecoverResult(m.run, m.path, m.sha, outcome, mode))
    return results


@register_op("doctor.recover_kept_blobs")
def _recover_op(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Handler body for `doctor.recover_kept_blobs`: params `repo_root` (else the envelope's), `base_sha`."""
    raw = params.get("repo_root") if isinstance(params, dict) else None
    if raw is not None and not isinstance(raw, str):
        raise CallerFacingValidationError("doctor.recover_kept_blobs repo_root must be a string path.")
    target = Path(raw) if raw else repo_root
    if target is None or not (Path(target) / ".git").exists():
        raise CallerFacingValidationError("doctor.recover_kept_blobs needs repo_root: a git repository root.")
    base = params.get("base_sha")
    if base is not None and not isinstance(base, str):
        raise CallerFacingValidationError("doctor.recover_kept_blobs base_sha must be a string.")
    results = recover(Path(target), base_sha=base)
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    return {
        "repo_root": str(target),
        "counts": counts,
        "results": [
            {"run": r.run, "path": r.path, "sha": r.sha, "outcome": r.outcome, "mode": r.mode} for r in results
        ],
    }
