"""Shared post-fire predicate: once an EM main-thread session has launched a Workflow run, its
product writes belong to the run. The Edit/Write hook and the Bash write guard decide through
this one module; `GUARD_NAME` is a floor guard, so no guard level turns the deny into an advisory."""

from __future__ import annotations

import json
import os
import re

from coordinator_core._hook_envelope import deny
from coordinator_core.machine_profile import apply_guard_level
from coordinator_core.session import record_homes

GUARD_NAME = "block-em-post-fire-product-write"

_POST_FIRE_DENY = (
    "This session already fired engine-ask; its product edits belong to the run. "
    "Send the gap through workflow.revise, or fire a follow-up engine-ask whose em_context "
    "quotes the receipt's gap line."
)

_NON_PRODUCT_TOP_DIRS = ("state", ".coordinator-local")


def fired(session_id: str) -> bool:
    """True when this session launched a Workflow run, live or landed. False on an empty id or any
    read failure: the deny fails open."""
    if not session_id:
        return False
    try:
        from coordinator_core.hooks import flag_em_poll_in_flight

        return bool(flag_em_poll_in_flight.session_workflow_run_ids(session_id))
    except Exception:
        return False


def is_product_path(abs_path: str, root: str, session_id: str) -> bool:
    """True for a path inside `root` that is not scratch/tmp, under `state/` or `.coordinator-local/`,
    or a bootstrap/out-of-repo dir, or the surface an open `performer: em` row of a run `session_id`
    fired names in `writes:`. The bare repo root counts as product."""
    from coordinator_core.hooks._em_write_budget import (
        _is_bootstrap_or_out_of_repo,
        _is_scratch_or_tmp,
    )

    if not abs_path or not root:
        return False
    path = os.path.normcase(os.path.abspath(abs_path))
    base = os.path.normcase(os.path.abspath(root))
    if path != base and not path.startswith(base + os.sep):
        return False
    probe = abs_path
    if path != base:
        top = path[len(base) + 1 :].split(os.sep, 1)[0]
        if top in _NON_PRODUCT_TOP_DIRS:
            return False
    else:
        # the work-tree probe looks at the parent dir; give it the root's own
        probe = os.path.join(abs_path, ".")
    if _is_scratch_or_tmp(abs_path):
        return False
    if _is_bootstrap_or_out_of_repo(probe):
        return False
    return path == base or not _is_em_row_surface(path, base, session_id)


def _index_path(root: str) -> str:
    """`<gitdir>/index` for `root`, resolving a `.git` FILE's `gitdir:` line (linked worktree)."""
    dot_git = os.path.join(root, ".git")
    if os.path.isfile(dot_git):
        with open(dot_git, encoding="utf-8") as fh:
            line = fh.readline().strip()
        if not line.startswith("gitdir:"):
            raise ValueError("unreadable .git file")
        gitdir = line[len("gitdir:"):].strip()
        if not os.path.isabs(gitdir):
            gitdir = os.path.join(root, gitdir)
        return os.path.join(gitdir, "index")
    return os.path.join(dot_git, "index")


def _index_holds(data: bytes, rel: bytes) -> bool:
    """True when the v2/v3 index `data` has an entry for `rel`. Raises ValueError for a layout whose
    miss proves nothing: other versions (v4 prefix-compresses paths), a `link` (split index)
    extension, or a truncated file."""
    if len(data) < 12 or data[:4] != b"DIRC":
        raise ValueError("not a git index")
    version = int.from_bytes(data[4:8], "big")
    if version not in (2, 3):
        raise ValueError("unsupported index version")
    count = int.from_bytes(data[8:12], "big")
    pos = 12
    found = False
    for _ in range(count):
        if pos + 62 > len(data):
            raise ValueError("truncated index")
        flags = int.from_bytes(data[pos + 60 : pos + 62], "big")
        name_at = pos + 62 + (2 if version == 3 and flags & 0x4000 else 0)
        end = data.find(b"\x00", name_at)
        if end < 0:
            raise ValueError("truncated index")
        if data[name_at:end] == rel:
            found = True
        pos += (end - pos + 8) // 8 * 8
    ext_end = len(data) - 20
    while pos + 8 <= ext_end:
        sig = data[pos : pos + 4]
        if sig == b"link":
            raise ValueError("split index")
        pos += 8 + int.from_bytes(data[pos + 4 : pos + 8], "big")
    return found


