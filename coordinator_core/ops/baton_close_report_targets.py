"""Done-ness of the plan, sizing, roadmap and goal records that open batons cite.

Each distinct target is read once, header bytes only, `status:` taken by line scan. No YAML
parse, no subprocess, no write.

Plans are also read plan to baton, from the same one pass over docs/plans that resolves
`plan_id`. Join rules: docs/plans/2026-10-21-2026-10-21-close-report-reverse-plan-link.md,
section Problem.
"""

from __future__ import annotations

import os
import re

from coordinator_core.ops.baton_close_report_contract import (
    REVERSE_FIELD_DELIVERABLE,
    REVERSE_FIELD_PREDECESSOR,
    BatonHeader,
    Evidence,
    EvidenceClass,
)
from coordinator_core.ops.baton_close_report_decide import STATUS_MISSING, STATUS_UNREADABLE
from coordinator_core.session.record_homes import home_dir

# `landed` is non-terminal per plan.schema.json's status description (spine rows still open):
# do not re-add it. `complete` stays as defensive tolerance.
PLAN_DONE = frozenset({"implemented", "complete"})
SIZING_DONE = frozenset({"shipped"})
ROADMAP_DONE = frozenset({"shipped"})
# No goal on disk carries a terminal status, so no value is a guess here.
GOAL_DONE: frozenset[str] = frozenset()

# Evidence.target_status is the decide module's STATUS_MISSING for a cited target with no file,
# STATUS_UNREADABLE for a file that exists but cannot be opened (one sentinel set for both
# sides); None means a file read with no status line.

_HEAD_BYTES = 16384
_PLAN_HEAD_BYTES = 4096
_STATUS_RE = re.compile(r"^status:[ \t]*(.*)$")
_PLAN_ID_RE = re.compile(r"^plan_id:[ \t]*(.*)$")
_DELIVERABLE_RE = re.compile(r"^deliverable_id:[ \t]*(.*)$")
_PREDECESSOR_RE = re.compile(r"^predecessor_handoff:[ \t]*(.*)$")
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
    """Leading lines of `path` (CRLF-tolerant), or None when the file does not exist.

    Any other OSError propagates: the caller decides what an unreadable file means.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(nbytes)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return data.decode("utf-8", errors="replace").splitlines()


def _head_or_none(path: str, nbytes: int) -> list[str] | None:
    """`_head` for index scans, where an unreadable file is skipped like a missing one."""
    try:
        return _head(path, nbytes)
    except OSError:
        return None


def _plan_frontmatter(path: str) -> list[str] | None:
    """Frontmatter lines of a plan from one open; reads on only when the closing fence is cut off.

    None when the file is missing or unreadable.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(_PLAN_HEAD_BYTES)
            lines = data.decode("utf-8", errors="replace").splitlines()
            if (len(data) == _PLAN_HEAD_BYTES and lines and lines[0].strip() == "---"
                    and not any(ln.strip() == "---" for ln in lines[1:])):
                data += fh.read(_HEAD_BYTES - _PLAN_HEAD_BYTES)
                lines = data.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return _frontmatter(lines)


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
        self._by_deliverable: dict[str, list[str]] = {}
        self._by_predecessor: dict[str, list[str]] = {}
        self._goal_status: dict[str, str | None] | None = None

    def status_of(self, rel: str, *, fenced: bool) -> tuple[bool, str | None]:
        """(file exists, status) for `rel`, read once per path."""
        hit = self._status.get(rel)
        if hit is not None:
            return hit
        if fenced and rel.startswith("docs/plans/") and rel.count("/") == 2:
            self._plan_index()
            hit = self._status.get(rel)
            if hit is not None:
                return hit
        full = _safe_join(self.root, rel)
        try:
            lines = _head(full, _HEAD_BYTES) if full else None
        except OSError:
            result: tuple[bool, str | None] = (True, STATUS_UNREADABLE)
        else:
            if lines is None:
                result = (False, None)
            else:
                body = _frontmatter(lines) if fenced else lines
                result = (True, _key_value(body, _STATUS_RE))
        self._status[rel] = result
        return result

    def _plan_index(self) -> None:
        """One pass over docs/plans/*.md: plan_id, status, deliverable_id, predecessor_handoff.

        Each file is opened once; `_status` is seeded so a forward cite of the same plan is
        not reread.
        """
        if self._plan_ids is not None:
            return
        self._plan_ids = {}
        plans = os.path.join(self.root, "docs", "plans")
        try:
            names = sorted(os.listdir(plans))
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".md"):
                continue
            fm = _plan_frontmatter(os.path.join(plans, name))
            if fm is None:
                continue
            rel = "docs/plans/" + name
            self._status.setdefault(rel, (True, _key_value(fm, _STATUS_RE)))
            pid = _key_value(fm, _PLAN_ID_RE)
            if pid:
                self._plan_ids.setdefault(pid, rel)
            did = _key_value(fm, _DELIVERABLE_RE)
            if did:
                self._by_deliverable.setdefault(did, []).append(rel)
            pre = _key_value(fm, _PREDECESSOR_RE)
            if pre:
                self._by_predecessor.setdefault(pre.replace("\\", "/"), []).append(rel)

    def plan_path_for_id(self, plan_id: str) -> str | None:
        self._plan_index()
        assert self._plan_ids is not None
        return self._plan_ids.get(plan_id)

    def plans_for_deliverable(self, deliverable_id: str) -> list[str]:
        self._plan_index()
        return self._by_deliverable.get(deliverable_id, [])

    def plans_for_predecessor(self, baton_rel: str) -> list[str]:
        self._plan_index()
        return self._by_predecessor.get(baton_rel, [])

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
                lines = _head_or_none(os.path.join(gdir, name), _HEAD_BYTES)
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
    shown = status if exists else STATUS_MISSING  # exists + STATUS_UNREADABLE passes through
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

    A cited target with no file reads `target_status == STATUS_MISSING`; a file that exists but
    cannot be opened reads `STATUS_UNREADABLE`; a file read with no top-level `status:` line
    reads `None`. None of the three is done.
    """
    rd = _Reader(worktree_root)
    out: dict[str, list[Evidence]] = {}
    carriers: dict[str, int] = {}
    for h in headers.values():
        d = h.get("deliverable_id")
        if d:
            carriers[d] = carriers.get(d, 0) + 1
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
        seen = {it["target_path"] for it in items}
        did = h.get("deliverable_id")
        if did and h.get("kind") != "spinoff" and carriers.get(did, 0) == 1:
            for p in rd.plans_for_deliverable(did):
                if p not in seen:
                    seen.add(p)
                    items.append(_path_item(rd, EvidenceClass.PLAN, REVERSE_FIELD_DELIVERABLE, p, True, PLAN_DONE))
        for p in rd.plans_for_predecessor(rel.replace("\\", "/")):
            if p not in seen:
                seen.add(p)
                items.append(_path_item(rd, EvidenceClass.PLAN, REVERSE_FIELD_PREDECESSOR, p, True, PLAN_DONE))
        if items:
            out[rel] = items
    return out
