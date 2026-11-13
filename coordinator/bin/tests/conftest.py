from __future__ import annotations

import os
import importlib.util
import subprocess
import importlib.util
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_stamp_probe import (  # noqa: E402  (import after path setup)
    _ENGINE_ROOT_VAR,
    _stamped_dispatch_root,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


@pytest.fixture
def stamped_engine_env(monkeypatch) -> str:
    root = _stamped_dispatch_root()
    if root is None:
        pytest.skip(
            f"no stamped engine on this box ({_ENGINE_ROOT_VAR} unresolvable or "
            "the resolved root carries no build stamp) — a real dispatch cannot "
            "be exercised here"
        )
    monkeypatch.setenv(_ENGINE_ROOT_VAR, root)
    return root


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "fold_bar_real: exercise the real fold bar; the green-verdict stub skips this test"
    )


def _published_module_absent(name):
    parts = name.split(".")
    if any(sys.modules.get(".".join(parts[:i]), False) is None for i in range(1, len(parts) + 1)):
        return True
    try:
        return importlib.util.find_spec(name) is None
    except ModuleNotFoundError as exc:
        if exc.name == name or name.startswith((exc.name or "") + "."):
            return True
        raise


@pytest.fixture(autouse=True)
def _isolate_settings_home(request, tmp_path_factory):
    """Ships with these tests so every checkout and mirror isolates them; overrides the repo-root
    fixture of the same name. Body: unimatrix_test_home.isolated_settings_home. Opt out with
    @pytest.mark.real_home."""
    if request.node.get_closest_marker("real_home"):
        yield
        return
    from unimatrix_test_home import isolated_settings_home

    yield from isolated_settings_home(tmp_path_factory)


@pytest.fixture(autouse=True)
def _fail_on_live_registry_write(request, _isolate_settings_home):
    """Overrides coordinator/bin/conftest.py's tripwire of the same name only to pull the
    isolation up before it: the tripwire must snapshot and compare the isolated home, and the
    shared `monkeypatch` (set up after it) must unwind its settings-home edits before isolation
    undoes."""
    from coordinator_core.testing.registry_sandbox import fail_on_live_registry_write_fixture

    yield from fail_on_live_registry_write_fixture(request)


_REAL_SETTINGS_HOME = Path.home() / ".coordinator-claude-settings"  # fixed at import: a test may re-point HOME


def _live_unimatrix_pair_state():
    """The real home's `unimatrix.keys.*` registry lines and `.secrets/ssh` file names."""
    live = _REAL_SETTINGS_HOME
    reg = live / "machine-local" / "registry.local.toml"
    try:
        lines = sorted(l for l in reg.read_text(encoding="utf-8").splitlines() if l.lstrip().startswith("unimatrix.keys."))
    except OSError:
        lines = []
    ssh = live / ".secrets" / "ssh"
    return lines, sorted(p.name for p in ssh.iterdir()) if ssh.is_dir() else []


@pytest.fixture(autouse=True)
def _unimatrix_tests_never_write_a_real_pair_key(request, _isolate_settings_home):
    """A test that records a `unimatrix.keys.*` entry or mints under the real settings home
    leaves a stray key every later `request` could adopt; fail the test that did it.
    A concurrent real-box writer during the test also trips this; rerun before suspecting the test."""
    if request.node.get_closest_marker("real_home"):
        yield
        return
    before = _live_unimatrix_pair_state()
    yield
    after = _live_unimatrix_pair_state()
    assert after == before, f"{request.node.nodeid} changed the real unimatrix pair-key state: {before} -> {after}"


_DESKTOP_NOTIFIERS = frozenset({"osascript", "notify-send"})


@pytest.fixture(autouse=True)
def _no_desktop_notifier(monkeypatch):
    """A test never raises a real desktop notification: unimatrix-membership's default notifier
    shells to osascript, and a test that forgets its stub pops a "join the unimatrix" alert on the
    PM's screen every run. A test that sets its own `shutil.which` stub still wins."""
    import shutil

    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: None if os.path.basename(str(cmd)) in _DESKTOP_NOTIFIERS else real_which(cmd, *a, **k))


class _ChildReaper:
    """Owns every process a test spawns through `subprocess.Popen` and kills the tree at teardown.
    Windows: one kill-on-close Job object per test. POSIX: each child leads its own process group,
    killed whole. A child that detaches itself (double fork, CREATE_BREAKAWAY) escapes."""

    def __init__(self) -> None:
        self._procs: list = []
        self._job = None

    def _windows_job(self):
        if self._job is None:
            import ctypes
            from ctypes import wintypes

            class _Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class _Extended(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _Basic),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = k32.CreateJobObjectW(None, None)
            info = _Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not handle or not k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return None
            self._job = (k32, handle)
        return self._job

    def adopt(self, proc) -> None:
        self._procs.append(proc)
        if sys.platform == "win32":
            job = self._windows_job()
            if job is not None:
                job[0].AssignProcessToJobObject(job[1], int(proc._handle))

    def reap(self) -> None:
        if sys.platform == "win32":
            if self._job is not None:
                k32, handle = self._job
                k32.TerminateJobObject(handle, 1)
                k32.CloseHandle(handle)
                self._job = None
        else:
            import signal

            for proc in self._procs:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        for proc in self._procs:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        self._procs.clear()

    def install(self, monkeypatch) -> None:
        reaper = self
        real_popen = subprocess.Popen

        class _Reaped(real_popen):
            def __init__(self, *args, **kwargs):
                # `is not None`: an explicit start_new_session=False is the caller's choice and is kept.
                if sys.platform != "win32" and not any(
                    kwargs.get(k) is not None for k in ("start_new_session", "process_group", "preexec_fn")
                ):
                    kwargs["start_new_session"] = True
                super().__init__(*args, **kwargs)
                reaper.adopt(self)

        monkeypatch.setattr(subprocess, "Popen", _Reaped)


