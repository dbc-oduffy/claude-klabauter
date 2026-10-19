"""Lean plan reader for plan.overlap_edges: only the fields write-overlap derivation reads.

`plan_seam_fix.derive_edges` needs, per plan, each live row's `id`, `writes`, `writes_under`,
`appends` and chunk `depends_on`, the plan's `depends_on_plan` targets, and whether the plan is
certified. `LeanPlan` carries exactly that surface and duck-types the `plan_seam_check._Plan`
members `_collision_findings`, `_edge_closure`, `_topo_ids` and `_writer_rows` read, so
`derive_edges` runs over it unchanged. `read_lean_plan` fills it from a declared-fields scan of
the plan text.

Liveness is disposition-not-closed and nothing else (APM ruling
state/apm-rulings/2026-10-19-plan-write-overlap-edges-shape.md): no deferral, performer, gate,
memo or transitive drop. A pair `read_spine`-live rows order is ordered here too, plus pairs they
drop, by design: over-ordering is the safe error. This module opens no file for write and spawns
no process.
"""

from __future__ import annotations

import posixpath
import re
from types import SimpleNamespace
from typing import Iterable, List, Optional, Tuple

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.ops.dispatch_emit.spine_read import (
    NON_DISPATCHABLE_DISPOSITIONS,
    UNDECLARED,
    with_canonical_disposition,
)
from coordinator_core.ops.plan_seam_check import _Plan


class _BadField(Exception):
    """A declared field has the wrong type; the plan reads as unreadable."""


class LeanPlan:
    """One certified-or-not plan reduced to what write-overlap derivation reads.

    Build with `from_fields` (rows already parsed) or `read_lean_plan` (from disk). `rel` is the
    repo-relative posix path; `certified_sha` is "" when the plan carries no stamp."""

    append_only_paths = _Plan.append_only_paths

    def __init__(self, rel: str, certified_sha: str, rows: List[dict], plan_edges: Iterable[str],
                 unreadable: Optional[str] = None):
        self.rel = rel
        self.certified_sha = certified_sha
        self.raw_rows: List[dict] = list(rows)
        self.raw_by_id = {
            r["id"]: with_canonical_disposition(r) for r in self.raw_rows if r.get("id")
        }
        self._plan_edges = {posixpath.normpath(str(p).replace("\\", "/")) for p in plan_edges}
        self.unreadable = unreadable
        self._live: Optional[list] = None

    @classmethod
    def from_fields(cls, rel: str, *, certified_sha: str, rows: List[dict],
                    plan_edges: Iterable[str] = ()) -> "LeanPlan":
        """`rows` are raw spine row dicts; `plan_edges` are `depends_on_plan` target paths
        (frontmatter and rows). A row of the wrong shape marks the plan `unreadable` on first
        `load_rows`."""
        return cls(rel, certified_sha, rows, plan_edges)

    @property
    def live_rows(self) -> list:
        return self.load_rows()

    def load_rows(self) -> list:
        """Rows whose canonical disposition is not closed; empty, with `unreadable` set, on a
        wrong-typed `writes` or `writes_under`."""
        if self._live is None:
            self._live = []
            if self.unreadable is None:
                try:
                    self._live = self._build_live()
                except _BadField as exc:
                    self.unreadable = str(exc)
                    self._live = []
        return self._live

    def _build_live(self) -> list:
        out = []
        for raw in self.raw_rows:
            raw = with_canonical_disposition(raw)
            if raw.get("disposition") in NON_DISPATCHABLE_DISPOSITIONS:
                continue
            row_id = raw.get("id")
            writes = raw.get("writes")
            if writes is None:
                writes = UNDECLARED
            elif not isinstance(writes, list):
                raise _BadField(f"row {row_id!r} declares writes: as {writes!r}, not a list")
            under = raw.get("writes_under")
            if under is None:
                under = ()
            elif not isinstance(under, list):
                raise _BadField(f"row {row_id!r} declares writes_under: as {under!r}, not a list")
            else:
                under = tuple(p.replace("\\", "/") for p in under if isinstance(p, str))
                if writes is UNDECLARED:
                    writes = []
            out.append(SimpleNamespace(id=row_id, writes=writes, writes_under=under))
        return out

    def edge_plans(self) -> set:
        return self._plan_edges


_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_STAMP_RE = re.compile(r"^mise_prepped_sha:[ \t]*(.*?)[ \t]*$", re.M)
_FM_EDGE_RE = re.compile(r"^depends_on_plan:", re.M)
_FENCE = "```yaml plan-tasks"
_WANTED = frozenset({"id", "disposition", "writes", "writes_under", "appends",
                     "depends_on", "depends_on_plan"})
