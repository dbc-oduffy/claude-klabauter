"""Kept work: the finished product and reviewed plan a halted run leaves, fingerprinted for a refire.

Invariant: ``kept.json`` lives in the run record dir beside ``notes.jsonl``. A kept chunk or plan
gate is honoured only while every recorded fingerprint still equals the bytes on disk. Stdlib only;
spawns no process.

Invariant: ``post_checkpoint`` holds hunks no kept chunk records, including a path a peer co-declares
whose chunk was not kept, so no reader may treat its keys as row ids.

Invariant: one hash mode. Every fingerprint in ``kept.json`` is a RAW-bytes ``blob_sha``; a committed
blob is the checkin-filtered form (CRLF folded to LF under ``core.autocrlf``). The two domains cross
only through ``checkin_form`` and ``matches_committed``, never by comparing a raw sha to a committed one.
Trap: these in-process consumers never lift the committed side to raw; that needs a ``--filters``
spawn, and the only sanctioned lift is doctor_kept_blobs's one batched spawn per repo, off the hot path.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional, Sequence

from coordinator_core.ops.dispatch_emit import row_returns
from coordinator_core.session.declared_writes import declare_write

KEPT_FILE = "kept.json"
HALT_RECORD_ONLY = "halt-record-only"
POST_CHECKPOINT_KEY = "post_checkpoint"


def blob_sha(data: bytes) -> str:
    """Git's blob id: sha1 over ``blob <len>\\0`` plus the bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def fingerprint(root: Path, rel: str) -> Optional[str]:
    """``blob_sha`` of ``root/rel`` read in binary mode, or None when absent or unreadable."""
    try:
        return blob_sha((Path(root) / rel).read_bytes())
    except OSError:
        return None


_SHA40 = re.compile(r"[0-9a-f]{40}")


def checkin_form(root: Path, rel: str, data: bytes) -> Optional[bytes]:
    """The bytes ``git add`` would hash for ``data`` at ``rel`` (``git.commit._checkin_bytes``), or
    None when that conversion is refused (``FilterUnsupported``)."""
    from coordinator_core.git.commit import FilterUnsupported, _checkin_bytes

    try:
        return _checkin_bytes(Path(root), rel, data)
    except FilterUnsupported:
        return None


def matches_committed(root: Path, rel: str, raw_sha: object, committed_sha: object) -> bool:
    """True when the raw kept fingerprint ``raw_sha`` is the same content as the committed blob
    ``committed_sha`` once converted to checkin form. The raw bytes come from the object store, else
    from the worktree file when it still fingerprints to ``raw_sha``. False when the bytes are
    unavailable, the conversion is refused, or either sha is not a str. Zero spawns."""
    if not isinstance(raw_sha, str) or not isinstance(committed_sha, str):
        return False
    if raw_sha == committed_sha:
        return True
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object

    data: Optional[bytes] = None
    if _SHA40.fullmatch(raw_sha):
        try:
            obj = read_object(resolve_git_common_dir(root), raw_sha)
        except (OSError, ValueError):
            obj = None
        if obj is not None and obj[0] == "blob":
            data = obj[1]
    if data is None and fingerprint(root, rel) == raw_sha:
        try:
            data = (Path(root) / rel).read_bytes()
        except OSError:
            return False
    if data is None:
        return False
    form = checkin_form(root, rel, data)
    return form is not None and blob_sha(form) == committed_sha


def held_paths(root: Path, rels: Iterable[str]) -> dict[str, str]:
    """``{rel: run_id}`` for each normalised ``rel`` listed in ``kept.json`` (``chunks[*].paths`` or
    ``post_checkpoint``) of a still-halted run, i.e. one in the withheld index and not superseded
    (``halted_hold_release.supersession_reason``). Trap: ``withheld_by_path``
    maps a path two runs hold to the lexically last run only, so the run set is that map's values plus
    every indexed run file. Spawn-free; an unreadable index or record contributes nothing."""
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_hold_release, halted_withheld_index

    wanted = {k for k in (_norm(r) for r in rels) if k is not None}
    if not wanted:
        return {}
    try:
        run_ids = set(halted_withheld_index.withheld_by_path(root).values())
        run_ids.update(halted_withheld_index.run_ids(root))
    except OSError:
        return {}
    out: dict[str, str] = {}
    heads: Optional[dict] = None
    for run_id in sorted(run_ids):
        if halted_hold_release.supersession_reason(root, run_id) is not None:
            continue
        try:
            prints = kept_fingerprints(ask_contract.record_dir(root, run_id))
        except (OSError, ValueError):
            continue
        for rel in wanted.intersection(prints):
            if rel in out:
                continue
            if heads is None:
                heads = _head_blob_shas(root, wanted)
            if _pin_is_stale(root, rel, prints[rel], heads.get(rel)):
                continue
            out[rel] = run_id
    return out


