"""
coordinator_core.ops.review_reverify — registers ``review.reverify``: lists the product files a
review stage changed since ``review.prep`` froze them, and restores the ones failing tests implicate.

Params: ``base`` (str), ``slice_prefix`` (str), optional ``session_id``, optional ``failing``
(list of pytest node ids), optional ``sidecars`` (list of review sidecar paths). Keying scope:
show_top.

Reads the write-once pre-review snapshot ``review.prep`` left at
``<share>/<slice_prefix>.pre-review/`` (``manifest.json`` plus one blob per sha256). By path
convention only; no ``review_prep`` import.

List mode (no ``failing``): ``{"edited", "created", "snapshot", "error"}``. ``edited`` is every
manifest path whose bytes now differ (a missing file counts); ``created`` is every path the
session's review stage wrote that the manifest and its declared ``paths`` do not name.

Revert mode (``failing`` given): ``{"reverted", "unattributed", "record", "error"}``. A failing
node id's test file implicates itself when edited or created, and every edited ``.py`` module it
imports. ``restored`` writes the snapshot bytes back; ``removed`` deletes a created path. An id
implicating nothing is ``unattributed`` and reverts nothing. Every restored or removed path is
passed to ``declare_write`` so the session's write record carries the new bytes. The record
``reverify.json`` merges across calls.

Negative-spec: zero process spawns, no git verb, no test execution. A refusal (no manifest, a
path escaping the worktree, an unreadable or corrupt blob) is returned as ``error`` with nothing
written, never raised. Attribution is per file, never per hunk.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.pyimports import collect_imports, module_name_for_file, resolve_relative_module
from coordinator_core.session.core import resolve_session_id
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.session.machinery_paths import share_dir
from coordinator_core.session.touch_record import compute_content_hash

MUTATES = [
    ".coordinator-local/subagent-share/*/*.pre-review/reverify.json",
    "<review-edited product files named by the snapshot manifest>",
]

_MANIFEST = "manifest.json"
_RECORD = "reverify.json"
_MACHINERY_PREFIXES = (".coordinator-local/", "state/")


def _fail(message: str, revert: bool) -> dict:
    if revert:
        return {"reverted": [], "unattributed": [], "record": None, "error": message}
    return {"edited": [], "created": [], "snapshot": None, "error": message}


def _norm(path: str) -> str:
    text = str(path).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _rel(path: str, repo_root: Path) -> str:
    """Repo-relative POSIX form; an absolute path under the root is relativised."""
    text = _norm(path)
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        try:
            return Path(text).resolve().relative_to(repo_root.resolve()).as_posix()
        except (ValueError, OSError):
            return text
    return text


def _node_file(node_id: str) -> str:
    return _norm(node_id.split("::", 1)[0])


def _read_manifest(snapshot_dir: Path) -> Optional[dict]:
    try:
        data = json.loads((snapshot_dir / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    return data


def _since(recorded_at: object) -> Optional[float]:
    if not isinstance(recorded_at, str):
        return None
    try:
        parsed = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _created(repo_root: Path, session: str, manifest: dict, share: Path) -> List[str]:
    from coordinator_core.ops.dispatch_emit import review_edits

    writes = review_edits.review_writes(repo_root, session, _since(manifest.get("recorded_at")))
    if not writes:
        return []
    known: Set[str] = {_norm(p) for p in manifest["files"]}
    known.update(_norm(p) for p in (manifest.get("paths") or []) if isinstance(p, str))
    share_rel = _rel(str(share), repo_root)
    out: Set[str] = set()
    for raw in writes:
        rel = _rel(raw, repo_root)
        if rel in known or rel.startswith(_MACHINERY_PREFIXES) or rel.startswith(share_rel + "/"):
            continue
        if (repo_root / rel).is_file():
            out.add(rel)
    return sorted(out)


def _edited(repo_root: Path, manifest: dict) -> List[str]:
    return sorted(
        _norm(rel)
        for rel, digest in manifest["files"].items()
        if compute_content_hash(repo_root / _norm(rel)) != digest
    )


def _contained(repo_root: Path, rels: List[str]) -> bool:
    return all(contained_path(repo_root / rel, [repo_root]) is not None for rel in rels)


def _module_files(module: str) -> List[str]:
    base = module.replace(".", "/")
    return [f"{base}.py", f"{base}/__init__.py"]


def _imported_edited(repo_root: Path, test_file: str, edited_py: Set[str]) -> Set[str]:
    """Edited ``.py`` paths that ``test_file`` imports; empty when it is unreadable or unparseable."""
    try:
        tree = ast.parse((repo_root / test_file).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return set()
    records, _ = collect_imports(tree)
    is_pkg = test_file.rsplit("/", 1)[-1] == "__init__.py"
    hit: Set[str] = set()
    for rec in records:
        if rec.level == 0:
            base = rec.module or None
        else:
            base = resolve_relative_module(rec.module, rec.level, module_name_for_file(test_file), is_pkg)
        if not base:
            continue
        wanted = list(_module_files(base))
        for name in rec.names:
            if name != "*":
                wanted.extend(_module_files(f"{base}.{name}"))
        hit.update(p for p in wanted if p in edited_py)
    return hit


def _findings(repo_root: Path, sidecars: List[str], rel: str) -> List[dict]:
    from coordinator_core.ops import review_findings_ledger

    out: List[dict] = []
    for sidecar in sidecars:
        path = Path(sidecar)
        path = path if path.is_absolute() else repo_root / path
        if contained_path(path, [repo_root]) is None:
            continue
        got = review_findings_ledger.read_ledger(path)
        rows = got[1] if got else []
        for row in rows:
            if _norm(str(row.get("file") or "")) == rel:
                out.append({"sidecar": sidecar, "id": row.get("id")})
    return out


def _load_record(record: Path) -> dict:
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"reverted": [], "unattributed": []}
    if not isinstance(data, dict):
        return {"reverted": [], "unattributed": []}
    data.setdefault("reverted", [])
    data.setdefault("unattributed", [])
    return data


def review_reverify(
    repo_root: Path,
    *,
    base: str,
    slice_prefix: str,
    session_id: str = "",
    failing: Optional[List[str]] = None,
    sidecars: Optional[List[str]] = None,
) -> dict:
    """List the review edits since the snapshot, or (``failing`` given) restore the ones those
    tests implicate; see the module docstring."""
    revert = failing is not None
    if not base or not slice_prefix:
        return _fail("base and slice_prefix are required", revert)
    session = session_id or resolve_session_id(str(repo_root))
    if not session:
        return _fail("no session id: pass session_id or run under a session", revert)
    for label, value in (("slice_prefix", slice_prefix), ("session_id", session)):
        if "/" in value or "\\" in value or ".." in value:
            return _fail(f"{label} must be a bare name, not a path", revert)
    share = Path(share_dir(str(repo_root), session))
    snapshot_dir = share / f"{slice_prefix}.pre-review"
    manifest = _read_manifest(snapshot_dir)
    if manifest is None:
        return _fail(f"no pre-review snapshot for {slice_prefix}", revert)
    recorded_base = manifest.get("base")
    if isinstance(recorded_base, str) and recorded_base and recorded_base != base:
        return _fail(f"base {base} does not match the snapshot base {recorded_base}", revert)

    rels = [_norm(p) for p in manifest["files"]]
    if not _contained(repo_root, rels):
        return _fail("a snapshot path escapes the worktree", revert)
    edited = _edited(repo_root, manifest)
    created = _created(repo_root, session, manifest, share)
    if not _contained(repo_root, created):
        return _fail("a review-written path escapes the worktree", revert)
    snapshot_rel = _rel(str(snapshot_dir / _MANIFEST), repo_root)
    if not revert:
        return {"edited": edited, "created": created, "snapshot": snapshot_rel, "error": None}

    ids = [str(i) for i in failing if isinstance(i, str) and i.strip()]
    edited_set, created_set = set(edited), set(created)
    edited_py = {p for p in edited if p.endswith(".py")}
    implicated: Dict[str, List[str]] = {}
    unattributed: List[str] = []
    record_path = snapshot_dir / _RECORD
    prior = _load_record(record_path)
    already = {t for entry in prior["reverted"] if isinstance(entry, dict) for t in entry.get("tests", [])}
    for node in ids:
        test_file = _node_file(node)
        targets: Set[str] = set()
        if test_file in edited_set or test_file in created_set:
            targets.add(test_file)
        if test_file.endswith(".py"):
            targets.update(_imported_edited(repo_root, test_file, edited_py))
        for target in targets:
            implicated.setdefault(target, []).append(node)
        if not targets and node not in already:
            unattributed.append(node)

    blobs: Dict[str, bytes] = {}
    for rel in implicated:
        if rel in created_set:
            continue
        digest = manifest["files"].get(rel)
        try:
            data = (snapshot_dir / str(digest)).read_bytes()
        except OSError:
            return _fail(f"snapshot blob for {rel} is unreadable", True)
        if hashlib.sha256(data).hexdigest() != digest:
            return _fail(f"snapshot blob for {rel} does not match its hash", True)
        blobs[rel] = data

    reverted: List[dict] = []
    for rel in sorted(implicated):
        target = repo_root / rel
        if rel in created_set:
            try:
                target.unlink()
            except OSError:
                return _fail(f"could not remove {rel}", True)
            action = "removed"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, blobs[rel])
            action = "restored"
        declare_write(target)
        reverted.append({
            "path": rel,
            "action": action,
            "tests": sorted(set(implicated[rel])),
            "findings": _findings(repo_root, list(sidecars or []), rel),
        })

    merged = {
        "reverted": [*prior["reverted"], *reverted],
        "unattributed": sorted(set(prior["unattributed"]) | set(unattributed)),
    }
    atomic_write_bytes(record_path, (json.dumps(merged, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    declare_write(record_path)
    return {
        "reverted": reverted,
        "unattributed": unattributed,
        "record": _rel(str(record_path), repo_root),
        "error": None,
    }


@register_op("review.reverify")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "review.reverify". Params: ``base`` (str), ``slice_prefix`` (str), optional
    ``session_id``, optional ``failing`` (list[str] node ids; presence selects revert mode),
    optional ``sidecars`` (list[str]). Keying scope: show_top."""
    revert = params.get("failing") is not None
    if repo_root is None:
        return _fail("review.reverify requires a show_top-keyed dispatch; repo_root was not supplied", revert)
    failing, sidecars = params.get("failing"), params.get("sidecars")
    if failing is not None and not isinstance(failing, list):
        return _fail("review.reverify failing must be a list of node ids", True)
    if sidecars is not None and not isinstance(sidecars, list):
        return _fail("review.reverify sidecars must be a list of paths", revert)
    return review_reverify(
        repo_root,
        base=str(params.get("base") or ""),
        slice_prefix=str(params.get("slice_prefix") or ""),
        session_id=str(params.get("session_id") or ""),
        failing=failing,
        sidecars=[str(s) for s in sidecars] if sidecars else None,
    )
