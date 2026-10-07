"""
coordinator_core.session.autonomous_sentinel — single-source resolver for the
autonomous-run sentinel path, keyed by session id.

Prior to this module the sentinel's location was reimplemented three ways in
three files and two of them disagreed with the third on Windows:

  - WRITER (coordinator/bin/misc-session-and-guards.py) hardcoded
    ``Path("/tmp") / f"autonomous-run-{session_id}"``.
  - Two of four READERS (coordinator_core/hooks/nudge_em_code_dispatch.py,
    coordinator_core/hooks/postuse_advisory_dispatch.py) correctly used
    ``tempfile.gettempdir()``.
  - One READER (coordinator/bin/wsc-coverage-gate-runner.py) also hardcoded
    ``Path("/tmp", ...)``, with a docstring that falsely claimed it "mirrors
    the sentinel's own convention."

On Windows ``tempfile.gettempdir()`` resolves TMPDIR/TEMP/TMP to something
like ``%LOCALAPPDATA%\\Temp``, while the hardcoded writer wrote to
``<CWD-drive>:\\tmp`` — the sentinel was written and read at two different
paths, so autonomous mode silently failed to suppress /handoff nudges on
Windows. Every writer and reader MUST call ``sentinel_path()`` below instead
of constructing the path locally.

Spec backlink: F2+F3 in the 2026-07-28 Windows-tempdir-convergence dispatch
(coordinator/bin/misc-session-and-guards.py, coordinator/bin/
wsc-coverage-gate-runner.py, coordinator_core/hooks/nudge_em_code_dispatch.py,
coordinator_core/hooks/postuse_advisory_dispatch.py).

Dual-read window: the sentinel is written under ``<gettempdir()>/coordinator/_fleet/``
and every reader calls ``sentinel_read_path()``, which also accepts the legacy
bare-Temp location for one release. The fleet-scratch-hygiene queue row for
dropping the legacy fallback ends the window.

Negative-spec:
    - Do NOT hardcode ``/tmp`` or reach for ``tempfile.gettempdir()``
      directly at a new call site for this sentinel — import and call
      ``sentinel_path()`` so there is exactly one place this convention can
      drift again.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from coordinator_core.temp_layout import fleet_temp_root

_SENTINEL_PREFIX = "autonomous-run-"


def sentinel_path(session_id: str) -> Path:
    """Write path: ``fleet_temp_root()/autonomous-run-<sid>``."""
    return fleet_temp_root() / f"{_SENTINEL_PREFIX}{session_id}"


def legacy_sentinel_path(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{_SENTINEL_PREFIX}{session_id}"


def sentinel_read_path(session_id: str) -> Path | None:
    """First existing of the ``_fleet`` path and the legacy bare-Temp path, else None."""
    for path in (sentinel_path(session_id), legacy_sentinel_path(session_id)):
        if path.exists():
            return path
    return None
