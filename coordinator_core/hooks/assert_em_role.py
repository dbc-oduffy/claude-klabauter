"""coordinator_core.hooks.assert_em_role — SessionStart(*) op: asserts EM
identity to the main coordinator session, via an ordered manifest of
EM-only doctrine snippets plus a bounded peer-contention/Group-EM read.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/assert-em-role.py`. This is a
standalone top-level SessionStart registration in DoE's own hooks.json,
deliberately NOT folded into either dispatcher fan-in landed alongside it —
its own docstring records the measured reason (a shared-stdout fan-in
truncated its payload before it reached actual session context in 271 of 279
archived sessions). Composed neither into `sessionstart_dispatch.py` nor
`sessionstart_async_dispatch.py` here either, for the same reason: its
op-level envelope is delivered on its own, not folded into a concatenated
aggregate.

ADAPTATION (class 1): the PLUGIN-root manifest anchor
(`_PLUGIN_ROOT / "snippets"`, previously `Path(__file__).resolve().parents[2]`
under DoE's own `coordinator/hooks/scripts/` tree) is replaced with
`CLAUDE_PLUGIN_ROOT`-anchored resolution — this op's own `__file__` never sits
under the doctrine plugin tree, matching the convention every other displaced-
doctrine-asset arrival in this row uses. The `group-em-nomination.py` loader
resolves the SAME way: `<plugin_root>/bin/group-em-nomination.py`, a doctrine-
plane sibling reached at runtime via the env var, never a hardcoded DoE path.
The REPO-slot manifest entry (`.claude/em-context.md`) and the peer-
contention/session-registry reads are unchanged — both were already
payload-cwd/`CLAUDE_PROJECT_DIR`-anchored, never `__file__`-anchored, in the
source script.

Op contract: `params["payload"]` supplies `cwd` and `session_id`. Returns
`context_only("SessionStart", <manifest text>)` — the concatenated PLUGIN/REPO
snippet bodies, per-entry error banners, and the bounded peer-contention/
Group-EM lines, matching the source script's raw-stdout emission byte-for-
byte in content (never `no_advisory()`: the source script always writes at
least a leading newline, so this op always returns SOME text).

Negative-spec:
    Does NOT read stdin with a bounded background thread — this op already
    receives its payload as a parsed dict (the JSON-RPC contract), so the
    source script's `_read_stdin(timeout=2.0)` thread-based guard against a
    hanging raw stdin read has no analogue; dropped, not ported.
    Does NOT anchor the manifest under `Path(__file__)` — see ADAPTATION.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.hooks._envelope import context_only
from coordinator_core.ipc import register_op

_ROOT_PLUGIN = "PLUGIN"
_ROOT_REPO = "REPO"

_EM_SNIPPET_MANIFEST = [
    (_ROOT_PLUGIN, "agent-role-em.md"),
    (_ROOT_REPO, ".claude/em-context.md"),
]

#: See DoE source script's own docstring for the byte-budget derivation this
#: constant is pinned to (coordinator/tests/baselines/em-payload-budget.json,
#: legs.em_context).
_REPO_SNIPPET_SOFT_CAP_BYTES = 815

_GEM_NAME_MAX_CHARS = 32
_GEM_SESSION_PREFIX_CHARS = 8
_GEM_CLAUSE = "G-EM active: {name} ({session}) -- see wiki group-em-standing.md\n\n"

_PEER_READ_POINTER = (
    "assert-em-role: {repo_count} peer session(s) in this repo, {box_count} "
    "on this machine -- existence only. A count is not a stand-down signal "
    "and not permission to send.\n"
)


def _plugin_root() -> "Optional[Path]":
    raw = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not raw:
        return None
    try:
        return Path(raw)
    except Exception:
        return None


def _consumer_repo_root(payload: dict) -> "Optional[Path]":
    candidates = [os.environ.get("CLAUDE_PROJECT_DIR"), payload.get("cwd")]
    start = None
    for candidate in candidates:
        if candidate:
            try:
                start = Path(candidate).resolve()
            except OSError:
                continue
            break
    if start is None:
        try:
            start = Path.cwd().resolve()
        except OSError:
            return None

    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return start


def _resolve_claude_config_dir() -> "Optional[Path]":
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        try:
            return Path(override).resolve()
        except OSError:
            return None
    home_override = os.environ.get("CLAUDE_HOME")
    try:
        home = Path(home_override).resolve() if home_override else Path.home()
    except OSError:
        return None
    return home / ".claude"


def _compute_contention(repo_root, session_id, timeout: float = 0.3):
    if repo_root is None:
        return None

    box = {"result": None}

    def _work(exclude_session_id) -> None:
        try:
            config_dir = _resolve_claude_config_dir()
            if config_dir is None:
                return
            sessions_dir = config_dir / "sessions"
            if not sessions_dir.is_dir():
                return
            repo_count = 0
            box_count = 0
            for entry in sessions_dir.glob("*.json"):
                try:
                    record = json.loads(entry.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if exclude_session_id and record.get("sessionId") == exclude_session_id:
                    continue
                box_count += 1
                raw_cwd = record.get("cwd")
                if not isinstance(raw_cwd, str) or not raw_cwd:
                    continue
                try:
                    cwd_path = Path(raw_cwd).resolve()
                except OSError:
                    continue
                for directory in (cwd_path, *cwd_path.parents):
                    if directory == repo_root:
                        repo_count += 1
                        break
                    if (directory / ".git").exists():
                        break
            box["result"] = (repo_count, box_count)
        except Exception:
            box["result"] = None

    t = threading.Thread(target=_work, args=(session_id,), daemon=True)
    t.start()
    t.join(timeout)
    return box["result"]


def _group_em_nomination_module(plugin_root: "Optional[Path]"):
    if plugin_root is None:
        return None
    try:
        import importlib.util

        path = plugin_root / "bin" / "group-em-nomination.py"
        spec = importlib.util.spec_from_file_location("_assert_em_gem_nomination", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _gem_display_name(repo_root, session_id: str, nominated_name) -> str:
    name = str(nominated_name or "").strip()
    if name:
        return name
    try:
        watch = json.loads(
            (Path(str(repo_root)) / "state" / "group-em-watch.json").read_text(
                encoding="utf-8"
            )
        )
        if str(watch.get("holder_session_id") or "") == session_id:
            holder_name = str(watch.get("holder_name") or "").strip()
            if holder_name:
                return holder_name
    except Exception:
        pass
    return "name unrecorded"


def _group_em_clause(repo_root, plugin_root, timeout: float = 0.3) -> str:
    if repo_root is None:
        return ""

    box = {"result": ""}

    def _work() -> None:
        try:
            gem = _group_em_nomination_module(plugin_root)
            if gem is None:
                return
            record = gem.read_record(str(repo_root))
            if not isinstance(record, dict):
                return
            live, _row = gem.is_live(record)
            if not live:
                return
            holder = str(record.get("session_id") or "")
            name = _gem_display_name(repo_root, holder, record.get("peer_name"))
            box["result"] = _GEM_CLAUSE.format(
                name=name[:_GEM_NAME_MAX_CHARS],
                session=holder[:_GEM_SESSION_PREFIX_CHARS] or "unknown",
            )
        except Exception:
            box["result"] = ""

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout)
    return box["result"]


def _exc_reason(exc: Exception) -> str:
    strerror = getattr(exc, "strerror", None)
    if strerror:
        return str(strerror)
    return type(exc).__name__


def _compose_missing_snippet_banner(rel_path: str, exc: Exception, root: str) -> str:
    reason = _exc_reason(exc)
    if root == _ROOT_PLUGIN:
        return (
            f"assert-em-role: {rel_path} MISSING ({reason}) -- EM role not "
            f"fully asserted; restore coordinator/snippets/{rel_path}.\n\n"
        )
    return (
        f"assert-em-role: {rel_path} unreadable ({reason}) -- its content was "
        f"not delivered this session.\n\n"
    )


def _compose_oversize_repo_banner(rel_path: str, byte_len: int) -> str:
    return (
        f"assert-em-role: {rel_path} is {byte_len}B, over its "
        f"{_REPO_SNIPPET_SOFT_CAP_BYTES}B share of the 1,700B ceiling "
        f"(2KB-First Rule, doctrine-channel-purposes.md:175). Delivered "
        f"anyway -- consider a wiki.\n\n"
    )


@register_op("hooks.assert_em_role")
def _handler(params: dict, repo_root=None) -> dict:
    payload = params.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    payload = dict(payload)

    plugin_root = _plugin_root()
    snippets_dir = (plugin_root / "snippets") if plugin_root else None
    consumer_repo_root = _consumer_repo_root(payload)

    parts: "list[str]" = ["\n"]
    for root, rel_path in _EM_SNIPPET_MANIFEST:
        if root == _ROOT_PLUGIN:
            if snippets_dir is None:
                continue
            snippet_path = snippets_dir / rel_path
        else:
            if consumer_repo_root is None:
                continue
            snippet_path = consumer_repo_root / rel_path
            if not snippet_path.exists():
                continue

        try:
            snippet_text = snippet_path.read_text(encoding="utf-8")
        except OSError as exc:
            parts.append(_compose_missing_snippet_banner(rel_path, exc, root))
            continue

        if root == _ROOT_REPO and len(snippet_text.encode("utf-8")) > _REPO_SNIPPET_SOFT_CAP_BYTES:
            parts.append(_compose_oversize_repo_banner(rel_path, len(snippet_text.encode("utf-8"))))

        parts.append(snippet_text)
        parts.append("\n")

    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    contention = _compute_contention(consumer_repo_root, session_id)
    if contention is not None and any(contention):
        repo_count, box_count = contention
        try:
            parts.append(_PEER_READ_POINTER.format(repo_count=repo_count, box_count=box_count))
        except Exception:
            pass
        try:
            gem_clause = _group_em_clause(consumer_repo_root, plugin_root)
            if gem_clause:
                parts.append(gem_clause)
        except Exception:
            pass

    return context_only("SessionStart", "".join(parts))
