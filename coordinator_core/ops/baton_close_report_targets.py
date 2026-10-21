"""Done-ness of the plan, sizing, roadmap and goal records that open batons cite.

Each distinct target is read once, header bytes only, `status:` taken by line scan. No YAML
parse, no subprocess, no write.
"""

from __future__ import annotations

import os
import re

from coordinator_core.ops.baton_close_report_contract import BatonHeader, Evidence, EvidenceClass
from coordinator_core.session.record_homes import home_dir

PLAN_DONE = frozenset({"implemented", "landed", "complete"})
SIZING_DONE = frozenset({"shipped"})
ROADMAP_DONE = frozenset({"shipped"})
# No goal on disk carries a terminal status, so no value is a guess here.
GOAL_DONE: frozenset[str] = frozenset()

# Evidence.target_status for a cited target with no file; None means a file with no status line.
STATUS_MISSING = "<missing>"

_HEAD_BYTES = 16384
_PLAN_ID_BYTES = 2048
_STATUS_RE = re.compile(r"^status:[ \t]*(.*)$")
_PLAN_ID_RE = re.compile(r"^plan_id:[ \t]*(.*)$")
_GOAL_ID_RE = re.compile(r"^id:[ \t]*(.*)$")


def _scalar(raw: str) -> str:
    """Unquote a scalar and drop a trailing ` # comment`."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    cut = raw.find(" #")
    return (raw[:cut] if cut >= 0 else raw).strip()


def _head(path: str, nbytes: int) -> list[str] | None:
    """Leading lines of `path` (CRLF-tolerant), or None when the file cannot be read."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(nbytes)
    except OSError:
        return None
    return data.decode("utf-8", errors="replace").splitlines()


def _frontmatter(lines: list[str]) -> list[str]:
    """Lines between a leading `---` fence and its close; the whole input when unfenced."""
    if not lines or lines[0].strip() != "---":
        return lines
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i]
    return lines[1:]


def _key_value(lines: list[str], pattern: re.Pattern[str]) -> str | None:
    """First column-zero match of `pattern`; an indented line never matches."""
    for line in lines:
        m = pattern.match(line)
        if m:
            return _scalar(m.group(1))
    return None


def _safe_join(root: str, rel: str) -> str | None:
    """Path under `root` for a repo-relative `rel`, or None when it is absolute or escapes."""
    rel = rel.replace("\\", "/")
    if not rel or rel.startswith("/") or ":" in rel.split("/", 1)[0]:
        return None
    parts = rel.split("/")
    if ".." in parts:
        return None
    return os.path.join(root, *parts)


class _Reader:
    def __init__(self, root: str) -> None:
        self.root = root
        self._status: dict[str, tuple[bool, str | None]] = {}
        self._plan_ids: dict[str, str] | None = None
        self._goal_status: dict[str, str | None] | None = None

    def status_of(self, rel: str, *, fenced: bool) -> tuple[bool, str | None]:
        """(file exists, status) for `rel`, read once per path."""
        hit = self._status.get(rel)
        if hit is not None:
            return hit
        full = _safe_join(self.root, rel)
        lines = _head(full, _HEAD_BYTES) if full else None
        if lines is None:
            result: tuple[bool, str | None] = (False, None)
        else:
            body = _frontmatter(lines) if fenced else lines
            result = (True, _key_value(body, _STATUS_RE))
        self._status[rel] = result
        return result

    def plan_path_for_id(self, plan_id: str) -> str | None:
        if self._plan_ids is None:
            index: dict[str, str] = {}
            plans = os.path.join(self.root, "docs", "plans")
            try:
                names = sorted(os.listdir(plans))
            except OSError:
                names = []
            for name in names:
                if not name.endswith(".md"):
                    continue
                lines = _head(os.path.join(plans, name), _PLAN_ID_BYTES)
                if lines is None:
                    continue
                pid = _key_value(_frontmatter(lines), _PLAN_ID_RE)
                if pid:
                    index.setdefault(pid, "docs/plans/" + name)
            self._plan_ids = index
        return self._plan_ids.get(plan_id)

    def goal_status(self, goal_id: str) -> tuple[str, str | None] | None:
        """(goal file rel path, status) for a goal id, or None when no goal file carries it."""
        if self._goal_status is None:
            self._goal_status = {}
            self._goal_paths: dict[str, str] = {}
            gdir = home_dir(self.root, "goals")
            try:
                names = sorted(os.listdir(gdir))
            except OSError:
                names = []
            for name in names:
                if not name.endswith((".yaml", ".yml")):
                    continue
                lines = _head(os.path.join(gdir, name), _HEAD_BYTES)
                if lines is None:
                    continue
                gid = _key_value(lines, _GOAL_ID_RE)
                if gid:
                    self._goal_status[gid] = _key_value(lines, _STATUS_RE)
                    self._goal_paths[gid] = "state/goals/" + name
        if goal_id not in self._goal_status:
            return None
        return self._goal_paths[goal_id], self._goal_status[goal_id]


