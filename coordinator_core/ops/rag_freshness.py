"""coordinator_core.ops.rag_freshness -- example-retrieval-repo index freshness precondition.

One adapter the session-start, sweep and dispatch sites share: it reads
Example-retrieval-repo's stale-or-absent verdict, and when the index is stale or absent it
queues example-retrieval-repo's own remedy as a detached background spawn. Advisory and
fail-open: no path here raises into a caller or denies work. No fetch or index
code lives here; the remedy command is whatever example-retrieval-repo's verdict names.

Invariants:
  * The verdict is read from a cache under the settings home and never computed
    inline: ``project_rag_cli.py freshness-verdict`` costs seconds, so a missing
    or expired cache reads ``unknown`` and queues one detached worker
    (``python -m coordinator_core.ops.rag_freshness --fetch <repo>``) that runs
    the CLI and writes the cache. example_retrieval_repo_core is never imported.
  * Only ``schema_version`` 1, a ``current`` verdict with null reason and action
    ``none``, or a ``stale``/``absent`` verdict with a recognised reason and
    action reads as a known state; anything else reads ``unknown`` (never fresh).
  * Only action ``refresh_from_release`` ever spawns a remedy.
  * Dedupe is a timestamped marker under the settings home (never example-retrieval-repo's
    store); an expired marker never suppresses later queues.
  * Every spawn is closed argv, ``shell=False``, detached, and admitted through
    ``admit_before_spawn``. An unresolvable settings home is a typed
    ``FreshnessRefusal`` naming the key, caught at the ``freshness_precondition``
    boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

STATES = ("fresh", "stale", "absent", "unknown")
SCHEMA_VERSION = 1
KNOWN_REASONS = (
    "no_structural_db",
    "no_structural_stamp",
    "behind_head",
    "sha_not_in_repo",
    "git_budget_exceeded",
    "git_unavailable",
    "unknown_repo",
)
REMEDY_ACTIONS = ("none", "refresh_from_release", "register_publish_repo")
MARKER_TTL_SECONDS = 1800
VERDICT_MARKER_TTL_SECONDS = 120
VERDICT_CACHE_TTL_SECONDS = 600
VERDICT_TIMEOUT_SECONDS = 30
SETTINGS_HOME_KEY = "COORDINATOR_SETTINGS_HOME"
MARKER_DIRNAME = "rag-freshness-queue"
REMEDY_ADMISSION_KEY = "rag-freshness-remedy"
REMEDY_PEAK_GIB = 1.0
VERDICT_ADMISSION_KEY = "rag-freshness-verdict"
VERDICT_PEAK_GIB = 0.25

SpawnFn = Callable[[Sequence[str], str], None]


@dataclass(frozen=True)
class FreshnessVerdict:
    """example-retrieval-repo's freshness answer: ``state`` in STATES, ``remedy`` an argv
    (empty when none), ``source`` naming where the verdict came from, ``action``
    the contract's remedy action (``register_publish_repo`` is surfaced, never run)."""

    state: str
    remedy: Sequence[str] = ()
    source: str = ""
    action: str = ""


class FreshnessRefusal(Exception):
    """Typed refusal: ``key`` names the settings-home key that is missing."""

    def __init__(self, key: str, detail: str = ""):
        self.key = key
        super().__init__(f"missing settings-home key {key}" + (f": {detail}" if detail else ""))


def _unknown(source: str) -> FreshnessVerdict:
    return FreshnessVerdict(state="unknown", remedy=(), source=source)


def verdict_from_reply(reply: object, slugs: Sequence[str]) -> FreshnessVerdict:
    """Map a example-retrieval-repo ``freshness-verdict`` reply onto a FreshnessVerdict for the
    one row whose slug is in ``slugs``. Anything unrecognised reads ``unknown``."""
    if not isinstance(reply, dict) or type(reply.get("schema_version")) is not int or reply["schema_version"] != SCHEMA_VERSION:
        return _unknown("schema_version")
    rows = reply.get("repos")
    if not isinstance(rows, list):
        return _unknown("malformed")
    matched = [r for r in rows if isinstance(r, dict) and r.get("slug") in slugs]
    if len(matched) != 1:
        return _unknown("row_selection")
    row = matched[0]
    verdict, reason = row.get("verdict"), row.get("reason")
    remedy = row.get("remedy")
    action = remedy.get("action") if isinstance(remedy, dict) else None
    if action not in REMEDY_ACTIONS:
        return _unknown("unrecognised_action")
    if verdict == "current":
        if reason is None and action == "none":
            return FreshnessVerdict("fresh", (), "project-rag", "none")
        return _unknown("current_not_clean")
    if verdict not in ("stale", "absent"):
        return _unknown("verdict_" + str(verdict))
    if reason not in KNOWN_REASONS:
        return _unknown("unrecognised_reason")
    if action == "refresh_from_release":
        argv = remedy.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            return _unknown("malformed_argv")
        return FreshnessVerdict(verdict, tuple(argv), "project-rag", action)
    if action == "register_publish_repo":
        return FreshnessVerdict(verdict, (), "project-rag", action)
    return _unknown("stale_without_action")


