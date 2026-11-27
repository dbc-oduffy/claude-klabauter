"""Falsifier benchmark for the declared-touch recorder: recorded/declared per repo, process ms, spawn count.

Builds temp git repos, writes files spread across them, and drives
``ipc._record_self_reported_touches`` with a resolvable session id.

    python coordinator_core/benchmarks/declared_touch_batch.py --paths 40 --repos 3
    python coordinator_core/benchmarks/declared_touch_batch.py --matrix
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core import ipc  # noqa: E402
from coordinator_core.session import core, liveness, scope, touch_record  # noqa: E402
from coordinator_core.session_hub_path import session_dir_for  # noqa: E402

_SID = "bench-declared-touch"
_NATIVE_PREFIX = "bench-native-"
_MATRIX_PATHS = (16, 40, 256)
_MATRIX_REPOS = (1, 3)
_SESSION_ENV = ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "COORDINATOR_SESSION_ID")


def _no_console() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q"], cwd=path, check=True, creationflags=_no_console()
    )


def _recorded_by_repo(repos: list) -> list:
    """Per repo, the count of KIND_WRITE events under the benchmark sid."""
    counts = []
    for repo in repos:
        sink = session_dir_for(repo / ".git", _SID) / "touch-record.jsonl"
        if not sink.is_file():
            counts.append(0)
            continue
        raw = sink.read_bytes()
        events = [
            touch_record.decode_line(line)
            for line in touch_record.iter_complete_lines(raw)
        ]
        counts.append(sum(1 for e in events if e.kind == touch_record.KIND_WRITE))
    return counts


def _count_spawns() -> tuple:
    """Patch ``subprocess.Popen.__init__`` to count child spawns; returns (counter, restore)."""
    counter = {"n": 0}
    original = subprocess.Popen.__init__

    def counting(self, *args, **kwargs) -> None:
        counter["n"] += 1
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = counting

    def restore() -> None:
        subprocess.Popen.__init__ = original

    return counter, restore


def _native_only_live(sid: str, cwd: "str | None" = None) -> bool:
    """Liveness stand-in: only the seeded native sessions read live, whatever the host harness says."""
    return sid.startswith(_NATIVE_PREFIX)


def _seed_native_claims(roots: list, declared: list) -> int:
    """One live native session per foreign repo claims its first declared file; returns the count seeded."""
    seeded = 0
    for idx, root in enumerate(roots[1:], start=1):
        first = next((p for p in declared if Path(p).parent == root), None)
        if first is None:
            continue
        sid = f"{_NATIVE_PREFIX}{idx}"
        core.init(sid, cwd=str(root))
        scope.touch(sid, first, cwd=str(root), root=str(root))
        seeded += 1
    return seeded


def run_once(paths: int, repos: int, live_sessions: bool = False) -> dict:
    """Declare ``paths`` files over ``repos`` repos (caller sits in repo 0) and measure the recorder."""
    scratch = Path(tempfile.mkdtemp(prefix="declared-touch-bench-"))
    saved_env = {k: os.environ.get(k) for k in _SESSION_ENV}
    try:
        roots = []
        for i in range(repos):
            root = scratch / f"repo{i}"
            root.mkdir()
            _git_init(root)
            roots.append(Path(os.path.realpath(root)))
        declared = []
        declared_per_repo = [0] * repos
        for n in range(paths):
            idx = n % repos
            target = roots[idx] / f"f{n}.txt"
            target.write_text("x")
            declared.append(str(target))
            declared_per_repo[idx] += 1

        seeded = _seed_native_claims(roots, declared) if live_sessions else 0
        real_session_live = liveness.session_live
        if live_sessions:
            setattr(liveness, "session_live", _native_only_live)

        for key in _SESSION_ENV:
            os.environ.pop(key, None)
        os.environ["CLAUDE_SESSION_ID"] = _SID

        reply: dict = {"ok": True, ipc._SCOPE_TOUCH_PATHS_KEY: declared}
        counter, restore = _count_spawns()
        cpu_start = time.process_time()
        try:
            ipc._record_self_reported_touches(reply, str(roots[0]))
        finally:
            cpu_ms = (time.process_time() - cpu_start) * 1000.0
            restore()

        recorded_per_repo = _recorded_by_repo(roots)
        return {
            "paths": paths,
            "repos": repos,
            "declared_per_repo": declared_per_repo,
            "recorded_per_repo": recorded_per_repo,
            "recorded": sum(recorded_per_repo),
            "live_sessions": seeded,
            "skipped_claimed": sum(reply.get(ipc._TOUCH_SKIPPED_CLAIMED_KEY, {}).values()),
            "process_ms": cpu_ms,
            "spawns": counter["n"],
        }
    finally:
        if live_sessions:
            liveness.session_live = real_session_live
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(scratch, ignore_errors=True)


def format_result(result: dict) -> str:
    lines = []
    for i, (rec, dec) in enumerate(
        zip(result["recorded_per_repo"], result["declared_per_repo"])
    ):
        lines.append(f"  repo{i}: recorded {rec} of {dec}")
    lines.append(
        f"recorded {result['recorded']} of {result['paths']} across {result['repos']} repos; "
        f"live_sessions={result['live_sessions']} skipped_claimed={result['skipped_claimed']} "
        f"process_ms={result['process_ms']:.1f} spawns={result['spawns']}"
    )
    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--paths", type=int, default=40)
    parser.add_argument("--repos", type=int, default=3)
    parser.add_argument(
        "--live-sessions",
        action="store_true",
        help="seed one live native session per foreign repo claiming one declared path",
    )
    parser.add_argument(
        "--matrix",
        action="store_true",
        help=f"run paths in {_MATRIX_PATHS} x repos in {_MATRIX_REPOS}",
    )
    args = parser.parse_args(argv)
    if args.matrix:
        grid = [(p, r) for r in _MATRIX_REPOS for p in _MATRIX_PATHS]
    else:
        grid = [(args.paths, args.repos)]
    for paths, repos in grid:
        print(format_result(run_once(paths, repos, args.live_sessions)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
