"""Section porter — ReviewTrail (envelope key: ``review_trail``).

Emits one ReviewTrail record per ``state/review-trail/**/*.json`` +
``archive/review-trail/**/*.json`` file (the live+archived union produced by
``coordinator_core.ops.list_review_trail_records``). ``reviewed_at`` is derived from the filename
timestamp (``YYYY-MM-DD-HHMMSS[ns]-<session>.json``); the verdict is case-normalized;
records with a missing sha_range / reviewer / verdict, an out-of-set verdict, a JSON parse
error, a non-object body, or an unparseable HHMMSS timestamp segment are quarantined.
``provenance.path`` and malformed-bucket ``path`` are repo-root-relative POSIX paths, never
the lister's raw absolute output — see ``_relativize_path``.

Port of: emit-cockpit-snapshot.sh (example-doctrine-repo 07eedcfb, 2026-07-19) — § SECTION 3, ReviewTrail
  (the embedded python3 heredoc). Byte/semantic parity port — the embedded heredoc's
  logic is reproduced in-process here via the shared ``_validate_review_trail_file``
  helper; the file lister (``coordinator_core.ops.list_review_trail_records``, itself
  the native port of list-review-trail-records.sh, example-doctrine-repo b5a4192c, 2026-07-20) is now
  called in-process — no subprocess, no bash.
Spec backlink: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § P03
"""

from __future__ import annotations

import functools
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections._shared import _validate_review_trail_file
from coordinator_core.ops.list_review_trail_records import (
    ReviewTrailListError,
    list_paths as _native_list_paths,
)


@functools.lru_cache(maxsize=None)
def _resolved_root(root_str: str) -> Path:
    """Cache ``Path(root_str).resolve()`` — the Win32 ``_getfinalpathname`` cost.

    2026-08-08 load-norm C3/lever-2: ``_relativize_path`` used to re-resolve the
    (invariant, per-emission) root on every call — once per review-trail record,
    ~3,000 redundant ``nt._getfinalpathname`` syscalls that bought nothing, since
    the root never changes within a single ``collect()`` walk (or across the whole
    process, which is spawn-per-call, not resident). Caching by the *unresolved*
    root string is semantically identical to always resolving fresh: symlinks,
    junctions, UNC paths and case-insensitive comparison are all still honoured —
    ``Path.resolve()`` still runs, exactly once per distinct root string, and its
    result is reused rather than its syscalls repeated. The per-*filepath* resolve
    below is untouched (each file is a distinct real path and must still be
    resolved individually for the same symlink/junction correctness reasons).
    """
    return Path(root_str).resolve()


def _relativize_path(ctx: EmitContext, filepath: str) -> str:
    """Reduce an absolute review-trail *filepath* to a repo-root-relative POSIX path.

    2026-07-21 golden-emission fixture real-name + abs-path portability, DR-060: the
    native lister (``list_review_trail_records.list_paths``) returns absolute paths
    joined against the state root it resolved (``ctx.subprocess_root`` when set for
    frozen-fixture test isolation, else ``ctx.repo_root`` — same root the lister was
    pinned to via ``COORDINATOR_ROOT``, mirrors the ``sections/lessons.py:77`` idiom).
    Emitting that absolute path verbatim into provenance leaked the machine-local
    checkout path (e.g. ``/Users/<name>/...``) into every emission. Relativizing here
    matches the existing ``sections/goals.py`` convention (``state/goals-log....jsonl``)
    and keeps provenance paths portable across machines.

    Falls back to the original (absolute) *filepath* when it is not under the
    resolved root, rather than raising — a file outside the root is unexpected but
    should not crash collection.
    """
    root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    try:
        return Path(filepath).resolve().relative_to(_resolved_root(str(root))).as_posix()
    except ValueError:
        return filepath


_COORDINATOR_ROOT_ENV = "COORDINATOR_ROOT"


@contextmanager
def _coordinator_root_env_override(value: Optional[str]) -> Iterator[None]:
    """Temporarily set/restore ``COORDINATOR_ROOT`` for an in-process native call.

    Mirrors the old subprocess ``env={**os.environ, "COORDINATOR_ROOT": ...}`` override —
    ``list_review_trail_records._resolve_state_root`` reads the var from ``os.environ``
    directly (test-isolation seam), so an in-process caller must stage/restore it around
    the call rather than pass it as a subprocess-local env dict.
    """
    if value is None:
        yield
        return
    had_prior = _COORDINATOR_ROOT_ENV in os.environ
    prior = os.environ.get(_COORDINATOR_ROOT_ENV)
    os.environ[_COORDINATOR_ROOT_ENV] = value
    try:
        yield
    finally:
        if had_prior:
            os.environ[_COORDINATOR_ROOT_ENV] = prior  # type: ignore[assignment]
        else:
            os.environ.pop(_COORDINATOR_ROOT_ENV, None)


def _list_review_trail_paths(ctx: EmitContext) -> list[str]:
    """Return the live+archived review-trail file paths via the native lister.

    In-process replacement for the retired ``bash <script>`` bridge (bash:671):
    stderr/exceptions discarded, any resolution failure tolerated (empty result is valid),
    exactly matching the oracle's ``… 2>/dev/null`` tolerance posture.

    Scoping: always pins ``COORDINATOR_ROOT`` to ``ctx.subprocess_root`` (frozen-fixture
    test isolation) when set, else ``ctx.central_state_root`` — the state root
    ``resolve_context()`` already resolved for THIS emission (repo_root/state for a
    per-repo emission, CLAUDE_KLABAUTER_ROOT/state or the CLAUDE_HOME fallback for the legacy
    meta-repo path). Never falls through to the bare "no override" case, unlike the old
    subprocess bridge (which, lacking an explicit ``cwd=``, inherited the calling
    process's ambient cwd) — a bare in-process call reads ``os.environ``/``git
    rev-parse`` against THIS process's cwd, which has no relationship to the repo being
    emitted for and would leak a real tree's state into an isolated-context caller
    (e.g. a test constructing an ``EmitContext`` for a ``tmp_path`` repo while pytest's
    own cwd is the live claude-klabauter checkout).
    """
    override = str(ctx.subprocess_root if ctx.subprocess_root is not None else ctx.central_state_root)
    try:
        with _coordinator_root_env_override(override):
            return _native_list_paths()
    except ReviewTrailListError:
        return []


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build ReviewTrail records + quarantine list (parity: bash SECTION 3 heredoc).

    Delegates per-file validation to ``_validate_review_trail_file`` from ``_shared`` so
    the quarantine rules (verdict set, timestamp format, required fields) stay in sync with
    ``rollups._review_trail_facts``.
    """
    paths = _list_review_trail_paths(ctx)

    valid_records: list[dict] = []
    malformed: list[dict] = []

    for filepath in paths:
        if not filepath or not os.path.isfile(filepath):
            continue

        validated, reason = _validate_review_trail_file(filepath)
        if validated is None:
            malformed.append({"path": _relativize_path(ctx, filepath), "reason": reason})
            continue

        record = {
            "repo": ctx.repo_name,
            "coordinator_root_path": ".",
            "sha_range": validated["sha_range"],
            "reviewer": validated["reviewer"],
            "verdict": validated["verdict"],
            "diff_loc": validated["diff_loc"],
            "reviewed_at": validated["reviewed_at"],
            "workstream": validated["workstream"],
            "provenance": ctx.provenance(
                "local_fs", path=_relativize_path(ctx, filepath), derivation="parsed"
            ),
        }
        valid_records.append(record)

    return valid_records, malformed
