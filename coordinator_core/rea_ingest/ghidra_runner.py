"""Drives Ghidra's analyzeHeadless over a caller-held binary and records the program facts.

Purpose: copy the bytes into scratch under a sha256-derived name, run the ExportProgramFacts
post-script in a persistent Ghidra project, read the export and hand it to
``program_facts.record`` (the only path to the ledger). Ghidra analyses statically; the target
is never spawned. Permissive by design (PM revision R3): the project persists across runs, no
confinement unless ``confine`` is set, and a timeout bounds the child.

Invariants:
    - The copy's name derives from the content hash, so ``program.name`` in the export and the
      program's key in the persistent project are stable across runs.
    - ``-import`` when the project lacks the program, ``-process`` when it holds it; the
      project is never deleted.
    - The only spawn is ``_spawn_headless`` (named in docs/reference/shell-out-carve-outs.md).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.rea_ingest import program_facts

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent / "ghidra"
SCRIPT_NAME = "ExportProgramFacts.java"
PROJECT_NAME = "rea-ingest"
# Floor, not a pin: the PM ruled the runner permissive, and the export script uses only
# long-stable FlatProgramAPI calls.
MIN_MAJOR = 11
DEFAULT_TIMEOUT_S = 1800
_EXPORT_SCHEMA = "ghidra-program-facts/v1"
_TAIL = 2000


class GhidraRefusal(RuntimeError):
    """The run cannot start or finish: missing/unsupported Ghidra, failed child, bad export."""


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_install(ghidra: Optional[str], env: Optional[dict] = None) -> Path:
    """Return the Ghidra install dir from the argument or GHIDRA_INSTALL_DIR, or refuse."""
    env = os.environ if env is None else env
    raw = ghidra or env.get("GHIDRA_INSTALL_DIR")
    if not raw:
        raise GhidraRefusal("Ghidra install not given: set GHIDRA_INSTALL_DIR or pass --ghidra DIR")
    root = Path(raw)
    if not root.is_dir():
        raise GhidraRefusal(f"GHIDRA_INSTALL_DIR is not a directory: {root}")
    return root


def read_version(install: Path) -> str:
    """Return ``application.version`` from the install, refusing a major below ``MIN_MAJOR``."""
    props = install / "Ghidra" / "application.properties"
    version = ""
    try:
        for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "application.version":
                version = value.strip()
                break
    except OSError as exc:
        raise GhidraRefusal(f"cannot read {props}: {exc}") from exc
    major = version.split(".", 1)[0]
    if not major.isdigit() or int(major) < MIN_MAJOR:
        raise GhidraRefusal(f"Ghidra {version or 'of unknown version'} unsupported: need {MIN_MAJOR}.x or later")
    return version


def analyze_headless_path(install: Path, windows: Optional[bool] = None) -> Path:
    """Return the analyzeHeadless launcher (``.bat`` on Windows), refusing when absent."""
    windows = (sys.platform == "win32") if windows is None else windows
    exe = install / "support" / ("analyzeHeadless.bat" if windows else "analyzeHeadless")
    if not exe.is_file():
        raise GhidraRefusal(f"analyzeHeadless not found at {exe}; check GHIDRA_INSTALL_DIR")
    return exe


def stage_binary(binary: Path, scratch: Path) -> tuple[Path, str]:
    """Copy the caller's bytes to ``scratch/prog-<sha16>`` and return (copy, sha256)."""
    if not binary.is_file():
        raise GhidraRefusal(f"binary not found: {binary}")
    sha = _hash_file(binary)
    scratch.mkdir(parents=True, exist_ok=True)
    copy = scratch / f"prog-{sha[:16]}"
    if not copy.is_file() or _hash_file(copy) != sha:
        shutil.copyfile(binary, copy)
    return copy, sha


def project_holds(project_dir: Path, name: str) -> bool:
    """True when the persistent project already stores a program called ``name``."""
    idata = project_dir / f"{PROJECT_NAME}.rep" / "idata"
    if not idata.is_dir():
        return False
    needle = name.encode("utf-8")
    for prp in idata.rglob("*.prp"):
        try:
            if needle in prp.read_bytes():
                return True
        except OSError:
            continue
    return False


