"""baseline-export: export a commit, run a runner argv inside it, remove the export in a finally.

The CLI owns the export root name, the `git archive` extraction (tarfile data filter), the
`--link` links, the runner's process group (POSIX) or job object (Windows), and the removal.
Two git spawns per run: one `rev-parse` (toplevel + commit check), one `archive`.
Runs as `python -m coordinator_core.testing.baseline_export`.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_EXPORT_FAILED = 3
EXIT_REMOVAL_FAILED = 4

ROOT_PREFIX = "test-base-"
USAGE = "usage: baseline-export --sha <sha> [--link <repo-relative-path>]... -- <runner argv...>"

_SHA_RE = re.compile(r"[0-9a-fA-F]{4,40}")
_IS_NT = os.name == "nt"
_TERM_GRACE_S = 1.0


class UsageError(Exception):
    """argv is not a well-formed baseline-export invocation."""


class ExportError(Exception):
    """The commit could not be resolved, extracted or linked."""


class _Interrupted(BaseException):
    """A handled termination signal; unwinds to the `finally` that removes the export."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@dataclass(frozen=True)
class Invocation:
    sha: str
    links: tuple[str, ...]
    runner: tuple[str, ...]


def _bad_link(path: str) -> bool:
    if not path:
        return True
    if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
        return True
    if path.startswith(("/", "\\")):
        return True
    return ".." in re.split(r"[\\/]", path)


def parse_argv(argv: list[str]) -> Invocation:
    """Parse argv (without the program name); raise UsageError when malformed."""
    if "--" not in argv:
        raise UsageError("missing '--' before the runner argv")
    cut = argv.index("--")
    head, runner = argv[:cut], argv[cut + 1:]
    if not runner:
        raise UsageError("no runner argv after '--'")
    sha: str | None = None
    links: list[str] = []
    i = 0
    while i < len(head):
        flag = head[i]
        if flag not in ("--sha", "--link"):
            raise UsageError(f"unknown argument: {flag}")
        if i + 1 >= len(head):
            raise UsageError(f"{flag} needs a value")
        value = head[i + 1]
        if flag == "--sha":
            if sha is not None:
                raise UsageError("--sha given more than once")
            if not value:
                raise UsageError("--sha is empty")
            sha = value
        else:
            if _bad_link(value):
                raise UsageError(f"--link must be a non-empty repo-relative path without '..': {value!r}")
            links.append(value)
        i += 2
    if sha is None:
        raise UsageError("--sha is required")
    return Invocation(sha=sha, links=tuple(links), runner=tuple(runner))


def export_root(toplevel: PurePath | str, sha40: str, pid: int) -> Path:
    """The directory this run owns: <toplevel>/scratch/test-base-<sha8>-<pid>."""
    return Path(toplevel) / "scratch" / f"{ROOT_PREFIX}{sha40[:8]}-{pid}"


def counterpart_cwd(toplevel: PurePath | str, cwd: PurePath | str, root: PurePath | str) -> Path:
    """Map the invoking cwd to the same relative position inside the export root."""
    try:
        rel = Path(cwd).relative_to(Path(toplevel))
    except ValueError:
        return Path(root)
    return Path(root) / rel


def render_result_line(sha: str, export: str, why: str | None, runner_exit: int | None, removed: bool) -> str:
    """One JSON object, keys in the memo's order; `sha` is the resolved 40-hex, else --sha as supplied."""
    return json.dumps(
        {"baseline_export": {
            "sha": sha,
            "export": export,
            "why": why,
            "runner_exit": runner_exit,
            "removed": removed,
        }}
    )


def _no_console() -> dict:
    if not _IS_NT:
        return {}
    from coordinator_core.win_portability import no_console_creationflags

    return no_console_creationflags()


def _git_popen(args: list[str], cwd: str | os.PathLike[str], **kw) -> subprocess.Popen:
    """The only door to git; tests count spawns here."""
    return subprocess.Popen(["git", *args], cwd=cwd, **_no_console(), **kw)


