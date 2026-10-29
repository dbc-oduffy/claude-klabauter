"""coordinator_core.ops.claude_home_census -- measure the Claude-home footprint and
pin the retention falsifier.

Two jobs, one module:

* Census: bytes and file counts per folder under the Claude home (`projects/` with
  `<session>/subagents/` broken out), the regenerable caches, the warm-engine
  `pool-worker-stderr` dirs, `<tempdir>/claude`, and each top-level subdir of the
  settings home with the writer that grows it. Every walk is budget-bounded and
  resumable (`Walk.pending`), so a 4M-file tree is never one blocking pass.
* `--fixture-ratchet`: builds a fixture home (251 MB ended transcript, 49 h ended
  subagent transcript, 300 MB live transcript, an over-cap cache, a dead and a live
  Temp session dir) from sparse files, calls `retention_run`'s entry point when it
  exists, and prints bytes before/after per axis. With no entry point the two are
  equal.

Defines the machine-local key names the retention rows consume:
`transcript_retention.max_mb` (250), `transcript_retention.max_age_hours` (48),
`cache_caps.<class>.<field>`.

Entry-point contract for `retention_run`: a callable named in `ENTRY_POINT_NAMES`
taking any of `claude_home`, `temp_root`, `live_session_ids`, `apply` by keyword; only
the parameters it declares are passed. Never writes into `example-retrieval-repo/` (the rag
store is example-retrieval-repo's to bound).
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

if __name__ == "__main__":  # direct `python coordinator_core/ops/claude_home_census.py` runs
    _repo = str(Path(__file__).resolve().parents[2])
    if _repo not in sys.path:
        sys.path.insert(0, _repo)

GENERATES = []

KEY_MAX_MB = "transcript_retention.max_mb"
KEY_MAX_AGE_HOURS = "transcript_retention.max_age_hours"
KEY_CACHE_CAPS_PREFIX = "cache_caps"
DEFAULT_MAX_MB = 250
DEFAULT_MAX_AGE_HOURS = 48
MB = 1024 * 1024
HOUR = 3600
DEFAULT_BUDGET_SECONDS = 0.3

ENTRY_POINT_MODULE = "coordinator_core.ops.retention_run"
ENTRY_POINT_NAMES = ("run_retention", "run", "main")

# Claude-home subdirs C0 measures. `regenerable` is C0's finding, consumed by the
# cache_caps table; file-history is NOT claimed regenerable (it backs rewind).
CLAUDE_HOME_FOLDERS = (
    "projects", "plugins/cache", "file-history", "shell-snapshots", "debug", "todos",
    "paste-cache", "session-env", "cache", "logs",
)
REGENERABLE_CLAUDE_HOME = ("shell-snapshots", "debug", "todos", "paste-cache", "cache")

# Settings-home top-level subdirs -> (writer, regenerable). Unlisted subdirs report
# writer "unmapped" and are never capped. `example-retrieval-repo` is measured and never bounded
# here: it is example-retrieval-repo's own per-repo index home.
SETTINGS_HOME_WRITERS: Dict[str, "tuple[str, bool]"] = {
    "project-rag": ("example-retrieval-repo per-repo index/cache home (example-retrieval-repo repo)", False),
    "state": ("claude-klabauter engine state (handoffs, queues, audits)", False),
    "machine-local": ("machine-local registry and per-box config", False),
    "group-em": ("group-em session records (coordinator_core.group_em)", False),
    "cache": ("coordinator caches (derived, rebuilt on demand)", True),
    "scratch": ("session scratch dirs", True),
    "telemetry": ("telemetry sinks (coordinator_core hooks)", False),
    "install": ("scripts/setup.py install receipts and rendezvous", False),
    "secrets": ("door-key and credentials", False),
    "bin": ("installed shims (scripts/setup.py)", False),
    "claude-klabauter": ("klabauter publish twin clone", False),
    "coordinator-claude": ("coordinator-claude settings clone", False),
    "heavy-admission": ("heavy-process admission tickets", True),
    "tier-t-slots": ("test-slot leases", True),
    "taskstop-reaper": ("taskstop reaper markers", True),
    "retired-shims": ("shims retired by setup.py", False),
}


def _registry_get(key: str) -> Optional[str]:
    try:
        from coordinator_core.machine_resolver import registry_get
    except Exception:  # noqa: BLE001 -- census must run where the registry cannot
        return None
    try:
        return registry_get(key)
    except Exception:  # noqa: BLE001
        return None


def _positive_number(raw: Optional[str], default: float) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def transcript_max_bytes(get: Callable[[str], Optional[str]] = _registry_get) -> int:
    """Per-file transcript size ceiling in bytes from machine-local `transcript_retention.max_mb`."""
    return int(_positive_number(get(KEY_MAX_MB), DEFAULT_MAX_MB) * MB)


def transcript_max_age_seconds(get: Callable[[str], Optional[str]] = _registry_get) -> float:
    """Idle-age ceiling in seconds from machine-local `transcript_retention.max_age_hours`."""
    return _positive_number(get(KEY_MAX_AGE_HOURS), DEFAULT_MAX_AGE_HOURS) * HOUR


def cache_cap_key(cache_class: str, field_name: str) -> str:
    """Machine-local key for one cache class field, e.g. `cache_caps.debug.max_mb`."""
    return f"{KEY_CACHE_CAPS_PREFIX}.{cache_class}.{field_name}"


def claude_home() -> Path:
    """The Claude home: `CLAUDE_HOME` when set, else `~/.claude` (never hardcoded)."""
    override = os.environ.get("CLAUDE_HOME")
    return Path(override) if override else Path.home() / ".claude"


def claude_temp_root() -> Path:
    """The `claude` dir under tempfile.gettempdir(): Windows Temp, TMPDIR on mac."""
    return Path(tempfile.gettempdir()) / "claude"


def warm_stderr_dirs() -> List[Path]:
    """`<warm dir>/*/pool-worker-stderr` for every clone hash, platform-resolved."""
    try:
        from coordinator_core.warm.door_credential import warm_dir
        base = warm_dir()
    except Exception:  # noqa: BLE001
        return []
    try:
        return [p / "pool-worker-stderr" for p in sorted(base.iterdir())
                if (p / "pool-worker-stderr").is_dir()]
    except OSError:
        return []


@dataclass
class Walk:
    """Resumable size walk. `pending` holds directories still to scan; feed a
    finished-or-not Walk back into `measure` to continue."""

    bytes: int = 0
    files: int = 0
    pending: List[str] = field(default_factory=list)
    started: bool = False

    @property
    def complete(self) -> bool:
        return self.started and not self.pending


def measure(path: "os.PathLike[str] | str", budget_seconds: Optional[float] = DEFAULT_BUDGET_SECONDS,
            resume: Optional[Walk] = None) -> Walk:
    """Sum file bytes and count under `path`, stopping once `budget_seconds` of wall
    time is spent (None = unbounded). Symlinks and junctions are not followed. A
    missing path is an empty, complete walk."""
    walk = resume if resume is not None else Walk()
    if not walk.started:
        walk.started = True
        if os.path.isdir(path):
            walk.pending = [os.fspath(path)]
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
    while walk.pending:
        if deadline is not None and time.monotonic() >= deadline:
            return walk
        current = walk.pending.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            walk.pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            walk.bytes += entry.stat(follow_symlinks=False).st_size
                            walk.files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return walk


def _row(path: Path, budget: Optional[float]) -> dict:
    walk = measure(path, budget)
    return {"path": str(path), "bytes": walk.bytes, "files": walk.files, "complete": walk.complete}


def projects_breakdown(projects: Path, budget: Optional[float] = DEFAULT_BUDGET_SECONDS) -> dict:
    """`projects/` split into transcripts (`<uuid>.jsonl`), `subagents/` and other bytes."""
    out = {"path": str(projects), "transcripts_bytes": 0, "transcripts_files": 0,
           "subagents_bytes": 0, "subagents_files": 0, "other_bytes": 0, "complete": True}
    deadline = None if budget is None else time.monotonic() + budget
    stack = [os.fspath(projects)] if projects.is_dir() else []
    while stack:
        if deadline is not None and time.monotonic() >= deadline:
            out["complete"] = False
            break
        current = stack.pop()
        in_sub = os.path.basename(current) == "subagents" or "subagents" in Path(current).parts[
            len(projects.parts):]
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            size = entry.stat(follow_symlinks=False).st_size
                            if in_sub:
                                out["subagents_bytes"] += size
                                out["subagents_files"] += 1
                            elif entry.name.endswith(".jsonl"):
                                out["transcripts_bytes"] += size
                                out["transcripts_files"] += 1
                            else:
                                out["other_bytes"] += size
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def settings_home_rows(home: Path, budget: Optional[float] = DEFAULT_BUDGET_SECONDS) -> List[dict]:
    """Each top-level settings-home subdir with its writer and regenerable flag."""
    rows: List[dict] = []
    try:
        names = sorted(n for n in os.listdir(home) if (home / n).is_dir())
    except OSError:
        return rows
    for name in names:
        writer, regen = SETTINGS_HOME_WRITERS.get(name, ("unmapped", False))
        row = _row(home / name, budget)
        row.update(name=name, writer=writer, regenerable=regen)
        rows.append(row)
    return rows


def census(home: Optional[Path] = None, temp_root: Optional[Path] = None,
           settings: Optional[Path] = None, budget: Optional[float] = DEFAULT_BUDGET_SECONDS) -> dict:
    """Per-folder bytes for the Claude home, warm stderr dirs, Temp/claude and the
    settings home. `budget` applies per folder; incomplete rows say `complete: false`."""
    home = home if home is not None else claude_home()
    temp_root = temp_root if temp_root is not None else claude_temp_root()
    if settings is None:
        try:
            from coordinator_core._settings_home import settings_home
            settings = settings_home()
        except Exception:  # noqa: BLE001
            settings = Path.home() / ".coordinator-claude-settings"
    folders = []
    for rel in CLAUDE_HOME_FOLDERS:
        if rel == "projects":
            continue
        row = _row(home / rel, budget)
        row.update(name=rel, regenerable=rel in REGENERABLE_CLAUDE_HOME)
        folders.append(row)
    return {
        "claude_home": str(home),
        "projects": projects_breakdown(home / "projects", budget),
        "claude_home_folders": folders,
        "warm_pool_worker_stderr": [_row(p, budget) for p in warm_stderr_dirs()],
        "temp_claude": _row(temp_root, budget),
        "settings_home": settings_home_rows(Path(settings), budget),
    }


def _sparse(path: Path, size: int, age_hours: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)
    if age_hours:
        stamp = time.time() - age_hours * HOUR
        os.utime(path, (stamp, stamp))


def build_fixture(root: Path) -> dict:
    """Build the falsifier fixture under `root` from sparse files. Returns the
    paths and the live session id."""
    home = root / "claude-home"
    temp_root = root / "temp" / "claude"
    proj = home / "projects" / "proj-a"
    live, ended = "live0000-0000-0000-0000-000000000001", "dead0000-0000-0000-0000-000000000002"
    paths = {
        "big_ended": proj / f"{ended}.jsonl",
        "old_subagent": proj / ended / "subagents" / "agent-old.jsonl",
        "live_big": proj / f"{live}.jsonl",
        "cache": home / "debug" / "debug.log",
        "temp_dead": temp_root / "proj-a" / ended / "scratch.bin",
        "temp_live": temp_root / "proj-a" / live / "scratch.bin",
    }
    _sparse(paths["big_ended"], 251 * MB)
    _sparse(paths["old_subagent"], 1 * MB, age_hours=49)
    _sparse(paths["live_big"], 300 * MB)
    _sparse(paths["cache"], 600 * MB)
    _sparse(paths["temp_dead"], 1 * MB, age_hours=49)
    _sparse(paths["temp_live"], 1 * MB, age_hours=49)
    stamp = time.time() - 49 * HOUR
    for key in ("temp_dead", "temp_live"):  # idle is judged on the dir mtime too
        os.utime(paths[key].parent, (stamp, stamp))
    return {"home": home, "temp_root": temp_root, "live": live, "paths": paths}


def _tree_bytes(path: Path) -> int:
    return measure(path, None).bytes


def _find_entry_point() -> Optional[Callable]:
    try:
        module = importlib.import_module(ENTRY_POINT_MODULE)
    except ImportError:
        return None
    for name in ENTRY_POINT_NAMES:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


def _call_entry_point(fn: Callable, **available) -> None:
    params = inspect.signature(fn).parameters
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        fn(**available)
    else:
        fn(**{k: v for k, v in available.items() if k in params})


def fixture_ratchet(root: Path, entry_point: Optional[Callable] = None) -> dict:
    """Run the falsifier: build the fixture, call the retention entry point if one
    exists, return bytes before/after per axis and which fixture files survive."""
    fx = build_fixture(root)
    axes = {
        "transcripts": fx["home"] / "projects",
        "caches": fx["home"] / "debug",
        "temp_claude": fx["temp_root"],
    }
    before = {k: _tree_bytes(v) for k, v in axes.items()}
    fn = entry_point if entry_point is not None else _find_entry_point()
    if fn is not None:
        kwargs = dict(claude_home=fx["home"], live_session_ids=frozenset({fx["live"]}), apply=True)
        saved_tempdir = tempfile.tempdir
        if entry_point is not None:
            kwargs["temp_root"] = fx["temp_root"]
        else:
            # The real entry point prunes the platform Temp/claude root itself: aim it at the fixture.
            tempfile.tempdir = str(fx["temp_root"].parent)
        try:
            _call_entry_point(fn, **kwargs)
        finally:
            tempfile.tempdir = saved_tempdir
    after = {k: _tree_bytes(v) for k, v in axes.items()}
    survivors = {name: p.exists() for name, p in fx["paths"].items()}
    return {
        "entry_point": getattr(fn, "__module__", None) and f"{fn.__module__}.{fn.__name__}",
        "bytes_before": before,
        "bytes_after": after,
        "bytes_before_total": sum(before.values()),
        "bytes_after_total": sum(after.values()),
        "survives": survivors,
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fixture-ratchet", action="store_true",
                    help="build the retention fixture and print bytes before/after per axis")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET_SECONDS,
                    help="seconds of walk per folder; 0 = unbounded")
    ap.add_argument("--root", help="fixture root (default: a fresh temp dir)")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.fixture_ratchet:
        if args.root:
            root = Path(args.root)
            root.mkdir(parents=True, exist_ok=True)
            result = fixture_ratchet(root)
        else:
            with tempfile.TemporaryDirectory(prefix="claude-home-ratchet-") as tmp:
                result = fixture_ratchet(Path(tmp))
    else:
        result = census(budget=args.budget or None)
    json.dump(result, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
