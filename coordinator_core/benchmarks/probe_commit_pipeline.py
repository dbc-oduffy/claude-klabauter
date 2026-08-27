"""Measure `run_commit_pipeline` end-to-end: process time and job-object spawn
count, against this repo's REAL checkin surface.

Job object attached to this process, so every `git` child AND the `conhost.exe`
Windows allocates alongside one (DR-373) is counted -- the undercount a
`subprocess.Popen` patch produces is exactly what this exists to avoid.
"""
import os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, r"X:\claude-klabauter")
from coordinator_core.benchmarks.process_time import LiveTreeAccountant
from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline

SRC = Path(r"X:\claude-klabauter")


def build_repo(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    q = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)
    q("init", "-q", "-b", "main")
    q("config", "user.name", "probe")
    q("config", "user.email", "probe@example.com")
    q("config", "core.autocrlf", "true")
    q("config", "core.fileMode", "false")
    shutil.copyfile(SRC / ".gitattributes", root / ".gitattributes")
    (root / "seed.md").write_text("seed\n")
    q("add", "-A")
    q("commit", "-q", "-m", "seed")
    return q


def main(n=25):
    tmp = Path(tempfile.mkdtemp(prefix="pipeprobe-"))
    repo = tmp / "r"
    q = build_repo(repo)

    rows = []
    acc = LiveTreeAccountant(os.getpid())
    try:
        for i in range(n):
            name = f"docs/note_{i:04d}.md"
            p = repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"body {i}\n")

            before = acc.snapshot()
            t0 = time.perf_counter()
            res = run_commit_pipeline(
                repo,
                session_id=f"probe-{i}",
                subject=f"probe: commit {i}",
                stage_paths=[name],
                caller_paths={name},
            )
            wall = (time.perf_counter() - t0) * 1000.0
            after = acc.snapshot()
            if res.commit_failed:
                print(f"  [{i}] FAILED: {res.stage.failed} / {res.commit_reason if hasattr(res,'commit_reason') else ''}")
            rows.append(
                (
                    after["process_time_ms"] - before["process_time_ms"],
                    after["procs"] - before["procs"],
                    wall,
                    not res.commit_failed,
                )
            )
    finally:
        acc.close()

    ok = sum(1 for r in rows if r[3])
    warm = rows[5:]  # drop first 5: import/JIT warmup
    warm.sort(key=lambda r: r[0])
    med = warm[len(warm) // 2]
    print()
    print(f"  run_commit_pipeline  n={len(warm)} (warm)  landed={ok}/{len(rows)}")
    print(f"    median process time : {med[0]:8.2f} ms")
    print(f"    median procs        : {sum(r[1] for r in warm)/len(warm):8.2f}  (job-object, incl. conhost)")
    print(f"    min / max proc time : {warm[0][0]:.2f} / {warm[-1][0]:.2f} ms")
    print(f"    median wall clock   : {sorted(r[2] for r in warm)[len(warm)//2]:8.2f} ms  (peer load, not the bar)")
    st = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    print(f"    status clean        : {'YES' if not st.stdout.strip() else repr(st.stdout[:200])}")
    fs = subprocess.run(["git", "fsck", "--strict"], cwd=repo, capture_output=True, text=True)
    print(f"    fsck --strict       : {'clean' if fs.returncode == 0 else fs.stderr[:200]}")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 25)