def _head_blob_shas(root: Path, rels: Iterable[str]) -> dict[str, str]:
    """``{rel: HEAD blob sha}`` for the paths HEAD tracks; empty when HEAD is unreadable. Spawn-free."""
    from coordinator_core.git.git_state import head_blobs

    try:
        return {rel: sha for rel, (_mode, sha) in head_blobs(root, sorted(rels)).items()}
    except Exception:
        return {}


def _pin_is_stale(root: Path, rel: str, pins: frozenset, head_sha: Optional[str]) -> bool:
    """True only when the path's working bytes and HEAD blob are both readable and no pin equals either
    (CRLF-safe for HEAD). Fail-closed: any unreadable side keeps the hold."""
    if head_sha is None:
        return False
    working = fingerprint(root, rel)
    if working is None or working in pins:
        return False
    return not any(matches_committed(root, rel, pin, head_sha) for pin in pins)


def kept_chunks(
    root: Path,
    owner: Mapping[str, str],
    withheld: Mapping[str, str],
    baseline: Optional[Mapping[str, Optional[str]]] = None,
    returns: Optional[Mapping[str, str]] = None,
    finished: Iterable[str] = (),
) -> dict:
    """``{chunk: {"paths": {rel: fingerprint}}}`` for each chunk all of whose withheld paths carry
    ``halt-record-only``; ``owner`` maps path to chunk, ``withheld`` maps path to reason.
    With ``baseline``, a chunk any of whose paths still equals it (this run never wrote it) is not kept.
    A ``finished`` chunk with no withheld path but a persisted return (``returns``: row to sha256)
    is kept as ``{"paths": {}, "return": sha256}``."""
    unwritten: set[str] = set()
    if baseline:
        # Deferred: write_baseline imports this module's `fingerprint`.
        from coordinator_core.ops.dispatch_emit.write_baseline import unwritten as _unwritten

        unwritten = set(_unwritten(root, baseline, withheld))
    by_chunk: dict[str, dict[str, str]] = {}
    for path, reason in withheld.items():
        chunk = owner.get(path)
        if chunk:
            by_chunk.setdefault(chunk, {})[path] = reason
    out: dict[str, dict] = {}
    for chunk, paths in by_chunk.items():
        if all(r == HALT_RECORD_ONLY for r in paths.values()) and not unwritten.intersection(paths):
            prints = {p: fingerprint(root, p) for p in paths}
            if all(v is not None for v in prints.values()):
                out[chunk] = {"paths": prints}
    for chunk in finished:
        if chunk not in by_chunk and chunk in (returns or {}):
            out[chunk] = {"paths": {}, "return": returns[chunk]}
    return out


def _pulled(blitz: object, plan: str) -> Optional[dict]:
    if not isinstance(blitz, Mapping):
        return None
    regate = blitz.get("regate")
    for holder in (regate, blitz):
        pulled = holder.get("pulled") if isinstance(holder, Mapping) else None
        if isinstance(pulled, list):
            for entry in pulled:
                if isinstance(entry, Mapping) and entry.get("planPath") == plan:
                    return dict(entry)
    return None


def plan_gate(halted: Mapping, root: Path) -> Optional[dict]:
    """``{plan, plan_sha256, reviews, coverage}`` for a refusal halt whose blitz pulled ``plan``; else None."""
    if halted.get("halted") != "refusal":
        return None
    plan = halted.get("plan")
    if not isinstance(plan, str) or not plan:
        return None
    entry = _pulled(halted.get("blitz"), plan)
    if entry is None:
        return None
    try:
        digest = hashlib.sha256((Path(root) / plan).read_bytes()).hexdigest()
    except OSError:
        return None
    return {
        "plan": plan,
        "plan_sha256": digest,
        "reviews": entry.get("reviews"),
        "coverage": entry.get("coverage"),
    }


def _norm(rel: object) -> Optional[str]:
    """POSIX-normalised repo-relative path, or None for an empty, absolute, drive-lettered or ``..`` path."""
    if not isinstance(rel, str):
        return None
    p = rel.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    parts = PurePosixPath(p).parts
    if not p or p.startswith("/") or (len(p) > 1 and p[1] == ":") or ".." in parts:
        return None
    return "/".join(parts)