_LISTS = frozenset({"writes", "writes_under", "appends"})
_ITEM_KEY = {"depends_on": "chunk", "depends_on_plan": "plan"}
_NULLISH = frozenset({"", "null", "Null", "NULL", "~", "true", "false", "True", "False", "yes",
                      "no", "on", "off", "Yes", "No", "On", "Off", "TRUE", "FALSE"})
_BAD_START = frozenset("'\"|>&*!{[%@`#?:-,]}")
_NUM = re.compile(r"^[-+]?(\d|\.\d)")


class _Fallback(Exception):
    """The hand scan met a shape YAML could type differently; the plan is read through YAML."""


def _scalar(v: str) -> str:
    """A plain or simple-quoted scalar as str; `_Fallback` when YAML could type it otherwise."""
    i = v.find(" #")
    if i >= 0:
        v = v[:i]
    v = v.strip()
    if not v or "	" in v:
        raise _Fallback
    c = v[0]
    if c in "'\"":
        if len(v) < 2 or v[-1] != c or v.count(c) != 2 or "\\" in v:
            raise _Fallback
        return v[1:-1]
    if c in _BAD_START or ": " in v or v[-1] == ":" or v in _NULLISH or _NUM.match(v):
        raise _Fallback
    return v


def _flow(v: str) -> list:
    v = v.strip()
    if v == "[]":
        return []
    if not (v.startswith("[") and v.endswith("]")):
        raise _Fallback
    inner = v[1:-1]
    if "[" in inner or "{" in inner or "#" in inner:
        raise _Fallback
    return [_scalar(p) for p in inner.split(",")]


def _scan_fast(body: str) -> List[dict]:
    """Wanted keys of each top-level spine row by line scan; `_Fallback` on any other shape.

    Lists of plain scalars (flow or block) and `depends_on` / `depends_on_plan` items (block or
    one-line flow mapping, reading only `chunk` / `plan`) are understood. An empty `writes:` value
    stays None so it reads UNDECLARED, as a null does through YAML."""
    rows: List[dict] = []
    cur: Optional[dict] = None
    key: Optional[str] = None
    mode = None
    item: Optional[dict] = None
    for line in body.split("\n"):
        if not line.strip():
            continue
        s = line.lstrip(" ")
        ind = len(line) - len(s)
        if ind == 0:
            if s.startswith("- "):
                cur = {}
                rows.append(cur)
                s = s[2:]
                ind = 2
                key = None
            elif s.startswith("#"):
                continue
            else:
                raise _Fallback
        if cur is None:
            raise _Fallback
        if ind == 2 and not s.startswith("- ") and not s.startswith("#"):
            k, sep, v = s.partition(":")
            if not sep:
                raise _Fallback
            key = k.strip()
            mode = None
            item = None
            if key not in _WANTED:
                key = None
                continue
            v = v.strip()
            inline = bool(v) and not v.startswith("#")
            if key in _LISTS:
                if inline:
                    cur[key] = _flow(v)
                else:
                    cur[key] = None
                    mode = ("list", key)
            elif key in _ITEM_KEY:
                if inline:
                    if v != "[]":
                        raise _Fallback
                    cur[key] = []
                else:
                    cur[key] = []
                    mode = ("map", key)
            else:
                cur[key] = _scalar(v)
            continue
        if key is None or s.startswith("#"):
            continue
        if mode is None:
            raise _Fallback
        kind, name = mode
        if kind == "list":
            if not s.startswith("- "):
                raise _Fallback
            if cur[name] is None:
                cur[name] = []
            cur[name].append(_scalar(s[2:]))
            continue
        want = _ITEM_KEY[name]
        if s.startswith("- "):
            item = {}
            cur[name].append(item)
            s = s[2:].strip()
            if s.startswith("{"):
                if (not s.endswith("}") or "[" in s or "#" in s or "'" in s or '"' in s
                        or s.count("{") != 1):
                    raise _Fallback
                for part in s[1:-1].split(","):
                    k, sep, v = part.partition(":")
                    if not sep:
                        raise _Fallback
                    if k.strip() == want:
                        item[want] = _scalar(v)
                continue
        elif item is None:
            raise _Fallback
        k, sep, v = s.partition(":")
        if not sep:
            raise _Fallback
        if k.strip() == want:
            item[want] = _scalar(v)
    return rows