def build_argv(exe: Path, project_dir: Path, copy: Path, out: Path, holds: bool) -> list[str]:
    """Return the analyzeHeadless argv; the target is only ever an -import file argument."""
    load = ["-process", copy.name, "-noanalysis"] if holds else ["-import", str(copy)]
    return [
        str(exe), str(project_dir), PROJECT_NAME, *load,
        "-scriptPath", str(SCRIPT_DIR),
        "-postScript", SCRIPT_NAME, str(out),
    ]


class _KillOnCloseJob:
    """Windows Job Object with KILL_ON_JOB_CLOSE: closing it ends the whole process tree."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class _Extended(ctypes.Structure):
            _fields_ = [("Basic", _Basic), ("Io", _Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        self._handle = k32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = _Extended()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, proc: subprocess.Popen) -> None:
        if not self._k32.AssignProcessToJobObject(self._handle, int(proc._handle)):  # type: ignore[attr-defined]
            raise OSError("AssignProcessToJobObject failed")

    def close(self) -> None:
        if self._handle:
            self._k32.CloseHandle(self._handle)
            self._handle = None


def _spawn_headless(
    argv: list[str],
    timeout: float,
    confine: bool = False,
    *,
    popen: Callable = subprocess.Popen,
    windows: Optional[bool] = None,
    job_factory: Callable = _KillOnCloseJob,
    killpg: Callable = os.killpg,
) -> tuple[int, str]:
    """Run analyzeHeadless and return (returncode, output tail). The sole spawn site.

    ``confine`` adds a Windows kill-on-close Job Object or a POSIX new session whose group is
    killed on timeout; without it the child runs plainly under the timeout.
    """
    windows = (sys.platform == "win32") if windows is None else windows
    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True, "errors": "replace"}
    if windows:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    elif confine:
        kwargs["start_new_session"] = True
    job = None
    proc = popen(argv, **kwargs)
    try:
        if windows and confine:
            job = job_factory()
            job.assign(proc)
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if confine and not windows:
                killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
            proc.communicate()
            raise GhidraRefusal(f"analyzeHeadless exceeded {timeout}s timeout") from None
    finally:
        if job is not None:
            job.close()
    return proc.returncode, (output or "")[-_TAIL:]


def ingest(
    binary: Path,
    *,
    ghidra: Optional[str] = None,
    project_dir: Optional[Path] = None,
    confine: bool = False,
    timeout: float = DEFAULT_TIMEOUT_S,
    observed_at: Optional[str] = None,
    repo_root: Path = REPO_ROOT,
    scratch: Optional[Path] = None,
    spawn: Callable = _spawn_headless,
    env: Optional[dict] = None,
) -> dict:
    """Export the binary's program facts with Ghidra and record them; return a report dict."""
    install = resolve_install(ghidra, env)
    version = read_version(install)
    exe = analyze_headless_path(install)
    scratch = scratch or repo_root / "scratch" / "ghidra_ingest"
    project_dir = project_dir or scratch / "projects"
    copy, sha = stage_binary(Path(binary), scratch)
    project_dir.mkdir(parents=True, exist_ok=True)
    out = scratch / f"export-{sha[:16]}.json"
    out.unlink(missing_ok=True)
    holds = project_holds(project_dir, copy.name)
    argv = build_argv(exe, project_dir, copy, out, holds)
    rc, tail = spawn(argv, timeout, confine)
    if rc != 0:
        raise GhidraRefusal(f"analyzeHeadless exited {rc}: {tail}")
    if not out.is_file():
        raise GhidraRefusal(f"analyzeHeadless wrote no export at {out}: {tail}")
    try:
        export = json.loads(out.read_text(encoding="utf-8"))
        if export.get("schema_version") != _EXPORT_SCHEMA:
            raise GhidraRefusal(f"export schema_version {export.get('schema_version')!r} is not {_EXPORT_SCHEMA}")
        if export["program"]["sha256"] != sha:
            raise GhidraRefusal("export program.sha256 differs from the staged bytes")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GhidraRefusal(f"export at {out} is malformed: {exc!r}") from exc
    written = program_facts.record(export, repo_root, observed_at)
    return {
        "ghidra_version": version,
        "sha256": sha,
        "mode": "process" if holds else "import",
        "export": str(out),
        "project_dir": str(project_dir),
        "counts": {k: len(export[k]) for k in ("functions", "xrefs", "imports", "strings")},
        "written": written,
    }
