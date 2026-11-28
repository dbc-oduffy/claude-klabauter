"""Owner marks for engine-written directories, the liveness predicate over them, and the never-sweep keep-set.

A writer stamps `<dir>/.owner.json` with `write_mark`; a pruner reads it with `read_mark` and asks
`owner_alive`. Invariant: every unknown, unreadable or erroring answer is `None`, and every caller
treats `None` like `True` (fail closed to keep). Invariant: no sweep deletes a path for which
`is_kept` is true, whatever its age; age is a floor below which nothing is touched, never a verdict.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from coordinator_core import liveness

MARK_FILE = ".owner.json"

OWNER_SESSION = "session"
OWNER_RUN = "run"
OWNER_PID = "pid"
OWNER_KINDS = frozenset({OWNER_SESSION, OWNER_RUN, OWNER_PID})

#: Slack between a mark's `created` stamp and a live pid's start time before the pid counts as reused.
_PID_REUSE_SLACK_SECS = 5.0

KEEP_PUBLISH_ENGINE = "publish-engine"
KEEP_CLEAN_MIRROR = "clean-mirror"
KEEP_PUBLISH_CLONE = "publish-clone"

#: The path classes no sweep may delete: the publish engine dir, every live clean-mirror holder and the
#: publish clone of each registered mirror. `keep_paths` resolves each name to concrete paths.
KEEP_SET = (KEEP_PUBLISH_ENGINE, KEEP_CLEAN_MIRROR, KEEP_PUBLISH_CLONE)

_PUBLISH_ENGINE_DIRNAME = "claude-klabauter-publish-engine"
_PUBLISH_MIRRORS_PREFIX = "publish.mirrors."
_PUBLISH_MIRROR_PATH_SUFFIX = ".path"
_REPOS_PREFIX = "repos."


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_stamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def write_mark(
    directory: os.PathLike[str] | str,
    owner_kind: str,
    owner_id: object,
    repo: Optional[str] = None,
    ttl_hours: Optional[float] = None,
) -> dict:
    """Stamp `<directory>/.owner.json` (created if absent) and return the mark written.

    `owner_kind` is `session`, `run` or `pid`; `owner_id` is stringified. `repo` is the owning repo
    root (a run mark needs it to find its ledger). `ttl_hours` is the age floor for a pruner, or None.
    Raises ValueError for an unknown kind or an empty owner id.
    """
    if owner_kind not in OWNER_KINDS:
        raise ValueError(f"owner_kind {owner_kind!r} is not one of {sorted(OWNER_KINDS)}")
    oid = str(owner_id).strip()
    if not oid:
        raise ValueError("owner_id is empty")
    mark = {
        "owner_kind": owner_kind,
        "owner_id": oid,
        "repo": str(repo) if repo else None,
        "created": _utc_now().isoformat(),
        "ttl_hours": ttl_hours,
    }
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    tmp = target / f"{MARK_FILE}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(mark, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target / MARK_FILE)
    return mark


def read_mark(directory: os.PathLike[str] | str) -> Optional[dict]:
    """The mark in `directory`, or None when absent, unreadable, or not a well-formed mark."""
    try:
        data = json.loads((Path(directory) / MARK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("owner_kind") not in OWNER_KINDS:
        return None
    if not isinstance(data.get("owner_id"), str) or not data["owner_id"]:
        return None
    return data


def past_ttl(mark: dict, now: Optional[datetime] = None) -> Optional[bool]:
    """True when the mark's age floor has passed; None when the mark carries no usable ttl or stamp."""
    ttl = mark.get("ttl_hours")
    created = _parse_stamp(mark.get("created"))
    if created is None or isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl < 0:
        return None
    return ((now or _utc_now()) - created).total_seconds() >= ttl * 3600


def _same_dir(a: str, b: str) -> bool:
    def norm(p: str) -> str:
        try:
            return os.path.normcase(os.path.realpath(p))
        except (OSError, ValueError):
            return os.path.normcase(os.path.normpath(p))

    return norm(a) == norm(b)


def _session_alive(mark: dict) -> Optional[bool]:
    repo = mark.get("repo")
    if repo:
        from coordinator_core.session import core as session_core

        # TRAP: the live set is keyed on the process cwd's sessions dir; another repo's session ids
        # would read dead there.
        own = session_core.sessions_dir(None)
        theirs = session_core.sessions_dir(str(repo))
        if not own or not theirs or not _same_dir(own, theirs):
            return None
    live = liveness.resolve_live_session_ids()
    # The resolver returns an empty set on any error, so an empty set is no evidence of death.
    if not live:
        return None
    return mark["owner_id"] in live


