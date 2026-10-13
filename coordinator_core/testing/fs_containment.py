"""Fail any test that leaves a new top-level entry outside the repo and scratch.

Contract: coordinator-content-repo ``coordinator/docs/wiki/test-design-discipline/
test-filesystem-containment.md``. Watched roots are the real home and the
repo root's parent, snapshotted at depth 1 around every test. Writes under
the repo tree or ``<gettempdir()>/coordinator/`` never count; bare Temp top
level does. An unset basetemp becomes a per-run dir under
``<gettempdir()>/coordinator/<repo>/pytest/`` (xdist workers inherit it).

Cleanup, controller only, at session finish: a run it minted the basetemp for
is deleted on pass (exit 0) or nothing collected (exit 5). Any other exit keeps
the dir, marks it, and prunes marked siblings beyond the newest 3 failing runs.
An explicit ``--basetemp`` and an xdist worker never delete.

Redirect: the controller points TMP/TEMP/TMPDIR and ``tempfile.tempdir`` at
``<gettempdir()>/coordinator/<repo>/run-temp/<pid>-<hex>/`` (workers inherit it;
a configure already inside one does not nest). That root is not watched per
test: every worker and subprocess writes it, so an entry cannot be attributed
to a test. At session finish anything but ``coordinator`` left in it is
reported and turns a passing exit status into 1; the root is then deleted.

Stdlib + pytest only, and no package-relative imports: repos without
``coordinator_core`` load this file by path and register the module object.
Register via ``pytest_plugins`` / ``-p``, never by re-exporting hook names.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import stat
import sys
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
_MINTED: "Path | None" = None
_REDIRECT: "_Redirect | None" = None

# Mirrors coordinator_core.temp_layout.RUN_TEMP_DIRNAME (pinned by a test).
_RUN_TEMP_DIRNAME = "run-temp"
_TEMP_ENV = ("TMP", "TEMP", "TMPDIR")
_LEFT_REPORT_CAP = 20

# Mirrors coordinator_core.temp_layout.PYTEST_KEPT_MARKER (pinned by a test).
_KEPT_MARKER = ".coordinator-kept-failing"
_KEEP_FAILING = 3


def _resolve(p) -> Path:
    return Path(p).resolve()


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


def _coordinator_temp() -> Path:
    # Mirrors coordinator_core.temp_layout; inlined because this file loads by path.
    return Path(tempfile.gettempdir()) / "coordinator"


class _Redirect:
    def __init__(self, root: Path, saved_env: "dict[str, str | None]", saved_tempdir):
        self.root = root
        self.saved_env = saved_env
        self.saved_tempdir = saved_tempdir


def _redirect_temp(rootpath: Path) -> _Redirect:
    root = (
        _coordinator_temp()
        / rootpath.name
        / _RUN_TEMP_DIRNAME
        / f"{os.getpid()}-{os.urandom(3).hex()}"
    )
    root.mkdir(parents=True, exist_ok=True)
    saved = {k: os.environ.get(k) for k in _TEMP_ENV}
    redirect = _Redirect(root, saved, tempfile.tempdir)
    for key in _TEMP_ENV:
        os.environ[key] = str(root)
    tempfile.tempdir = str(root)
    return redirect


def _end_redirect(session: pytest.Session, exitstatus) -> None:
    global _REDIRECT
    redirect, _REDIRECT = _REDIRECT, None
    if redirect is None:
        return
    try:
        left = sorted(p.name for p in redirect.root.iterdir() if p.name != "coordinator")
    except OSError:
        left = []
    for name in left[:_LEFT_REPORT_CAP]:
        print(f"fs_containment: left in run Temp: {name}", file=sys.stderr)
    if left and exitstatus == 0:
        session.exitstatus = 1
    for key, value in redirect.saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    tempfile.tempdir = redirect.saved_tempdir
    _rmtree(redirect.root)


def _per_run_basetemp(rootpath: Path) -> Path:
    suffix = os.urandom(3).hex()
    return _coordinator_temp() / rootpath.name / "pytest" / f"{os.getpid()}-{suffix}"


# tryfirst: pytest's tmpdir plugin reads option.basetemp in its own configure.
@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    global _MINTED, _REDIRECT
    _MINTED = None
    controller = not hasattr(config, "workerinput")
    if config.option.basetemp is None and controller:
        basetemp = _per_run_basetemp(config.rootpath)
        basetemp.parent.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(basetemp)
        _MINTED = basetemp
    nested = Path(tempfile.gettempdir()).parent.name == _RUN_TEMP_DIRNAME
    if controller and not nested:
        _REDIRECT = _redirect_temp(config.rootpath)
    # Captured here, before any fixture sandboxes HOME/USERPROFILE: a later
    # Path.home() read would watch the sandbox instead of the real home.
    repo = _resolve(config.rootpath)
    # Needed for the sanctioned managed dir only: every worker and subprocess writes the
    # run-temp root, so a per-test snapshot cannot attribute an entry to a test.
    if _REDIRECT is not None:
        redirect_root: "Path | None" = _REDIRECT.root
    elif nested:
        redirect_root = Path(tempfile.gettempdir())
    else:
        redirect_root = None
    managed = redirect_root / "coordinator" if redirect_root else _coordinator_temp()
    _SANCTIONED[:] = [repo, _resolve(managed)]
    roots: "list[Path]" = []
    candidates = [Path.home(), config.rootpath.parent]
    for candidate in candidates:
        try:
            root = _resolve(candidate)
        except (OSError, RuntimeError):
            continue
        if root not in roots and not any(_contains(s, root) for s in _SANCTIONED):
            roots.append(root)
    _WATCHED[:] = roots


def _rmtree(path: Path) -> None:
    def handler(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=handler)
    else:
        shutil.rmtree(path, onerror=handler)
    if path.exists():
        print(f"fs_containment: {path} left for the sweep", file=sys.stderr)


def _prune_kept(parent: Path) -> None:
    marked = []
    for child in parent.iterdir():
        marker = child / _KEPT_MARKER
        if child.is_dir() and not child.is_symlink() and marker.is_file():
            marked.append((marker.stat().st_mtime, child.name, child))
    marked.sort(key=lambda m: (m[0], m[1]), reverse=True)
    for _, _, stale in marked[_KEEP_FAILING:]:
        _rmtree(stale)


# trylast: after xdist stops its workers and orphan_reaper scans the tree.
@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus) -> None:
    if not hasattr(session.config, "workerinput"):
        try:
            _end_redirect(session, exitstatus)
        except Exception:
            pass
        # A redirect leftover turns a pass into a failure; the basetemp is kept to match.
        exitstatus = getattr(session, "exitstatus", exitstatus)
    try:
        minted = _MINTED
        if hasattr(session.config, "workerinput") or minted is None:
            return
        if exitstatus in (0, 5):
            if minted.exists():
                _rmtree(minted)
        elif minted.is_dir():
            (minted / _KEPT_MARKER).touch()
            _prune_kept(minted.parent)
    except Exception:
        pass


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
