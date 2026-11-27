"""JSON-RPC "baton.bulk_set": stamp `owner_session` and `eta` on many batons and optionally close them.

Reads only the listed paths, and only each file's frontmatter block. The keys are edited in place as
line-level text, so every other byte is preserved. A `close` item applies the terminal-flip line edit
in the same pass. Each file is opened once, read once and rewritten in place under one batch lock; all land as one
`commit_paths` commit; `dry_run` writes and commits nothing, and a repeat call is a no-op.

Self-registration: importing this module registers the op; ops/__init__.py imports it eagerly.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Optional

from coordinator_core.frontmatter.primitives import (
    replace_fm_field_raw,
    serialize_yaml_scalar,
    unquote_yaml_scalar,
    _strip_trailing_comment,
)
from coordinator_core.git.commit import (
    CommitRefused,
    FilterUnsupported,
    commit_paths,
    hash_worktree_blobs_via_spawn,
)
from coordinator_core.git.commit_trailers import apply_missing_trailers
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.index_write import IndexStaleAfterCommit, IndexWriteError
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.locked_write import LockTimeout, held_lock
from coordinator_core.ops.baton_bulk_set_contract import (
    CLOSE_VERDICTS,
    OP_KEY,
    SCHEMA_VERSION,
    RefusalReason,
)
from coordinator_core.ops.deliverable_cascade import _iso_now

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_FM_OPEN = re.compile(rb"---[ \t]*\r?\n")
_FM_CLOSE = re.compile(rb"^---[ \t]*\r?$", re.MULTILINE)
_OPEN_RO = os.O_RDONLY | getattr(os, "O_BINARY", 0)
_OPEN_RW = os.O_RDWR | getattr(os, "O_BINARY", 0)
_SHIPPED_IN_KIND = "successor"
# trap: trailer resolution reads every pathspec file to infer one Deliverable-Id, and a set this wide spans deliverables, so it resolves to none.
_TRAILER_PATH_CAP = 20
_NESTED_AFTER_KEY = re.compile(r"\r?\n(?:[ \t]|- )")


class _Unwritable(Exception):
    """A frontmatter shape a single-line edit would corrupt."""


@lru_cache(maxsize=None)
def _key_rx(key: str) -> re.Pattern[str]:
    return re.compile(r"^(" + re.escape(key) + r":)([ \t]*)([^\r\n]*)", re.MULTILINE)


@lru_cache(maxsize=2048)
def _scalar(value: str, numeric: bool) -> str:
    return serialize_yaml_scalar(value, numeric_quoting=numeric)


def _refuse(path: str, reason: RefusalReason, detail: str) -> dict:
    return {"handoff_path": path, "reason": reason.value, "detail": detail}


def _valid_eta(eta: str) -> bool:
    try:
        if len(eta) == 10:
            date.fromisoformat(eta)
        else:
            datetime.fromisoformat(eta)
    except ValueError:
        return False
    return True


def _evidence_refusal(close: Any, git_dir: Path, known: dict[str, Optional[tuple[RefusalReason, str]]]) -> Optional[tuple[RefusalReason, str]]:
    if not isinstance(close, dict):
        return RefusalReason.CLOSE_WITHOUT_EVIDENCE, "close must be an object {verdict, reason, evidence}"
    if close.get("verdict") not in CLOSE_VERDICTS:
        return RefusalReason.CLOSE_WITHOUT_EVIDENCE, f"close.verdict must be one of {list(CLOSE_VERDICTS)}"
    evidence = close.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return RefusalReason.CLOSE_WITHOUT_EVIDENCE, "close.evidence is required"
    evidence = evidence.strip()
    if evidence in known:
        return known[evidence]
    verdict: Optional[tuple[RefusalReason, str]] = None
    if not _SHA_RE.match(evidence):
        verdict = RefusalReason.BAD_EVIDENCE, "close.evidence is not a 40-hex commit sha"
    else:
        found = read_object(git_dir, evidence)
        if found is None or found[0] != "commit":
            verdict = RefusalReason.BAD_EVIDENCE, "close.evidence names no commit object in this repository"
    known[evidence] = verdict
    return verdict


def _frontmatter_span(data: bytes) -> Optional[tuple[int, int]]:
    """Return (frontmatter start, closing-fence start), or None when the file has no leading `---` block."""
    opened = _FM_OPEN.match(data)
    if opened is None:
        return None
    closed = _FM_CLOSE.search(data, opened.end())
    if closed is None:
        return None
    return opened.end(), closed.start()


def _read_all(fd: int, size: int) -> bytes:
    data = os.read(fd, size + 1) if size else b""
    while len(data) < size:
        more = os.read(fd, size + 1 - len(data))
        if not more:
            break
        data += more
    return data


def _value(fm: str, key: str) -> Optional[str]:
    match = _key_rx(key).search(fm)
    if match is None:
        return None
    raw = match.group(3)
    if "#" in raw or raw[:1] in ("'", '"'):
        return unquote_yaml_scalar(_strip_trailing_comment(raw.strip()))
    return raw.strip()


def _set_line(fm: str, key: str, serialized: str, eol: str, after: Optional[str] = None) -> str:
    """Set `key` to the already-serialized scalar: replace in place, else insert after `after`, else
    append before the closing fence. Raises _Unwritable for a block-scalar or nested-block value."""
    match = _key_rx(key).search(fm)
    if match is not None:
        raw = match.group(3)
        stripped = raw.strip()
        if stripped[:1] in (">", "|") or (
            stripped == "" and _NESTED_AFTER_KEY.match(fm, match.end()) is not None
        ):
            raise _Unwritable(f'field "{key}" holds a block value a single-line edit would truncate')
        if "#" in raw:
            return replace_fm_field_raw(fm, key, serialized)
        return fm[: match.start(2)] + (match.group(2) or " ") + serialized + fm[match.end(3):]
    line = f"{key}: {serialized}"
    if after is not None:
        anchor = _key_rx(after).search(fm)
        if anchor is not None:
            return fm[: anchor.end()] + eol + line + fm[anchor.end():]
    if fm and not fm.endswith("\n"):
        fm += eol
    return fm + line + eol


def _stamped_fm(fm: str, owner: str, eta: str, eol: str) -> str:
    fm = _set_line(fm, "owner_session", _scalar(owner, True), eol)
    return _set_line(fm, "eta", _scalar(eta, True), eol, after="owner_session")


def _flipped_fm(fm: str, deliverable_id: str, shipped_in: str, advanced_at: str, eol: str) -> str:
    anchor = "claimed_at" if _key_rx("claimed_at").search(fm) else "consumed_at"
    for key, value, after, numeric in (
        ("shipped_in", shipped_in, anchor, True),
        ("shipped_in_kind", _SHIPPED_IN_KIND, "shipped_in", False),
        ("deployment_state", "shipped", "status", False),
        ("pickup_ready", "false", "deployment_state", False),
        ("advanced_by", deliverable_id, "deployment_state", False),
        ("advanced_at", advanced_at, "advanced_by", False),
    ):
        fm = _set_line(fm, key, _scalar(value, numeric), eol, after=after)
    return fm


def _run(params: dict, repo_root: Optional[Path]) -> dict:
    if repo_root is None:
        return {"exit_code": 1, "error": f"{OP_KEY}: repo_root is required (no founding root available)"}
    if params.get("dry_run"):
        return _run_locked(params, repo_root)
    try:
        with held_lock(Path(repo_root) / OP_KEY):
            return _run_locked(params, repo_root)
    except LockTimeout as exc:
        return {"exit_code": 1, "error": f"{OP_KEY}: timed out waiting for the batch lock: {exc}"}


def _validate(items: list, git_dir: Path, refused: list[dict]) -> list[tuple[str, str, str, Optional[str]]]:
    """Shape-check every item once, before any file I/O; returns (rel, owner, eta, close_sha)."""
    valid: list[tuple[str, str, str, Optional[str]]] = []
    evidence_cache: dict[str, Optional[tuple[RefusalReason, str]]] = {}
    seen: set[str] = set()
    for item in items:
        raw = item.get("path") if isinstance(item, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            refused.append(_refuse(str(raw or ""), RefusalReason.NOT_FOUND, "item.path is required"))
            continue
        rel = raw.strip().replace("\\", "/")
        rel = rel[2:] if rel.startswith("./") else rel
        if rel in seen:
            refused.append(_refuse(rel, RefusalReason.WRITE_FAILED, "path is listed more than once in items"))
            continue
        seen.add(rel)
        owner, eta, close = item.get("owner_session"), item.get("eta"), item.get("close")
        if not isinstance(owner, str) or not owner.strip():
            refused.append(_refuse(rel, RefusalReason.BAD_OWNER, "owner_session must be a non-empty string"))
            continue
        if not isinstance(eta, str) or not _valid_eta(eta.strip()):
            refused.append(_refuse(rel, RefusalReason.BAD_ETA, "eta must be an ISO-8601 date or datetime string"))
            continue
        close_sha: Optional[str] = None
        if close is not None:
            problem = _evidence_refusal(close, git_dir, evidence_cache)
            if problem is not None:
                refused.append(_refuse(rel, problem[0], problem[1]))
                continue
            close_sha = close["evidence"].strip()
        valid.append((rel, owner.strip(), eta.strip(), close_sha))
    return valid


def _contained(root: str, rel: str) -> Optional[str]:
    """Join `rel` under `root` lexically; None when it is absolute or leaves the root."""
    if os.path.isabs(rel) or rel[:1] == "/" or os.path.splitdrive(rel)[0]:
        return None
    joined = os.path.normpath(os.path.join(root, rel))
    if not joined.startswith(root + os.sep):
        return None
    return joined


def _restore_uncommitted(worktree: Path, written: dict[str, tuple[bytes, int]], originals: dict[str, bytes]) -> None:
    """Put back the pre-edit bytes of every file this call wrote, so a retry re-stamps instead of
    reading the uncommitted edit as `unchanged`. A file a peer has since changed is left alone."""
    for rel, (data, _mtime) in written.items():
        target = Path(worktree) / rel
        try:
            if target.read_bytes() == data:
                target.write_bytes(originals[rel])
        except OSError:
            continue


def _run_locked(params: dict, repo_root: Path) -> dict:
    items = params.get("items")
    if not isinstance(items, list) or not items:
        return {"exit_code": 1, "error": f"{OP_KEY}: 'items' must be a non-empty list of {{path, owner_session, eta, close?}}"}
    dry_run = bool(params.get("dry_run", False))

    git_dir = git_common_dir(Path(repo_root))
    worktree = main_worktree_root(git_dir)
    root = os.path.normpath(str(worktree))

    applied: list[dict] = []
    unchanged: list[dict] = []
    refused: list[dict] = []
    pending: list[str] = []
    written: dict[str, tuple[bytes, int]] = {}
    originals: dict[str, bytes] = {}
    advanced_at = _iso_now()

    for rel, owner, eta, close_sha in _validate(items, git_dir, refused):
        path = _contained(root, rel)
        if path is None:
            refused.append(_refuse(rel, RefusalReason.NOT_FOUND, "path is not an existing file in this repository"))
            continue
        try:
            fd = os.open(path, _OPEN_RO if dry_run else _OPEN_RW)
        except OSError:
            refused.append(_refuse(rel, RefusalReason.NOT_FOUND, "path is not an existing file in this repository"))
            continue
        wrote: Optional[bytes] = None
        wrote_mtime = 0
        try:
            try:
                seen_stat = os.fstat(fd)
                whole = _read_all(fd, seen_stat.st_size)
                span = _frontmatter_span(whole)
                fm = None if span is None else whole[span[0]:span[1]].decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                refused.append(_refuse(rel, RefusalReason.UNREADABLE_FRONTMATTER, str(exc)))
                continue
            if span is None or fm is None:
                refused.append(_refuse(rel, RefusalReason.UNREADABLE_FRONTMATTER, "no parseable YAML frontmatter"))
                continue

            terminal = _value(fm, "deployment_state") in HANDOFF_TERMINAL_DEPLOYMENT
            deliverable_id: Optional[str] = None
            closing = close_sha is not None and not terminal
            if close_sha is not None:
                deliverable_id = _value(fm, "deliverable_id")
                if not deliverable_id:
                    refused.append(_refuse(rel, RefusalReason.MISSING_DELIVERABLE_ID, "baton frontmatter has no deliverable_id"))
                    continue

            same = _value(fm, "owner_session") == owner and _value(fm, "eta") == eta
            if same and not closing:
                unchanged.append({"handoff_path": rel, "owner_session": owner, "eta": eta})
                continue

            if not dry_run:
                eol = "\r\n" if "\r\n" in fm else "\n"
                try:
                    new_fm = fm if same else _stamped_fm(fm, owner, eta, eol)
                    if closing:
                        new_fm = _flipped_fm(new_fm, deliverable_id or "", close_sha or "", advanced_at, eol)
                except _Unwritable as exc:
                    refused.append(_refuse(rel, RefusalReason.WRITE_FAILED, str(exc)))
                    continue
                data = whole[:span[0]] + new_fm.encode("utf-8") + whole[span[1]:]
                try:
                    now_stat = os.fstat(fd)
                    if (now_stat.st_mtime_ns, now_stat.st_size) != (seen_stat.st_mtime_ns, seen_stat.st_size):
                        refused.append(_refuse(rel, RefusalReason.WRITE_FAILED, "changed since read"))
                        continue
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.write(fd, data) != len(data):
                        raise OSError("short write")
                    if len(data) < len(whole):
                        os.ftruncate(fd, len(data))
                    wrote_mtime = os.fstat(fd).st_mtime_ns
                except OSError as exc:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        os.write(fd, whole)
                        os.ftruncate(fd, len(whole))
                    except OSError:
                        pass
                    refused.append(_refuse(rel, RefusalReason.WRITE_FAILED, f"write failed: {exc}"))
                    continue
                wrote = data
        finally:
            os.close(fd)

        if wrote is not None:
            pending.append(rel)
            written[rel] = (wrote, wrote_mtime)
            originals[rel] = whole
        applied.append({"handoff_path": rel, "owner_session": owner, "eta": eta, "closed": closing})

    pathspec = sorted(pending)
    commit_sha: Optional[str] = None
    if pathspec:
        message = apply_missing_trailers(
            f"{OP_KEY}: {len(pathspec)} baton(s)",
            worktree,
            pathspec if len(pathspec) <= _TRAILER_PATH_CAP else None,
        )
        try:
            landed = commit_paths(
                worktree,
                pathspec,
                message,
                prefer_deliberate_stage=True,
                blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree),
                written_content=written,
            )
            commit_sha = landed.sha
        except IndexStaleAfterCommit as exc:
            commit_sha = getattr(exc.outcome, "sha", None)
        except (CommitRefused, FilterUnsupported, IndexWriteError) as exc:
            _restore_uncommitted(worktree, written, originals)
            return {
                "exit_code": 1,
                "error": f"{OP_KEY}: commit failed: {exc}",
                "applied": applied,
                "unchanged": unchanged,
                "refused": refused,
                "pathspec": pathspec,
            }
    if dry_run:
        pathspec = sorted(a["handoff_path"] for a in applied)

    return {
        "exit_code": 0,
        "schema_version": SCHEMA_VERSION,
        "op": OP_KEY,
        "applied": applied,
        "unchanged": unchanged,
        "refused": refused,
        "dry_run": dry_run,
        "commit_sha": commit_sha,
        "pathspec": pathspec,
    }


@register_op(OP_KEY)
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Stamp owner_session/eta (and optionally close) on the listed batons in one commit.

    Required param `items`: a non-empty list of {path, owner_session, eta, close?: {verdict, reason,
    evidence}}; `path` is repo-relative, `eta` an ISO-8601 date or datetime, `close.evidence` a
    40-hex sha that names a commit in this repository. Optional `dry_run` (default false): report
    the changes, write and commit nothing. Returns {exit_code, schema_version, op, applied,
    unchanged, refused, dry_run, commit_sha, pathspec}; `commit_sha` is null when nothing was written.
    """
    return await asyncio.to_thread(_run, params, repo_root)