@pytest.fixture(autouse=True)
def _reap_test_spawned_children(monkeypatch):
    """A process a test starts (a unimatrix bridge, an autolink) never outlives the test."""
    reaper = _ChildReaper()
    reaper.install(monkeypatch)
    yield
    reaper.reap()


def _published_module_absent(name):
    parts = name.split(".")
    if any(sys.modules.get(".".join(parts[:i]), False) is None for i in range(1, len(parts) + 1)):
        return True
    try:
        return importlib.util.find_spec(name) is None
    except ModuleNotFoundError as exc:
        if exc.name == name or name.startswith((exc.name or "") + "."):
            return True
        raise


@pytest.fixture(autouse=True)
def _stub_fold_bar_verdict(request, monkeypatch):
    """Round and publish tests drive fake trees with no recorded suite verdict. A module sets
    REAL_FOLD_BAR = True, or a test carries the fold_bar_real marker, to exercise the real fold bar."""
    if getattr(request.module, "REAL_FOLD_BAR", False) or request.node.get_closest_marker("fold_bar_real"):
        return
    if _published_module_absent("coordinator_core.ops.fold_bar"):
        return
    try:
        from coordinator_core.ops import fold_bar
    except ImportError:
        if _published_module_absent("coordinator_core.ops.fold_bar"):
            return
        raise

    monkeypatch.setattr(fold_bar, "verdict", lambda sha, repo_root: fold_bar.FoldBarResult(True, sha))

    # Fixture source dirs carry no commit; the shipped-sha rev-parse in _resolve_shipped_sha would
    # refuse before the stubbed verdict runs. Only that one call shape falls back to a fixed sha
    # (every such rev-parse in these tests, not just the fold bar's).
    from coordinator_core.git import run as git_run

    real_git_out = git_run.git_out

    def _git_out_with_shipped_sha_fallback(args, **kwargs):
        argv = list(args)
        if argv[:3] == ["rev-parse", "--verify", "--quiet"] and argv[-1:] and argv[-1].endswith("^{commit}"):
            return "0" * 40
        return real_git_out(args, **kwargs)

    monkeypatch.setattr(git_run, "git_out", _git_out_with_shipped_sha_fallback)


@pytest.fixture(autouse=True)
def _stub_source_preflight(request, monkeypatch):
    """The round's source-published gate runs real git and a real push; the
    round tests fake the source dir. A module sets REAL_SOURCE_PREFLIGHT = True
    to exercise the real gate."""
    if getattr(request.module, "REAL_SOURCE_PREFLIGHT", False):
        return
    if _published_module_absent("coordinator_core.percolate.source_preflight"):
        return
    try:
        from coordinator_core.percolate import source_preflight
    except ImportError:
        if _published_module_absent("coordinator_core.percolate.source_preflight"):
            return
        raise

    monkeypatch.setattr(source_preflight, "check_source_published", lambda _source_dir, *_a, **_k: None)


_SCRIPT = Path(__file__).resolve().parents[1] / "plan-spine-check.py"
_SIZING = "state/sizings/s.yaml"

_SIZING_YAML = """\
schema: sizing-object
name: s
requirement_register:
  sources:
  - doc: state/sizings/s.yaml
    ref: ask
  rows:
  - id: R1
    source_text: Do the thing.
    anchor: relayed
    surface: internal
    status: open
    claimed_by: []
  rollup:
    open: 1
    partial: 0
"""

_ROW = """\
- id: C1
  title: do the thing
  change_kind: script-edit
  surface: a.py
  writes: [a.py]
  reads_at_head: []
  consumes: []
  model: {model}
  effort: medium
  agent_type: {agent_type}
  register_rows: {register_rows}
  traces_to_brief: "do the thing"
  acceptance: "a.py does the thing"
  queue_scope: project
  disposition: open
{body}"""

_CLEAN_BODY = "  body: |\n    Edit a.py so that it parses the register and prints each row id.\n"


def _plan(
    root: Path,
    *,
    body: str = _CLEAN_BODY,
    register_rows: str = "[R1]",
    agent_type: str = "coordinator:executor",
    model: str = "sonnet",
    anchors: str = "anchor_premises: []\n",
    claims: bool = True,
) -> Path:
    (root / ".git").mkdir(exist_ok=True)
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / "state" / "sizings" / "s.yaml").write_text(_SIZING_YAML, encoding="utf-8")
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    claim_block = f'register_claims:\n  sizing: "{_SIZING}"\n  rows: ["R1"]\n' if claims else ""
    row = _ROW.format(agent_type=agent_type, model=model, register_rows=register_rows, body=body)
    text = (
        "---\ntitle: t\nstatus: executing\n"
        f'sizing_object: "{_SIZING}"\n{claim_block}{anchors}---\n\n# t\n\n## Tasks\n\n'
        f"```yaml plan-tasks\n{row}```\n"
    )
    plan = root / "docs" / "plans" / "p.md"
    plan.write_text(text, encoding="utf-8")
    return plan


def _run(plan: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *extra, str(plan)],
        capture_output=True,
        text=True,
        cwd=plan.parents[2],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def spine_script() -> Path:
    """Path to plan-spine-check.py."""
    return _SCRIPT


@pytest.fixture
def make_plan():
    """Build a one-row plan under a root; keyword args vary the row (agent_type, model, body, ...)."""
    return _plan


@pytest.fixture
def run_spine_check():
    """Run plan-spine-check.py on a plan path with optional extra CLI args; returns CompletedProcess."""
    return _run
