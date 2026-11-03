"""Profile baseline_export across N sequential calls for one sha: archive spawns, bytes extracted, process time.

Runs `baseline_export.main` in-process against a throwaway fixture repo (or `--repo`) with a
no-op runner. Prints one JSON line per call plus a summary line. Process time is
os.times() self+children CPU, never wall clock.
Runs as `python coordinator_core/benchmarks/baseline_export_cache_profile.py --calls 3`.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[2]))

from coordinator_core.git.run import run_git  # noqa: E402
from coordinator_core.testing import baseline_export  # noqa: E402

_FILES = 300
_FILE_BYTES = 17_000


def _git(repo: Path, *args: str) -> str:
    done = run_git(
        ["-c", "user.name=profile", "-c", "user.email=profile@example.invalid", *args],
        cwd=str(repo),
    )
    if not done.ok:
        raise RuntimeError(f"git {' '.join(args)} failed ({done.returncode}): {done.stderr.strip()}")
    return done.stdout.strip()


def build_fixture(repo: Path) -> str:
    """A repo of ~300 incompressible files (~5 MB) in one commit; returns the commit sha."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    for i in range(_FILES):
        d = repo / "pkg" / f"d{i % 12}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"f{i}.bin").write_bytes(os.urandom(_FILE_BYTES))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return _git(repo, "rev-parse", "HEAD")


class _Counters:
    def __init__(self) -> None:
        self.archive_spawns = 0
        self.bytes_extracted = 0


@contextlib.contextmanager
def _instrumented(counters: _Counters):
    """Count archive spawns at `_git_popen` and member bytes at `TarFile.extract`."""
    real_popen = baseline_export._git_popen
    real_extract = tarfile.TarFile.extract

    def popen(args, cwd, **kw):
        if "archive" in args:
            counters.archive_spawns += 1
        return real_popen(args, cwd, **kw)

    def extract(self, member, *a, **kw):
        size = getattr(member, "size", 0) if not isinstance(member, str) else 0
        counters.bytes_extracted += size
        return real_extract(self, member, *a, **kw)

    baseline_export._git_popen = popen
    tarfile.TarFile.extract = extract
    try:
        yield
    finally:
        baseline_export._git_popen = real_popen
        tarfile.TarFile.extract = real_extract


def _cpu_ms() -> float:
    t = os.times()
    return (t.user + t.system + t.children_user + t.children_system) * 1000.0


def profile(repo: Path, sha: str, calls: int) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    prev = os.getcwd()
    os.chdir(repo)
    try:
        for n in range(1, calls + 1):
            counters = _Counters()
            sink = io.StringIO()
            before = _cpu_ms()
            with _instrumented(counters), contextlib.redirect_stdout(sink):
                rc = baseline_export.main(["--sha", sha, "--", sys.executable, "-c", "pass"])
            rows.append({
                "call": n,
                "exit": rc,
                "archive_spawns": counters.archive_spawns,
                "bytes_extracted": counters.bytes_extracted,
                "process_ms": round(_cpu_ms() - before, 1),
            })
    finally:
        os.chdir(prev)
    summary = {
        "summary": {
            "calls": calls,
            "archive_spawns": sum(r["archive_spawns"] for r in rows),
            "bytes_extracted": sum(r["bytes_extracted"] for r in rows),
            "process_ms": round(sum(r["process_ms"] for r in rows), 1),
            "all_exit_ok": all(r["exit"] == 0 for r in rows),
        }
    }
    return rows, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--calls", type=int, default=3)
    ap.add_argument("--repo", help="existing repo to profile instead of a fixture")
    ap.add_argument("--sha", default="HEAD", help="commit in --repo (default HEAD)")
    args = ap.parse_args(argv)
    tmp: Path | None = None
    try:
        if args.repo:
            repo = Path(args.repo).resolve()
            sha = _git(repo, "rev-parse", args.sha)
        else:
            tmp = Path(tempfile.mkdtemp(prefix="bec-profile-"))
            repo = tmp / "repo"
            sha = build_fixture(repo)
        rows, summary = profile(repo, sha, args.calls)
    finally:
        if tmp is not None:
            baseline_export.remove(tmp)
    for r in rows:
        print(json.dumps(r))
    print(json.dumps(summary))
    return 0 if summary["summary"]["all_exit_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
