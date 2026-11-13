"""coordinator_core.install.klabauter_freshness_apply — probe, fetch, guarded fast-forward,
engine-leg deferral, mapped installer re-run and surface-once for a klabauter checkout.

Builds on the pure names in `klabauter_freshness`. Never discards, stashes, rebases or
merges non-ff: a dirty or diverged tree only surfaces. Installers run in-process.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

from coordinator_core.git.run import GitResult, run_git
from coordinator_core.install import klabauter_freshness as kf

UNIMATRIX_INSTALLER_RELPATH = ("coordinator", "bin", "unimatrix-install.py")
UNIMATRIX_DEST_DIRNAME = "unimatrix"


@dataclass
class FreshnessResult:
    """Outcome of one `apply_freshness` call. `moved` is True only when a ref moved."""

    action: str = kf.ACTION_CURRENT
    reason: str = ""
    hostname: str = ""
    local_sha: str = ""
    origin_sha: str = ""
    moved: bool = False
    changed_paths: list = field(default_factory=list)
    installers_run: list = field(default_factory=list)
    installer_errors: dict = field(default_factory=dict)
    stale_installs: list = field(default_factory=list)
    surfaced: bool = False
    fetch_error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Probe:
    """One status read of a checkout. `upstream` is empty for a detached or untracked branch."""

    local_sha: str
    upstream: str
    ahead: int
    behind: int
    dirty: bool


def _git(checkout: Path, *args: str, remote: bool = False) -> GitResult:
    """A timeout or unspawnable git is a nonzero `returncode` (-1 / 127), never an exception."""
    return run_git(
        ["--no-optional-locks", *args],
        cwd=str(checkout),
        remote=remote,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def probe(checkout: Path) -> Optional[Probe]:
    """One `git status` read: HEAD sha, upstream, ahead/behind, tracked-file dirtiness.

    `None` when `checkout` is not a readable git work tree. Untracked files are not
    dirt (`-uno`): a conflicting one makes the later `--ff-only` refuse, which is
    reported as a failed move, not a moved ref.
    """
    try:
        out = _git(checkout, "status", "--porcelain=v2", "--branch", "-uno")
    except OSError:
        return None
    if out.returncode != 0:
        return None
    sha = upstream = ""
    ahead = behind = 0
    dirty = False
    for line in out.stdout.splitlines():
        if line.startswith("# branch.oid "):
            sha = line.split(" ", 2)[2]
        elif line.startswith("# branch.upstream "):
            upstream = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            parts = line.split()
            ahead, behind = int(parts[2].lstrip("+")), abs(int(parts[3]))
        elif not line.startswith("#"):
            dirty = True
    if not sha or sha == "(initial)":
        return None
    return Probe(sha, upstream, ahead, behind, dirty)


def fetch(checkout: Path) -> str:
    """`git fetch` of the branch's upstream remote; returns an error line, `""` on success.

    A remote round trip: callers run this off the sync path only. Bounded by
    `run_git`'s remote lane (30s process); a call site cannot buy more.
    """
    try:
        out = _git(checkout, "fetch", "--quiet", remote=True)
    except OSError as exc:
        return f"fetch failed: {exc}"
    return "" if out.returncode == 0 else f"fetch failed: {out.stderr.strip()[:200]}"


def _rev(checkout: Path, ref: str) -> str:
    try:
        out = _git(checkout, "rev-parse", "--verify", "--quiet", ref)
    except OSError:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def engine_leg_in_flight(
    engine_root: Path,
    *,
    sup_count: Callable[[Path], Optional[int]] | None = None,
    door_count: Callable[[Path], Optional[int]] | None = None,
    own_pid: Optional[int] = None,
) -> int:
    """1 when an engine leg other than this pass is in flight at `engine_root`, else 0.

    Staged runs between legs hold no request and never count. When this pass is served by the
    root's own supervisor (its discovery pid is this process), its own request is subtracted from
    both probes; an unanswerable probe (None) reads in flight.
    """
    from coordinator_core.warm import front_door, supervisor

    sup_count = sup_count or supervisor.in_flight_count
    door_count = door_count or front_door.in_flight_count
    sup, door = sup_count(engine_root), door_count(engine_root)
    if sup is None or door is None:
        return 1
    record = supervisor.read_discovery(engine_root)
    pid = own_pid if own_pid is not None else os.getpid()
    if isinstance(record, dict) and record.get("pid") == pid:
        sup, door = max(sup - 1, 0), max(door - 1, 0)
    return 1 if sup > 0 or door > 0 else 0


def _load_unimatrix_installer(checkout: Path):
    path = Path(checkout).joinpath(*UNIMATRIX_INSTALLER_RELPATH)
    spec = importlib.util.spec_from_file_location("_klabauter_unimatrix_install", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def unimatrix_stale(checkout: Path, settings_home: Path) -> tuple:
    """Installed unimatrix copies that differ from the checkout source (refresh-only).

    Empty when the destination dir does not exist: a box that never installed
    unimatrix is not seeded here.
    """
    dest = Path(settings_home) / UNIMATRIX_DEST_DIRNAME
    src = Path(checkout) / "coordinator" / "bin"
    if not dest.is_dir() or not src.is_dir():
        return ()
    try:
        module = _load_unimatrix_installer(checkout)
        wanted = list(module.VENDORED_SCRIPTS) + sorted(
            module.script_closure(src) - set(module.VENDORED_SCRIPTS)
        )
    except Exception:
        return ()
    source = {n: d for n in wanted if (d := _digest(src / n)) is not None}
    installed = {n: _digest(dest / n) for n in source}
    return kf.stale_installs(source, installed)


def _run_unimatrix(checkout: Path, settings_home: Path, engine_root: Optional[Path]) -> None:
    module = _load_unimatrix_installer(checkout)
    module.install_scripts(
        Path(checkout) / "coordinator" / "bin", Path(settings_home) / UNIMATRIX_DEST_DIRNAME
    )


def _run_bin_shims(checkout: Path, settings_home: Path, engine_root: Optional[Path]) -> None:
    from coordinator_core.hooks.support import bin_impl_drift

    bin_impl_drift.check_and_refresh(Path(settings_home) / "bin")


def _run_currency_cache(checkout: Path, settings_home: Path, engine_root: Optional[Path]) -> None:
    from coordinator_core.warm import skew

    skew.write_currency_cache(Path(engine_root or checkout), Path(checkout))


DEFAULT_INSTALLERS: Mapping[str, Callable[[Path, Path, Optional[Path]], None]] = {
    kf.INSTALLER_UNIMATRIX: _run_unimatrix,
    kf.INSTALLER_BIN_SHIMS: _run_bin_shims,
    kf.INSTALLER_CURRENCY_CACHE: _run_currency_cache,
}


def read_record(settings_home: Path) -> object:
    """The persisted surfaced record, `None` when absent or unreadable."""
    try:
        return json.loads(kf.freshness_record_path(settings_home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_record(settings_home: Path, key: kf.SurfacedKey) -> None:
    path = kf.freshness_record_path(settings_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(kf.key_to_record(key), sort_keys=True), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _clear_record(settings_home: Path) -> None:
    try:
        kf.freshness_record_path(settings_home).unlink()
    except OSError:
        pass


def apply_freshness(
    checkout: Path,
    settings_home: Path,
    *,
    do_fetch: bool = True,
    hostname: Optional[str] = None,
    engine_root: Optional[Path] = None,
    live_runs: Optional[Callable[[], int]] = None,
    installers: Optional[Mapping[str, Callable]] = None,
) -> FreshnessResult:
    """Compare `checkout` to its upstream and act on the verdict.

    Fetches (unless `do_fetch` is False), probes once, and fast-forwards only a
    behind, clean, non-diverged tree with no engine leg in flight (staged runs between legs
    do not block it). Dirty and diverged trees surface once per `SurfacedKey`. Stale
    installed unimatrix copies are
    re-installed on every verdict except `deferred_live_run`, ref moved or not.
    """
    checkout, settings_home = Path(checkout), Path(settings_home)
    installers = DEFAULT_INSTALLERS if installers is None else installers
    live_runs = live_runs or (lambda: engine_leg_in_flight(engine_root or checkout))
    result = FreshnessResult(hostname=hostname or socket.gethostname())

    if do_fetch:
        result.fetch_error = fetch(checkout)
    snap = probe(checkout)
    if snap is None:
        result.reason = "not a readable git checkout"
        return result
    result.local_sha = snap.local_sha
    result.origin_sha = _rev(checkout, snap.upstream) if snap.upstream else ""
    if not result.origin_sha:
        result.origin_sha = snap.local_sha
        result.reason = "no upstream to compare"

    needs_run_check = snap.behind > 0 and snap.ahead == 0 and not snap.dirty
    verdict = kf.decide(
        snap.local_sha,
        result.origin_sha,
        snap.ahead,
        snap.behind,
        snap.dirty,
        live_runs() if needs_run_check else 0,
    )
    result.action = verdict.action
    result.reason = result.reason or verdict.reason

    to_run: list = []
    if verdict.action == kf.ACTION_FAST_FORWARD:
        merge = _git(checkout, "merge", "--ff-only", result.origin_sha)
        if merge.returncode != 0:
            result.reason = f"ff-only refused: {merge.stderr.strip()[:200]}"
            result.action = kf.ACTION_CURRENT
        else:
            new_sha = _rev(checkout, "HEAD")
            diff = _git(checkout, "diff", "--name-only", f"{snap.local_sha}..{new_sha}")
            result.changed_paths = diff.stdout.split()
            result.moved = new_sha != snap.local_sha
            result.local_sha = new_sha
            to_run = list(kf.installers_for(result.changed_paths))

    if verdict.action != kf.ACTION_DEFERRED_LIVE_RUN and kf.INSTALLER_UNIMATRIX not in to_run:
        result.stale_installs = list(unimatrix_stale(checkout, settings_home))
        if result.stale_installs:
            to_run.insert(0, kf.INSTALLER_UNIMATRIX)

    for name in to_run:
        runner = installers.get(name)
        if runner is None:
            continue
        try:
            runner(checkout, settings_home, engine_root)
            result.installers_run.append(name)
        except Exception as exc:
            result.installer_errors[name] = f"{type(exc).__name__}: {exc}"

    state = kf.surfaced_state_for(verdict.action)
    if state is None:
        _clear_record(settings_home)
    else:
        key = kf.SurfacedKey(result.hostname, snap.local_sha, result.origin_sha, state)
        if kf.is_new_surface(read_record(settings_home), key):
            _write_record(settings_home, key)
            result.surfaced = True
    return result
