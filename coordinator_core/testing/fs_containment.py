"""Fail any test that leaves a new top-level entry outside the repo and scratch.

Contract: coordinator-content-repo ``coordinator/docs/wiki/test-design-discipline/
test-filesystem-containment.md``. Watched roots are the real home and the
repo root's parent, snapshotted at depth 1 around every test. Writes under
the repo tree or ``<gettempdir()>/coordinator/`` never count; bare Temp top
level does. An unset basetemp becomes a per-run dir under
``<gettempdir()>/coordinator/<repo>/pytest/`` (xdist workers inherit it). The guard never deletes.

Stdlib + pytest only, and no package-relative imports: repos without
``coordinator_core`` load this file by path and register the module object.
Register via ``pytest_plugins`` / ``-p``, never by re-exporting hook names.
"""

from __future__ import annotations

import fnmatch
import os
import tempfile
import warnings
from pathlib import Path

import pytest

# Stable markers of live peer processes, measured in a claude-klabauter fast-suite run
# (2026-10-03): Claude Code's atomic ~/.claude.json writes and a concurrent
# publish round's sibling staging dir, and (2026-10-06, six live sessions) the
# harness's transient `~/.claude.lock`. Exact names or fixed-prefix globs only,
# never a dot-prefix blanket -- a test could create `~/.foo`.
_AMBIENT = (
    ".claude.lock",
    ".claude.json.lock",
    ".claude.json.tmp.*",
    ".*.publish-staging-*",
)

_WATCHED: "list[Path]" = []
_SANCTIONED: "list[Path]" = []


def _resolve(p) -> Path:
    return Path(p).resolve()


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


def _coordinator_temp() -> Path:
    # Mirrors coordinator_core.temp_layout; inlined because this file loads by path.
    return Path(tempfile.gettempdir()) / "coordinator"


def _per_run_basetemp(rootpath: Path) -> Path:
    suffix = os.urandom(3).hex()
    return _coordinator_temp() / rootpath.name / "pytest" / f"{os.getpid()}-{suffix}"


# tryfirst: pytest's tmpdir plugin reads option.basetemp in its own configure.
@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    if config.option.basetemp is None and not hasattr(config, "workerinput"):
        basetemp = _per_run_basetemp(config.rootpath)
        basetemp.parent.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(basetemp)
    # Captured here, before any fixture sandboxes HOME/USERPROFILE: a later
    # Path.home() read would watch the sandbox instead of the real home.
    repo = _resolve(config.rootpath)
    _SANCTIONED[:] = [repo, _resolve(_coordinator_temp())]
    roots: "list[Path]" = []
    for candidate in (Path.home(), config.rootpath.parent):
        try:
            root = _resolve(candidate)
        except (OSError, RuntimeError):
            continue
        if root not in roots and not any(_contains(s, root) for s in _SANCTIONED):
            roots.append(root)
    _WATCHED[:] = roots


def _snapshot() -> "dict[Path, set[str] | None]":
    snap: "dict[Path, set[str] | None]" = {}
    for root in _WATCHED:
        try:
            snap[root] = set(os.listdir(root))
        except OSError as exc:
            warnings.warn(f"fs_containment: cannot list {root}: {exc}")
            snap[root] = None
    return snap


def _leads_into_sanctioned(root: Path, name: str) -> bool:
    entry = root / name
    return any(_contains(entry, s) or _contains(s, entry) for s in _SANCTIONED)


@pytest.fixture(autouse=True)
def _fs_containment(request: pytest.FixtureRequest):
    before = _snapshot()
    yield
    after = _snapshot()
    gained = [
        f"{root / name}"
        for root, names in after.items()
        if names is not None and before.get(root) is not None
        for name in sorted(names - before[root])  # type: ignore[operator]
        if not _leads_into_sanctioned(root, name)
        and not any(fnmatch.fnmatchcase(name, pat) for pat in _AMBIENT)
    ]
    if gained:
        pytest.fail(
            f"{request.node.nodeid} left new entries outside the repo and "
            f"scratch: {gained}. Root paths under tmp_path; for a home, use "
            "coordinator_core.testing.home_sandbox.sandbox_home.",
            pytrace=False,
        )
