"""Fleet Temp layout resolver: every path under system Temp that coordinator owns.

Stdlib-only. Paths are built from ``tempfile.gettempdir()`` at call time, never a
literal ``%TEMP%`` or ``/tmp``, so a monkeypatched ``gettempdir`` is honored.
"""

from __future__ import annotations

import re
import tempfile
from fnmatch import fnmatchcase
from pathlib import Path

FLEET_DIRNAME = "_fleet"
PYTEST_KEPT_MARKER = ".coordinator-kept-failing"
_PYTEST_RUN_DIR_RE = re.compile(r"^(\d+)-[0-9a-f]{6}$")
_ROOT_DIRNAME = "coordinator"


def _coordinator_root() -> Path:
    return Path(tempfile.gettempdir()) / _ROOT_DIRNAME


def coordinator_temp_root(repo_root: Path | str) -> Path:
    """``<gettempdir()>/coordinator/<repo dir name>/``; ValueError for a repo named ``_fleet``."""
    name = Path(repo_root).name
    if not name:
        raise ValueError(f"repo_root has no directory name: {repo_root!r}")
    if name == FLEET_DIRNAME:
        raise ValueError(f"repo directory name {FLEET_DIRNAME!r} is reserved for host-scoped Temp")
    return _coordinator_root() / name


def fleet_temp_root() -> Path:
    """``<gettempdir()>/coordinator/_fleet/``: host-scoped, never a purge root."""
    return _coordinator_root() / FLEET_DIRNAME


def pytest_basetemp_root(repo_root: Path | str) -> Path:
    """``coordinator_temp_root(repo_root)/pytest/``: parent of per-run pytest basetemps."""
    return coordinator_temp_root(repo_root) / "pytest"


RUN_TEMP_DIRNAME = "run-temp"

# Top-level Temp name shapes coordinator wrote before the managed layout. Each is checked
# against its producer; ``coordinator-probe-spray-*`` has no live producer (deleted) and stays
# so leftovers from older runs are still recognized.
#
# TRAP: these delete third-party entries on every install, so a generic prefix is pinned to
# our producer's suffix shape (pid digits, ``.json``), never a bare ``<word>-*``.
LEGACY_TOP_LEVEL_GLOBS: tuple[str, ...] = (
    "git-pathspec-[0-9]*.txt",
    "git-index-[0-9]*",
    "coordinator-pending-dispatch-*",
    "coordinator-nudged-*",
    "coordinator-dispatch-nudge-*",
    "coordinator-guard-unlock-*",
    "coordinator-probe-spray-*",
    "advisory-hook-state-*",
    "first-agent-dispatch-advisory-*",
    "rt-bark-once-*",
    "group-em-watch-*",
    "workflow-run-*.json",
    "em-poll-count-*",
    "hand-written-plan-nudge-*",
    "autonomous-run-*",
    "pickup-autofire-events.jsonl",
    "coordinator-bt-python3-invocation-cache.json",
    "coordinator-gitless-advisory-dedupe",
    "coordinator-fleet-no-repo-root",
    "backfill-initiative-fk.lock",
    "coordinator-falsifier",
    "bash-dispatch-probe-root",
    # Test and audit-probe leftovers from our own code; each has a producer in claude-klabauter,
    # coordinator-content-repo or example-store-repo (the producer is named in the commit message).
    "altlive-*",
    "sgc-cold-*",
    "fmhook-test-*",
    "fmhook-lineage-test-*",
    "runtime-tripwire-em-dispatch-default-cursor-*",
    "land_pull_test_*",
    "c[0-9]-census-fixture-*",
    "hook-census-fixture-*",
    "claude-klabauter-throwaway-tree-*",
    "claude-klabauter-publish-materialize-*",
)


def managed_tempdir(repo_root: Path | str | None = None) -> Path:
    """Created managed Temp dir: the repo's root when given a repo, else the fleet root."""
    root = coordinator_temp_root(repo_root) if repo_root is not None else fleet_temp_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def legacy_top_level_path(name: str) -> Path:
    """``<gettempdir()>/<name>``: the sanctioned read of a pre-managed-layout location."""
    return Path(tempfile.gettempdir()) / name


def is_legacy_top_level(name: str) -> bool:
    """True when a top-level Temp entry name matches a shape coordinator used to write."""
    return any(fnmatchcase(name, g) for g in LEGACY_TOP_LEVEL_GLOBS)


def parse_pytest_run_dir(name: str) -> int | None:
    """Owning pid of a ``<pid>-<6 hex>`` per-run basetemp dir name, else None."""
    m = _PYTEST_RUN_DIR_RE.fullmatch(name)
    return int(m.group(1)) if m else None
