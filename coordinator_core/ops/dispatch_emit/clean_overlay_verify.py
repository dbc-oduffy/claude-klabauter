"""Clean-overlay verify: run a landing set's scoped tests in ONE persistent clean mirror of the repo, moved to
the measured candidate commit (HEAD plus only the landing set), so a peer's dirty tracked file or uncommitted
module can neither fail nor falsely green the verdict.

The mirror is a `clone --shared` under `temp_layout.coordinator_temp_root(root)`, advanced between verifies by
`diff-tree` plus `update-index --index-info` and `checkout-index` (`read-tree` re-reads every tree object: seconds) so a verify writes only the paths that differ, and serialised on a per-mirror
lock. Runs in the workflow verify stage (`python -m coordinator_core.ops.dispatch_emit.clean_overlay_verify
--run-id <run>`), never inside an engine op: it writes `overlay-verdict.json` beside the run's `measured-rev.json`,
keyed to the landing set's blob shas and the base sha. `dispatch.land_run` only calls `check_verdict`, a single
file read. `--export-run -- <argv>` runs a test command in the mirror at HEAD plus the run's declared paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, ContextManager, Iterator, Optional, Sequence

from coordinator_core import temp_layout
from coordinator_core.git.run import _git_exe, run_git
from coordinator_core.locked_write import LockTimeout, held_lock
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.interpreter_pin import resolve_targeted_test_python
from coordinator_core.testing import scoped_pytest
from coordinator_core.win_portability import no_console_creationflags

VERDICT_FILE = "overlay-verdict.json"

MIRROR_DIRNAME = "clean-mirror"
SETUP_TIMEOUT_S = 60
TEST_TIMEOUT_S = 120
IMPORTER_CAP = 25
_PYTEST_FAILED = 1
_PYTEST_INTERRUPTED = 2
_OUTPUT_TAIL = 2000
_STATE_FILE = "state.json"
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_ADD_BATCH = 200


@dataclass(frozen=True)
class OverlayVerdict:
    """`verdict` is "green" (tests ran and passed), "red" (a test failed or a test file failed to collect),
    "pre_existing" (every failing test also fails at the base sha: lands with an advisory) or "inconclusive"
    (no mirror or no test ran: land_run refuses it). "missing" (no verdict was produced) is built by
    `check_verdict`: land_run lands it with an advisory. `detail` is the tail of the runner output or the reason."""

    verdict: str
    detail: str = ""
    seeds: tuple = field(default_factory=tuple)

    @property
    def red(self) -> bool:
        return self.verdict == "red"


def _run(
    argv: list, cwd: Path, timeout: int, env: Optional[dict] = None, stdin_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run `argv` in a capped job (a killpg session off Windows); the tree is closed on every exit, so a
    timed-out pytest leaves no grandchild. `stdin_text` (NUL-terminated records, no newline) feeds stdin.
    Raises `subprocess.TimeoutExpired` at the timeout."""
    from coordinator_core.diagnostics.contained_run import spawn_in_capped_job

    proc, close = spawn_in_capped_job(
        argv,
        popen_kwargs=dict(
            cwd=str(cwd), env=env, stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", **no_console_creationflags(),
        ),
    )
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        close()
        proc.communicate()
        raise
    finally:
        close()
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _gate_python(root: Path) -> str:
    """The interpreter the repo's test gate pins (same ladder as `retest_targeted`), read from the live
    repo's coordinator.local.md; `sys.executable` when none is pinned or the pin does not resolve."""
    pin = resolve_targeted_test_python(root)
    return pin.command if pin.command and pin.defect is None else sys.executable


def engine_python_command(root: Path) -> str:
    """`_gate_python(root)` as a token safe to paste into a Bash command: an absolute path is posix-slashed
    and double-quoted when it holds whitespace; a pinned bare name or launcher clause is returned as declared.
    Never a bare `python`: an agent's PATH `python` can lack `coordinator_core`."""
    py = _gate_python(root)
    if Path(py).is_absolute():
        py = Path(py).as_posix()
        return f'"{py}"' if any(c.isspace() for c in py) else py
    return py


_PASSED_RE = re.compile(r"\b\d+ passed\b")
_NOT_PASSED_RE = re.compile(r"\b\d+ (?:failed|errors?)\b|^(?:FAILED|ERROR) ", re.MULTILINE)
_TEARDOWN_MARKERS = ("pytest_sessionfinish", "pytest_unconfigure", "cleanup_dead_symlinks", "pytest_terminal_summary")
_PYTEST_INTERNAL_ERROR = 3
_COLLECTION_RE = re.compile(r"\berrors? during collection\b|^ERROR collecting ", re.MULTILINE)


