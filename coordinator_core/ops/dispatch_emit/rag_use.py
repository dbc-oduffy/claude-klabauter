"""Pinned interface and host-CLI adapter for example-retrieval-repo usage and reachability.

Reads `example-retrieval-repo usage --session <id> --since <iso> --until <iso> --json` and
`example-retrieval-repo probe-reachability --json`. Every reader returns None when the CLI is
absent, times out, or emits unparseable output: None means "unknown", never "zero".
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

DANGER_CALLS = 0
# Provisional: recalibrate from host meter rows once example-retrieval-repo usage lands.
WARNING_BELOW = 5
DEBOUNCE_SECONDS = 180
# A probe is a real engine-corpus call, so it must not ride the 5s scan tick.
PROBE_INTERVAL_SECONDS = 60
HALT_RAG_CORPUS_DOWN = "rag_corpus_down"
HALT_EXAMPLE_RETRIEVAL_REPO_UNUSED = "example_retrieval_repo_unused"
HALT_EXAMPLE_RETRIEVAL_REPO_CADENCE = "example_retrieval_repo_cadence"
HALT_EXAMPLE_RETRIEVAL_REPO_NOT_SERVING = "example_retrieval_repo_not_serving"
HALT_UE_CORPUS_GAP = "ue_corpus_gap"
HALT_UE_RETRIEVAL_MISSING = "ue_retrieval_missing"
HALT_UE_RETRIEVAL_CHECK_FAILED = "ue_retrieval_check_failed"
HALT_EXAMPLE_RETRIEVAL_REPO_OUTAGE_REPORTED = "example_retrieval_repo_outage_reported"
HALT_UE_PHASE_RETRIEVAL_MISSING = "ue_phase_retrieval_missing"
TIER_NONE, TIER_RAG, TIER_UE = "none", "rag", "ue"
GATED_ROLES_RELPATH = "coordinator/contract/example-retrieval-repo-gated-roles.json"
DEFAULT_TOOL_PREFIX = "mcp__project-rag__"
DEGRADED = "degraded"
SEARCH_DEGRADED_KEY = "search_degraded"
# The checker made no example-retrieval-repo call: a checker-behaviour gap, not an outage.
RAG_NOT_USED = "rag_not_used"
# The checker transcript could not be located or read: unknown, not an outage.
TRANSCRIPT_UNREADABLE = "transcript_unreadable"
RESTORE_MESSAGE = (
    "The code-index engine corpus is down: UE work will not succeed without it. "
    "Restore the code index, then refire this run."
)

_CLI = "project" + "-rag"
_STATES = ("ok", "degraded", "down")

# The single seam: runner(argv) -> stdout text, or None when the CLI is absent or fails.
# No runner means unknown (None), never zero.
Runner = Callable[[list[str]], Optional[str]]


@dataclass(frozen=True)
class RagUsage:
    """Calls to example-retrieval-repo made by one session inside one window."""

    calls: int
    by_tool: dict[str, int] = field(default_factory=dict)
    timeouts: int = 0
    degraded: bool = False


@dataclass(frozen=True)
class ProbeState:
    """Reachability of the engine corpus: state is ok, degraded or down."""

    state: str
    per_source: dict[str, Any] = field(default_factory=dict)


def _json_object(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def read_usage(
    session_id: str, since: str, until: str, *, runner: Optional[Runner] = None
) -> Optional[RagUsage]:
    """Usage of `session_id` between ISO `since` and `until`; None when unreadable."""
    if runner is None:
        return None
    out = runner(
        [_CLI, "usage", "--session", session_id, "--since", since,
         "--until", until, "--json"]
    )
    data = _json_object(out)
    if data is None:
        return None
    by_tool_raw = data.get("by_tool")
    calls_raw = data.get("calls")
    if not isinstance(by_tool_raw, dict) and isinstance(calls_raw, dict):
        by_tool_raw = calls_raw
    by_tool = (
        {str(k): _as_int(v) for k, v in by_tool_raw.items()}
        if isinstance(by_tool_raw, dict)
        else {}
    )
    if isinstance(calls_raw, (int, float)) and not isinstance(calls_raw, bool):
        calls = int(calls_raw)
    elif by_tool:
        calls = sum(by_tool.values())
    elif "calls" in data or "by_tool" in data:
        calls = 0
    else:
        return None
    return RagUsage(
        calls=calls,
        by_tool=by_tool,
        timeouts=_as_int(data.get("timeouts")),
        degraded=bool(data.get("degraded")),
    )


def probe(*, runner: Optional[Runner] = None) -> Optional[ProbeState]:
    """Engine-corpus reachability; None when the CLI cannot answer."""
    if runner is None:
        return None
    data = _json_object(runner([_CLI, "probe-reachability", "--json"]))
    if data is None:
        return None
    state = data.get("state")
    if state not in _STATES:
        return None
    per_source = data.get("per_source")
    return ProbeState(
        state=state, per_source=per_source if isinstance(per_source, dict) else {}
    )


def is_ue_repo(root: str | Path) -> bool:
    """True when `root` declares UE or carries a UE marker (same predicate as the compose legs)."""
    from coordinator_core.ops.dispatch_emit.ue_route_source import is_ue_repo as _is_ue_repo

    return _is_ue_repo(root)


#: A path the UE-corpus docs-check serves: control/plugin/**, Source/**, *.cpp, *.h, *.uasset.
#: One source for the Python predicate and the composed JS test; keep it free of Python-only syntax.
UE_PATH_PATTERN = r"(?:^|/)(?:control/plugin/|Source/)|\.(?:cpp|h|uasset)$"
UE_PATH_RE = re.compile(UE_PATH_PATTERN, re.IGNORECASE)
NO_UE_PATHS_GAP = "no_ue_paths"


def touches_ue_path(paths: Iterable[str]) -> bool:
    """True when any of `paths` is a UE path (`UE_PATH_PATTERN`); backslashes read as `/`."""
    return any(UE_PATH_RE.search(str(p).replace("\\", "/")) for p in paths)


def rag_installed(repo_root: str | Path) -> bool:
    """True when a example-retrieval-repo install is discoverable from `repo_root`; False on any error."""
    try:
        from coordinator_core.hooks.example_retrieval_repo_detect import find_example_retrieval_repo_dir

        return find_example_retrieval_repo_dir(str(repo_root)) is not None
    except Exception:
        return False


_MCP_CACHE: dict[str, tuple[int, frozenset]] = {}


def _mcp_names_in(path: Path, *, nested: bool) -> frozenset:
    """Lower-cased MCP server names declared in the JSON file at `path`; empty on any error.

    `nested` also reads each `projects[*].mcpServers` (the `~/.claude.json` shape). Memoised on mtime.
    """
    try:
        key, mtime = str(path), path.stat().st_mtime_ns
    except OSError:
        return frozenset()
    hit = _MCP_CACHE.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    names: set[str] = set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        scopes = [data] if isinstance(data, dict) else []
        if nested and scopes:
            scopes += [p for p in (data.get("projects") or {}).values() if isinstance(p, dict)]
        for scope in scopes:
            servers = scope.get("mcpServers")
            if isinstance(servers, dict):
                names.update(str(n).lower() for n in servers)
    except (OSError, ValueError, AttributeError):
        pass
    out = frozenset(names)
    _MCP_CACHE[key] = (mtime, out)
    return out


def rag_tier(
    repo_root: str | Path,
    *,
    settings: Optional[Path] = None,
    plugin_roots: Optional[Iterable[Path]] = None,
    mcp_names: Optional[Iterable[str]] = None,
) -> str:
    """Enforcement tier for `repo_root`: "none" (example-retrieval-repo not installed), "rag", or "ue".

    Installed means an in-repo or settings-home index/dir, an installed plugin or a registered MCP
    server named for example-retrieval-repo. "ue" means a ue-addon or example-game-repo plugin/server is installed, or
    `is_ue_repo(repo_root)` holds on an installed box. Staleness or a broken layer never lowers the
    tier. No subprocess; never raises (an unreadable source reads as absent).
    """
    try:
        root = Path(repo_root)
        if mcp_names is None:
            from coordinator_core._settings_home import home_dir

            names = _mcp_names_in(home_dir() / ".claude.json", nested=True) | _mcp_names_in(
                root / ".mcp.json", nested=False
            )
        else:
            names = frozenset(str(n).lower() for n in mcp_names)
        if plugin_roots is None:
            from coordinator_core.ops.dispatch_emit.plugin_review_overlay import installed_plugin_roots

            plugin_roots = installed_plugin_roots()
        from coordinator_core.ops.dispatch_emit.plugin_review_overlay import _plugin_name

        plugin_names = {_plugin_name(Path(r)).lower() for r in plugin_roots}
        if settings is None:
            from coordinator_core._settings_home import settings_home

            settings = settings_home()
        every = names | plugin_names
        ue_addon = any(("ue-addon" in n or "example-game-repo" in n) for n in every)
        installed = (
            any("project-rag" in n for n in every)
            or (Path(settings) / "project-rag").is_dir()
            or rag_installed(root)
        )
        if not installed and not ue_addon:
            return TIER_NONE
        return TIER_UE if ue_addon or is_ue_repo(root) else TIER_RAG
    except Exception:
        return TIER_NONE


_EXEMPT_FALLBACK = ("*executor*", "*test-runner*", "*op-relay*")
_GATED_FALLBACK = (
    "*sizing-scout*", "*plan-author*", "*enricher*", "*prior-art-checker*", "*premise-checker*",
    "*research*", "*specialist*", "*scout*", "*reviewer*", "*staff-eng*",
)
#: Fallback cadence when the DoE file is missing: one example-retrieval-repo call in every 12 tool calls.
_UE_CADENCE_FALLBACK = {"window_tool_calls": 12, "min_rag_calls": 1}


def _pattern_list(value: Any) -> list[str]:
    return [str(v).strip().lower() for v in value if str(v).strip()] if isinstance(value, list) else []


def load_gated_roles(plugin_root: str | Path | None = None) -> dict:
    """Gated-role config `{gated, exempt, ue_cadence, tool_prefix, source[, reason]}`.

    Reads `coordinator/contract/example-retrieval-repo-gated-roles.json` under `plugin_root` (else the first
    installed plugin root carrying it). A missing, unreadable or malformed file yields the built-in
    fallback with `source: "fallback"` and a `reason`, so the hard rule never lapses silently.
    Role entries are fnmatch patterns over the agent type (plugin prefix optional).
    """
    path: Optional[Path] = None
    if plugin_root is not None:
        path = Path(plugin_root) / GATED_ROLES_RELPATH
    else:
        try:
            from coordinator_core.ops.dispatch_emit.plugin_review_overlay import installed_plugin_roots

            path = next((r / GATED_ROLES_RELPATH for r in installed_plugin_roots()
                         if (r / GATED_ROLES_RELPATH).is_file()), None)
        except Exception:
            path = None
    fallback = {
        "gated": list(_GATED_FALLBACK), "exempt": list(_EXEMPT_FALLBACK),
        "ue_cadence": dict(_UE_CADENCE_FALLBACK), "tool_prefix": DEFAULT_TOOL_PREFIX,
        "source": "fallback",
    }
    if path is None:
        return {**fallback, "reason": "gated-roles file not found"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        return {**fallback, "reason": f"gated-roles file unreadable: {exc}"}
    gated, exempt = _pattern_list(data.get("gated")), _pattern_list(data.get("exempt"))
    if not gated:
        return {**fallback, "reason": "gated-roles file lists no gated roles"}
    cad = data.get("ue_cadence")
    cadence = dict(_UE_CADENCE_FALLBACK)
    if isinstance(cad, dict) and _as_int(cad.get("window_tool_calls")) > 0 and _as_int(cad.get("min_rag_calls")) > 0:
        cadence = {"window_tool_calls": _as_int(cad["window_tool_calls"]),
                   "min_rag_calls": _as_int(cad["min_rag_calls"])}
    prefix = data.get("tool_prefix")
    return {"gated": gated, "exempt": exempt, "ue_cadence": cadence,
            "tool_prefix": prefix if isinstance(prefix, str) and prefix else DEFAULT_TOOL_PREFIX,
            "source": "file"}


def is_gated(agent_type: Any, roles: dict) -> bool:
    """True when `agent_type` matches a gated pattern and no exempt pattern (exempt wins)."""
    t = str(agent_type or "").strip().lower()
    if not t:
        return False
    forms = {t, t.rsplit(":", 1)[-1]}

    def hit(patterns: Any) -> bool:
        return any(fnmatch.fnmatchcase(f, p) for p in _pattern_list(patterns) for f in forms)

    return hit(roles.get("gated")) and not hit(roles.get("exempt"))


def cadence_gap(positions: Iterable[int], total: int, window: int, minimum: int) -> bool:
    """True when some window of `window` consecutive tool calls out of `total` holds fewer than `minimum` hits.

    `positions` are 0-based indices of example-retrieval-repo calls among the agent's tool calls. A sequence
    shorter than `window` is one window.
    """
    pos = sorted({int(p) for p in positions if 0 <= int(p) < max(total, 1)})
    if window <= 0 or minimum <= 0:
        return False
    span = min(window, total) if total > 0 else 0
    if span == 0:
        return False
    lo = 0
    hi = 0
    for start in range(total - span + 1):
        while lo < len(pos) and pos[lo] < start:
            lo += 1
        hi = max(hi, lo)
        while hi < len(pos) and pos[hi] < start + span:
            hi += 1
        if hi - lo < min(minimum, span):
            return True
    return False


def _agent_entry(row: dict) -> dict:
    return {"agent_id": str(row.get("agent_id") or ""), "agent_type": str(row.get("agent_type") or ""),
            "label": str(row.get("label") or ""), "rag_calls": _as_int(row.get("rag_calls"))}


def agent_rag_halt(
    rows: Optional[list[dict]],
    *,
    tier: str,
    roles: dict,
    serving: Any = None,
) -> Optional[dict]:
    """The halted_by dict for per-agent example-retrieval-repo use, or None when the run may continue.

    `rows` are agent_usage rows (`agent_id`, `agent_type`, `label`, `rag_calls`, `rag_errors`,
    `rag_positions`, optional `tool_calls`, `rag_empty`); a row whose `rag_calls` is None is unknown
    and never halts. Any example-retrieval-repo tool_use counts as an attempt, a failed call included. Tier none
    never halts. Tier rag halts on zero attempts by a gated agent: `{halted, agents}`. Tier ue checks,
    in order, `serving` (False or `{serving: False, layer}`) -> example_retrieval_repo_not_serving
    `{halted, layer, queries, agents}`; every successful call an empty ue-addon answer (`rag_empty` an
    int or a list of queries, fleet-wide) -> ue_corpus_gap `{halted, layer, queries, agents}`; zero
    attempts -> example_retrieval_repo_unused; below `roles["ue_cadence"]` -> example_retrieval_repo_cadence
    `{halted, agents}` (agents carry actual `rag_calls`).
    """
    if tier not in (TIER_RAG, TIER_UE):
        return None
    live = [r for r in rows or [] if isinstance(r, dict)]
    gated = [r for r in live if r.get("rag_calls") is not None and is_gated(r.get("agent_type"), roles)]
    if tier == TIER_UE:
        down = serving is False or (isinstance(serving, dict) and serving.get("serving") is False)
        if down:
            layer = str(serving.get("layer") or "project-rag") if isinstance(serving, dict) else "project-rag"
            return {"halted": HALT_EXAMPLE_RETRIEVAL_REPO_NOT_SERVING, "layer": layer, "queries": [], "agents": []}
        queries: list[str] = []
        empty = ok_calls = 0
        for r in live:
            e = r.get("rag_empty")
            if isinstance(e, list):
                queries += [str(q) for q in e]
                empty += len(e)
            else:
                empty += _as_int(e)
            ok_calls += max(_as_int(r.get("rag_calls")) - _as_int(r.get("rag_errors")), 0)
        if empty > 0 and empty >= ok_calls:
            return {"halted": HALT_UE_CORPUS_GAP, "layer": "ue-addon", "queries": queries, "agents": []}
    silent = [_agent_entry(r) for r in gated if _as_int(r.get("rag_calls")) <= 0]
    if silent:
        return {"halted": HALT_EXAMPLE_RETRIEVAL_REPO_UNUSED, "agents": silent}
    if tier == TIER_UE:
        cad = roles.get("ue_cadence") or _UE_CADENCE_FALLBACK
        window, minimum = _as_int(cad.get("window_tool_calls")), _as_int(cad.get("min_rag_calls"))
        low = []
        for r in gated:
            pos = [p for p in (r.get("rag_positions") or []) if isinstance(p, int) and not isinstance(p, bool)]
            total = max(_as_int(r.get("tool_calls")), (max(pos) + 1) if pos else 0)
            if cadence_gap(pos, total, window, minimum):
                low.append(_agent_entry(r))
        if low:
            return {"halted": HALT_EXAMPLE_RETRIEVAL_REPO_CADENCE, "agents": low}
    return None


def ue_retrieval_verdict(
    *,
    ue: bool,
    rows: Optional[list[dict]],
    friction_rows: Optional[list] = None,
    outage_reports: Optional[list] = None,
    metrics_rows: Optional[list] = None,
) -> Optional[dict]:
    """Terminal UE-retrieval halted_by (friction > outage > zero ue_calls per judged row), or None to pass; each agent entry carries ue_calls, denies and metrics_rows."""
    if not ue:
        return None
    live = [r for r in rows or [] if isinstance(r, dict)]
    friction = [f for f in friction_rows or []]
    metrics = [m for m in metrics_rows or [] if isinstance(m, dict)]

    def _owned(rows_: list, agent_id: str) -> int:
        return sum(1 for x in rows_ if isinstance(x, dict) and agent_id and str(x.get("agent_id") or "") == agent_id)

    entries = []
    for r in live:
        e = _agent_entry(r)
        entries.append({**e, "ue_calls": _as_int(r.get("ue_calls")),
                        "denies": _owned(friction, e["agent_id"]),
                        "metrics_rows": _owned(metrics, e["agent_id"])})
    reports = [x for x in outage_reports or []]
    if friction:
        cause = HALT_UE_RETRIEVAL_CHECK_FAILED
        agents = entries
    elif reports:
        cause = HALT_EXAMPLE_RETRIEVAL_REPO_OUTAGE_REPORTED
        agents = entries
    else:
        agents = [e for r, e in zip(live, entries) if r.get("ue_calls") is not None and e["ue_calls"] <= 0]
        if not agents:
            return None
        cause = HALT_UE_RETRIEVAL_MISSING
    return {"halted": cause, "cause": cause, "agents": agents,
            "friction_count": len(friction), "reports": reports}


def stage_rag_verdict(*, served: bool, rows: Optional[list[dict]]) -> dict:
    """Stage verdict `{verdict, rag_calls, per_worker}` from per-worker usage rows.

    `rows` are agent_usage rows (`label`, `agent_id`, `rag_calls`, `rag_failed`); None or empty
    means no worker transcript was read: TRANSCRIPT_UNREADABLE, never zero. A served repo whose
    workers made no example-retrieval-repo call in total is RAG_NOT_USED; calls that all failed are DEGRADED.
    """
    if not served:
        return {"verdict": "not_applicable", "rag_calls": 0, "per_worker": []}
    per_worker: list[dict] = []
    calls = failed = 0
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        n, bad = _as_int(r.get("rag_calls")), _as_int(r.get("rag_failed"))
        calls += n
        failed += bad
        per_worker.append({"label": str(r.get("label") or ""), "agent_id": str(r.get("agent_id") or ""),
                           "rag_calls": n, "rag_failed": bad})
    if not per_worker:
        return {"verdict": TRANSCRIPT_UNREADABLE, "rag_calls": 0, "per_worker": []}
    if calls <= 0:
        verdict = RAG_NOT_USED
    elif failed >= calls:
        verdict = DEGRADED
    else:
        verdict = "ok"
    return {"verdict": verdict, "rag_calls": calls, "per_worker": per_worker}


def docs_check_rag_verdict(
    *,
    ue: bool,
    rag_installed: bool,
    rag_calls: int,
    rag_failed: int,
    rag_timeouts: int = 0,
    rag_refused: int = 0,
    engine_queryable: Optional[bool] = None,
) -> str:
    """Gate verdict: "not_applicable", "ok", DEGRADED, RAG_NOT_USED or HALT_RAG_CORPUS_DOWN.

    Down is definite only: engine_queryable False, or every call failed with a refused
    connection. No example-retrieval-repo call at all is RAG_NOT_USED, never down. Any other failure set
    (timeouts, unclassified errors, or any failure while engine_queryable is True) is DEGRADED;
    a partial failure that includes a timeout is DEGRADED too. The gate op passes its
    `engine_queryable` param.
    """
    if not ue or not rag_installed:
        return "not_applicable"
    if engine_queryable is False:
        return HALT_RAG_CORPUS_DOWN
    if rag_calls <= 0:
        return RAG_NOT_USED
    if rag_failed >= rag_calls:
        if rag_refused > 0:
            return HALT_RAG_CORPUS_DOWN
        return DEGRADED
    if rag_timeouts > 0:
        return DEGRADED
    return "ok"
