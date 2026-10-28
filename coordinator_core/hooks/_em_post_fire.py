"""Shared post-fire predicate: once an EM main-thread session has launched a Workflow run, the
product paths a LIVE run owns (`is_product_path`) belong to it; every other product path stays with
the XS budget. A halted or landed run owns nothing. The Edit/Write hook and the Bash write guard
decide through this one module; `GUARD_NAME` is not in `deny_registry`, so `apply_guard_level`
resolves the would-be deny to a log-only advisory record and the write proceeds."""

from __future__ import annotations

import json
import os
import re

from coordinator_core._hook_envelope import deny
from coordinator_core.bash_guards._commit_route import COMMIT_ROUTE
from coordinator_core.machine_profile import apply_guard_level
from coordinator_core.session import record_homes

GUARD_NAME = "block-em-post-fire-product-write"

_POST_FIRE_DENY = (
    "Engine route for a gap in a fired run: workflow.revise, or a follow-up engine-ask whose "
    "em_context quotes the receipt's gap line. "
    f"Commit via: `{COMMIT_ROUTE}`."
)

_NON_PRODUCT_TOP_DIRS = ("state", ".coordinator-local")


def live_run_ids(session_id: str, root: str) -> list:
    """Engine run ids this session launched that are neither halted nor landed. Empty on an empty
    id or any read failure: the record fails open."""
    if not session_id:
        return []
    try:
        from pathlib import Path

        from coordinator_core.hooks import flag_em_poll_in_flight
        from coordinator_core.ops import warp_run_deliverable as wrd

        live = []
        for run_id in flag_em_poll_in_flight.session_workflow_run_ids(session_id):
            engine_id = _engine_run_id(run_id, session_id)
            if root and (wrd._halted(Path(root), engine_id) or wrd._landed(Path(root), engine_id)):
                continue
            live.append(engine_id)
        return live
    except Exception:
        return []


def fired(session_id: str, root: str = "") -> bool:
    """True when this session has a live Workflow run: launched, and neither halted nor landed
    under `root`. Without `root` liveness is unknowable and any launched run counts. False on an
    empty id or any read failure."""
    if not session_id:
        return False
    if root:
        return bool(live_run_ids(session_id, root))
    try:
        from coordinator_core.hooks import flag_em_poll_in_flight

        return bool(flag_em_poll_in_flight.session_workflow_run_ids(session_id))
    except Exception:
        return False


def _owned_by_live_run(path: str, base: str, session_id: str) -> bool:
    """True when `path` (normcase, absolute) is, or is a directory holding, a path a live run of
    this session declares: a manifest or commit-request path, a declared prefix, or a `writes:`
    entry of an engine row of the run's plan. Halted and landed runs own nothing."""
    from pathlib import Path

    from coordinator_core.ops import warp_run_deliverable as wrd

    root = os.path.abspath(base)
    held = path.rstrip(os.sep) + os.sep

    def hit(rel: str) -> bool:
        owned = os.path.normcase(os.path.abspath(os.path.join(root, rel)))
        return owned == path or owned.startswith(held)

    for run_id in live_run_ids(session_id, root):
        try:
            paths, prefixes, _unreadable = wrd._manifest_declared(run_id, Path(root))
        except Exception:
            paths, prefixes = (), ()
        if any(hit(p) for p in paths):
            return True
        for pre in prefixes:
            pre_abs = os.path.normcase(os.path.abspath(os.path.join(root, pre)))
            if path == pre_abs or path.startswith(pre_abs + os.sep):
                return True
        try:
            plan = _run_plan_file(root, run_id, session_id)
            if plan:
                with open(plan, encoding="utf-8") as fh:
                    if any(hit(w) for w in _engine_row_writes(fh.read())):
                        return True
        except Exception:
            continue
    return False


def is_product_path(abs_path: str, root: str, session_id: str) -> bool:
    """True for a path inside `root` that a live run of `session_id` declares (manifest or
    commit-request) and that is not scratch/tmp, under `state/` or `.coordinator-local/`, a
    bootstrap/out-of-repo dir, or the surface an open `performer: em` row names in `writes:`.
    The bare repo root counts as product."""
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
    if path == base:
        return True
    return _owned_by_live_run(path, base, session_id) and not _is_em_row_surface(path, base, session_id)


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


def _plan_rows(plan_text: str) -> list[tuple[str, dict]]:
    """`(row_id, {writes, performer?, disposition?})` per plan row. A line scan, not a YAML parse: it
    runs on the deny path under the DR-344 bar."""
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
    return rows


def _open_em_row_writes(plan_text: str) -> list[tuple[str, list[str]]]:
    """`(row_id, writes)` for each plan row declaring `performer: em` with no disposition or
    `disposition: open`."""
    return [
        (rid, info["writes"])
        for rid, info in _plan_rows(plan_text)
        if info.get("performer") == "em" and info.get("disposition", "open") == "open"
    ]


def _engine_row_writes(plan_text: str) -> list[str]:
    """`writes:` of every plan row an engine run executes (any row not `performer: em`)."""
    return [w for _rid, info in _plan_rows(plan_text) if info.get("performer") != "em" for w in info["writes"]]


def _engine_run_id(run_id: str, session_id: str) -> str:
    """The engine run id behind a harness `wf_` id: the `<run_id>.workflow.mjs` stem of the launch
    record's `scriptPath`. Any other id, or an unmappable one, comes back unchanged."""
    if not run_id.startswith("wf_"):
        return run_id
    try:
        import glob

        from coordinator_core.hooks import flag_em_poll_in_flight

        pattern = os.path.join(
            glob.escape(flag_em_poll_in_flight._tmpdir()), f"workflow-run-{glob.escape(session_id)}-*.json")
        for name in glob.glob(pattern):
            with open(name, encoding="utf-8") as fh:
                record = json.load(fh)
            if not isinstance(record, dict) or record.get("run_id") != run_id:
                continue
            script = str(record.get("scriptPath") or "").replace("\\", "/").rsplit("/", 1)[-1]
            if script.endswith(".workflow.mjs") and len(script) > len(".workflow.mjs"):
                return script[: -len(".workflow.mjs")]
    except Exception:
        pass
    return run_id


def _run_plan_file(root: str, run_id: str, session_id: str = "") -> str | None:
    """The plan file a run's sizing names, else the run's own minted spine."""
    run_id = _engine_run_id(run_id, session_id)
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
            plan = _run_plan_file(root, run_id, session_id)
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
    """The generic advisory, or the CLAUDE.md variant naming the EM grant route when any of `paths`
    is CLAUDE.md-class. Any import or classify failure answers the generic text."""
    try:
        from coordinator_core.claude_md_budget import is_claude_md_class
        from coordinator_core.session.claude_md_grant import GRANT_CLI_COMMAND

        if any(is_claude_md_class(p) for p in paths):
            return (
                "CLAUDE.md edit in a fired run: with the PM's approval run "
                f"`{GRANT_CLI_COMMAND}` from the repo that holds the CLAUDE.md, then send the edit "
                "through workflow.revise. "
                f"Commit via: `{COMMIT_ROUTE}`."
            )
    except Exception:
        pass
    return _POST_FIRE_DENY


def deny_envelope(session_id: str = "", cwd: str | None = None, paths: tuple[str, ...] = ()) -> dict | None:
    """The would-be PreToolUse deny passed through `apply_guard_level`: None, with the fire recorded
    under `session_id`/`cwd` when given, because the guard is unregistered."""
    return apply_guard_level(
        GUARD_NAME, deny("PreToolUse", _deny_text(paths)), session_id=session_id, cwd=cwd)
