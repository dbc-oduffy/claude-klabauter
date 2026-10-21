"""Done-ness of the plan, sizing, roadmap and goal records that open batons cite.

Each distinct target is read once, header bytes only, `status:` taken by line scan. No YAML
parse, no subprocess, no write.

Plans are also read plan to baton, from the same one pass over docs/plans that resolves
`plan_id`, plus one pass over archive/specs for plans the stamp archived. Join rules:
docs/plans/2026-10-21-2026-10-21-close-report-reverse-plan-link.md and
docs/plans/2026-10-21-close-report-plan-backlink-sizing-join-cascade-msg-4734e5.md, section Problem.
"""

from __future__ import annotations

import os
import re

from coordinator_core.ops.baton_close_report_contract import (
    REVERSE_FIELD_DELIVERABLE,
    REVERSE_FIELD_PREDECESSOR,
    REVERSE_FIELD_SIZING,
    BatonHeader,
    Evidence,
    EvidenceClass,
)
from coordinator_core.ops.baton_close_report_decide import STATUS_MISSING, STATUS_UNREADABLE
from coordinator_core.ops.baton_close_report_readers import run_claims
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
_PLAN_KEYS_RE = re.compile(
    r"^(status|plan_id|deliverable_id|sizing_object|predecessor_handoff):[ \t]*(.*)$"
)
_STATUS_RE = re.compile(r"^status:[ \t]*(.*)$")
_PLAN_ID_RE = re.compile(r"^plan_id:[ \t]*(.*)$")
_DELIVERABLE_RE = re.compile(r"^deliverable_id:[ \t]*(.*)$")
_PREDECESSOR_RE = re.compile(r"^predecessor_handoff:[ \t]*(.*)$")
_GOAL_ID_RE = re.compile(r"^id:[ \t]*(.*)$")
_SIZING_OBJECT_RE = re.compile(r"^sizing_object:[ \t]*(.*)$")
_BATON_PATH_RE = re.compile(r"state/handoffs/[^\s\"',()#]+\.md")


def _scalar(raw: str) -> str:
    """Unquote a scalar and drop a trailing ` # comment`."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    cut = raw.find(" #")
    return (raw[:cut] if cut >= 0 else raw).strip()


def _read_prefix(path: str, nbytes: int) -> bytes:
    """Up to `nbytes` leading bytes of `path` through bare descriptor calls.

    OSError propagates, FileNotFoundError and NotADirectoryError included. Bare descriptor calls
    cost about half of `open()` per file, which the full-plans index pays 1000+ times.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        return os.read(fd, nbytes)
    finally:
        os.close(fd)


def _head(path: str, nbytes: int) -> list[str] | None:
    """Leading lines of `path` (CRLF-tolerant), or None when the file does not exist.

    Any other OSError propagates: the caller decides what an unreadable file means.
    """
    try:
        data = _read_prefix(path, nbytes)
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
    """Frontmatter lines of a plan from one open; None when the file is missing or unreadable.

    A fenced plan is read to `_HEAD_BYTES` and cut at its closing fence before any line split,
    so the many plans whose frontmatter outruns `_PLAN_HEAD_BYTES` cost one open, not two. An
    unfenced file keeps the `_PLAN_HEAD_BYTES` window.
    """
    try:
        data = _read_prefix(path, _HEAD_BYTES)
    except OSError:
        return None
    head = data[:_PLAN_HEAD_BYTES]
    eol = head.find(b"\n")
    first = head[:eol] if eol >= 0 else head
    if first.decode("utf-8", errors="replace").strip() != "---":
        return _frontmatter(head.decode("utf-8", errors="replace").splitlines())
    close = _closing_fence(data, len(first) + 1)
    if close >= 0:
        data = data[:close]
    return data.decode("utf-8", errors="replace").splitlines()[1:]


def _closing_fence(data: bytes, start: int) -> int:
    """Offset of the first line from `start` that is only `---` and blanks, or -1."""
    pos = data.find(b"---", start)
    while pos >= 0:
        bol = data.rfind(b"\n", 0, pos) + 1
        eol = data.find(b"\n", pos)
        if eol < 0:
            eol = len(data)
        if not data[bol:pos].strip(b" \t") and not data[pos + 3:eol].strip(b" \t\r"):
            return bol
        pos = data.find(b"---", pos + 3)
    return -1


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


