"""Shared post-fire predicate: once an EM main-thread session has launched a Workflow run, the
product paths that run owns (`owns_path`) belong to it; every other product path stays with the XS
budget. The Edit/Write hook and the Bash write guard decide through
this one module; `GUARD_NAME` is a floor guard, so no guard level turns the deny into an advisory."""

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
    "This session already fired engine-ask; its product edits belong to the run. "
    "Send the gap through workflow.revise, or fire a follow-up engine-ask whose em_context "
    "quotes the receipt's gap line. Committing is not blocked: "
    f"{COMMIT_ROUTE}."
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


def _run_stage(root: str, run_id: str) -> str | None:
    try:
        with open(os.path.join(record_homes.record_path(root, "warp", run_id), "run-stage.json"), encoding="utf-8") as fh:
            stage = json.load(fh).get("stage")
        return stage if isinstance(stage, str) else None
    except (OSError, ValueError, AttributeError):
        return None


def owns_path(abs_path: str, root: str, session_id: str) -> bool:
    """True when `abs_path` is, or is a directory holding, a path owned by a run this session fired
    that has not landed: a path a halted run withholds, or a `writes:` entry of an engine row of the
    run's plan. Landed runs own nothing.
    Any read failure answers False: the deny fails open."""
    try:
        from pathlib import Path

        from coordinator_core.hooks import flag_em_poll_in_flight
        from coordinator_core.ops.dispatch_emit import halted_withheld_index

        base = os.path.abspath(root)
        path = os.path.normcase(os.path.abspath(abs_path))
        prefix = path.rstrip(os.sep) + os.sep

        def hit(rel: str) -> bool:
            owned = os.path.normcase(os.path.abspath(os.path.join(base, rel)))
            return owned == path or owned.startswith(prefix)

        live = [r for r in flag_em_poll_in_flight.session_workflow_run_ids(session_id)
                if _run_stage(base, r) != "landed"]
        if not live:
            return False
        for rel, run_id in halted_withheld_index.withheld_by_path(Path(base)).items():
            if run_id in live and hit(rel):
                return True
        for run_id in live:
            plan = _run_plan_file(base, run_id)
            if not plan:
                continue
            with open(plan, encoding="utf-8") as fh:
                writes = _engine_row_writes(fh.read())
            if any(hit(w) for w in writes):
                return True
    except Exception:
        return False
    return False


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
                "CLAUDE.md, then send the edit through workflow.revise. "
                f"Committing is not blocked: {COMMIT_ROUTE}."
            )
    except Exception:
        pass
    return _POST_FIRE_DENY


def deny_envelope(paths: tuple[str, ...] = ()) -> dict | None:
    """The PreToolUse deny, passed through `apply_guard_level` (floor: denies at every level)."""
    return apply_guard_level(GUARD_NAME, deny("PreToolUse", _deny_text(paths)))