def teardown_only_error(returncode: int, output: str) -> bool:
    """True when a non-zero pytest exit is only a session-teardown error: the run reports passed tests, no
    failed or errored test, and an INTERNALERROR (or a failure exit) whose trace sits in a sessionfinish-time
    hook. A failed test, a collection error or an INTERNALERROR with no teardown frame is not."""
    if returncode not in (_PYTEST_FAILED, _PYTEST_INTERNAL_ERROR) or not _PASSED_RE.search(output):
        return False
    if _NOT_PASSED_RE.search(output) or "INTERNALERROR" not in output:
        return False
    return any(marker in output for marker in _TEARDOWN_MARKERS)


def collection_error(returncode: int, output: str) -> bool:
    """True when pytest stopped at exit 2 because a test file failed to collect (an import error such as a
    module only a peer's worktree carries): a red of the run's own tree, never an inconclusive run."""
    return returncode == _PYTEST_INTERRUPTED and _COLLECTION_RE.search(output) is not None


_FAILED_ID_RE = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - |$)", re.MULTILINE)


def failing_ids(output: str) -> tuple:
    """The node ids a pytest short summary reports as FAILED or ERROR, in order, without duplicates."""
    return tuple(dict.fromkeys(m.strip() for m in _FAILED_ID_RE.findall(output)))


LegHook = Callable[[str], ContextManager]


def _no_leg(_name: str) -> ContextManager:
    return nullcontext()


def _note(extra, **fields) -> None:
    """Hand `fields` to the leg hook when it yielded a dict (a `LegMeter`); a hook yielding None ignores them."""
    if isinstance(extra, dict):
        extra.update(fields)


class ExportError(Exception):
    """The clean mirror could not be prepared; `str(exc)` is the reason, a timeout naming its budget."""


class _MirrorStale(Exception):
    """The mirror's recorded state no longer matches its worktree; the caller rebuilds it once."""


_SPARSE_EXCLUDE = ("state/warp/", "state/plan-blitz/", "state/queue-grind/", "state/cross-repo/", "state/sizings/", "archive/")


def _write_sparse(dest: Path) -> None:
    """Keep the run-artifact trees (about half the repo's files) out of the mirror's worktree: the clone's
    config and `info/sparse-checkout` are written directly, so `read-tree -u` needs no extra spawn or flag.
    Best effort: on any OSError the mirror is a full checkout."""
    try:
        git_dir = dest / ".git"
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info" / "sparse-checkout").write_text(
            "/*\n" + "".join(f"!/{p}\n" for p in _SPARSE_EXCLUDE), encoding="utf-8", newline="\n",
        )
        with open(git_dir / "config", "a", encoding="utf-8", newline="\n") as fh:
            fh.write("[core]\n\tsparseCheckout = true\n\tsparseCheckoutCone = false\n")
    except OSError:
        pass


def mirror_root(root: Path) -> Path:
    """The persistent mirror's holder for `root`: `<coordinator_temp_root>/clean-mirror/<8-hex of the resolved
    repo path>`, outside the repo. The worktree is `<holder>/<repo dir name>` (tests derive the repo identity
    from that basename); `state.json` beside it records the commit or tree the worktree reflects. The name
    matches no `temp_layout` managed-dir shape, so the dead-owner reap never touches it."""
    resolved = root.resolve()
    key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return temp_layout.coordinator_temp_root(resolved) / MIRROR_DIRNAME / key