def resolve(sha: str, cwd: str | os.PathLike[str]) -> tuple[str, str]:
    """(toplevel, sha40) for `sha` as a commit; one git spawn. Raises ExportError."""
    if not _SHA_RE.fullmatch(sha):
        raise ExportError(f"--sha is not a 4-40 hex commit id: {sha!r}")
    proc = _git_popen(
        ["rev-parse", "--show-toplevel", "--verify", "--end-of-options", f"{sha}^{{commit}}"],
        cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    out, err = proc.communicate()
    lines = out.splitlines()
    if proc.returncode != 0 or len(lines) != 2 or not re.fullmatch(r"[0-9a-f]{40}", lines[1]):
        raise ExportError(f"cannot resolve {sha!r} to a commit: {(err or out).strip() or 'git rev-parse failed'}")
    return os.path.realpath(lines[0]), lines[1]


def export(toplevel: str, sha40: str, root: Path) -> None:
    """Extract `git archive <sha40>` into `root` through tarfile's data filter; one git spawn."""
    with tempfile.TemporaryFile() as errf:
        proc = _git_popen(
            ["--no-optional-locks", "archive", "--format=tar", sha40],
            toplevel, stdout=subprocess.PIPE, stderr=errf,
        )
        failure: BaseException | None = None
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
                for member in tf:
                    tf.extract(member, path=root, filter="data")
        except (tarfile.TarError, OSError) as exc:
            failure = exc
        finally:
            if failure is not None or proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.kill()
            if proc.stdout is not None:
                proc.stdout.close()
            rc = proc.wait()
        if failure is None and rc != 0:
            errf.seek(0)
            failure = ExportError(errf.read().decode("utf-8", "replace").strip() or f"git archive exited {rc}")
        if failure is not None:
            raise ExportError(f"export of {sha40} failed: {failure}") from failure


def link(toplevel: str, root: Path, paths: tuple[str, ...]) -> None:
    """Link each <toplevel>/<path> to <root>/<path>: symlink, else a Windows junction. Raises ExportError."""
    for rel in paths:
        src = Path(toplevel) / rel
        dst = root / rel
        if not os.path.lexists(src):
            raise ExportError(f"--link target does not exist: {rel}")
        if os.path.lexists(dst):
            raise ExportError(f"--link path already exists in the export: {rel}")
        is_dir = os.path.isdir(src)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(src, dst, target_is_directory=is_dir)
            except OSError:
                if not (_IS_NT and is_dir):
                    raise
                from coordinator_core.install.junction import create_junction

                create_junction(dst, src)
        except OSError as exc:
            raise ExportError(f"cannot link {rel}: {exc}") from exc


def _is_link(path: str) -> bool:
    if os.path.islink(path):
        return True
    if _IS_NT:
        from coordinator_core.install.junction import is_junction

        return is_junction(path)
    return False


def _make_writable(path: str) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, os.stat(path).st_mode | 0o700)


def _retry_writable(fn, path: str) -> None:
    try:
        fn(path)
    except PermissionError:
        _make_writable(path)
        _make_writable(os.path.dirname(path))
        fn(path)


def _unlink_link(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)


def _remove_tree(root: Path) -> None:
    """Remove `root`: every symlink/junction as a link first, never descending into one."""
    dirs: list[str] = []
    for cur, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        keep = []
        for name in dirnames:
            p = os.path.join(cur, name)
            if _is_link(p):
                _unlink_link(p)
            else:
                keep.append(name)
                dirs.append(p)
        dirnames[:] = keep
        for name in filenames:
            p = os.path.join(cur, name)
            if _is_link(p):
                _unlink_link(p)
            else:
                _retry_writable(os.unlink, p)
    for d in reversed(dirs):
        _retry_writable(os.rmdir, d)
    _retry_writable(os.rmdir, str(root))


def remove(root: Path) -> bool:
    """Remove `root` and report whether it is absent afterwards."""
    try:
        _remove_tree(root)
    except OSError as exc:
        print(f"baseline-export: removal error under {root}: {exc}", file=sys.stderr)
    return not os.path.lexists(root)