def _plan_keys(lines: list[str]) -> dict[str, str]:
    """First column-zero value of each index key in one pass; same result as `_key_value` per key."""
    out: dict[str, str] = {}
    for line in lines:
        m = _PLAN_KEYS_RE.match(line)
        if m and m.group(1) not in out:
            out[m.group(1)] = _scalar(m.group(2))
    return out


def _safe_join(root: str, rel: str) -> str | None:
    """Path under `root` for a repo-relative `rel`, or None when it is absolute or escapes."""
    rel = rel.replace("\\", "/")
    if not rel or rel.startswith("/") or ":" in rel.split("/", 1)[0]:
        return None
    parts = rel.split("/")
    if ".." in parts:
        return None
    return os.path.join(root, *parts)


def _sizing_named_batons(lines) -> set[str]:
    """Batons a sizing names: a column-zero `baton:` value, or a line that is only `- <baton path>`.

    A path inside prose, another mapping value or a quoted `finding:` string is not a naming.
    """
    out: set[str] = set()
    for ln in lines:
        if ln.startswith("baton:"):
            value = _scalar(ln[len("baton:"):])
        else:
            text = ln.strip()
            if not text.startswith("- "):
                continue
            value = _scalar(text[2:])
        value = value.replace("\\", "/")
        if _BATON_PATH_RE.fullmatch(value):
            out.add(value)
    return out