def _state_read(mroot: Path) -> Optional[str]:
    try:
        data = json.loads((mroot / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    at = data.get("at") if isinstance(data, dict) else None
    return at if isinstance(at, str) and at else None


def _state_write(mroot: Path, at: Optional[str]) -> None:
    """Record what the mirror reflects; `None` marks it mid-mutation, so a crash leaves it to be rebuilt.
    Every verify ends with a write, which keeps the idle-age Temp pruners off a mirror in use."""
    mroot.mkdir(parents=True, exist_ok=True)
    (mroot / _STATE_FILE).write_text(json.dumps({"at": at}), encoding="utf-8", newline="\n")


def _drop(mroot: Path) -> None:
    try:
        temp_layout._rmtree_strict(mroot)
    except FileNotFoundError:
        pass
    except OSError:
        shutil.rmtree(mroot, ignore_errors=True)


@contextmanager
def _mirror_lock(root: Path, label: str = "clean-overlay") -> Iterator[Path]:
    """Hold the repo's mirror lock for the scope and yield the mirror holder. Every reader of the mirror holds
    it from advance through its last read. A lock not won within `TEST_TIMEOUT_S` is an `ExportError`."""
    try:
        mroot = mirror_root(root)
    except ValueError as exc:
        raise ExportError(f"clean mirror has no home for this repo: {exc}") from exc
    entered = False
    try:
        with held_lock(mroot, timeout=TEST_TIMEOUT_S, holder_label=label):
            entered = True
            yield mroot
    except LockTimeout as exc:
        if entered:
            raise
        raise ExportError(f"clean mirror busy: its lock was not free within {TEST_TIMEOUT_S}s: {exc}") from exc


def reset_mirror(root: Path) -> None:
    """Delete the repo's mirror under its lock, so the next verify is a cold build (the measurement harness)."""
    with _mirror_lock(root, "clean-overlay-reset") as mroot:
        _drop(mroot)


def _commit_sha(root: Path, rev: str) -> str:
    """`rev` as a full commit sha: a full sha passes through, anything else (HEAD) is resolved in `root`."""
    if _FULL_SHA.fullmatch(rev):
        return rev
    out = run_git(["rev-parse", "--verify", "-q", f"{rev}^{{commit}}"], cwd=str(root))
    sha = out.stdout.strip()
    if out.returncode or not _FULL_SHA.fullmatch(sha):
        raise ExportError(f"{rev!r} does not resolve to a commit in {root.name}")
    return sha


def _written(names: Sequence[str]) -> int:
    """Paths of `names` the mirror's sparse patterns leave on disk."""
    return sum(1 for n in names if n and not n.startswith(_SPARSE_EXCLUDE))


def _build(root: Path, mroot: Path, leg: LegHook) -> Path:
    """A fresh `clone --shared --no-checkout` of `root` as the mirror worktree `<mroot>/<repo name>`."""
    dest = mroot / root.resolve().name
    _drop(mroot)
    mroot.mkdir(parents=True, exist_ok=True)
    with leg("clone"):
        clone = run_git(["clone", "--shared", "--no-checkout", "-q", str(root), str(dest)], cwd=str(root))
    if clone.returncode:
        why = "timed out" if clone.timed_out else clone.stderr.strip()[-_OUTPUT_TAIL:]
        raise ExportError(f"overlay clone failed: {why}")
    _write_sparse(dest)
    return dest


def _fill(dest: Path, mroot: Path, sha: str) -> int:
    """First checkout of a fresh mirror: `read-tree -m -u <sha>` into its empty index. Returns the file count.
    Bulk transfer, not local plumbing: it runs under the overlay's pytest bound, not run_git's 2s budget."""
    _state_write(mroot, None)
    out = _run([_git_exe(), "read-tree", "-m", "-u", sha], dest, TEST_TIMEOUT_S)
    if out.returncode:
        raise ExportError(f"overlay checkout failed: {out.stderr.strip()[-_OUTPUT_TAIL:]}")
    listing = _run([_git_exe(), "ls-tree", "-r", "--name-only", "-z", sha], dest, TEST_TIMEOUT_S)
    _state_write(mroot, sha)
    return _written(listing.stdout.split("\0")) if not listing.returncode else 0


_PLAIN_MODES = ("100644", "100755")


def _apply_diff(dest: Path, raw: str) -> Optional[list]:
    """Apply `diff-tree -r --raw -z --no-renames` output to the mirror's index and worktree without walking the
    tree (`read-tree -m` re-reads every tree object of the repo: seconds, however few paths differ). Returns the
    changed names, or `None` when an entry is not a plain file (symlink, submodule, type change), which the
    caller moves by `read-tree` instead."""
    toks = raw.split("\0")
    gone: list = []
    put: list = []
    i = 0
    while i + 1 < len(toks) and toks[i].startswith(":"):
        meta = toks[i][1:].split()
        status, path = meta[4][0], toks[i + 1]
        i += 2
        new_mode, new_oid = meta[1], meta[3]
        if status == "D":
            if meta[0] not in _PLAIN_MODES:
                return None
            gone.append(path)
        elif status in "AM" and new_mode in _PLAIN_MODES and (status == "A" or meta[0] in _PLAIN_MODES):
            put.append((path, new_mode, new_oid))
        else:
            return None
    if gone:
        _must(_run([_git_exe(), "update-index", "-z", "--force-remove", "--stdin"], dest, TEST_TIMEOUT_S, stdin_text="".join(f"{p}\0" for p in gone)))
        for p in gone:
            try:
                (dest / p).unlink()
            except OSError:
                pass
    if put:
        recs = "".join(f"{mode} {oid}\t{p}\0" for p, mode, oid in put)
        _must(_run([_git_exe(), "update-index", "-z", "--add", "--index-info"], dest, TEST_TIMEOUT_S, stdin_text=recs))
        hidden = [p for p, _, _ in put if p.startswith(_SPARSE_EXCLUDE)]
        shown = [p for p, _, _ in put if not p.startswith(_SPARSE_EXCLUDE)]
        if hidden:
            _must(_run([_git_exe(), "update-index", "-z", "--skip-worktree", "--stdin"], dest, TEST_TIMEOUT_S, stdin_text="".join(f"{p}\0" for p in hidden)))
        for p in shown:
            target = dest / p
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            for anc in target.parents:
                if anc == dest:
                    break
                if anc.is_file():
                    anc.unlink()
        if shown:
            _must(_run([_git_exe(), "checkout-index", "-f", "-u", "-z", "--stdin"], dest, TEST_TIMEOUT_S, stdin_text="".join(f"{p}\0" for p in shown)))
    return [*gone, *(p for p, _, _ in put)]


def _must(out: subprocess.CompletedProcess) -> None:
    if out.returncode:
        raise _MirrorStale(f"{' '.join(out.args[1:3])} failed: {out.stderr.strip()[-_OUTPUT_TAIL:]}")


def _advance(dest: Path, mroot: Path, sha: str) -> int:
    """Move the mirror from its recorded state to `sha`'s tree: one `diff-tree` names the paths that differ
    (none: no write at all), and `_apply_diff` rewrites only those; a non-plain-file change falls back to
    `read-tree -u -m <old> <sha>`. Returns the path count. Raises `_MirrorStale` when the mirror cannot be
    advanced (it is left marked for rebuild)."""
    old = _state_read(mroot)
    if old is None:
        raise _MirrorStale("mirror state unknown")
    diff = _run([_git_exe(), "diff-tree", "-r", "--raw", "-z", "--no-renames", old, sha], dest, TEST_TIMEOUT_S)
    if diff.returncode:
        raise _MirrorStale(f"diff-tree {old[:12]}..{sha[:12]} failed: {diff.stderr.strip()[-_OUTPUT_TAIL:]}")
    if not diff.stdout.strip("\0"):
        return 0
    _state_write(mroot, None)
    names = _apply_diff(dest, diff.stdout)
    if names is None:
        moved = _run([_git_exe(), "read-tree", "-m", "-u", old, sha], dest, TEST_TIMEOUT_S)
        if moved.returncode:
            raise _MirrorStale(f"read-tree {old[:12]}..{sha[:12]} failed: {moved.stderr.strip()[-_OUTPUT_TAIL:]}")
        names = [t for t in diff.stdout.split("\0") if t and not t.startswith(":")]
    _state_write(mroot, sha)
    return _written(names)


def _declared(paths: Sequence[str]) -> tuple:
    """The declared repo-relative paths, normalised to posix and deduplicated; `ExportError` on one that is
    absolute, drive-qualified, climbs out of the repo or enters `.git`."""
    out: dict = {}
    for rel in paths:
        parts = PurePosixPath(rel.replace("\\", "/")).parts
        if (
            not rel or not parts or rel.startswith(("/", "\\")) or Path(rel).is_absolute() or PureWindowsPath(rel).drive
            or ".." in parts or parts[0].lower() == ".git"
        ):
            raise ExportError(f"declared path {rel!r} is not a plain repo-relative path")
        out[PurePosixPath(*parts).as_posix()] = None
    return tuple(out)


def _overlay(root: Path, dest: Path, mroot: Path, sha: str, rels: Sequence[str]) -> tuple:
    """Put the worktree bytes of each declared path of `root` into the mirror, staging only the paths whose bytes
    differ (a path absent from the worktree is a deletion); nothing else of the worktree is read. Paths under the
    mirror's sparse-excluded trees are skipped. Returns `(paths written, the mirror's new tree sha)`; no write
    leaves the state at `sha`. Raises `_MirrorStale` when git cannot record the overlay."""
    todo: list = []
    for rel in rels:
        if rel.startswith(_SPARSE_EXCLUDE):
            continue
        src, out = root / rel, dest / rel
        if src.is_file():
            data = src.read_bytes()
            if not (out.is_file() and out.read_bytes() == data):
                todo.append((rel, data))
        elif not src.exists() and out.is_file():
            todo.append((rel, None))
    if not todo:
        return 0, sha
    _state_write(mroot, None)
    for rel, data in todo:
        out = dest / rel
        if data is None:
            out.unlink()
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
    names = [rel for rel, _ in todo]
    for i in range(0, len(names), _ADD_BATCH):
        added = _run(
            [_git_exe(), "--literal-pathspecs", "add", "-f", "-A", "--", *names[i:i + _ADD_BATCH]], dest, TEST_TIMEOUT_S,
        )
        if added.returncode:
            raise _MirrorStale(f"staging the overlay failed: {added.stderr.strip()[-_OUTPUT_TAIL:]}")
    tree = _run([_git_exe(), "write-tree"], dest, TEST_TIMEOUT_S)
    at = tree.stdout.strip()
    if tree.returncode or not _FULL_SHA.fullmatch(at):
        raise _MirrorStale(f"recording the overlay tree failed: {tree.stderr.strip()[-_OUTPUT_TAIL:]}")
    _state_write(mroot, at)
    return len(names), at


def _prepare(root: Path, mroot: Path, sha: str, leg: LegHook, overlay: Sequence[str] = ()) -> Path:
    """The mirror worktree at commit `sha` plus the `overlay` paths' worktree bytes, under the held lock. A first
    use clones (leg "clone") and fills; later uses advance by `_advance` (leg "checkout", which also covers the
    overlay and reports `files_written`). A mirror that cannot advance is rebuilt once; a failure on the fresh
    build is an `ExportError`."""
    for rebuilt in (False, True):
        cold = rebuilt or _state_read(mroot) is None or not (mroot / root.resolve().name / ".git").exists()
        dest = _build(root, mroot, leg) if cold else mroot / root.resolve().name
        try:
            with leg("checkout") as extra:
                written = _fill(dest, mroot, sha) if cold else _advance(dest, mroot, sha)
                at = sha
                if overlay:
                    wrote, at = _overlay(root, dest, mroot, sha, overlay)
                    written += wrote
                _note(extra, files_written=written)
        except _MirrorStale as exc:
            if cold:
                raise ExportError(f"clean mirror unusable after a rebuild: {exc}") from exc
            _drop(mroot)
            continue
        _state_write(mroot, at)
        return dest
    raise AssertionError("unreachable: the second pass is cold and either returns or raises")


def _prepare_guarded(root: Path, mroot: Path, sha: str, leg: LegHook, overlay: Sequence[str] = ()) -> Path:
    """`_prepare` with a timeout or OS failure named as an `ExportError`."""
    try:
        return _prepare(root, mroot, sha, leg, overlay)
    except subprocess.TimeoutExpired as exc:
        raise ExportError(f"clean export timed out after {TEST_TIMEOUT_S}s: {exc}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExportError(f"clean export did not complete: {type(exc).__name__}: {exc}") from exc


@contextmanager
def clean_export(root: Path, run_id: str, paths: Sequence[str], leg: LegHook = _no_leg) -> Iterator[Path]:
    """Yield the mirror worktree at HEAD plus the worktree bytes of the declared `paths` only, never any other
    worktree dirt, so a run that imports an uncommitted peer module goes red there. The mirror lock is held
    until the `with` exits; the mirror persists. Raises `ExportError` (a timeout names its budget)."""
    rels = _declared(paths)
    with _mirror_lock(root, f"clean-export {run_id}") as mroot:
        yield _prepare_guarded(root, mroot, _commit_sha(root, "HEAD"), leg, rels)


def _mirror_env(env: dict, dest: Path) -> dict:
    """`env` with `coordinator_core` resolved from the mirror alone: PYTHONPATH is the mirror root (the caller's
    repo is not on it), and no bytecode is written, so a same-size same-second edit never runs a stale module."""
    return {**env, "PYTHONPATH": str(dest), "PYTHONDONTWRITEBYTECODE": "1"}


def run_in_export(root: Path, run_id: str, paths: Sequence[str], argv: Sequence[str]) -> subprocess.CompletedProcess:
    """Run `argv` in the clean export of `paths` (cwd and PYTHONPATH the mirror) under the mirror lock; the lock
    is held across the command. `ExportError` when the export fails; `TimeoutExpired` at `TEST_TIMEOUT_S`."""
    with clean_export(root, run_id, paths) as dest:
        return _run(list(argv), dest, TEST_TIMEOUT_S, env=_mirror_env(dict(os.environ), dest))


def _is_test_path(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return name.startswith("test_") or name.endswith("_test.py")


def _with_importers(dest: Path, seeds: tuple, touched: Sequence[str]) -> tuple:
    """`seeds` plus the tracked test files that mention a touched module's name (one `git grep -l` in the
    mirror), capped at `IMPORTER_CAP`; returns `(set, note)`. A failed scan runs the seeds alone."""
    stems = sorted(
        {
            PurePosixPath(p.replace("\\", "/")).stem for p in touched
            if p.endswith(".py") and not _is_test_path(p.replace("\\", "/"))
        } - {"__init__", "__main__", "conftest"}
    )
    if not stems:
        return seeds, ""
    argv = [
        _git_exe(), "grep", "-l", "-z", "-w", "-F", *(a for s in stems for a in ("-e", s)),
        "--", ":(glob)**/test_*.py", ":(glob)**/*_test.py",
    ]
    found = _run(argv, dest, TEST_TIMEOUT_S)
    if found.returncode not in (0, 1):
        return seeds, "importer scan failed: ran the touched tests only"
    extra = sorted(n for n in found.stdout.split("\0") if n and n not in seeds)
    kept = extra[:IMPORTER_CAP]
    note = f"importer cap {IMPORTER_CAP}: {len(extra) - len(kept)} importing test file(s) not run" if len(extra) > len(kept) else ""
    return (*seeds, *kept), note


def _base_failing(
    dest: Path, mroot: Path, root: Path, run_id: str, base_sha: str, ids: Sequence[str], leg: LegHook = _no_leg,
) -> frozenset:
    """The subset of `ids` that also fail with `base_sha`'s tree in the mirror. Any setup problem or timeout
    yields the empty set: an unmeasured base never excuses a red. The whole re-run is one `leg("base_rerun")`."""
    with leg("base_rerun") as extra:
        return _base_failing_body(dest, mroot, root, run_id, base_sha, ids, extra)


def _base_failing_body(
    dest: Path, mroot: Path, root: Path, run_id: str, base_sha: str, ids: Sequence[str], extra,
) -> frozenset:
    run_holder = None
    try:
        try:
            _note(extra, files_written=_advance(dest, mroot, base_sha))
        except _MirrorStale:
            return frozenset()
        run_holder, basetemp, tmp = scoped_pytest.per_run_dirs(root, f"{run_id}-base")
        ran = _run(
            _pytest_argv(root, dest, basetemp, ids), dest, TEST_TIMEOUT_S,
            env=_mirror_env(scoped_pytest.tmp_env(os.environ, tmp), dest),
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    finally:
        if run_holder is not None:
            shutil.rmtree(run_holder, ignore_errors=True)
    return frozenset(failing_ids(ran.stdout + ran.stderr)) & frozenset(ids)


def _pytest_argv(root: Path, dest: Path, basetemp: Path, targets: Sequence[str]) -> list:
    return [
        _gate_python(root), "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(dest),
        "--basetemp", str(basetemp), *targets,
    ]


def verify(
    root: Path, run_id: str, candidate_sha: str, seeds: Sequence[str], base_sha: Optional[str] = None,
    leg: LegHook = _no_leg, touched: Sequence[str] = (),
) -> OverlayVerdict:
    """Advance `root`'s persistent mirror to `candidate_sha`, run pytest over `seeds` (plus the test files that
    mention a `touched` module) there with `coordinator_core` resolved from the mirror, and hold the mirror lock
    across pytest and the base re-run. A red whose every failing node id also fails at `base_sha` (re-run in the
    same mirror, those ids only) is "pre_existing": the run did not cause it. A test file that fails to collect
    is red. Never raises: a setup failure, a busy mirror, a timeout or a run that collected nothing is
    "inconclusive". `leg(name)` is a context-manager factory entered around the "clone" (cold mirror only),
    "checkout", "pytest" and "base_rerun" legs and may yield a dict to receive `files_written` (see
    `overlay_verify_measure`); the default is a no-op."""
    seeds = tuple(seeds)
    if not seeds:
        return OverlayVerdict("inconclusive", "the landing set has no test seeds", seeds)
    run_dirs: list = []
    try:
        with _mirror_lock(root, f"clean-overlay {run_id}") as mroot:
            sha = _commit_sha(root, candidate_sha)
            dest = _prepare_guarded(root, mroot, sha, leg)
            picked, scan_note = _with_importers(dest, seeds, touched)
            run_holder, basetemp, tmp = scoped_pytest.per_run_dirs(root, run_id)
            run_dirs.append(run_holder)
            with leg("pytest"):
                ran = _run(
                    _pytest_argv(root, dest, basetemp, picked), dest, TEST_TIMEOUT_S,
                    env=_mirror_env(scoped_pytest.tmp_env(os.environ, tmp), dest),
                )
            full = ran.stdout + ran.stderr
            tail = full.strip()[-_OUTPUT_TAIL:]
            if scan_note:
                tail = f"{scan_note}: {tail}"
            at_base: frozenset = frozenset()
            ids = failing_ids(full)
            collected = collection_error(ran.returncode, full)
            failed = ran.returncode == _PYTEST_FAILED or collected
            if (
                failed and ids and isinstance(base_sha, str) and base_sha
                and base_sha != sha and not teardown_only_error(ran.returncode, full)
            ):
                at_base = _base_failing(dest, mroot, root, run_id, base_sha, ids, leg)
    except ExportError as exc:
        return OverlayVerdict("inconclusive", str(exc), seeds)
    except subprocess.TimeoutExpired as exc:
        return OverlayVerdict(
            "inconclusive", f"overlay verify timed out after {TEST_TIMEOUT_S}s: {type(exc).__name__}: {exc}", seeds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return OverlayVerdict("inconclusive", f"overlay verify did not complete: {type(exc).__name__}: {exc}", seeds)
    finally:
        for run_dir in run_dirs:
            shutil.rmtree(run_dir, ignore_errors=True)
    if ran.returncode == 0:
        return OverlayVerdict("green", tail, seeds)
    if teardown_only_error(ran.returncode, full):
        return OverlayVerdict("green", f"pytest teardown error after every test passed (exit {ran.returncode}): {tail}", seeds)
    if failed:
        if ids and at_base == frozenset(ids):
            return OverlayVerdict(
                "pre_existing", f"every failing test also fails at base {str(base_sha)[:12]}: {', '.join(ids)}: {tail}", seeds,
            )
        return OverlayVerdict("red", f"test collection failed: {tail}" if collected else tail, seeds)
    return OverlayVerdict("inconclusive", f"pytest exit {ran.returncode}: {tail}", seeds)


def overlay_candidate(measured: dict) -> Optional[str]:
    """The commit the overlay checks out: `overlay_sha` (declared paths carrying a peer's hunks split down to
    the run's own) when `measured_rev` recorded one, else the whole-file candidate `sha`."""
    sha = measured.get("overlay_sha") or measured.get("sha")
    return sha if isinstance(sha, str) else None


def name_peer_owners(outcome: OverlayVerdict, measured: dict) -> OverlayVerdict:
    """`outcome` with the peer hunks of the measured record named. A path whose peer hunks were split out is
    noted on any verdict; a path whose peer hunks could not be split turns a red into "inconclusive" (the
    red may be the peer's, so it is never a caused red) naming the owning run."""
    peers = measured.get("peer_hunks")
    if not isinstance(peers, dict) or not peers:
        return outcome
    kept = {p: v for p, v in peers.items() if isinstance(v, dict) and v.get("split")}
    left = {p: v for p, v in peers.items() if isinstance(v, dict) and not v.get("split")}
    def _named(group: dict) -> str:
        return "; ".join(f"{p} (run {v.get('owner')}, lines {v.get('lines')})" for p, v in sorted(group.items()))
    notes = []
    if kept:
        notes.append(f"peer hunks split out of the overlay candidate: {_named(kept)}")
    verdict = outcome.verdict
    if left:
        notes.append(f"peer hunks that could not be split out stay in the candidate: {_named(left)}")
        if outcome.red:
            verdict = "inconclusive"
            notes.append("the red may belong to that run's hunks, not this run: re-run once the owner lands or releases them")
    return OverlayVerdict(verdict, f"{'; '.join(notes)}: {outcome.detail}", outcome.seeds)


def landing_key(measured: dict) -> str:
    """Digest of what the overlay verified: the landing set's blob shas plus the base (HEAD) sha and the
    candidate sha, as recorded by `measured_rev`. A change to any of them changes the key."""
    blobs = measured.get("blobs") if isinstance(measured.get("blobs"), dict) else {}
    payload = json.dumps(
        {"blobs": blobs, "base_sha": measured.get("base_sha"), "sha": measured.get("sha")}, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verdict_path(root: Path, run_id: str) -> Path:
    return measured_rev_record_path(root, run_id).with_name(VERDICT_FILE)


def write_verdict(root: Path, run_id: str, measured: dict, outcome: OverlayVerdict) -> Path:
    path = verdict_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "verdict": outcome.verdict, "detail": outcome.detail, "seeds": list(outcome.seeds),
                "key": landing_key(measured), "base_sha": measured.get("base_sha"), "sha": measured.get("sha"),
            },
            indent=2, sort_keys=True,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _touched_left(root: Path, run_id: str, measured: dict) -> list:
    """Run-touched paths the measured landing set (`blobs` plus `removed`) omits. The run's touched
    set is `measured["touched"]` when the record carries one, else `measured_rev`'s measured-paths
    file; neither (or an unreadable file) means unknown, and unknown is not leftover."""
    touched = measured.get("touched")
    if not isinstance(touched, list):
        from coordinator_core.ops.dispatch_emit.measured_rev import touched_record_path

        try:
            touched = json.loads(touched_record_path(root, run_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(touched, list):
            return []
    from coordinator_core.ops.dispatch_emit.run_leftovers import run_touched_left

    blobs = measured.get("blobs") if isinstance(measured.get("blobs"), dict) else {}
    removed = measured.get("removed") if isinstance(measured.get("removed"), list) else []
    return run_touched_left(touched, frozenset(touched), [*blobs, *removed], {})


def check_verdict(root: Path, run_id: str, measured: dict) -> OverlayVerdict:
    """Read the run's overlay verdict for the `measured` record. One file read, no spawn: "green" or "red"
    (or "inconclusive", which land_run refuses, / "pre_existing", which it lands with an advisory) when a verdict
    keyed to exactly this landing set and base sha exists; otherwise "missing" (no file or an unreadable one,
    which land_run lands with an advisory) or
    "mismatch" (keyed to a different landing set or base sha; `detail` says which side moved). A run-touched
    path the `measured` landing set (`blobs` plus `removed`) omits is "mismatch" before any verdict is read
    (a verdict over half the run's work is not this run's verdict); no recorded touched set is never a
    mismatch on this account."""
    left = _touched_left(root, run_id, measured)
    if left:
        return OverlayVerdict("mismatch", f"the landing set omits run-touched path(s): {', '.join(left)}")
    try:
        data = json.loads(verdict_path(root, run_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return OverlayVerdict("missing", "no overlay verdict was written for this run")
    except (OSError, ValueError) as exc:
        return OverlayVerdict("missing", f"overlay verdict unreadable: {type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        return OverlayVerdict("missing", "overlay verdict unreadable: not an object")
    seeds = tuple(data.get("seeds") or ())
    if data.get("key") != landing_key(measured):
        moved = "base sha" if data.get("base_sha") != measured.get("base_sha") else "landing set blobs"
        return OverlayVerdict("mismatch", f"the overlay verdict was keyed to a different {moved}", seeds)
    verdict = data.get("verdict")
    if verdict == "inconclusive":
        return OverlayVerdict("inconclusive", str(data.get("detail", "")), seeds)
    if verdict not in ("green", "red", "pre_existing"):
        return OverlayVerdict("missing", f"overlay verdict was {verdict!r}: {data.get('detail', '')}", seeds)
    return OverlayVerdict(verdict, str(data.get("detail", "")), seeds)


REVERIFY_WAIT_S = SETUP_TIMEOUT_S + TEST_TIMEOUT_S
_REVERIFY_POLL_S = 1.0


def reverify_on_mismatch(
    root: Path, run_id: str, measured: dict, stale: OverlayVerdict, *,
    spawn=None, wait_s: float = REVERIFY_WAIT_S, sleep=time.sleep, clock=time.monotonic,
) -> OverlayVerdict:
    """Re-run the overlay for a "mismatch" `stale` verdict over the current `measured` candidate: this
    module's CLI is launched detached (the overlay's git and pytest children never run inside the engine
    op) and its verdict file is polled for at most `wait_s`. Returns the fresh verdict when it is green or
    red; any other `stale`, no recorded seeds, no candidate, a failed launch, a timeout or an inconclusive
    re-verify returns `stale` unchanged (the child still writes its verdict for the refire)."""
    if stale.verdict != "mismatch" or not stale.seeds or overlay_candidate(measured) is None:
        return stale
    if spawn is None:
        from coordinator_core.ops.ceremony.detached_spawn import spawn_detached as spawn
    engine = Path(__file__).resolve().parents[3]
    argv = ["--run-id", run_id, "--repo-root", str(root)]
    if not spawn(str(engine), str(Path(__file__).resolve()), argv):
        return stale
    deadline = clock() + wait_s
    while True:
        sleep(_REVERIFY_POLL_S)
        fresh = check_verdict(root, run_id, measured)
        if fresh.verdict not in ("mismatch", "missing"):
            return fresh if fresh.verdict in ("green", "red", "pre_existing") else stale
        if clock() >= deadline:
            return stale


def remedy_call(run_id: Optional[str], root: Optional[Path] = None) -> str:
    py = engine_python_command(root) if root is not None else Path(sys.executable).as_posix()
    return f"{py} -m coordinator_core.ops.dispatch_emit.clean_overlay_verify --run-id {run_id or '<run_id>'} --repo-root <repo_root>"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Verify-stage entry: read the run's `measured-rev.json`, run the overlay over the landing set's test
    seeds and write the verdict. Exit 0 on green, 1 on red, 2 when inconclusive or unmeasured.
    `--export-run -- <argv>` instead runs `argv` in the clean export (HEAD plus the run's declared paths) under
    the mirror lock and exits with its status; a failed export or a timed-out command exits 2 with the cause
    on stderr."""
    from coordinator_core.ops.dispatch_emit.landing_reach import seeds_for_landing

    raw = list(sys.argv[1:] if argv is None else argv)
    command: list = []
    if "--" in raw:
        cut = raw.index("--")
        raw, command = raw[:cut], raw[cut + 1:]
    parser = argparse.ArgumentParser(prog="clean_overlay_verify")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--export-run", action="store_true",
        help="run the command after `--` in the clean export (HEAD plus the run's declared paths)",
    )
    args = parser.parse_args(raw)
    if not safe_id(args.run_id):
        parser.error(f"--run-id {args.run_id!r} is not a plain run id")
    if args.export_run and not command:
        parser.error("--export-run needs the test command after `--`")
    if command and not args.export_run:
        parser.error("a command after `--` needs --export-run")
    root = Path(args.repo_root).resolve()
    try:
        measured = json.loads(measured_rev_record_path(root, args.run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"no readable measured-rev record for {args.run_id}: {exc}", file=sys.stderr)
        return 2
    paths = measured.get("paths") or []
    if args.export_run:
        try:
            ran = run_in_export(root, args.run_id, paths, command)
        except ExportError as exc:
            print(f"clean export failed: {exc}", file=sys.stderr)
            return 2
        except subprocess.TimeoutExpired:
            print(f"clean export test command timed out after {TEST_TIMEOUT_S}s", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"clean export test command did not start: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        sys.stdout.write(ran.stdout or "")
        sys.stderr.write(ran.stderr or "")
        return ran.returncode
    sha = overlay_candidate(measured)
    if sha is None:
        print("measured-rev record carries no candidate sha", file=sys.stderr)
        return 2
    outcome = name_peer_owners(
        verify(
            root, args.run_id, sha, seeds_for_landing(root, paths), base_sha=measured.get("base_sha"), touched=paths,
        ), measured,
    )
    write_verdict(root, args.run_id, measured, outcome)
    print(f"overlay verdict: {outcome.verdict}")
    return {"green": 0, "pre_existing": 0, "red": 1}.get(outcome.verdict, 2)


if __name__ == "__main__":
    raise SystemExit(main())