def post_checkpoint_hunks(
    root: Path,
    candidates: Iterable[str],
    *,
    landed: Iterable[str],
    recorded: Iterable[str],
    baseline: Optional[Mapping[str, Optional[str]]] = None,
) -> dict:
    """``{rel: fingerprint}`` for each candidate the run wrote after its last checkpoint: contained in
    the worktree, outside ``scratch/``, in neither ``landed`` nor ``recorded``, present, and (with
    ``baseline``) not still equal to it. Declared order, deduped."""
    skip = {_norm(p) for p in landed} | {_norm(p) for p in recorded}
    cands: list[str] = []
    for raw in candidates:
        rel = _norm(raw)
        if rel is None or rel in cands or rel in skip or rel == "scratch" or rel.startswith("scratch/"):
            continue
        cands.append(rel)
    if baseline and cands:
        # Deferred: write_baseline imports this module's `fingerprint`.
        from coordinator_core.ops.dispatch_emit.write_baseline import unwritten as _unwritten

        stale = set(_unwritten(root, baseline, cands))
        cands = [p for p in cands if p not in stale]
    base = Path(root).resolve()
    out: dict[str, str] = {}
    for rel in cands:
        try:
            (base / rel).resolve().relative_to(base)
        except (OSError, ValueError):
            continue
        print_ = fingerprint(root, rel)
        if print_ is not None:
            out[rel] = print_
    return out


def kept_post_checkpoint(root: Path, record_dir: Path) -> tuple[dict, list]:
    """``(holding, drifted)``: recorded ``post_checkpoint`` paths whose bytes still equal their blob,
    and the sorted rest (absent or other bytes)."""
    holding: dict[str, str] = {}
    drifted: list[str] = []
    for rel, want in read(record_dir)[POST_CHECKPOINT_KEY].items():
        if fingerprint(root, rel) == want:
            holding[rel] = want
        else:
            drifted.append(rel)
    return holding, sorted(drifted)


def kept_fingerprints(record_dir: Path) -> dict[str, frozenset]:
    """Per normalised path, the union of every ``chunks[*].paths`` blob and every ``post_checkpoint`` blob."""
    data = read(record_dir)
    out: dict[str, set] = {}
    for entry in data["chunks"].values():
        paths = entry.get("paths") if isinstance(entry, Mapping) else None
        if isinstance(paths, Mapping):
            for rel, blob in paths.items():
                key = _norm(rel)
                if key is not None and isinstance(blob, str):
                    out.setdefault(key, set()).add(blob)
    for rel, blob in data[POST_CHECKPOINT_KEY].items():
        key = _norm(rel)
        if key is not None:
            out.setdefault(key, set()).add(blob)
    return {k: frozenset(v) for k, v in out.items()}


def write(
    record_dir: Path,
    chunks: Mapping,
    plan_gate: Optional[Mapping],
    post_checkpoint: Optional[Mapping] = None,
) -> Optional[Path]:
    """Write ``kept.json`` (UTF-8, LF, sorted keys) when any part is non-empty; return its path or None.
    ``post_checkpoint`` is written only when non-empty."""
    if not chunks and not plan_gate and not post_checkpoint:
        return None
    path = Path(record_dir) / KEPT_FILE
    doc: dict = {"chunks": dict(chunks), "plan_gate": plan_gate or None}
    if post_checkpoint:
        doc[POST_CHECKPOINT_KEY] = dict(post_checkpoint)
    body = json.dumps(doc, sort_keys=True, indent=2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body + "\n")
    except OSError:
        return None
    declare_write(path)
    return path


def read(record_dir: Path) -> dict:
    """``{"chunks": {...}, "plan_gate": {...} | None, "post_checkpoint": {rel: blob}}``; empty parts
    when absent or malformed, non-str post_checkpoint entries dropped. Never raises."""
    empty: dict = {"chunks": {}, "plan_gate": None, POST_CHECKPOINT_KEY: {}}
    try:
        data = json.loads((Path(record_dir) / KEPT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    chunks = data.get("chunks")
    gate = data.get("plan_gate")
    post = data.get(POST_CHECKPOINT_KEY)
    return {
        "chunks": chunks if isinstance(chunks, dict) else {},
        "plan_gate": gate if isinstance(gate, dict) else None,
        POST_CHECKPOINT_KEY: (
            {k: v for k, v in post.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(post, dict)
            else {}
        ),
    }


def still_kept(
    root: Path,
    entry: object,
    notes: Sequence[str],
    row_id: str,
    record_dir: Optional[Path] = None,
) -> bool:
    """True while every recorded path still fingerprints the same and no note names the row or a path.
    A writes-empty entry (``return`` sha, no paths) holds only with ``record_dir`` whose returns.json
    carries that sha for the row and reads DONE."""
    paths = entry.get("paths") if isinstance(entry, Mapping) else None
    if not isinstance(paths, Mapping):
        return False
    if not paths:
        want_sha = entry.get("return")
        if record_dir is None or not isinstance(want_sha, str):
            return False
        got = row_returns.read(record_dir).get(row_id)
        if got is None or got["sha256"] != want_sha or not row_returns.is_done(got["text"]):
            return False
    for rel, want in paths.items():
        if not isinstance(want, str) or fingerprint(root, rel) != want:
            return False
    token = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(row_id) + r"(?![A-Za-z0-9_])")
    for note in notes:
        if token.search(note) or any(rel in note for rel in paths):
            return False
    return True
