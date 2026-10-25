"""coordinator_core.bash_guards._declared_delete_license -- the declared-delete license.

A dispatched subagent qualifies a delete with an assignment prefix naming its run row:

    COORDINATOR_DISPATCH_ROW=<run_id>/<row_id> git rm -q -- <path> [<path> ...]
    COORDINATOR_DISPATCH_ROW=<run_id>/<row_id> rm -- <path> [<path> ...]

The prefix is inert to git and rm; the guard reads it from the command text beside the operands
it licenses, because no hook payload carries the brief or the row id.

Trust model: the license is the row's ``writes`` in ``<record_dir>/manifest.json``. The marker is
accepted only when the run's emission receipt names the payload's ``session_id``, so a subagent
cannot borrow a foreign session's run.

Precision bound: the marker is self-asserted. A subagent can name a sibling row of its own run;
``scratch.delete`` ships with the same bound. ``git rm`` without ``-f`` refuses a file with
staged or unstaged changes, so a wrongly admitted delete removes only HEAD-recoverable content.

Pure: no subprocess. The dispatch_emit and git_state helpers import inside the functions.
"""

from __future__ import annotations

import json
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Union

MARKER_VAR = "COORDINATOR_DISPATCH_ROW"

_ROW_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DRIVE_RE = re.compile(r"^[A-Za-z]:/")
_GLOB_CHARS = frozenset("*?[]{}")


@dataclass(frozen=True)
class Marker:
    run_id: str
    row_id: str


class _Ambiguous:
    def __repr__(self) -> str:
        return "AMBIGUOUS"


#: Two distinct marker values appear in one command.
AMBIGUOUS = _Ambiguous()


@dataclass(frozen=True)
class License:
    run_id: str
    row_id: str
    paths: frozenset  # repo-relative POSIX file paths


def _marker_from_value(value: str) -> Optional[Marker]:
    from coordinator_core.ops._path_guard import safe_id

    run_id, sep, row_id = value.partition("/")
    if not sep or not run_id or not safe_id(run_id) or not _ROW_ID_RE.match(row_id):
        return None
    if row_id in (".", ".."):
        return None
    return Marker(run_id=run_id, row_id=row_id)


def parse_marker(cmd: str) -> Union[Marker, None, _Ambiguous]:
    """The marker in assignment-prefix position of any segment, else None.

    None also for an unparseable command, an unsafe value, or a marker elsewhere (an ``export``,
    an argument). AMBIGUOUS when two distinct values appear.
    """
    from coordinator_core.bash_guards._command_tokenizer import (
        segments_from_tokens_simple,
        tokenize_full_command,
    )

    tokens = tokenize_full_command(cmd)
    if not tokens:
        return None
    prefix = MARKER_VAR + "="
    found: set = set()
    for seg in segments_from_tokens_simple(tokens):
        for tok in seg:
            if not _ASSIGNMENT_RE.match(tok):
                break
            if tok.startswith(prefix):
                found.add(tok[len(prefix):])
    if not found:
        return None
    if len(found) > 1:
        return AMBIGUOUS
    return _marker_from_value(next(iter(found)))