def _item(cls: EvidenceClass, field: str, path: str, exists: bool, status: str | None,
          done_set: frozenset[str]) -> Evidence:
    shown = status if exists else STATUS_MISSING
    return {
        "class": cls.value,
        "field": field,
        "target_path": path,
        "target_status": shown,
        "done": exists and status is not None and status in done_set,
    }


def _path_item(rd: _Reader, cls: EvidenceClass, field: str, rel: str, fenced: bool,
               done_set: frozenset[str]) -> Evidence:
    rel = rel.replace("\\", "/")
    exists, status = rd.status_of(rel, fenced=fenced)
    return _item(cls, field, rel, exists, status, done_set)


def target_evidence(worktree_root: str, headers: dict[str, BatonHeader]) -> dict[str, list[Evidence]]:
    """Evidence items per baton rel path; batons citing nothing are absent from the result.

    A cited target with no file reads `target_status == STATUS_MISSING`; a file with no
    top-level `status:` line reads `None`. Neither is done.
    """
    rd = _Reader(worktree_root)
    out: dict[str, list[Evidence]] = {}
    for rel, h in headers.items():
        items: list[Evidence] = []
        gp = h.get("governing_plan")
        if gp:
            items.append(_path_item(rd, EvidenceClass.PLAN, "governing_plan", gp, True, PLAN_DONE))
        pid = h.get("origin_plan_id")
        if pid:
            path = rd.plan_path_for_id(pid)
            if path is None:
                items.append(_item(EvidenceClass.PLAN, "origin_plan_id", pid, False, None, PLAN_DONE))
            elif not (gp and gp.replace("\\", "/") == path):
                items.append(_path_item(rd, EvidenceClass.PLAN, "origin_plan_id", path, True, PLAN_DONE))
        so = h.get("sizing_object")
        if so:
            items.append(_path_item(rd, EvidenceClass.SIZING, "sizing_object", so, False, SIZING_DONE))
        rid = h.get("roadmap_id")
        if rid:
            ov = f"state/roadmap/{rid}/OVERVIEW.md"
            if "/" in rid or "\\" in rid or ".." in rid:
                items.append(_item(EvidenceClass.ROADMAP, "roadmap_id", ov, False, None, ROADMAP_DONE))
            else:
                items.append(_path_item(rd, EvidenceClass.ROADMAP, "roadmap_id", ov, True, ROADMAP_DONE))
        for gid in h.get("origin_goal_id") or []:
            found = rd.goal_status(gid)
            if found is None:
                items.append(_item(EvidenceClass.GOAL, "origin_goal_id", gid, False, None, GOAL_DONE))
            else:
                items.append(_item(EvidenceClass.GOAL, "origin_goal_id", found[0], True, found[1], GOAL_DONE))
        if items:
            out[rel] = items
    return out
