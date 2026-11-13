"""coordinator_core.ops.rag_freshness -- example-retrieval-repo index freshness precondition.

One adapter the session-start, sweep and dispatch sites share: it reads
Example-retrieval-repo's stale-or-absent verdict, and when the index is stale or absent it
queues example-retrieval-repo's own remedy as a detached background spawn. Advisory and
fail-open: no path here raises into a caller or denies work. No fetch or index
code lives here; the remedy command is whatever example-retrieval-repo's verdict names.

Invariants:
  * The default verdict seam reports ``unknown`` and queues nothing.
  * Dedupe is a timestamped marker under the settings home (never example-retrieval-repo's
    store); a marker older than ``MARKER_TTL_SECONDS`` is expired, so a refresh
    that died never suppresses later queues.
  * An unresolvable settings home is a typed ``FreshnessRefusal`` naming the
    key, caught at the ``freshness_precondition`` boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

STATES = ("fresh", "stale", "absent", "unknown")
MARKER_TTL_SECONDS = 1800
SETTINGS_HOME_KEY = "COORDINATOR_SETTINGS_HOME"
MARKER_DIRNAME = "rag-freshness-queue"


@dataclass(frozen=True)
class FreshnessVerdict:
    """example-retrieval-repo's freshness answer: ``state`` in STATES, ``remedy`` an argv
    (empty when none), ``source`` naming where the verdict came from."""

    state: str
    remedy: Sequence[str] = ()
    source: str = ""


class FreshnessRefusal(Exception):
    """Typed refusal: ``key`` names the settings-home key that is missing."""

    def __init__(self, key: str, detail: str = ""):
        self.key = key
        super().__init__(f"missing settings-home key {key}" + (f": {detail}" if detail else ""))


def default_verdict_fn(repo_root: str) -> FreshnessVerdict:
    """Seam for example-retrieval-repo's verdict call; reports ``unknown`` until bound."""
    return FreshnessVerdict(state="unknown", remedy=(), source="unbound")


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


def default_spawn_fn(argv: Sequence[str], cwd: str) -> None:
    """Spawn ``argv`` detached with no inherited stdio and do not wait."""
    subprocess.Popen(  # noqa: S603 -- argv comes from example-retrieval-repo's verdict
        list(argv),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **detach_kwargs(),
    )


def _marker_path(repo_root: str, marker_root: Optional[Path]) -> Path:
    if marker_root is None:
        try:
            from coordinator_core._settings_home import settings_home

            marker_root = Path(settings_home()) / MARKER_DIRNAME
        except Exception as exc:  # noqa: BLE001 -- any resolver failure is the same refusal
            raise FreshnessRefusal(SETTINGS_HOME_KEY, str(exc)) from exc
    digest = hashlib.sha256(os.path.abspath(repo_root).encode("utf-8")).hexdigest()[:16]
    return Path(marker_root) / f"{digest}.json"


def _marker_live(path: Path, now: float) -> bool:
    try:
        stamp = float(json.loads(path.read_text(encoding="utf-8"))["queued_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return 0 <= now - stamp < MARKER_TTL_SECONDS


def queue_refresh(
    repo_root: str,
    remedy: Sequence[str],
    *,
    spawn_fn: Optional[Callable[[Sequence[str], str], None]] = None,
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
    if _marker_live(path, stamp):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"queued_at": stamp, "remedy": list(remedy)}), encoding="utf-8")
    try:
        (spawn_fn or default_spawn_fn)(list(remedy), repo_root)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return True


def freshness_precondition(
    repo_root: str,
    *,
    verdict_fn: Optional[Callable[[str], FreshnessVerdict]] = None,
    spawn_fn: Optional[Callable[[Sequence[str], str], None]] = None,
    marker_root: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    """Return ``{state, refresh_queued, message}``. Never raises: a verdict,
    refusal or spawn failure yields ``refresh_queued`` False and a message."""
    try:
        verdict = (verdict_fn or default_verdict_fn)(repo_root)
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
    else:
        message = f"example-retrieval-repo index {state}; verdict names no remedy"
    return {"state": state, "refresh_queued": queued, "message": message}
