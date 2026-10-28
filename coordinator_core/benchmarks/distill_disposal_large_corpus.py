"""Large-corpus bench for distill.assemble_disposal_manifest and distill.apply_disposal.

Generates a git repo of >=5k files with >=200 disposal candidates, drives each op
through the real `python -m coordinator_core.invoke` entry point at default args,
and reports engine process time (root plus descendants) and spawn count per op.
Wall clock is context only. Exit 0 always: this measures, it does not gate.

Usage: python coordinator_core/benchmarks/distill_disposal_large_corpus.py [--files N] [--candidates N] [--keep]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin

declare_benchmark_origin()

from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time
from coordinator_core.win_portability import no_console_creationflags

DEFAULT_FILES = 5000
DEFAULT_CANDIDATES = 200
DEFAULT_CITED = 40
RUN_ID = "bench-large-corpus"
HARVEST_LOG = "state/distillation-log.md"

_HANDOFF = (
    "---\nshipped_in: 68b27420\nstatus: consumed\ndeployment_state: shipped\n"
    "realized_by: inline\n---\nbody {i}\n"
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def build_corpus(root: Path, files: int = DEFAULT_FILES, candidates: int = DEFAULT_CANDIDATES,
                 cited: int = DEFAULT_CITED) -> dict:
    """Builds the corpus repo under `root`; returns candidate paths and the harvest sha.

    `candidates` handoffs are eligible; `cited` further handoffs are each cited by
    several filler docs, so the reference guard has real matched files to read.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "bench@example.com")
    _git(root, "config", "user.name", "Bench")
    (root / "README.md").write_text("root\n", encoding="utf-8")
    log = root / HARVEST_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("## Run seed\n- seed -> EPHEMERAL, seed (run: seed)\n", encoding="utf-8")
    (root / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "harvest")
    harvest = _git(root, "rev-parse", "HEAD")

    handoffs = root / "archive" / "handoffs"
    handoffs.mkdir(parents=True, exist_ok=True)
    eligible = [f"archive/handoffs/eligible-{i:04d}.md" for i in range(candidates)]
    cited_paths = [f"archive/handoffs/cited-{i:04d}.md" for i in range(cited)]
    for i, rel in enumerate(eligible + cited_paths):
        (root / rel).write_text(_HANDOFF.format(i=i), encoding="utf-8")

    docs = root / "docs" / "filler"
    docs.mkdir(parents=True, exist_ok=True)
    filler_n = max(0, files - len(eligible) - len(cited_paths))
    for i in range(filler_n):
        body = "\n".join(f"filler line {i}-{j} lorem ipsum dolor sit amet" for j in range(20))
        if cited_paths and i % 25 == 0:
            body += f"\nsee {cited_paths[(i // 25) % len(cited_paths)]}\n"
        (docs / f"doc-{i:05d}.md").write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "corpus")
    return {"candidates": eligible + cited_paths, "eligible": eligible, "harvest": harvest}


def _write_params(path: Path, params: dict) -> None:
    path.write_text(json.dumps(params), encoding="utf-8")


def _measure(op: str, params: dict, corpus: Path, work: Path) -> dict:
    pfile = work / f"{op}.params.json"
    out = work / f"{op}.stdout"
    err = work / f"{op}.stderr"
    _write_params(pfile, params)
    m = single_invocation_tree_process_time(
        [sys.executable, "-m", "coordinator_core.invoke", op, "--params-file", str(pfile),
         "--repo", str(corpus), "--allow-unstamped-dispatch"],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout_path=str(out), stderr_path=str(err),
    )
    error_code: Optional[int] = None
    try:
        envelope = json.loads(out.read_text(encoding="utf-8"))
        if isinstance(envelope, dict) and "error" in envelope:
            error_code = envelope["error"].get("code")
    except (OSError, ValueError):
        error_code = None
    return {
        "op": op,
        "process_ms": round(m["process_time_ms"], 1),
        "spawns": max(0, m["procs"] - 1),
        "wall_ms": round(m["wall_ms"], 1),
        "rc": m["rc"],
        "error_code": error_code,
        "ok": m["rc"] == 0 and error_code is None,
    }


def run(files: int = DEFAULT_FILES, candidates: int = DEFAULT_CANDIDATES,
        base_dir: Optional[Path] = None, keep: bool = False) -> list[dict]:
    """Runs assemble then stamp (unmeasured) then apply; returns the two measured rows."""
    scratch = base_dir or (Path(__file__).resolve().parents[2] / "scratch")
    scratch.mkdir(parents=True, exist_ok=True)
    top = Path(tempfile.mkdtemp(prefix="distill-bench-", dir=scratch))
    try:
        corpus = top / "corpus"
        work = top / "work"
        work.mkdir()
        built = build_corpus(corpus, files, candidates)
        rows = [_measure("distill.assemble_disposal_manifest",
                         {"run_id": RUN_ID, "candidates": built["candidates"]}, corpus, work)]
        stamp = _measure("distill.stamp_disposal",
                         {"run_id": RUN_ID, "by": "bench", "at": "2026-01-01T00:00:00Z",
                          "note": "bench mass-throttle-ack"}, corpus, work)
        if rows[0]["ok"] and stamp["ok"]:
            rows.append(_measure("distill.apply_disposal",
                                 {"run_id": RUN_ID, "harvest_committed_sha": built["harvest"]},
                                 corpus, work))
        else:
            rows.append({"op": "distill.apply_disposal", "process_ms": None, "spawns": None,
                         "wall_ms": None, "rc": None, "error_code": None, "ok": False,
                         "skipped": "assemble or stamp failed"})
        return rows
    finally:
        if not keep:
            shutil.rmtree(top, ignore_errors=True)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--files", type=int, default=DEFAULT_FILES)
    ap.add_argument("--candidates", type=int, default=DEFAULT_CANDIDATES)
    ap.add_argument("--keep", action="store_true")
    ns = ap.parse_args(argv)
    for row in run(ns.files, ns.candidates, keep=ns.keep):
        print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