def _run_alive(mark: dict) -> Optional[bool]:
    from coordinator_core.ops.dispatch_emit import abandoned_runs, run_scratch_reap

    repo = mark.get("repo")
    if not repo:
        return None
    root = Path(repo)
    run_id = mark["owner_id"]
    if abandoned_runs._terminal_state(root, run_id) is None:
        return True
    hold, partial = run_scratch_reap._own_run_gate(root, run_id)
    return bool(hold) or partial


def _pid_alive(mark: dict) -> Optional[bool]:
    from coordinator_core.ask_spool import record

    try:
        pid = int(mark["owner_id"])
    except ValueError:
        return None
    if pid <= 0:
        return None
    state = record.pid_state(pid)
    if state is not True:
        return state
    created = _parse_stamp(mark.get("created"))
    started = record.pid_create_time(pid)
    if created is not None and started is not None and started > created.timestamp() + _PID_REUSE_SLACK_SECS:
        return False
    return True


def owner_alive(mark: Optional[dict]) -> Optional[bool]:
    """True when the mark's owner is alive, False when provably gone, None when unknown.

    Sessions key on `coordinator_core.liveness.resolve_live_session_ids`, runs on the run ledger's
    terminal state (a resumable halt or live landing retest still holds), pids on a spawn-free probe
    with a start-time reuse check. Never raises: a fault returns None, which every caller keeps.
    """
    if not isinstance(mark, dict):
        return None
    kind = mark.get("owner_kind")
    oid = mark.get("owner_id")
    if kind not in OWNER_KINDS or not isinstance(oid, str) or not oid:
        return None
    try:
        if kind == OWNER_SESSION:
            return _session_alive(mark)
        if kind == OWNER_RUN:
            return _run_alive(mark)
        return _pid_alive(mark)
    except Exception:  # noqa: BLE001 -- fail closed to keep
        return None


def may_reap(directory: os.PathLike[str] | str, now: Optional[datetime] = None) -> bool:
    """True only when `directory` carries a mark whose owner is provably gone, its ttl has passed
    (an absent ttl counts as passed), and it is not a keep-set member. Everything else keeps."""
    mark = read_mark(directory)
    if mark is None or is_kept(directory):
        return False
    if owner_alive(mark) is not False:
        return False
    if mark.get("ttl_hours") is None:
        return True
    return past_ttl(mark, now) is True


def _registry() -> dict:
    from coordinator_core import machine_resolver

    return machine_resolver.merged_flat_registry()


def _registered_repos(flat: dict) -> list[str]:
    return [
        v for k, v in flat.items()
        if k.startswith(_REPOS_PREFIX) and k.count(".") == 1 and isinstance(v, str) and v
    ]


def _live_clean_mirrors(flat: dict) -> list[Path]:
    """Mirror holders of each registered repo: `<key>` and its `<key>-<slot>` siblings."""
    from coordinator_core.ops.dispatch_emit.clean_overlay_verify import mirror_root

    out: list[Path] = []
    for repo in _registered_repos(flat):
        holder = mirror_root(Path(repo))
        out.append(holder)
        try:
            with os.scandir(holder.parent) as it:
                out.extend(Path(e.path) for e in it if e.name.startswith(f"{holder.name}-"))
        except OSError:
            continue
    return out


def _publish_clones(flat: dict) -> list[Path]:
    return [
        Path(v) for k, v in flat.items()
        if k.startswith(_PUBLISH_MIRRORS_PREFIX) and k.endswith(_PUBLISH_MIRROR_PATH_SUFFIX)
        and isinstance(v, str) and v
    ]


def keep_paths() -> dict[str, list[Path]]:
    """Concrete paths per `KEEP_SET` name. Raises when the registry or temp root cannot be read."""
    from coordinator_core import temp_layout

    flat = _registry()
    return {
        KEEP_PUBLISH_ENGINE: [temp_layout.fleet_temp_root() / _PUBLISH_ENGINE_DIRNAME],
        KEEP_CLEAN_MIRROR: _live_clean_mirrors(flat),
        KEEP_PUBLISH_CLONE: _publish_clones(flat),
    }


def _norm(path: os.PathLike[str] | str) -> Path:
    return Path(os.path.normcase(os.path.realpath(path)))


def is_kept(
    path: os.PathLike[str] | str,
    keep: Optional[dict[str, Iterable[os.PathLike[str] | str]]] = None,
) -> bool:
    """True when `path` is a keep-set member, lies inside one, or contains one (deleting an ancestor
    deletes the member). A fault resolving the keep-set answers True. `keep` overrides `keep_paths()`."""
    try:
        members = keep if keep is not None else keep_paths()
        target = _norm(path)
        for paths in members.values():
            for member in paths:
                m = _norm(member)
                if target == m or m in target.parents or target in m.parents:
                    return True
        return False
    except Exception:  # noqa: BLE001 -- fail closed to keep
        return True