class _Group:
    """The runner's process group (POSIX) or job object (Windows)."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._job = None

    def spawn(self, argv: tuple[str, ...], cwd: Path) -> subprocess.Popen:
        if not _IS_NT:
            self.proc = subprocess.Popen(list(argv), cwd=cwd, start_new_session=True)
            return self.proc
        from coordinator_core.diagnostics import contained_run as cr

        self._job = cr._create_kill_on_close_job()
        flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | cr.CREATE_SUSPENDED
        )
        try:
            self.proc = subprocess.Popen(list(argv), cwd=cwd, creationflags=flags)
        except OSError:
            self.close_job()
            raise
        try:
            cr._assign_to_job(self._job, int(self.proc._handle))
        finally:
            cr._resume_suspended_process(self.proc.pid)
        return self.proc

    def close_job(self) -> None:
        if self._job is not None:
            from coordinator_core.diagnostics import contained_run as cr

            cr._kernel32.CloseHandle(self._job)
            self._job = None

    def terminate(self) -> None:
        """Take down every process the runner started; returns when the group is empty."""
        proc = self.proc
        if proc is None:
            return
        if _IS_NT:
            self.close_job()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
            return
        pgid = proc.pid
        deadline = time.monotonic() + _TERM_GRACE_S
        sig = signal.SIGTERM
        while True:
            proc.poll()
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return
            except PermissionError:
                return
            if sig == signal.SIGKILL:
                if time.monotonic() >= deadline + 5.0:
                    return
                time.sleep(0.01)
            elif time.monotonic() >= deadline:
                sig = signal.SIGKILL
            else:
                time.sleep(0.02)


@contextlib.contextmanager
def _signals_blocked(sigs):
    """Defer delivery of `sigs` across a spawn so a child is never orphaned unrecorded."""
    if _IS_NT or not hasattr(signal, "pthread_sigmask"):
        yield
        return
    old = signal.pthread_sigmask(signal.SIG_BLOCK, sigs)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


def _handled_signals() -> list[int]:
    names = ("SIGBREAK", "SIGINT") if _IS_NT else ("SIGTERM", "SIGHUP", "SIGINT")
    return [getattr(signal, n) for n in names if hasattr(signal, n)]


def _finish(sha: str, export_state: str, why: str | None, runner_exit: int | None, removed: bool, code: int) -> int:
    sys.stdout.flush()
    sys.stderr.flush()
    print(render_result_line(sha, export_state, why, runner_exit, removed), flush=True)
    return code


def main(argv: list[str]) -> int:
    try:
        inv = parse_argv(list(argv))
    except UsageError as exc:
        print(f"baseline-export: {exc}\n{USAGE}", file=sys.stderr)
        return EXIT_USAGE

    cwd = os.path.realpath(os.getcwd())
    try:
        toplevel, sha40 = resolve(inv.sha, cwd)
    except ExportError as exc:
        print(f"baseline-export: {exc}", file=sys.stderr)
        return _finish(inv.sha, "failed", str(exc), None, True, EXIT_EXPORT_FAILED)

    root = export_root(toplevel, sha40, os.getpid())
    sigs = _handled_signals()
    previous: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        def _on_signal(signum, _frame):
            for s in sigs:
                signal.signal(s, signal.SIG_IGN)
            raise _Interrupted(signum)

        for s in sigs:
            previous[s] = signal.signal(s, _on_signal)

    owned = False
    group = _Group()
    export_state, why, runner_exit = "failed", None, None
    interrupted: int | None = None
    refused = False
    try:
        try:
            with _signals_blocked(sigs):
                root.parent.mkdir(parents=True, exist_ok=True)
                try:
                    root.mkdir()
                    owned = True
                except FileExistsError:
                    refused = True
            if refused:
                raise ExportError(f"export root already exists: {root}")
            export(toplevel, sha40, root)
            link(toplevel, root, inv.links)
            export_state = "ok"
            run_cwd = counterpart_cwd(toplevel, cwd, root)
            try:
                with _signals_blocked(sigs):
                    proc = group.spawn(inv.runner, run_cwd)
            except OSError as exc:
                why = f"runner spawn failed: {exc}"
            else:
                runner_exit = proc.wait()
        except ExportError as exc:
            why = str(exc)
            print(f"baseline-export: {why}", file=sys.stderr)
        except _Interrupted as exc:
            interrupted = exc.signum
    finally:
        for s in sigs:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(s, signal.SIG_IGN)
        group.terminate()
        removed = remove(root) if owned else True
        if interrupted is None and group.proc is not None and runner_exit is None:
            runner_exit = group.proc.poll()
        for s, handler in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(s, handler)

    if interrupted is not None:
        why = f"interrupted: {signal.Signals(interrupted).name}"
        if group.proc is not None:
            runner_exit = group.proc.poll()
    if not removed:
        print(f"baseline-export: export root not removed: {root}", file=sys.stderr)
        return _finish(sha40, export_state, why, runner_exit, False, EXIT_REMOVAL_FAILED)
    if export_state == "failed" and interrupted is None:
        return _finish(sha40, "failed", why, None, True, EXIT_EXPORT_FAILED)
    if interrupted is not None:
        return _finish(sha40, export_state, why, runner_exit, True, 128 + interrupted)
    return _finish(sha40, export_state, why, runner_exit, True, EXIT_OK)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
