"""Work-class agent routing: read a consumer repo's route table, match a row to its work class,
check an agent type's tool range against the class, and route plan rows to the class's agent type.

The table is `<repo root>/.claude/route-table.json` (`ROUTE_TABLE_RELPATH`); its schema is DoE's
`coordinator/schemas/route-table.schema.json`. This module checks only the fields it reads. Absent
table: every function here is a no-op and `route_rows` returns its input list unchanged.

No subprocess and no git spawn anywhere in this module.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED

ROUTE_TABLE_RELPATH = ".claude/route-table.json"

_ON_MISMATCH = ("refuse", "redirect")
_LEVELS = ("warn", "strict")
_ROOT_WALK_MAX_DEPTH = 64


class RouteTableError(ValueError):
    """The route table is present but a field the engine reads is malformed."""


class RouteAdmissionError(ValueError):
    """A row cannot be dispatched under the route table: refused, or no model resolves."""


@dataclass(frozen=True)
class WorkClass:
    id: str
    agent_type: str
    on_mismatch: str
    paths: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    triage_agent_type: Optional[str] = None
    required_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class BriefLint:
    patterns: tuple["re.Pattern[str]", ...]
    level: str


@dataclass(frozen=True)
class EditorPython:
    preferred_agent_type: str
    level: str
    tool_names: tuple[str, ...] = ()
    bash_patterns: tuple["re.Pattern[str]", ...] = ()


@dataclass(frozen=True)
class RouteTable:
    default_agent_type: str
    work_classes: tuple[WorkClass, ...]
    agent_tool_ranges: Optional[dict[str, tuple[str, ...]]] = None
    brief_lint: Optional[BriefLint] = None
    editor_python: Optional[EditorPython] = None


@dataclass(frozen=True)
class RouteDecision:
    agent_type: str
    triage_agent_type: Optional[str]
    work_class: Optional[WorkClass]
    note: Optional[str] = None


@dataclass(frozen=True)
class Admission:
    verdict: str
    target: Optional[str]
    reason: str
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class Route:
    """A routed row's dispatch plan. `targets` is the closed set of agent types an agent's redirect
    may name, each with its model; `primary` is always a member."""

    work_class: str
    primary: tuple[str, str]
    targets: dict[str, str]
    triage: Optional[tuple[str, str]] = None

    def to_json(self) -> dict[str, Any]:
        def pair(p: Optional[tuple[str, str]]) -> Optional[dict[str, str]]:
            return None if p is None else {"agent_type": p[0], "model": p[1]}

        return {
            "work_class": self.work_class,
            "triage": pair(self.triage),
            "primary": pair(self.primary),
            "targets": dict(self.targets),
        }


def _where(path: Path, field: str) -> str:
    return f"{path.as_posix()}: {field}"


def _names(value: Any, path: Path, field: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None:
        if required:
            raise RouteTableError(_where(path, f"{field} is required"))
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise RouteTableError(_where(path, f"{field} must be a list of non-empty strings"))
    return tuple(value)


def _text(value: Any, path: Path, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RouteTableError(_where(path, f"{field} must be a non-empty string"))
    return value


def _level(value: Any, path: Path, field: str) -> str:
    if value not in _LEVELS:
        raise RouteTableError(_where(path, f"{field} must be one of {', '.join(_LEVELS)}"))
    return value


def _regexes(value: Any, path: Path, field: str) -> tuple["re.Pattern[str]", ...]:
    out = []
    for i, src in enumerate(_names(value, path, field)):
        try:
            out.append(re.compile(src))
        except re.error as exc:
            raise RouteTableError(_where(path, f"{field}[{i}] is not a valid regex ({exc})")) from exc
    return tuple(out)


def _parse_class(raw: Any, path: Path, i: int) -> WorkClass:
    field = f"work_classes[{i}]"
    if not isinstance(raw, dict):
        raise RouteTableError(_where(path, f"{field} must be an object"))
    match = raw.get("match")
    if not isinstance(match, dict) or not match:
        raise RouteTableError(_where(path, f"{field}.match must be a non-empty object"))
    paths = _names(match.get("paths"), path, f"{field}.match.paths")
    keywords = _names(match.get("keywords"), path, f"{field}.match.keywords")
    tools = _names(match.get("tools"), path, f"{field}.match.tools")
    if not (paths or keywords or tools):
        raise RouteTableError(_where(path, f"{field}.match names no paths, keywords or tools"))
    on_mismatch = raw.get("on_mismatch")
    if on_mismatch not in _ON_MISMATCH:
        raise RouteTableError(
            _where(path, f"{field}.on_mismatch must be one of {', '.join(_ON_MISMATCH)}")
        )
    triage = raw.get("triage_agent_type")
    if triage is not None:
        triage = _text(triage, path, f"{field}.triage_agent_type")
    return WorkClass(
        id=_text(raw.get("id"), path, f"{field}.id"),
        agent_type=_text(raw.get("agent_type"), path, f"{field}.agent_type"),
        on_mismatch=on_mismatch,
        paths=paths,
        keywords=keywords,
        tools=tools,
        triage_agent_type=triage,
        required_tools=_names(raw.get("required_tools"), path, f"{field}.required_tools"),
    )


def _parse_table(data: Any, path: Path) -> RouteTable:
    if not isinstance(data, dict):
        raise RouteTableError(_where(path, "the table must be a JSON object"))
    if data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool):
        raise RouteTableError(_where(path, "schema_version must be 1"))
    default = _text(data.get("default_agent_type"), path, "default_agent_type")
    raw_classes = data.get("work_classes")
    if not isinstance(raw_classes, list):
        raise RouteTableError(_where(path, "work_classes must be a list"))
    classes = tuple(_parse_class(c, path, i) for i, c in enumerate(raw_classes))

    ranges = None
    raw_ranges = data.get("agent_tool_ranges")
    if raw_ranges is not None:
        if not isinstance(raw_ranges, dict):
            raise RouteTableError(_where(path, "agent_tool_ranges must be an object"))
        ranges = {
            str(k): _names(v, path, f"agent_tool_ranges.{k}", required=True)
            for k, v in raw_ranges.items()
        }

    lint = None
    raw_lint = data.get("brief_lint")
    if raw_lint is not None:
        if not isinstance(raw_lint, dict):
            raise RouteTableError(_where(path, "brief_lint must be an object"))
        if "prescribed_method_patterns" not in raw_lint:
            raise RouteTableError(_where(path, "brief_lint.prescribed_method_patterns is required"))
        lint = BriefLint(
            patterns=_regexes(raw_lint["prescribed_method_patterns"], path, "brief_lint.prescribed_method_patterns"),
            level=_level(raw_lint.get("level"), path, "brief_lint.level"),
        )

    editor = None
    raw_editor = data.get("editor_python")
    if raw_editor is not None:
        if not isinstance(raw_editor, dict):
            raise RouteTableError(_where(path, "editor_python must be an object"))
        if "tool_names" not in raw_editor and "bash_patterns" not in raw_editor:
            raise RouteTableError(_where(path, "editor_python needs tool_names or bash_patterns"))
        editor = EditorPython(
            preferred_agent_type=_text(
                raw_editor.get("preferred_agent_type"), path, "editor_python.preferred_agent_type"
            ),
            level=_level(raw_editor.get("level"), path, "editor_python.level"),
            tool_names=_names(raw_editor.get("tool_names"), path, "editor_python.tool_names"),
            bash_patterns=_regexes(raw_editor.get("bash_patterns"), path, "editor_python.bash_patterns"),
        )

    return RouteTable(
        default_agent_type=default,
        work_classes=classes,
        agent_tool_ranges=ranges,
        brief_lint=lint,
        editor_python=editor,
    )


_TABLE_CACHE: dict[str, tuple[int, int, RouteTable]] = {}


def load_route_table(repo_root: "str | os.PathLike[str]") -> Optional[RouteTable]:
    """The repo's route table, or `None` when `ROUTE_TABLE_RELPATH` does not exist.

    Raises `RouteTableError` naming the field when the file is present and malformed. The parse is
    memoised on (path, mtime_ns, size).
    """
    path = Path(repo_root) / ROUTE_TABLE_RELPATH
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _TABLE_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RouteTableError(_where(path, f"unreadable or not valid JSON ({exc})")) from exc
    table = _parse_table(data, path)
    _TABLE_CACHE[key] = (st.st_mtime_ns, st.st_size, table)
    return table


def repo_root_of(cwd: Optional[str]) -> Optional[Path]:
    """The nearest ancestor of `cwd` holding a route table, else the nearest holding `.git`.

    Stat-only walk-up, bounded depth; `None` when `cwd` is falsy or neither is found.
    """
    if not cwd:
        return None
    try:
        current = Path(os.path.abspath(cwd))
    except (OSError, ValueError):
        return None
    for _ in range(_ROOT_WALK_MAX_DEPTH):
        try:
            if (current / ROUTE_TABLE_RELPATH).exists() or (current / ".git").exists():
                return current
        except OSError:
            return None
        if current.parent == current:
            return None
        current = current.parent
    return None


def _posix(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def match_work_class(
    table: RouteTable, *, writes: Sequence[str] = (), title: str = "", body: str = ""
) -> Optional[WorkClass]:
    """The first class in list order whose any-of match fires, else `None`.

    paths: glob over the POSIX-normalised write paths. keywords: case-insensitive in title or body.
    tools: substring in body.
    """
    norm = [_posix(w) for w in writes]
    haystack = f"{title}\n{body}".lower()
    for cls in table.work_classes:
        if any(fnmatch.fnmatchcase(w, pat) for pat in cls.paths for w in norm):
            return cls
        if any(kw.lower() in haystack for kw in cls.keywords):
            return cls
        if any(t in body for t in cls.tools):
            return cls
    return None


def covers(tool_range: Optional[Sequence[str]], required: Sequence[str]) -> bool:
    """True when every required entry is matched by some tool in `tool_range`.

    A tool `t` matches required `r` when `t == r`, `t` starts with `r` and `r` ends in `__` (an MCP
    server prefix), or `t` ends in `*` and `r` starts with `t[:-1]`. An unknown range (`None`)
    covers nothing.
    """
    if not required:
        return True
    if not tool_range:
        return False

    def matched(r: str) -> bool:
        return any(
            t == r
            or (r.endswith("__") and t.startswith(r))
            or (t.endswith("*") and r.startswith(t[:-1]))
            for t in tool_range
        )

    return all(matched(r) for r in required)


class _Definitions:
    """The agent-definition scan, taken at most once and only when a lookup needs it."""

    def __init__(self) -> None:
        self._defs: Optional[dict[str, dict]] = None

    def get(self, agent_type: str) -> Optional[dict]:
        if self._defs is None:
            from coordinator_core.hooks.block_unenumerated_agent_type import scan_agent_definitions

            self._defs = scan_agent_definitions()
        return self._defs.get(agent_type)


def _parse_tools(raw: Any) -> Optional[tuple[str, ...]]:
    if isinstance(raw, str):
        items = [t.strip() for t in raw.split(",")]
    elif isinstance(raw, list):
        items = [str(t).strip() for t in raw]
    else:
        return None
    tools = tuple(t for t in items if t)
    return tools or None


def agent_tool_range(
    agent_type: str, table: Optional[RouteTable], *, _defs: Optional[_Definitions] = None
) -> Optional[tuple[str, ...]]:
    """`table.agent_tool_ranges[agent_type]` when listed, else the definition's `tools:`.

    `None` means no readable range (a harness built-in, an uninstalled plugin, no `tools:` key),
    which `covers` treats as covering nothing.
    """
    if table is not None and table.agent_tool_ranges is not None and agent_type in table.agent_tool_ranges:
        return table.agent_tool_ranges[agent_type]
    fm = (_defs or _Definitions()).get(agent_type)
    return None if fm is None else _parse_tools(fm.get("tools"))


def _missing(tool_range: Optional[Sequence[str]], required: Sequence[str]) -> list[str]:
    return [r for r in required if not covers(tool_range, [r])]


def admit(
    agent_type: str, work_class: WorkClass, table: RouteTable, *, _defs: Optional[_Definitions] = None
) -> Admission:
    """admit when `agent_type`'s range covers the class's required tools; otherwise the class's
    `on_mismatch` verdict, with `target` the class agent type under redirect."""
    missing = tuple(_missing(agent_tool_range(agent_type, table, _defs=_defs), work_class.required_tools))
    if not missing:
        return Admission("admit", None, "")
    reason = (
        f"agent {agent_type} lacks {', '.join(missing)} for work class {work_class.id}; "
        f"dispatch {work_class.agent_type}"
    )
    if work_class.on_mismatch == "redirect":
        return Admission("redirect", work_class.agent_type, reason, missing)
    return Admission("refuse", None, reason, missing)


def _executor_type() -> str:
    from coordinator_core.ops.dispatch_emit import emit

    return emit._EXECUTOR_AGENT_TYPE


def _model_of(
    agent_type: str, defs: _Definitions, override: Optional[str] = None
) -> str:
    from coordinator_core.hooks.block_unenumerated_agent_type import declared_model_pin
    from coordinator_core.ops.dispatch_emit import emit

    pinned = emit._AGENT_MODELS.get(agent_type)
    if pinned:
        return pinned
    if override:
        return override
    fm = defs.get(agent_type)
    model = declared_model_pin(fm) if fm else None
    if not model:
        raise RouteAdmissionError(
            f"routed agent {agent_type} has no model pin; add model: to its definition "
            "or agent_model to the row"
        )
    return model


def _row_writes(row: Any) -> list[str]:
    writes = [] if row.writes is UNDECLARED else [str(w) for w in row.writes]
    for prefix in row.writes_under or ():
        writes.append(_posix(str(prefix)).rstrip("/") + "/")
    return writes


def resolve_agent_type(
    row: Any, table: RouteTable, *, _defs: Optional[_Definitions] = None
) -> Optional[RouteDecision]:
    """The agent type `row` dispatches with under `table`, or `None` for a row the table leaves alone.

    Only a row whose derived type is the executor, or one that names an `agent_type`, is routed.
    First matching class in list order wins. A derived executor row takes the class agent type
    (triage first when the class sets one), or `default_agent_type` when no class matches. An
    explicit type is admitted when it covers the class's required tools; otherwise `refuse` raises
    `RouteAdmissionError` and `redirect` re-points the row at the class agent type.
    """
    from coordinator_core.ops.dispatch_emit import emit

    try:
        base = emit._row_agent_type(row)
    except Exception:
        return None  # the composer's own call raises this same refusal
    explicit = bool(row.agent_type)
    if not explicit and base != emit._EXECUTOR_AGENT_TYPE:
        return None

    defs = _defs or _Definitions()
    cls = match_work_class(table, writes=_row_writes(row), title=row.title or "", body=row.body or "")
    if cls is None:
        if explicit or table.default_agent_type == base:
            return None
        return RouteDecision(table.default_agent_type, None, None, f"{row.id} -> {table.default_agent_type} (default)")

    if explicit:
        verdict = admit(base, cls, table, _defs=defs)
        if verdict.verdict == "admit":
            return RouteDecision(base, None, cls)
        if verdict.verdict == "refuse":
            raise RouteAdmissionError(f"row {row.id}: {verdict.reason}")
        return RouteDecision(cls.agent_type, None, cls, f"{row.id} redirected: {verdict.reason}")

    note = None if cls.agent_type == base else f"{row.id} -> {cls.agent_type} (class {cls.id})"
    return RouteDecision(cls.agent_type, cls.triage_agent_type, cls, note)


def route_rows(
    rows: Sequence[Any], repo_root: "str | os.PathLike[str]"
) -> tuple[Sequence[Any], dict[str, Route], list[str]]:
    """Apply the repo's route table to `rows`: `(rows, routes by row id, narration notes)`.

    With no table returns `(rows, {}, [])`, the same list object. A routed row is rewritten with
    `_replace(agent_type=..., agent_model=...)`. Raises `RouteAdmissionError` for a refused row, a
    class whose agent type lacks its own required tools, or a routed type with no model.
    """
    table = load_route_table(repo_root)
    if table is None:
        return rows, {}, []

    defs = _Definitions()
    executor = _executor_type()
    out: list[Any] = []
    routes: dict[str, Route] = {}
    notes: list[str] = []
    for row in rows:
        decision = resolve_agent_type(row, table, _defs=defs)
        if decision is None:
            out.append(row)
            continue
        current = row.agent_type or executor
        changed = decision.agent_type != current
        primary_model = _model_of(
            decision.agent_type, defs, row.agent_model if not changed else None
        )
        if changed:
            row = row._replace(agent_type=decision.agent_type, agent_model=primary_model)
        out.append(row)
        if decision.note:
            notes.append(decision.note)
        cls = decision.work_class
        if cls is None:
            continue
        lacking = _missing(agent_tool_range(decision.agent_type, table, _defs=defs), cls.required_tools)
        if lacking:
            raise RouteAdmissionError(
                f"row {row.id}: work class {cls.id} routes to {decision.agent_type}, "
                f"which lacks {', '.join(lacking)}"
            )
        triage = None
        if decision.triage_agent_type:
            triage = (decision.triage_agent_type, _model_of(decision.triage_agent_type, defs))
        routes[row.id] = Route(
            work_class=cls.id,
            primary=(decision.agent_type, primary_model),
            targets=_targets(table, cls, decision.agent_type, primary_model, defs),
            triage=triage,
        )
    return out, routes, notes


def _targets(
    table: RouteTable, cls: WorkClass, primary: str, primary_model: str, defs: _Definitions
) -> dict[str, str]:
    candidates = [table.default_agent_type]
    for c in table.work_classes:
        candidates.append(c.agent_type)
        if c.triage_agent_type:
            candidates.append(c.triage_agent_type)
    candidates.extend(table.agent_tool_ranges or ())
    targets = {primary: primary_model}
    for t in candidates:
        if t in targets or not covers(agent_tool_range(t, table, _defs=defs), cls.required_tools):
            continue
        try:
            targets[t] = _model_of(t, defs)
        except RouteAdmissionError:
            continue
    return targets