def detach_kwargs(os_name: Optional[str] = None) -> dict:
    """Popen keyword arguments that detach the child, per platform."""
    if (os_name or os.name) == "nt":
        flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def _spawn_admitted(key: str, peak_gib: float, argv: Sequence[str], cwd: str, env: Optional[dict] = None) -> None:
    """Admit, spawn ``argv`` detached (closed argv, shell=False, no stdio) and hand the
    memory reservation to the child. Raises on a held admission or a spawn failure."""
    from coordinator_core.bash_guards import _heavy_command_class as capacity

    host = capacity._load_box_memory_host()
    admission = capacity.admit_before_spawn(key, peak_gib, host=host, register_pid=os.getpid())
    if not admission.admitted:
        raise RuntimeError(f"spawn held: {admission.reason}")
    try:
        proc = subprocess.Popen(  # noqa: S603 -- closed argv, shell=False
            list(argv),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **detach_kwargs(),
        )
        capacity.register_spawn(key, admission.peak_gib, proc.pid, host)
    finally:
        capacity.release_spawn(key, os.getpid(), host)


def default_spawn_fn(argv: Sequence[str], cwd: str) -> None:
    """Spawn example-retrieval-repo's remedy ``argv`` detached and admitted; do not wait."""
    _spawn_admitted(REMEDY_ADMISSION_KEY, REMEDY_PEAK_GIB, argv, cwd)


def default_verdict_spawn_fn(argv: Sequence[str], cwd: str) -> None:
    """Spawn the verdict-cache worker detached and admitted; do not wait."""
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
    _spawn_admitted(VERDICT_ADMISSION_KEY, VERDICT_PEAK_GIB, argv, cwd, env)


def _marker_path(repo_root: str, marker_root: Optional[Path], suffix: str = ".json") -> Path:
    if marker_root is None:
        try:
            from coordinator_core._settings_home import settings_home

            marker_root = Path(settings_home()) / MARKER_DIRNAME
        except Exception as exc:  # noqa: BLE001 -- any resolver failure is the same refusal
            raise FreshnessRefusal(SETTINGS_HOME_KEY, str(exc)) from exc
    digest = hashlib.sha256(os.path.abspath(repo_root).encode("utf-8")).hexdigest()[:16]
    return Path(marker_root) / f"{digest}{suffix}"


def _marker_live(path: Path, now: float, ttl: float = MARKER_TTL_SECONDS) -> bool:
    try:
        stamp = float(json.loads(path.read_text(encoding="utf-8"))["queued_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return 0 <= now - stamp < ttl


def _spawn_once(
    path: Path, argv: Sequence[str], repo_root: str, spawn_fn: SpawnFn, stamp: float, ttl: float
) -> bool:
    """Spawn unless ``path`` holds a live marker; the marker is removed on a spawn failure."""
    if _marker_live(path, stamp, ttl):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"queued_at": stamp, "remedy": list(argv)}), encoding="utf-8")
    try:
        spawn_fn(list(argv), repo_root)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return True


def queue_refresh(
    repo_root: str,
    remedy: Sequence[str],
    *,
    spawn_fn: Optional[SpawnFn] = None,
    marker_root: Optional[Path] = None,
    now: Optional[float] = None,
) -> bool:
    """Spawn ``remedy`` detached unless a live marker exists. Returns True when
    a spawn happened. Raises FreshnessRefusal on an unresolvable settings home
    and lets a spawn failure propagate (the marker is removed first)."""
    if not remedy:
        return False
    stamp = time.time() if now is None else now
    path = _marker_path(repo_root, marker_root)
    return _spawn_once(path, remedy, repo_root, spawn_fn or default_spawn_fn, stamp, MARKER_TTL_SECONDS)


def _slugs_for(repo_root: str) -> list[str]:
    """Every machine-local ``repos.<slug>`` key naming ``repo_root`` (aliases included:
    example-retrieval-repo may answer under any of them)."""
    from coordinator_core import machine_resolver
    from coordinator_core.install.rag_fleet_registration import _canon

    target = _canon(repo_root)
    return [
        key[6:]
        for key, value in machine_resolver.merged_flat_registry().items()
        if key.startswith("repos.") and "." not in key[6:] and isinstance(value, str) and value.strip()
        and _canon(value.strip()) == target
    ]


