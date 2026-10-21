"""Baton header reader for `baton.close_report`: one pass over state/handoffs/.

Reads each baton's frontmatter bytes only (up to the closing fence), extracts the join keys by
line scan, and builds the successor index in memory. No YAML parse, no subprocess, no write.
"""

from __future__ import annotations

import json
import os
import re

from coordinator_core.frontmatter.baton_class import is_spinoff_kind
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.baton_close_plan_delivery import PlanDeliveryCache
from coordinator_core.ops.baton_close_report_contract import (
    RUN_COMMITTED,
    RUN_ENDED,
    RUN_FIELD_BATON,
    RUN_FIELD_CLAIMED,
    RUN_FIELD_SIZING,
    RUN_LIVE,
    BatonHeader,
    Evidence,
    EvidenceClass,
)
from coordinator_core.session import record_homes

LIVE_STATUSES = frozenset({"open", "claimed"})

_CHUNK = 16 * 1024
# A frontmatter block longer than this is treated as unreadable, not read to the end.
_MAX_HEADER_BYTES = 256 * 1024

_FENCE = re.compile(r"\n---[ \t]*(\n|$)")
_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(.*)$")
_GOAL_ID = re.compile(r"goal-[A-Za-z0-9_-]+")
_SCALAR_KEYS = frozenset(
    {
        "status",
        "predecessor",
        "governing_plan",
        "origin_plan_id",
        "sizing_object",
        "roadmap_id",
        "deliverable_id",
        "kind",
        "deployment_state",
    }
)
_NULLS = frozenset({"", "none", "null", "~"})


def _scalar(raw: str) -> str | None:
    """Strip a trailing ` #comment` and surrounding quotes; None for empty/null forms."""
    value = raw.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        value = value[1:end] if end > 0 else value[1:]
    else:
        cut = value.find(" #")
        if cut >= 0:
            value = value[:cut]
        if value.startswith("#"):
            value = ""
    value = value.strip()
    return None if value.lower() in _NULLS else value


def _read_frontmatter(path: str) -> list[str] | None:
    """Return the frontmatter lines between the fences, or None when unreadable."""
    try:
        with open(path, "rb") as fh:
            data = b""
            while True:
                chunk = fh.read(_CHUNK)
                data += chunk
                eof = not chunk
                text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
                if not text.startswith("---\n"):
                    return None
                for m in _FENCE.finditer(text, 3):
                    # A bare "---" at the buffer end may be a cut "----"; only EOF confirms it.
                    if m.group(1) == "\n" or eof:
                        return text[4 : m.start()].split("\n") if m.start() >= 4 else []
                if eof or len(data) >= _MAX_HEADER_BYTES:
                    return None
    except OSError:
        return None


def _parse(rel: str, lines: list[str]) -> BatonHeader | None:
    scalars: dict[str, str | None] = {}
    goals: list[str] = []
    in_goal_block = False
    for line in lines:
        if in_goal_block:
            stripped = line.strip()
            if stripped.startswith("-"):
                goals.extend(_GOAL_ID.findall(stripped))
                continue
            if stripped == "" or stripped.startswith("#") or line[:1] in (" ", "\t"):
                continue
            in_goal_block = False
        m = _KEY_LINE.match(line)
        if m is None:
            continue
        key, rest = m.group(1), m.group(2)
        if key in _SCALAR_KEYS:
            scalars.setdefault(key, _scalar(rest))
        elif key == "origin_goal_id":
            body = rest.split(" #", 1)[0]
            found = _GOAL_ID.findall(body)
            goals.extend(found)
            in_goal_block = not found and body.strip() == ""
    status = scalars.get("status")
    if status is None:
        return None
    return BatonHeader(
        path=rel,
        status=status,
        predecessor=scalars.get("predecessor"),
        governing_plan=scalars.get("governing_plan"),
        origin_plan_id=scalars.get("origin_plan_id"),
        sizing_object=scalars.get("sizing_object"),
        roadmap_id=scalars.get("roadmap_id"),
        deliverable_id=scalars.get("deliverable_id"),
        kind=scalars.get("kind"),
        deployment_state=scalars.get("deployment_state"),
        origin_goal_id=list(dict.fromkeys(goals)),
    )


def read_baton_headers(worktree_root: str) -> tuple[dict[str, BatonHeader], list[str]]:
    """Return ({rel: header} for open|claimed batons, [rel of unreadable frontmatter]).

    `rel` is the repo-relative posix path. A missing handoffs directory yields two empties.
    Terminal-status batons and batons at a terminal `deployment_state` are neither returned nor
    listed as unreadable.
    """
    home = record_homes.home_dir(worktree_root, "handoffs")
    rel_home = os.path.relpath(home, worktree_root).replace(os.sep, "/")
    headers: dict[str, BatonHeader] = {}
    unreadable: list[str] = []
    try:
        entries = sorted(os.scandir(home), key=lambda e: e.name)
    except OSError:
        return headers, unreadable
    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        rel = f"{rel_home}/{entry.name}"
        lines = _read_frontmatter(entry.path)
        header = _parse(rel, lines) if lines is not None else None
        if header is None:
            unreadable.append(rel)
        elif header["status"] in LIVE_STATUSES and header["deployment_state"] not in HANDOFF_TERMINAL_DEPLOYMENT:
            headers[rel] = header
    return headers, unreadable


