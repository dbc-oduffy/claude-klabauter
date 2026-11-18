"""Cross-process reuse probe: what a second publish process of one commit repeats.

Builds a small throwaway git repo (two packages, a few importers), then runs two child
processes in sequence over its one commit. Each child loads `coordinator/bin/publish.py`, counts
`_extract_git_archive` calls and `import_closure._attr_importer_facts` calls, runs
`_git_materialize_ref` and `find_commit_module_attr_violations` against a gate-fact cache dir the
children share, and reports {extractions, importer_parses, process_ms}. The children's temp root
is a private directory, so a shadow store the engine keeps under it never reaches the real Temp.

    python coordinator_core/benchmarks/publish_cross_process_reuse_probe.py --json
    python coordinator_core/benchmarks/publish_cross_process_reuse_probe.py --kill-first-after 3 --json

`--kill-first-after N` makes the first child exit, without flushing, after N importer parses (the
interrupted-pass leg). `--expect-reuse` exits 1 unless the second child shows extractions=0 and
importer_parses=0. process_ms is the child's process time plus descendants (git archive included);
wall clock is never recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402

EXIT_OK = 0
EXIT_REUSE_MISSING = 1
EXIT_SETUP = 2
DEFAULT_IMPORTERS = 6
RESULT_PREFIX = "PROBE_RESULT "
_WORK_DIR = _REPO / "scratch" / "publish-cross-process-reuse-probe"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SetupError(RuntimeError):
    """The throwaway repo or a child process could not be set up."""


def _rmtree(path: Path) -> None:
    def retry(func, p, _exc) -> None:
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.autocrlf=false", "-c", "user.name=probe",
         "-c", "user.email=probe@example.invalid", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        raise SetupError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def build_repo(repo: Path, importers: int) -> str:
    """A one-commit repo: packages `pkg_a` and `pkg_b` under `coordinator_core`, `importers` distinct
    importer files reading attributes of both. Returns the commit sha."""
    files: dict[str, str] = {
        "coordinator_core/__init__.py": "",
        "coordinator_core/consumers/__init__.py": "",
    }
    for pkg in ("a", "b"):
        files[f"coordinator_core/pkg_{pkg}/__init__.py"] = ""
        files[f"coordinator_core/pkg_{pkg}/mod_{pkg}.py"] = f"VALUE = '{pkg}'\n\n\ndef run():\n    return VALUE\n"
    for i in range(importers):
        files[f"coordinator_core/consumers/use_{i}.py"] = (
            "from coordinator_core.pkg_a import mod_a\n"
            "from coordinator_core.pkg_b import mod_b\n\n\n"
            f"def use_{i}():\n    return mod_a.run(), mod_b.VALUE, {i}\n"
        )
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "probe commit")
    return _git(repo, "rev-parse", "HEAD")


def _child(repo: Path, sha: str, cache_dir: Path, kill_after: Optional[int]) -> int:
    """One publish-shaped process over `sha`; prints a `PROBE_RESULT` line."""
    from coordinator_core.benchmarks.publish_end_of_run_gates_profile import _Meter, load_publish_module
    from coordinator_core.percolate import gate_result_cache as grc

    declare_benchmark_origin()
    publish = load_publish_module()
    closure = sys.modules[publish.find_commit_module_attr_violations.__module__]
    counts = {"extractions": 0, "importer_parses": 0}
    meter = _Meter()
    started = meter.now_ms()

    def result() -> dict[str, Any]:
        return {**counts, "process_ms": round(meter.now_ms() - started, 1), "instrument": meter.instrument}

    real_extract = getattr(publish, "_extract_git_archive")
    real_facts = getattr(closure, "_attr_importer_facts")

    def counted_extract(*args: Any, **kwargs: Any) -> Any:
        counts["extractions"] += 1
        return real_extract(*args, **kwargs)

    def counted_facts(*args: Any, **kwargs: Any) -> Any:
        counts["importer_parses"] += 1
        facts = real_facts(*args, **kwargs)
        if kill_after is not None and counts["importer_parses"] >= kill_after:
            print(RESULT_PREFIX + json.dumps({**result(), "killed": True}), flush=True)
            os._exit(0)
        return facts

    setattr(publish, "_extract_git_archive", counted_extract)
    setattr(closure, "_attr_importer_facts", counted_facts)

    shadow = publish._git_materialize_ref(repo, sha)
    assert closure.__file__ is not None
    cache = grc.GateResultCache(cache_dir, "probe-module-attr", grc.gate_version_of(closure.__file__))
    examined, violations = publish.find_commit_module_attr_violations(repo, sha, cache=cache)
    cache.flush()
    publish._cleanup_shadow_roots((shadow,))
    print(RESULT_PREFIX + json.dumps({**result(), "examined": examined, "violations": len(violations),
                                      "killed": False}), flush=True)
    return EXIT_OK


def run_child(work: Path, repo: Path, sha: str, kill_after: Optional[int]) -> dict[str, Any]:
    temp = work / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TMP": str(temp), "TEMP": str(temp), "TMPDIR": str(temp)}
    cmd = [sys.executable, str(Path(__file__).resolve()), "--child", "--repo", str(repo), "--sha", sha,
           "--cache-dir", str(work / "gate-cache")]
    if kill_after is not None:
        cmd += ["--kill-first-after", str(kill_after)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        env=env, creationflags=_NO_WINDOW,
    )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            return json.loads(line[len(RESULT_PREFIX):])
    raise SetupError(f"child exit {proc.returncode} printed no result; stderr tail: {proc.stderr.strip()[-600:]}")


def run(importers: int, kill_first_after: Optional[int], work: Path, keep_work_dir: bool) -> dict[str, Any]:
    declare_benchmark_origin()
    if work.exists():
        _rmtree(work)
    work.mkdir(parents=True)
    try:
        repo = work / "repo"
        sha = build_repo(repo, importers)
        first = run_child(work, repo, sha, kill_first_after)
        second = run_child(work, repo, sha, None)
        return {"sha": sha, "importers": importers, "kill_first_after": kill_first_after,
                "first": first, "second": second}
    finally:
        if not keep_work_dir:
            _rmtree(work)


def render(report: dict[str, Any]) -> str:
    lines = [f"commit {report['sha'][:10]}, {report['importers']} importers, "
             f"kill_first_after={report['kill_first_after']}"]
    for name in ("first", "second"):
        child = report[name]
        lines.append(f"  {name:<7} extractions={child['extractions']} importer_parses={child['importer_parses']} "
                     f"process_ms={child['process_ms']}{' (killed)' if child.get('killed') else ''}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print both children as JSON")
    parser.add_argument("--kill-first-after", type=int, default=None, metavar="N",
                        help="first child exits unflushed after N importer parses")
    parser.add_argument("--importers", type=int, default=DEFAULT_IMPORTERS)
    parser.add_argument("--work-dir", type=Path, default=_WORK_DIR)
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument("--expect-reuse", action="store_true",
                        help="exit 1 unless the second child shows extractions=0 and importer_parses=0")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--repo", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--sha", help=argparse.SUPPRESS)
    parser.add_argument("--cache-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return _child(args.repo, args.sha, args.cache_dir, args.kill_first_after)
    try:
        report = run(max(1, args.importers), args.kill_first_after, args.work_dir, args.keep_work_dir)
    except SetupError as exc:
        print(f"publish_cross_process_reuse_probe: {exc}", file=sys.stderr)
        return EXIT_SETUP
    print(json.dumps(report, indent=2) if args.json else render(report))
    second = report["second"]
    reused = second["extractions"] == 0 and second["importer_parses"] == 0
    return EXIT_REUSE_MISSING if args.expect_reuse and not reused else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