def _read_cache(path: Path, now: float) -> Optional[object]:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        stamp = float(blob["fetched_at"])
        reply = blob["reply"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return reply if 0 <= now - stamp < VERDICT_CACHE_TTL_SECONDS else None


def _worker_argv(repo_root: str) -> list[str]:
    return [sys.executable, "-m", "coordinator_core.ops.rag_freshness", "--fetch", repo_root]


def default_verdict_fn(
    repo_root: str,
    *,
    marker_root: Optional[Path] = None,
    now: Optional[float] = None,
    verdict_spawn_fn: Optional[SpawnFn] = None,
) -> FreshnessVerdict:
    """Read the cached example-retrieval-repo verdict for ``repo_root``. A missing or expired cache
    reads ``unknown`` and queues at most one detached worker that rewrites it; the
    verdict CLI is never run inline."""
    stamp = time.time() if now is None else now
    cache = _marker_path(repo_root, marker_root, ".verdict.json")
    reply = _read_cache(cache, stamp)
    if reply is not None:
        slugs = _slugs_for(repo_root)
        return verdict_from_reply(reply, slugs) if slugs else _unknown("unregistered_repo")
    from coordinator_core.install.rag_fleet_registration import detect_example_retrieval_repo

    if detect_example_retrieval_repo() is None:
        return _unknown("cli_unresolved")
    queued = _marker_path(repo_root, marker_root, ".verdict-queued.json")
    try:
        _spawn_once(
            queued, _worker_argv(repo_root), repo_root, verdict_spawn_fn or default_verdict_spawn_fn,
            stamp, VERDICT_MARKER_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001 -- fail open: no cache yet
        return _unknown("verdict_spawn_failed")
    return _unknown("cache_miss")


def fetch_and_cache(
    repo_root: str,
    *,
    marker_root: Optional[Path] = None,
    run_fn: Optional[Callable[..., "subprocess.CompletedProcess"]] = None,
    now: Optional[float] = None,
) -> str:
    """Worker body: run ``project_rag_cli.py freshness-verdict --cwd <repo>`` (closed argv,
    shell=False) and atomically write the reply to the cache. Returns ``written`` or a
    ``failed: ...`` reason; a nonzero exit or non-JSON stdout writes nothing."""
    from coordinator_core.install.rag_fleet_registration import detect_example_retrieval_repo

    rag = detect_example_retrieval_repo()
    if rag is None:
        return "failed: cli_unresolved"
    cache = _marker_path(repo_root, marker_root, ".verdict.json")
    argv = [rag.interpreter, str(rag.root / "project_rag_cli.py"), "freshness-verdict", "--cwd", repo_root]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "cwd": str(rag.root),
                    "timeout": VERDICT_TIMEOUT_SECONDS, "shell": False}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = (run_fn or subprocess.run)(argv, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"failed: {type(exc).__name__}"
    if proc.returncode != 0:
        return f"failed: exit {proc.returncode}"
    try:
        reply = json.loads(proc.stdout)
    except ValueError:
        return "failed: non-json stdout"
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(cache.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"fetched_at": time.time() if now is None else now, "reply": reply}), encoding="utf-8")
    os.replace(tmp, cache)
    return "written"


def freshness_precondition(
    repo_root: str,
    *,
    verdict_fn: Optional[Callable[[str], FreshnessVerdict]] = None,
    spawn_fn: Optional[SpawnFn] = None,
    marker_root: Optional[Path] = None,
    now: Optional[float] = None,
    verdict_spawn_fn: Optional[SpawnFn] = None,
) -> dict:
    """Return ``{state, refresh_queued, message}``. Never raises: a verdict,
    refusal or spawn failure yields ``refresh_queued`` False and a message."""
    try:
        if verdict_fn is None:
            verdict = default_verdict_fn(repo_root, marker_root=marker_root, now=now, verdict_spawn_fn=verdict_spawn_fn)
        else:
            verdict = verdict_fn(repo_root)
    except Exception as exc:  # noqa: BLE001 -- fail open
        return {"state": "unknown", "refresh_queued": False, "message": f"example-retrieval-repo verdict unavailable: {exc}"}
    state = verdict.state if verdict.state in STATES else "unknown"
    if state not in ("stale", "absent"):
        return {"state": state, "refresh_queued": False, "message": f"example-retrieval-repo index {state}"}
    try:
        queued = queue_refresh(repo_root, verdict.remedy, spawn_fn=spawn_fn, marker_root=marker_root, now=now)
    except FreshnessRefusal as exc:
        return {"state": state, "refresh_queued": False, "message": f"example-retrieval-repo index {state}; refresh not queued: {exc}"}
    except Exception as exc:  # noqa: BLE001 -- spawn failure fails open
        return {"state": state, "refresh_queued": False, "message": f"example-retrieval-repo index {state}; refresh spawn failed: {exc}"}
    if queued:
        message = f"example-retrieval-repo index {state}; refresh queued"
    elif verdict.remedy:
        message = f"example-retrieval-repo index {state}; refresh already queued"
    elif verdict.action == "register_publish_repo":
        message = f"example-retrieval-repo index {state}; no publish target registered (set-publish-repo)"
    else:
        message = f"example-retrieval-repo index {state}; verdict names no remedy"
    return {"state": state, "refresh_queued": queued, "message": message}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Worker entry: ``--fetch <repo_root>`` refreshes the verdict cache."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--fetch":
        print("usage: python -m coordinator_core.ops.rag_freshness --fetch <repo_root>", file=sys.stderr)
        return 2
    try:
        result = fetch_and_cache(args[1])
    except FreshnessRefusal as exc:
        print(str(exc), file=sys.stderr)
        return 3
    return 0 if result == "written" else 1


if __name__ == "__main__":
    raise SystemExit(main())