class _Reader:
    def __init__(self, root: str) -> None:
        self.root = root
        self._status: dict[str, tuple[bool, str | None]] = {}
        self._plan_ids: dict[str, str] | None = None
        self._by_deliverable: dict[str, list[str]] = {}
        self._by_predecessor: dict[str, list[str]] = {}
        self._by_sizing: dict[str, list[str]] = {}
        self._live_names: set[str] = set()
        self._arch_by_deliverable: dict[str, list[str]] = {}
        self._arch_by_predecessor: dict[str, list[str]] = {}
        self._arch_by_sizing: dict[str, list[str]] = {}
        self._sizing_batons: dict[str, list[str]] | None = None
        self._goal_status: dict[str, str | None] | None = None
        self._months: list[str] | None = None
        self._archived_ids: dict[str, str] | None = None

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
            if lines is None and fenced and rel.startswith("docs/plans/") and rel.count("/") == 2:
                lines = self._archived_head(rel.rsplit("/", 1)[1])
            if lines is None:
                result = (False, None)
            else:
                body = _frontmatter(lines) if fenced else lines
                result = (True, _key_value(body, _STATUS_RE))
        self._status[rel] = result
        return result

    def _archive_months(self) -> list[str]:
        """archive/specs/<yyyy-mm> dirs, newest first, listed once."""
        if self._months is None:
            base = os.path.join(self.root, "archive", "specs")
            try:
                names = sorted(os.listdir(base), reverse=True)
            except OSError:
                names = []
            self._months = [os.path.join(base, n) for n in names]
        return self._months

    def _archived_head(self, name: str) -> list[str] | None:
        """Head lines of docs/plans/`name` after the stamp archived it, or None.

        Runs only after a docs/plans miss; one open per month dir, stops at the first hit.
        """
        for month in self._archive_months():
            try:
                lines = _head(os.path.join(month, name), _HEAD_BYTES)
            except OSError:
                continue
            if lines is not None:
                return lines
        return None

    def _archive_index(self) -> dict[str, str]:
        """One pass over archive/specs/<yyyy-mm>/*.md, at most once per op.

        Fills the archived plan_id map and the archived deliverable / predecessor / sizing
        reverse maps. A basename present in docs/plans contributes no reverse entry, so a plan
        is never counted twice; the newest month wins a basename archived twice. `_status` is
        seeded from the archived header, keyed by the docs/plans rel.
        """
        if self._archived_ids is not None:
            return self._archived_ids
        self._plan_index()
        self._archived_ids = ids = {}
        seen: set[str] = set()
        for month in self._archive_months():
            try:
                names = sorted(os.listdir(month))
            except OSError:
                continue
            for name in names:
                if not name.endswith(".md"):
                    continue
                fm = _plan_frontmatter(os.path.join(month, name))
                if fm is None:
                    continue
                keys = _plan_keys(fm)
                rel = "docs/plans/" + name
                pid = keys.get("plan_id")
                if pid:
                    ids.setdefault(pid, rel)
                if name in self._live_names or name in seen:
                    continue
                seen.add(name)
                self._status.setdefault(rel, (True, keys.get("status")))
                did = keys.get("deliverable_id")
                if did:
                    self._arch_by_deliverable.setdefault(did, []).append(rel)
                szo = keys.get("sizing_object")
                if szo:
                    self._arch_by_sizing.setdefault(szo.replace("\\", "/"), []).append(rel)
                pre = keys.get("predecessor_handoff")
                if pre:
                    self._arch_by_predecessor.setdefault(pre.replace("\\", "/"), []).append(rel)
        return ids

    def _archived_plan_id(self, plan_id: str) -> str | None:
        """docs/plans rel of the archived plan carrying `plan_id`."""
        return self._archive_index().get(plan_id)

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
            self._live_names.add(name)
            keys = _plan_keys(fm)
            self._status.setdefault(rel, (True, keys.get("status")))
            pid = keys.get("plan_id")
            if pid:
                self._plan_ids.setdefault(pid, rel)
            did = keys.get("deliverable_id")
            if did:
                self._by_deliverable.setdefault(did, []).append(rel)
            szo = keys.get("sizing_object")
            if szo:
                self._by_sizing.setdefault(szo.replace("\\", "/"), []).append(rel)
            pre = keys.get("predecessor_handoff")
            if pre:
                self._by_predecessor.setdefault(pre.replace("\\", "/"), []).append(rel)

    def plan_path_for_id(self, plan_id: str) -> str | None:
        self._plan_index()
        assert self._plan_ids is not None
        return self._plan_ids.get(plan_id) or self._archived_plan_id(plan_id)

    def plans_for_deliverable(self, deliverable_id: str) -> list[str]:
        self._archive_index()
        return self._by_deliverable.get(deliverable_id, []) + self._arch_by_deliverable.get(deliverable_id, [])

    def plans_for_predecessor(self, baton_rel: str) -> list[str]:
        self._archive_index()
        return self._by_predecessor.get(baton_rel, []) + self._arch_by_predecessor.get(baton_rel, [])

    def sizing_hop(self, baton_rel: str) -> list[tuple[str, str]]:
        """(sizing rel, plan rel) pairs for sizings that name `baton_rel` and have a producing plan.

        Only sizings some plan, live or archived, cites as `sizing_object` are read (head bytes,
        once each). A sizing names a baton only by `_sizing_named_batons`; a baton named by more
        than one such sizing yields nothing.
        """
        self._archive_index()
        if self._sizing_batons is None:
            self._sizing_batons = {}
            for szo in dict.fromkeys([*self._by_sizing, *self._arch_by_sizing]):
                full = _safe_join(self.root, szo)
                lines = _head_or_none(full, _HEAD_BYTES) if full else None
                for b in _sizing_named_batons(lines or ()):
                    self._sizing_batons.setdefault(b, []).append(szo)
        sizings = self._sizing_batons.get(baton_rel, [])
        if len(sizings) != 1:
            return []
        szo = sizings[0]
        return [(szo, p) for p in self._by_sizing.get(szo, []) + self._arch_by_sizing.get(szo, [])]

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
    runs = run_claims(worktree_root, headers)
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
        if did and carriers.get(did, 0) == 1:
            for p in rd.plans_for_deliverable(did):
                if p not in seen:
                    seen.add(p)
                    items.append(_path_item(rd, EvidenceClass.PLAN, REVERSE_FIELD_DELIVERABLE, p, True, PLAN_DONE))
        brel = rel.replace("\\", "/")
        for p in rd.plans_for_predecessor(brel):
            if p not in seen:
                seen.add(p)
                items.append(_path_item(rd, EvidenceClass.PLAN, REVERSE_FIELD_PREDECESSOR, p, True, PLAN_DONE))
        for _sz, p in rd.sizing_hop(brel):
            if p not in seen:
                seen.add(p)
                items.append(_path_item(rd, EvidenceClass.PLAN, REVERSE_FIELD_SIZING, p, True, PLAN_DONE))
        for it in runs.get(rel, ()):
            if it["target_path"] not in seen:
                seen.add(it["target_path"])
                items.append(it)
        if items:
            out[rel] = items
    return out