def _norm_rel(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def resolve_license(repo_root: Path, marker: Marker, session_id: str) -> Union[License, str]:
    """The row's declared write set, or a refusal reason string."""
    from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest, record_dir
    from coordinator_core.ops.dispatch_emit.halted_run import recorded_emitter

    try:
        raw = (record_dir(repo_root, marker.run_id) / "manifest.json").read_text(encoding="utf-8")
        manifest = StageManifest.from_json(json.loads(raw))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return f"no readable manifest for run {marker.run_id}"
    row = next((r for r in manifest.rows if r.id == marker.row_id), None)
    if row is None:
        return f"run {marker.run_id} has no row {marker.row_id}"
    emitter = recorded_emitter(repo_root, marker.run_id)
    if not emitter:
        return f"run {marker.run_id} has no emission receipt"
    if not session_id or emitter != session_id:
        return f"run {marker.run_id} was emitted by another session"
    return License(
        run_id=marker.run_id,
        row_id=marker.row_id,
        paths=frozenset(_norm_rel(w) for w in row.writes),
    )


def normalize_operand(op: str, cwd: str, repo_root: Path) -> Optional[str]:
    """The repo-relative POSIX path of `op`, else None.

    None for an operand outside the repo, naming the repo root, containing glob characters or
    ``$``, or starting with ``:`` (pathspec magic).
    """
    if not op or op.startswith(":") or "$" in op or _GLOB_CHARS & set(op):
        return None
    root = posixpath.normpath(str(repo_root).replace("\\", "/"))
    path = op.replace("\\", "/")
    if not (path.startswith("/") or _DRIVE_RE.match(path)):
        base = cwd.replace("\\", "/") if cwd else root
        path = base + "/" + path
    path = posixpath.normpath(path)
    if _DRIVE_RE.match(root):
        root_cmp, path_cmp = root.casefold(), path.casefold()
    else:
        root_cmp, path_cmp = root, path
    if not path_cmp.startswith(root_cmp.rstrip("/") + "/"):
        return None
    return path[len(root.rstrip("/")) + 1:]


_CASE_MEMO: dict = {}
_IGNORECASE_RE = re.compile(r"^\s*ignorecase\s*=\s*(\S+)", re.IGNORECASE)
_SECTION_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9.-]+)(\s+\"[^\"]*\")?")


def _config_ignorecase(config: Path) -> Optional[bool]:
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_core = False
    value: Optional[bool] = None
    for line in text.splitlines():
        sec = _SECTION_RE.match(line)
        if sec:
            in_core = sec.group(1).lower() == "core" and sec.group(2) is None
            continue
        m = _IGNORECASE_RE.match(line) if in_core else None
        if m:
            value = m.group(1).strip().lower() in ("true", "yes", "on", "1")
    return value


def case_insensitive(repo_root: Path) -> bool:
    """Whether the repo's filesystem folds path case: git config ``core.ignorecase``, else the OS default.

    Reads the common then the private git dir's config as text (no spawn); include and global config are not read.
    Memoised per repo root for the process lifetime.
    """
    key = str(repo_root)
    if key in _CASE_MEMO:
        return _CASE_MEMO[key]
    from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir

    result: Optional[bool] = None
    for d in dict.fromkeys((resolve_git_common_dir(repo_root), resolve_git_dir(repo_root))):
        result = _config_ignorecase(d / "config")
        if result is not None:
            break
    if result is None:
        result = sys.platform in ("win32", "darwin")
    _CASE_MEMO[key] = result
    return result


def declared(rel: str, paths: frozenset, repo_root: Path) -> bool:
    """Whether `rel` is in the row's declared `paths`, case-folded on a case-insensitive repo.

    Folds only after the exact lookup misses.
    """
    if rel in paths:
        return True
    return case_insensitive(repo_root) and rel.casefold() in {p.casefold() for p in paths}


def tracked_subset(repo_root: Path, rels: Iterable[str]) -> Optional[frozenset]:
    """The members of `rels` in the git index, or None when the index is unreadable or split.

    None means "assume tracked": the caller fails closed. On a case-insensitive repo a member
    missing by exact case is matched against the casefolded index.
    """
    from coordinator_core.git.git_state import IndexParseError, read_index_paths

    try:
        indexed = read_index_paths(repo_root)
    except (IndexParseError, OSError):
        return None
    rels = list(rels)
    hit = {r for r in rels if r in indexed}
    missed = [r for r in rels if r not in hit]
    if missed and case_insensitive(repo_root):
        folded = {p.casefold() for p in indexed}
        hit.update(r for r in missed if r.casefold() in folded)
    return frozenset(hit)


def brief_clause(run_id: str, row_id: str) -> str:
    """One brief line naming both delete forms for this row."""
    marker = f"{MARKER_VAR}={run_id}/{row_id}"
    return (
        f"To delete a file this row declares, prefix the command with {marker}: "
        f"`{marker} git rm -q -- <path>` or `{marker} rm -- <path>`. "
        "A path this row does not declare, -r and -f are refused."
    )