_JOBS_REL = ".coordinator-local/close-jobs"
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_RUN_JSON_BYTES = 64 * 1024  # a larger file is truncated, fails to parse, and reads as no evidence


def _read_json(path: str) -> dict | None:
    """Parsed JSON object at `path` through bare descriptor calls; None when absent or malformed."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            data = os.read(fd, _RUN_JSON_BYTES)
        finally:
            os.close(fd)
        doc = json.loads(data)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _norm(rel: object) -> str | None:
    return rel.replace("\\", "/") if isinstance(rel, str) and rel else None


def run_claims(
    worktree_root: str,
    headers: dict[str, BatonHeader],
    plans: PlanDeliveryCache | None = None,
) -> dict[str, list[Evidence]]:
    """Run evidence per baton rel: the warp runs that name it, and how far each got.

    A run joins a baton through its `run-baton.json` `baton`, through its `run.json` `sizing_rel`
    when exactly one live baton carries that `sizing_object`, or through its close-job
    `claimed_batons`. `target_status` is `committed` for a close-job with a full-hex
    `terminal_sha`, `ended` for a close-job without one, `live` for no close-job. Reads nothing
    unless some baton is a spinoff or non-open; a close-job is read only for a run that joined,
    so `claimed_batons` adds batons only to runs already joined to another.

    A committed run is `done` only when its close-job `plan_rel` names a delivered plan (status
    terminal, every dispatchable row coded), and its `ship_ref` is that plan's last coded commit;
    a planning run, or a close-job with no `plan_rel`, is never done.
    """
    if not any(is_spinoff_kind(h.get("kind")) or h["status"] != "open" for h in headers.values()):
        return {}
    by_sizing: dict[str, list[str]] = {}
    for rel, h in headers.items():
        so = _norm(h.get("sizing_object"))
        if so:
            by_sizing.setdefault(so, []).append(rel)
    live = {rel.replace("\\", "/"): rel for rel in headers}
    warp = record_homes.home_dir(worktree_root, "warp")
    try:
        run_ids = sorted(os.listdir(warp))
    except OSError:
        return {}
    try:
        jobs = set(os.listdir(os.path.join(worktree_root, *_JOBS_REL.split("/"))))
    except OSError:
        jobs = set()
    warp_rel = os.path.relpath(warp, worktree_root).replace(os.sep, "/")
    out: dict[str, list[Evidence]] = {}
    for run_id in run_ids:
        rdir = os.path.join(warp, run_id)
        direct = _read_json(os.path.join(rdir, "run-baton.json"))
        run = _read_json(os.path.join(rdir, "run.json"))
        joins: dict[str, str] = {}
        named = _norm(direct.get("baton")) if direct else None
        if named in live:
            joins[live[named]] = RUN_FIELD_BATON
        sizing = _norm(run.get("sizing_rel")) if run else None
        carriers = by_sizing.get(sizing or "", [])
        if len(carriers) == 1:
            joins.setdefault(carriers[0], RUN_FIELD_SIZING)
        if not joins:
            continue
        job_name = next((n for n in (f"{run_id}.workflow.json", f"{run_id}.json") if n in jobs), None)
        job = _read_json(os.path.join(worktree_root, *_JOBS_REL.split("/"), job_name)) if job_name else None
        for c in (job.get("claimed_batons") if job else None) or ():
            if _norm(c) in live:
                joins.setdefault(live[_norm(c)], RUN_FIELD_CLAIMED)
        if job_name is None:
            path, status = f"{warp_rel}/{run_id}", RUN_LIVE
        else:
            path = f"{_JOBS_REL}/{job_name}"
            sha = job.get("terminal_sha") if job else None
            status = RUN_COMMITTED if isinstance(sha, str) and _FULL_SHA.fullmatch(sha) else RUN_ENDED
        done, ship_ref = False, None
        plan_rel = _norm(job.get("plan_rel")) if job else None
        if status == RUN_COMMITTED and plan_rel:
            if plans is None:
                plans = PlanDeliveryCache(worktree_root)
            delivery = plans.get(plan_rel)
            if delivery is not None and delivery.delivered:
                done, ship_ref = True, delivery.ship_ref
        for baton, field in joins.items():
            out.setdefault(baton, []).append({
                "class": EvidenceClass.RUN.value,
                "field": field,
                "target_path": path,
                "target_status": status,
                "done": done,
                "ship_ref": ship_ref,
            })
    return out


def successor_index(headers: dict[str, BatonHeader]) -> set[str]:
    """Rels named as `predecessor` by some live baton in `headers`."""
    named: set[str] = set()
    for header in headers.values():
        pred = header["predecessor"]
        if pred:
            named.add(pred.replace("\\", "/"))
    return named
