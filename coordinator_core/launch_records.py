"""Per-session workflow launch-record layout.

Records live at ``<tmpdir>/workflow-runs/<session>/<task>.json`` so one session's
lookup lists only its own dir, never the managed temp dir.

Producer: ``postuse_advisory_dispatch.resolve_workflow_run_records``.
Readers: ``flag_em_poll_in_flight``, ``_em_post_fire``,
``context_pressure_precompact``, ``warp_live``.

The legacy flat name ``workflow-run-<session>-<task>.json`` is built only by
``flat_record_path``; ``migrate_flat_records`` moves the flat files it can name
by stat, and lists nothing. Readers see only the per-session layout, so a flat record whose id
is never named to the migrator (its tool_use fell out of the transcript tail) is invisible.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, List, Sequence

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

RECORDS_DIRNAME = "workflow-runs"


def _check(*ids: str) -> None:
    for i in ids:
        if not isinstance(i, str) or not SAFE_ID_RE.fullmatch(i):
            raise ValueError("unsafe session_id/task_id for workflow run record path")


def _records_dir(tmpdir: str, session_id: str) -> str:
    """The temp-dir record dir; not a coordinator session dir (no ``meta.json``).

    Named apart from ``session_dir`` so ``test_session_dir_has_one_constructor``
    does not read the mkdir in ``record_path`` as a hub session-dir creation.
    """
    _check(session_id)
    return os.path.join(tmpdir, RECORDS_DIRNAME, session_id)


def session_dir(tmpdir: str, session_id: str) -> str:
    """Return ``<tmpdir>/workflow-runs/<session_id>``; ValueError on an unsafe id."""
    return _records_dir(tmpdir, session_id)


def record_path(tmpdir: str, session_id: str, task_id: str) -> str:
    """Return the per-session record path, creating the session dir.

    ValueError on an unsafe id. Dir-creation failure is left to the writer's
    own fail-open handling.
    """
    _check(session_id, task_id)
    sdir = _records_dir(tmpdir, session_id)
    try:
        os.makedirs(sdir, exist_ok=True)
    except OSError:
        pass
    return os.path.join(sdir, f"{task_id}.json")


def flat_record_path(tmpdir: str, session_id: str, task_id: str) -> str:
    """Return the legacy flat record path; the only builder of that name."""
    _check(session_id, task_id)
    return os.path.join(tmpdir, f"workflow-run-{session_id}-{task_id}.json")


def session_record_paths(tmpdir: str, session_id: str) -> List[str]:
    """Return the ``.json`` record paths of one session; [] when the dir is absent.

    Scans only the session dir, never ``tmpdir``.
    """
    try:
        sdir = session_dir(tmpdir, session_id)
        with os.scandir(sdir) as it:
            return [e.path for e in it if e.name.endswith(".json")]
    except (OSError, ValueError):
        return []


def migrate_flat_records(
    tmpdirs: Sequence[str], session_id: str, ids: Iterable[str]
) -> int:
    """Move each existing flat record named by ``ids`` into the session dir.

    Paths are built from ``ids``; no directory is listed. An existing per-session
    record is never overwritten. Returns the number moved; unsafe ids and OS
    errors are skipped.
    """
    moved = 0
    seen = set()
    for task_id in ids:
        if not isinstance(task_id, str) or task_id in seen:
            continue
        seen.add(task_id)
        for tmpdir in tmpdirs:
            try:
                src = flat_record_path(tmpdir, session_id, task_id)
                if not os.path.exists(src):
                    continue
                dest = record_path(tmpdirs[0], session_id, task_id)
                if not os.path.exists(dest):
                    os.replace(src, dest)
                    moved += 1
                break
            except (OSError, ValueError):
                continue
    return moved
