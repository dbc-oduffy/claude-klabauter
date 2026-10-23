"""Per-session XS by-hand lane / residue window: record + zero-spawn diff budget.

The record lives at `<git common dir>/coordinator-sessions/<sid>/xs-lane.json` as
`{kind: xs|residue, base_sha, state: open|spent, paths: [repo-relative]}`. `measure()` prices the
record's paths plus the triggering path as added+deleted lines (the `git diff --numstat` unit)
between the blob at the pinned `base_sha` and the working text, with the triggering edit applied in
memory. Nothing the EM declares enters the measurement.

Negative-spec:
  - No subprocess: base blobs are read in-process (`git_state.read_commit` ->
    `read_tree_spine` -> `git_objects.read_object`). A `git diff` spawn per edit is a defect.
  - `base_sha` is pinned at open and never re-read from HEAD, so a commit is not a budget refill.
  - A file whose text equals its base does not count; a revert frees its slot.
  - Line endings are priced as `git diff --numstat` prices them under autocrlf: CRLF is read as LF
    on both sides, so an EOL-only difference costs 0. LF-only files stay byte-exact.
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core.git import git_objects, git_state
from coordinator_core.lifecycle import git_common_dir

BUDGET_FILES = 2
BUDGET_LINES = 30

KIND_XS = "xs"
KIND_RESIDUE = "residue"
STATE_OPEN = "open"
STATE_SPENT = "spent"

_LANE_FILENAME = "xs-lane.json"


class Usage(NamedTuple):
    files: int
    lines: int
    over: bool


def lane_path(repo_root, sid: str) -> Path:
    from coordinator_core.session import core as _core

    hub = _core._sessions_dir_resolve(str(repo_root))
    if not hub:
        raise RuntimeError(f"session hub unresolvable for {repo_root!s}")
    return Path(hub) / sid / _LANE_FILENAME


def read_lane(repo_root, sid: str) -> Optional[dict]:
    """The session's lane record, or None when absent. An unreadable or malformed record reads as
    a `spent` xs record: the budget fails closed, never open."""
    try:
        path = lane_path(repo_root, sid)
    except Exception:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return _corrupt_record()
    try:
        rec = json.loads(raw)
    except ValueError:
        return _corrupt_record()
    if (
        not isinstance(rec, dict)
        or rec.get("kind") not in (KIND_XS, KIND_RESIDUE)
        or rec.get("state") not in (STATE_OPEN, STATE_SPENT)
        or not isinstance(rec.get("base_sha"), str)
        or not isinstance(rec.get("paths"), list)
    ):
        return _corrupt_record()
    return rec


def _corrupt_record() -> dict:
    return {"kind": KIND_XS, "state": STATE_SPENT, "base_sha": "", "paths": [], "corrupt": True}


def _write(repo_root, sid: str, rec: dict) -> None:
    path = lane_path(repo_root, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _create_exclusive(repo_root, sid: str, rec: dict) -> bool:
    """Create the record only if none exists. Atomic: a racing opener loses."""
    path = lane_path(repo_root, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    except OSError:
        if path.exists():
            return False
        os.replace(tmp, path)
        return True
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return True


def open_xs_lane(repo_root, sid: str, base_sha: str) -> bool:
    """Open the XS lane at `base_sha`. False, writing nothing, when any record exists."""
    return _create_exclusive(
        repo_root, sid, {"kind": KIND_XS, "base_sha": base_sha, "state": STATE_OPEN, "paths": []}
    )


def open_residue_window(repo_root, sid: str, base_sha: str) -> bool:
    """Open the post-landing residue window. Idempotent: True when a residue record exists or was
    created; False when an xs record holds the slot."""
    created = _create_exclusive(
        repo_root,
        sid,
        {"kind": KIND_RESIDUE, "base_sha": base_sha, "state": STATE_OPEN, "paths": []},
    )
    if created:
        return True
    rec = read_lane(repo_root, sid)
    return bool(rec) and rec.get("kind") == KIND_RESIDUE and not rec.get("corrupt")


def add_path(repo_root, sid: str, rel_path: str) -> None:
    rec = read_lane(repo_root, sid)
    if rec is None or rec.get("corrupt"):
        return
    rel = _norm_rel(repo_root, rel_path)
    if rel not in rec["paths"]:
        rec["paths"].append(rel)
        _write(repo_root, sid, rec)


def mark_spent(repo_root, sid: str) -> None:
    rec = read_lane(repo_root, sid)
    if rec is None or rec.get("corrupt") or rec.get("state") == STATE_SPENT:
        return
    rec["state"] = STATE_SPENT
    _write(repo_root, sid, rec)


def _norm_rel(repo_root, path: str) -> str:
    p = path.replace("\\", "/")
    root = os.path.abspath(str(repo_root)).replace("\\", "/").rstrip("/")
    if os.path.isabs(path):
        ap = os.path.abspath(path).replace("\\", "/")
        if ap == root or ap.startswith(root + "/"):
            return ap[len(root) + 1 :]
        return ap
    return os.path.normpath(p).replace("\\", "/")


def _abs(repo_root, rel: str) -> str:
    return rel if os.path.isabs(rel) else os.path.join(str(repo_root), rel)


def _read_text(abs_path: str) -> Optional[str]:
    try:
        with open(abs_path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None


def _lf(text: Optional[str]) -> Optional[str]:
    return text.replace("\r\n", "\n") if text else text


def project(file_path: str, tool_input: dict) -> Optional[str]:
    """The file's text after this tool call, computed in memory. None when it cannot be
    projected: a MultiEdit without an `edits` list, an `old_string` that is absent, or an
    unreadable file. A CRLF file is projected in LF, so an LF `old_string` finds its text."""
    if not isinstance(tool_input, dict):
        return None
    if isinstance(tool_input.get("content"), str) and "old_string" not in tool_input:
        return tool_input["content"]
    try:
        text = _read_text(file_path)
    except OSError:
        return None
    if text is None:
        return None
    crlf = "\r\n" in text
    text = _lf(text)
    edits = tool_input.get("edits")
    if "old_string" in tool_input:
        edits = [tool_input]
    if not isinstance(edits, list) or not edits:
        return None
    for ed in edits:
        if not isinstance(ed, dict):
            return None
        old, new = ed.get("old_string"), ed.get("new_string")
        if crlf:
            old, new = _lf(old), _lf(new)
        if not isinstance(old, str) or not isinstance(new, str) or not old or old not in text:
            return None
        text = text.replace(old, new) if ed.get("replace_all") else text.replace(old, new, 1)
    return text


def project_shell_write(file_path: str, body: str, append: bool) -> Optional[str]:
    """The file's text after a `>` (body replaces) or `>>` (current text + body) heredoc write,
    computed in memory in LF. An absent append target projects to the body; None when an append
    target is unreadable for any other reason."""
    if not isinstance(body, str):
        return None
    body = _lf(body)
    if not append:
        return body
    try:
        text = _read_text(file_path)
    except OSError:
        return None
    return body if text is None else _lf(text) + body


def _base_texts(repo_root, base_sha: str, rels: list) -> dict:
    """{rel: base text or None when absent at base}. Raises when the base is unreadable."""
    commit = git_state.read_commit(repo_root, base_sha)
    if commit is None:
        raise RuntimeError(f"base commit unreadable: {base_sha!r}")
    spine = git_state.read_tree_spine(repo_root, rels, root_tree_sha=commit.tree)
    if spine is None:
        raise RuntimeError("base tree unreadable")
    common_dir = git_common_dir(Path(repo_root))
    out: dict = {}
    for rel in rels:
        directory, _, name = rel.rpartition("/")
        entry = spine.get(directory, {}).get(name)
        if entry is None:
            out[rel] = None
            continue
        obj = git_objects.read_object(common_dir, entry[1])
        if obj is None or obj[0] != "blob":
            raise RuntimeError(f"base blob unreadable: {rel}")
        out[rel] = obj[1].decode("utf-8", errors="replace")
    return out


def _changed_lines(base: Optional[str], now: Optional[str]) -> int:
    base = _lf(base) or ""
    now = _lf(now) or ""
    if base == now:
        return 0
    a, b = base.splitlines(), now.splitlines()
    if not a or not b:
        return len(a) + len(b)
    total = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            total += (i2 - i1) + (j2 - j1)
    # A trailing-newline-only or EOL-only change has no line delta but is a real change.
    return total or 1


def measure(repo_root, record: dict, triggering_path: str, projected_text: str) -> Usage:
    """Budget use if `projected_text` becomes `triggering_path`'s content. Raises on an unreadable
    base; the caller denies that edit as unmeasurable."""
    trigger = _norm_rel(repo_root, triggering_path)
    rels = list(dict.fromkeys([*(_norm_rel(repo_root, p) for p in record.get("paths", [])), trigger]))
    bases = _base_texts(repo_root, record["base_sha"], rels)
    files = lines = 0
    for rel in rels:
        now = projected_text if rel == trigger else _read_text(_abs(repo_root, rel))
        n = _changed_lines(bases[rel], now)
        if n:
            files += 1
            lines += n
    return Usage(files, lines, files > BUDGET_FILES or lines > BUDGET_LINES)


def touched_s_or_larger(sid: str, repo_root) -> bool:
    """True when this session touched a sizing object whose tshirt is not XS (an unreadable object
    counts as larger). The single predicate for the re-size ratchet and the guard's branch order."""
    import re

    from coordinator_core.session.scope import _TOUCH_RECORD_FILENAME, _read_touch_record_as_legacy_lines
    from coordinator_core.session.scope import parse_touch_event

    try:
        sink = lane_path(repo_root, sid).parent / _TOUCH_RECORD_FILENAME
        lines, degraded = _read_touch_record_as_legacy_lines(sink)
    except Exception:
        return False
    if degraded:
        return False
    pat = re.compile(r"^state/sizings/[^/]+\.ya?ml$")
    rels = {p for p in (parse_touch_event(ln)[2] for ln in lines) if p and pat.match(p)}
    if not rels:
        return False
    import yaml

    for rel in rels:
        try:
            with open(os.path.join(str(repo_root), rel), "r", encoding="utf-8") as fh:
                obj = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            return True
        est = obj.get("estimate") if isinstance(obj, dict) else None
        if isinstance(est, dict):
            tshirt = est.get("tshirt")
        else:
            tshirt = obj.get("tshirt") if isinstance(obj, dict) else None
        if tshirt != "XS":
            return True
    return False
