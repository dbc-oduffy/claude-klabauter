"""Measure `run_commit_pipeline` end-to-end: process time and job-object spawn
count, against this repo's REAL checkin surface.

Each measured window commits N times into the same repo, so history grows by N
across the window. That biases toward OVER-reporting, never under: a later call
sees a longer history and a larger index than an earlier one, so the amortised
figure is an upper bound on the per-call cost at the starting size.

Job object attached to this process, so every `git` child AND the `conhost.exe`
Windows allocates alongside one (DR-373) is counted -- the undercount a
`subprocess.Popen` patch produces is exactly what this exists to avoid.
"""
import os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

#: This file's own location, never a literal: `coordinator_core/benchmarks/<this>`,
#: so the repo root is three parents up. A drive-anchored literal here was wrong on
#: every other host AND blocked the commit of any `coordinator_core/` change,
#: because the hardcoded-path gate runs at pre-commit over the whole package.
SRC = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(SRC))
from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.process_time import LiveTreeAccountant
from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline


WARMUP = 6


def _q(root: Path, *a):
    return subprocess.run(
        ["git", *a], cwd=root, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def build_repo(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    q = lambda *a: _q(root, *a)
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


def main(n=40, reps=3):
    """One job window per N calls, NOT one snapshot pair per call.

    A per-call `snapshot()` pair can only ever return a multiple of the
    15.625ms job-accounting tick, so it reports a tick count rather than a
    cost, and a median over tick-quantised samples then picks the low mode.
    That artifact published 15.62ms and 31.25ms -- exactly 1x and 2x the tick
    -- in this repo's own audit before it was caught. Bracketing N calls in
    ONE window divides the quantisation error by N.
    """
    declare_benchmark_origin()
    for label, tracked in (("edit of a tracked file", True),
                           ("new file, new directory", False)):
        rows = []
        for rep in range(reps):
            rows.append(_one_window(label, tracked, n, rep))
        per_call = "  ".join(f"{ms:6.2f}ms/{procs:.2f}p" for ms, procs, _ in rows)
        landed = sum(l for _, _, l in rows)
        print(f"  {label:26s} {per_call}   landed={landed}/{n * reps}")


def _one_window(label, tracked, n, rep):
    tmp = Path(tempfile.mkdtemp(prefix="pipeprobe-"))
    repo = tmp / "r"
    build_repo(repo)
    names = [f"src/m{i:03d}.py" for i in range(n + WARMUP)]
    if tracked:
        # Seed every path as TRACKED first, so the measured calls are edits.
        for nm in names:
            f = repo / nm
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("v0" + chr(10))
        _q(repo, "add", "-A")
        _q(repo, "commit", "-q", "-m", "seed-tracked")

    def one(i):
        nm = names[i] if tracked else f"docs/d{i:03d}/note.md"
        f = repo / nm
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"v{i}" + chr(10))
        return run_commit_pipeline(
            repo,
            session_id=f"{label}{rep}_{i}",
            subject=f"{label} {i}",
            stage_paths=[nm],
            caller_paths={nm},
        )

    # WARMUP calls are excluded because the FIRST call through this path pays
    # one-off import and page-in cost that no subsequent commit pays. It is a
    # cold-start exclusion, not a discard of slow samples: every call after it
    # is kept, including the slowest.
    for i in range(WARMUP):
        one(i)

    acc = LiveTreeAccountant(os.getpid())
    before = acc.snapshot()
    landed = sum(1 for i in range(WARMUP, WARMUP + n) if not one(i).commit_failed)
    after = acc.snapshot()
    acc.close()

    ms = (after["process_time_ms"] - before["process_time_ms"]) / n
    procs = (after["procs"] - before["procs"]) / n
    st = _q(repo, "status", "--porcelain")
    fs = _q(repo, "fsck", "--strict")
    if st.stdout.strip() or fs.returncode != 0:
        raise SystemExit(
            f"{label}: repo not clean after the window -- status={st.stdout[:200]!r} "
            f"fsck rc={fs.returncode}"
        )
    shutil.rmtree(tmp, ignore_errors=True)
    return ms, procs, landed


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