def is_empty_untracked(abs_path: str, root: str) -> bool:
    """True only for a regular, non-symlink, zero-byte file inside `root` whose repo-relative path
    the index does not hold. Fails closed (False) on a hit or on anything unreadable, including a
    v4 or split index. Spawns no process."""
    try:
        if os.path.islink(abs_path) or not os.path.isfile(abs_path):
            return False
        if os.path.getsize(abs_path) != 0:
            return False
        rel = os.path.relpath(os.path.abspath(abs_path), os.path.abspath(root))
        if rel.startswith("..") or os.path.isabs(rel):
            return False
        with open(_index_path(root), "rb") as fh:
            data = fh.read()
        return not _index_holds(data, rel.replace(os.sep, "/").encode("utf-8"))
    except (OSError, ValueError):
        return False


_ROW_START = re.compile(r"^\s*-\s+id:\s*['\"]?([^'\"\s]+)")
_KEY_LINE = re.compile(r"^\s*([A-Za-z_]+):\s*(.*?)\s*$")
_ITEM_LINE = re.compile(r"^\s*-\s+['\"]?(.+?)['\"]?\s*$")


def _open_em_row_writes(plan_text: str) -> list[tuple[str, list[str]]]:
    """`(row_id, writes)` for each plan row declaring `performer: em` with no disposition or
    `disposition: open`. A line scan, not a YAML parse: it runs on the deny path under the DR-344 bar."""
    rows: list[tuple[str, dict]] = []
    in_writes = False
    for line in plan_text.splitlines():
        start = _ROW_START.match(line)
        if start:
            rows.append((start.group(1), {"writes": []}))
            in_writes = False
            continue
        if not rows:
            continue
        info = rows[-1][1]
        if in_writes:
            item = _ITEM_LINE.match(line)
            if item:
                info["writes"].append(item.group(1))
                continue
            in_writes = False
        key = _KEY_LINE.match(line)
        if not key:
            continue
        name, value = key.groups()
        if name == "writes":
            in_writes = True
        elif name in ("performer", "disposition"):
            info[name] = value.strip("'\"")
    return [
        (rid, info["writes"])
        for rid, info in rows
        if info.get("performer") == "em" and info.get("disposition", "open") == "open"
    ]


def _run_plan_file(root: str, run_id: str) -> str | None:
    """The plan file a run's sizing names, else the run's own minted spine."""
    run_dir = record_homes.record_path(root, "warp", run_id)
    try:
        with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as fh:
            sizing_rel = json.load(fh).get("sizing_rel")
        if isinstance(sizing_rel, str) and sizing_rel:
            with open(os.path.join(root, sizing_rel), encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("plan:"):
                        plan = line[5:].strip().strip("'\"")
                        if plan:
                            return os.path.join(root, plan)
                        break
        for name in os.listdir(run_dir):
            if name.endswith(".spine.md"):
                return os.path.join(run_dir, name)
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _is_em_row_surface(path: str, base: str, session_id: str) -> bool:
    """True when `path` (normcased, absolute) is named in `writes:` of an open `performer: em` row of
    a run this session fired. Any read failure answers False: the path stays product."""
    try:
        from coordinator_core.hooks import flag_em_poll_in_flight

        root = os.path.abspath(base)
        for run_id in flag_em_poll_in_flight.session_workflow_run_ids(session_id):
            plan = _run_plan_file(root, run_id)
            if not plan:
                continue
            with open(plan, encoding="utf-8") as fh:
                rows = _open_em_row_writes(fh.read())
            hits = [rid for rid, writes in rows if any(
                os.path.normcase(os.path.abspath(os.path.join(root, w))) == path for w in writes)]
            if not hits:
                continue
            from pathlib import Path

            from coordinator_core.ops.plan_tasks_mutate import read_em_discharges

            discharged = read_em_discharges(Path(plan))
            if any(rid not in discharged for rid in hits):
                return True
    except Exception:
        return False
    return False


def _deny_text(paths: tuple[str, ...]) -> str:
    """The generic deny, or the CLAUDE.md variant naming the EM grant route when any of `paths` is
    CLAUDE.md-class. Any import or classify failure answers the generic text."""
    try:
        from coordinator_core.claude_md_budget import is_claude_md_class
        from coordinator_core.session.claude_md_grant import GRANT_CLI_COMMAND

        if any(is_claude_md_class(p) for p in paths):
            return (
                "This session already fired engine-ask; its CLAUDE.md edit belongs to the run. "
                f"With the PM's approval run `{GRANT_CLI_COMMAND}` from the repo that holds the "
                "CLAUDE.md, then send the edit through workflow.revise."
            )
    except Exception:
        pass
    return _POST_FIRE_DENY


def deny_envelope(paths: tuple[str, ...] = ()) -> dict | None:
    """The PreToolUse deny, passed through `apply_guard_level` (floor: denies at every level)."""
    return apply_guard_level(GUARD_NAME, deny("PreToolUse", _deny_text(paths)))
