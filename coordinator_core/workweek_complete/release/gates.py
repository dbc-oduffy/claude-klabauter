"""Gate run for workweek-complete: export one commit under scratch/, run the named gates there.

`run_gates` returns `GateRecord`s in `contract.GATE_IDS` order, each naming the commit it
tested. Spawns nothing itself: every child goes through `run_git` or the caller's `run`.
Never called from an engine op; the export and its removal are part of a test run.

`run(argv, *, cwd, env)` returns an object with `returncode` (and `stdout` for the probe);
`OSError` from it means the child could not start.

`d0` tests the installed plugin, not the commit: its record is a `GateEvidence` with
`bound_to_candidate=False` and names the plugin root and version. `d6` reads the commit's
tree from the live object store and checks each path component in this process; it never
reads the live `HEAD` or index.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from coordinator_core.git.run import run_git
from coordinator_core.workweek_complete.release.contract import GATE_IDS, GateRecord

_RELEASE_GATE_DIR = ("scratch", "release-gate")
_FAST_SCRIPT = ("coordinator", "bin", "validate-fast-and-packageability.py")
_CSN_MODULE = ("coordinator", "bin", "lib", "coordinator_safe_name.py")

# Tokens: {python} {export} {repo} {sha} {entrypoint}. `d6` is run through `run_git` and
# checked in process; `d0` is `node` on the installed plugin's runner.
GATE_ARGV: Dict[str, Tuple[str, ...]] = {
    "d6": ("-C", "{repo}", "ls-tree", "-r", "-z", "--name-only", "{sha}"),
    "d0": ("node", "--test", "{entrypoint}"),
    "validate-fast-and-packageability": (
        "{python}",
        "{export}/" + "/".join(_FAST_SCRIPT),
        "fast",
        "--repo-root",
        "{export}",
    ),
}

# Tokens: {export} {export_parent}. The ceiling stops git walking from the export up to the
# live repo; the engine root pins bin scripts to the export.
GATE_ENV: Dict[str, str] = {
    "GIT_CEILING_DIRECTORIES": "{export_parent}",
    "COORDINATOR_ENGINE_ROOT": "{export}",
    "PYTHONDONTWRITEBYTECODE": "1",
}
GATE_ENV_UNSET: Tuple[str, ...] = (
    "PYTHONPATH",
    "COORDINATOR_FAST_TEST_CMD",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
)

_PROBE_SOURCE = "import coordinator_core; print(coordinator_core.__file__)"


@dataclass(frozen=True)
class GateEvidence(GateRecord):
    """A `GateRecord` that says whether it is bound to the tested commit, and what it ran."""

    bound_to_candidate: bool = True
    detail: str = ""


class GateRun(tuple):
    """The records of one run; `leftover` names an export directory that could not be removed."""

    leftover: str

    def __new__(cls, records: Sequence[GateRecord] = (), leftover: str = "") -> "GateRun":
        obj = super().__new__(cls, records)
        obj.leftover = leftover
        return obj

    def bound(self) -> Tuple[GateRecord, ...]:
        return tuple(r for r in self if getattr(r, "bound_to_candidate", True))

    def unbound(self) -> Tuple[GateRecord, ...]:
        return tuple(r for r in self if not getattr(r, "bound_to_candidate", True))

    def summary(self) -> str:
        def line(r: GateRecord) -> str:
            return f"{r.gate}: {r.status} exit={r.exit_code}"

        out = ["bound to candidate:"] + [f"  {line(r)}" for r in self.bound()]
        out += ["NOT bound to candidate:"] + [
            f"  {line(r)} {getattr(r, 'detail', '')}" for r in self.unbound()
        ]
        if self.leftover:
            out.append(f"leftover export: {self.leftover}")
        return "\n".join(out)


def _clear_readonly_and_retry(func: Callable[[str], Any], path: str, _exc: Any) -> None:
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    func(path)


def _remove_tree(path: Path) -> str:
    """Remove `path`, clearing read-only bits; return `""` or the path if it survived."""
    if not os.path.lexists(path):
        return ""
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_clear_readonly_and_retry)
        else:
            shutil.rmtree(path, onerror=_clear_readonly_and_retry)
    except OSError:
        pass
    return str(path) if os.path.lexists(path) else ""


def _gate_root(repo_root: Path) -> Path:
    return Path(repo_root).resolve() / _RELEASE_GATE_DIR[0] / _RELEASE_GATE_DIR[1]


def export_commit(repo_root: Path, sha: str, *, run_git: Callable[..., Any] = run_git) -> Path:
    """Unpack the files of commit `sha` into `<repo_root>/scratch/release-gate/<sha>/`.

    Sweeps every directory under `release-gate/` first. Raises if `git archive` fails, if
    the destination is not under `scratch/`, or if nothing can be unpacked; nothing is left.
    """
    if not sha or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError(f"not a full commit sha: {sha!r}")
    repo = Path(repo_root).resolve()
    gate_root = _gate_root(repo)
    dest = (gate_root / sha).resolve()
    if not dest.is_relative_to(repo / "scratch"):
        raise ValueError(f"export destination {dest} is not under {repo / 'scratch'}")
    if gate_root.is_dir():
        for child in gate_root.iterdir():
            _remove_tree(child)
    result = run_git(
        ["-C", str(repo), "archive", "--format=tar", sha], binary=True
    )
    if result.returncode != 0 or getattr(result, "timed_out", False):
        why = "timed out" if getattr(result, "timed_out", False) else result.stderr.strip()
        raise RuntimeError(f"git archive {sha} failed (rc={result.returncode}): {why}")
    dest.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(result.stdout_bytes), mode="r:") as tar:
            tar.extractall(dest, filter="data")
    except BaseException:
        _remove_tree(dest)
        raise
    return dest


def _expand(tokens: Sequence[str], **values: str) -> list[str]:
    return [sys.executable if t == "{python}" else t.format(**values) for t in tokens]


def _gate_env(export: Path) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in GATE_ENV_UNSET}
    fields = {"export": str(export), "export_parent": str(export.parent)}
    env.update({k: v.format(**fields) for k, v in GATE_ENV.items()})
    return env


def _inside(path: str, root: Path) -> bool:
    try:
        return Path(os.path.normcase(os.path.realpath(path))).is_relative_to(
            Path(os.path.normcase(os.path.realpath(root)))
        )
    except (OSError, ValueError):
        return False


def _probe_ok(export: Path, env: Dict[str, str], run: Callable[..., Any]) -> bool:
    try:
        out = run([sys.executable, "-c", _PROBE_SOURCE], cwd=str(export), env=env)
    except OSError:
        return False
    if getattr(out, "returncode", 1) != 0:
        return False
    reported = (getattr(out, "stdout", "") or "").strip()
    return bool(reported) and _inside(reported, export)


def _load_csn_check(export: Path) -> Callable[[str], Tuple[bool, Any]]:
    path = export.joinpath(*_CSN_MODULE)
    spec = importlib.util.spec_from_file_location("_release_gate_csn", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.csn_check


def _run_d6(repo: Path, sha: str, export: Path, run_git: Callable[..., Any]) -> GateRecord:
    argv = _expand(GATE_ARGV["d6"], repo=str(repo), sha=sha)
    tree = run_git(argv, binary=True)
    if tree.returncode != 0 or getattr(tree, "timed_out", False):
        return GateRecord("d6", "not-started", None, sha)
    try:
        csn_check = _load_csn_check(export)
    except Exception:
        return GateRecord("d6", "not-started", None, sha)
    raw = tree.stdout_bytes.decode("utf-8", errors="surrogateescape")
    illegal = 0
    for path in (p for p in raw.split("\0") if p):
        for component in path.split("/"):
            if component and not csn_check(component)[0]:
                illegal += 1
    return GateRecord("d6", "ran", 1 if illegal else 0, sha)


def _plugin_version(plugin_root: Path) -> str:
    for base in (plugin_root, plugin_root.parent):
        manifest = base / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            try:
                return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "unknown"))
            except (OSError, ValueError):
                return "unreadable"
    return "unknown"


def _run_d0(
    sha: str, export: Path, env: Dict[str, str], run: Callable[..., Any]
) -> GateEvidence:
    from coordinator_core.merge_assemble import node_ceremony_gate_entrypoint
    from coordinator_core.subagent_sandbox.provision_report import resolve_plugin_root

    unbound = "NOT bound to the candidate commit: tests the installed plugin"
    root = resolve_plugin_root()
    if root is None:
        return GateEvidence("d0", "not-present", None, sha, False, f"{unbound}; plugin root unresolvable")
    entry = node_ceremony_gate_entrypoint(Path(root))
    named = f"plugin_root={root} version={_plugin_version(Path(root))}; {unbound}"
    if not entry.is_file():
        return GateEvidence("d0", "not-present", None, sha, False, f"{named}; runner absent {entry}")
    argv = _expand(GATE_ARGV["d0"], entrypoint=str(entry))
    node = shutil.which("node")
    if node is None:
        return GateEvidence("d0", "not-started", None, sha, False, f"{named}; node not on PATH")
    argv[0] = node
    try:
        out = run(argv, cwd=str(export), env=env)
    except OSError:
        return GateEvidence("d0", "not-started", None, sha, False, named)
    return GateEvidence("d0", "ran", out.returncode, sha, False, named)


def _run_fast(
    sha: str, export: Path, env: Dict[str, str], run: Callable[..., Any]
) -> GateRecord:
    argv = _expand(GATE_ARGV["validate-fast-and-packageability"], export=str(export))
    try:
        out = run(argv, cwd=str(export), env=env)
    except OSError:
        return GateRecord("validate-fast-and-packageability", "not-started", None, sha)
    return GateRecord("validate-fast-and-packageability", "ran", out.returncode, sha)


def _green(record: GateRecord) -> bool:
    return record.status == "not-present" or (record.status == "ran" and record.exit_code == 0)


def run_gates(
    repo_root: Path,
    sha: str,
    *,
    run: Callable[..., Any],
    run_git: Callable[..., Any] = run_git,
) -> GateRun:
    """Export `sha`, probe the export, start each gate in `GATE_IDS` order; stop at the first not green."""
    repo = Path(repo_root).resolve()
    export = export_commit(repo, sha, run_git=run_git)
    records: list[GateRecord] = []
    try:
        env = _gate_env(export)
        if not _probe_ok(export, env, run):
            records.append(GateRecord(GATE_IDS[0], "not-started", None, sha))
        else:
            starters = {
                "d6": lambda: _run_d6(repo, sha, export, run_git),
                "d0": lambda: _run_d0(sha, export, env, run),
                "validate-fast-and-packageability": lambda: _run_fast(sha, export, env, run),
            }
            for gate in GATE_IDS:
                record = starters[gate]()
                records.append(record)
                if not _green(record):
                    break
    finally:
        leftover = _remove_tree(export)
    return GateRun(records, leftover)