def _scan_segments(body: str) -> Optional[List[str]]:
    """Per top-level row, the lines of its wanted keys only; None when the body is not a plain
    sequence of `- key:` rows."""
    out: List[List[str]] = []
    cur: Optional[List[str]] = None
    keep = False
    for line in body.split("\n"):
        if line.startswith("- "):
            if ":" not in line:
                return None
            keep = line[2:].split(":", 1)[0].strip() in _WANTED
            cur = [line if keep else "- _: 0"]
            out.append(cur)
            continue
        if cur is None:
            if line.strip() == "" or line.lstrip().startswith("#"):
                continue
            return None
        if line.startswith("  ") and len(line) > 2 and line[2] not in " -#":
            keep = line[2:].split(":", 1)[0].strip() in _WANTED
        if keep:
            cur.append(line)
    return ["\n".join(seg) for seg in out]


def _load_rows_yaml(body: str):
    """Rows through YAML, loading only the wanted keys when the body allows it."""
    segs = _scan_segments(body)
    return yaml.load("\n".join(segs) if segs is not None else body, Loader=_LOADER) or []


def _stamp_and_edges(text: str) -> Tuple[str, list]:
    """(certified sha or "", frontmatter `depends_on_plan` items). The frontmatter is
    YAML-loaded only when it carries that key."""
    if not text.startswith("---"):
        return "", []
    end = text.find("\n---", 3)
    if end < 0:
        return "", []
    fm = text[3:end]
    m = _STAMP_RE.search(fm)
    sha = ""
    if m:
        v = m.group(1)
        if " #" in v:
            v = v.split(" #", 1)[0]
        v = v.strip().strip("'\"").strip()
        sha = "" if v in ("null", "~", "Null", "NULL") else v
    edges: list = []
    if sha and _FM_EDGE_RE.search(fm):
        try:
            doc = yaml.load(fm, Loader=_LOADER)
        except yaml.YAMLError:
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("depends_on_plan"), list):
            edges = doc["depends_on_plan"]
    return sha, edges


def _edge_targets(items) -> set:
    """Normalized in-repo `plan` targets of `depends_on_plan` items; escaping paths are dropped."""
    targets = {
        posixpath.normpath(e["plan"].replace("\\", "/"))
        for e in items
        if isinstance(e, dict) and isinstance(e.get("plan"), str) and e["plan"].strip()
    }
    return {t for t in targets if ".." not in t.split("/") and not t.startswith("/")}


def _spine_body(text: str):
    """(body, None) or (None, status detail). A lone `plan-tasks` fence outside any HTML
    comment is cut by string search; any other layout goes through `locate_fenced_block`."""
    i = text.find(_FENCE)
    if i >= 0 and text.find(_FENCE, i + 1) < 0 and text[i + len(_FENCE):i + len(_FENCE) + 1] == "\n":
        if text.rfind("<!--", 0, i) <= text.rfind("-->", 0, i):
            j = text.find("\n```", i + len(_FENCE) + 1)
            if j > 0:
                return text[i + len(_FENCE) + 1:j], None
    located = locate_fenced_block(text)
    if located.status is not LocateStatus.LOCATED:
        return None, f"spine {located.status.value}"
    return located.body, None


def read_lean_plan(rel: str, root) -> LeanPlan:
    """Read `root/rel` read-only, once, into a `LeanPlan`.

    Certified means a non-empty `mise_prepped_sha` inside the frontmatter fence; an uncertified
    plan returns with no rows. A certified plan is `unreadable` (with a detail) only when its file
    cannot be read, its spine is absent or will not parse, or a declared field has the wrong
    type; the spine is not re-validated the way `read_spine` does."""
    try:
        with open(f"{root}/{rel}", "rb") as fh:
            text = fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return LeanPlan(rel, "", [], (), unreadable=f"cannot read plan: {exc}")
    sha, fm_edges = _stamp_and_edges(text)
    if not sha:
        return LeanPlan(rel, "", [], ())
    if "\r" in text:
        text = text.replace("\r\n", "\n")
    body, detail = _spine_body(text)
    if body is None:
        return LeanPlan(rel, sha, [], (), unreadable=detail)
    try:
        rows = _scan_fast(body)
    except _Fallback:
        try:
            rows = _load_rows_yaml(body)
        except yaml.YAMLError as exc:
            return LeanPlan(rel, sha, [], (), unreadable=f"spine parse: {exc}")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return LeanPlan(rel, sha, [], (), unreadable="spine is not a list of mappings")
    edges = list(fm_edges)
    for r in rows:
        items = r.get("depends_on_plan")
        if isinstance(items, list):
            edges.extend(items)
    return LeanPlan(rel, sha, rows, _edge_targets(edges))
