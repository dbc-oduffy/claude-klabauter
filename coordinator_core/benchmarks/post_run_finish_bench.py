"""Bench for `post_run.finish`: process time, spawn count and residuals over a seeded fixture wiki.

Per pipeline (lessons, distill) it builds a tmp git repo holding a run's pages (broken links, a stale
decisions/ link, wrong H1s, no `_index.md`), an evidence file and an unrelated dirty file, runs the
op's handler in-process after one warm-up on a separate fixture, and reports CPU time (own process
plus reaped children), spawns, and the residuals found by checkers that share no code with the op.
Also records the base sha as `op-not-registered` and censuses the new modules for hardcoded roots.

Usage: python -m coordinator_core.benchmarks.post_run_finish_bench [--pages N] [--json]
Exit 0 when every bar holds, 1 when one fails, 2 when the op is not registered.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.git.run import run_git
from coordinator_core.telemetry import spawn_counter

OP = "post_run.finish"
BASE_SHA = "c90cf75a0b"
BASE_PATH = "coordinator_core/ops/post_run_finish.py"
PROCESS_MS_BAR = 500.0
DEFAULT_PAGES = 12
PIPELINES = ("lessons", "distill")
_SESSION_ID = "00000000-0000-4000-8000-000000000000"
_WIKI = "docs/wiki"

_NEW_MODULES = (
    "coordinator_core/post_run/__init__.py",
    "coordinator_core/post_run/scope.py",
    "coordinator_core/post_run/linkfix.py",
    "coordinator_core/post_run/h1_normalize.py",
    "coordinator_core/post_run/nav_index.py",
    "coordinator_core/post_run/commit_step.py",
    "coordinator_core/post_run/p5_audit.py",
    "coordinator_core/ops/post_run_finish.py",
)
_ROOT_LITERAL_RE = re.compile(
    r"""["'](?:[A-Za-z]:[\\/]|/(?:Users|home|mnt|Volumes|private|tmp|var)/)"""
)
_MD_LINK_RE = re.compile(r"\]\(([^)\n#]+\.md)(?:#[^)\n]*)?\)")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str:
    result = run_git(
        ["-c", "user.name=bench", "-c", "user.email=bench@example.com", *args], cwd=str(root)
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed rc={result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_fixture(root: Path, pipeline: str, pages: int = DEFAULT_PAGES) -> dict:
    """Seed `root` as a committed repo plus one run's uncommitted output; return the op params."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _write(root, "README.md", "root\n")
    _write(root, "peer.txt", "peer\n")
    _write(root, "docs/decisions/DR-12-new-slug.md", "# DR-12\n\nA decision.\n")
    _write(root, f"{_WIKI}/old/target.md", "---\ntitle: Target\n---\n# Target\n\nBody.\n")
    _write(root, f"{_WIKI}/old/_index.md", "# old\n\n- [Target](target.md)\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")

    fam = f"{_WIKI}/{pipeline}-run"
    touched = []
    for i in range(pages):
        nxt = (i + 1) % pages
        _write(
            root,
            f"{fam}/page-{i:03d}.md",
            f"---\ntitle: Page {i:03d}\n---\n# Wrong Heading {i}\n\n"
            f"Sibling [next](./page-{nxt:03d}.md), moved [t](../elsewhere/target.md), "
            f"stale decision [d](../../decisions/DR-12-old-slug.md).\n",
        )
        touched.append(f"{fam}/page-{i:03d}.md")
    evidence = [f"state/evidence/{pipeline}-run.json"]
    _write(root, evidence[0], "{}\n")
    (root / "peer.txt").write_text("peer dirt\n", encoding="utf-8")
    params: dict = {
        "pipeline": pipeline, "run_id": f"bench-{pipeline}", "touched": touched, "evidence": evidence,
    }
    if pipeline == "distill":
        params["result"] = {}
        params["porcelain"] = []
    return params


def _fenced_lines(text: str) -> list[tuple[str, bool]]:
    out, fenced = [], False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            out.append((line, True))
        else:
            out.append((line, fenced))
    return out


def count_broken_links(root: Path) -> int:
    """Relative `.md` links under the wiki that resolve to no file."""
    broken = 0
    for page in sorted((root / _WIKI).rglob("*.md")):
        for line, fenced in _fenced_lines(page.read_text(encoding="utf-8")):
            if fenced:
                continue
            for target in _MD_LINK_RE.findall(line):
                if _SCHEME_RE.match(target):
                    continue
                if not (page.parent / target).resolve().is_file():
                    broken += 1
    return broken


def count_noncanonical_h1(root: Path, touched: list[str]) -> int:
    """Touched pages whose H1 count is not one, or whose single H1 differs from the frontmatter title."""
    bad = 0
    for rel in touched:
        text = (root / rel).read_text(encoding="utf-8")
        title = ""
        m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        if m:
            t = re.search(r"^title:\s*(.+?)\s*$", m.group(1), re.MULTILINE)
            title = t.group(1).strip("'\"") if t else ""
        body = text[m.end():] if m else text
        h1s = [ln[2:].strip() for ln, fenced in _fenced_lines(body) if not fenced and ln.startswith("# ")]
        if len(h1s) != 1 or (title and h1s[0] != title):
            bad += 1
    return bad


def count_stale_indexes(root: Path) -> int:
    """Wiki directories holding pages whose `_index.md` is missing or omits a direct page."""
    stale = 0
    for d in sorted({p.parent for p in (root / _WIKI).rglob("*.md") if p.name != "_index.md"}):
        index = d / "_index.md"
        pages = [p.name for p in d.glob("*.md") if p.name != "_index.md"]
        if not index.is_file():
            stale += 1
            continue
        text = index.read_text(encoding="utf-8")
        if any(name not in text for name in pages):
            stale += 1
    return stale


def run_scoped_porcelain(root: Path) -> list[str]:
    """Porcelain paths under the run's output roots (wiki, evidence); the unrelated dirty file is excluded."""
    lines = _git(root, "status", "--porcelain", "-uall").splitlines()
    paths = [ln[3:] for ln in lines]
    return [p for p in paths if p.startswith((f"{_WIKI}/", "state/evidence/"))]


def hardcoded_root_census(base: Optional[Path] = None) -> list[str]:
    """`path:line` of every quoted drive-root or user-home literal in the post_run modules."""
    base = base or repo_root()
    hits = []
    for rel in _NEW_MODULES:
        path = base / rel
        if not path.is_file():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _ROOT_LITERAL_RE.search(line):
                hits.append(f"{rel}:{n}")
    return hits


def classify_base(path_present_at_base: Optional[bool]) -> str:
    """Verdict for the base-sha run: the op file absent there means the op was not registered."""
    if path_present_at_base is None:
        return "base-unavailable"
    return "registered" if path_present_at_base else "op-not-registered"


def base_verdict(base_sha: str = BASE_SHA, root: Optional[Path] = None) -> str:
    cwd = str(root or repo_root())
    if run_git(["cat-file", "-e", f"{base_sha}^{{commit}}"], cwd=cwd).returncode != 0:
        return classify_base(None)
    present = run_git(["cat-file", "-e", f"{base_sha}:{BASE_PATH}"], cwd=cwd).returncode == 0
    return classify_base(present)


def _cpu_ms() -> float:
    t = os.times()
    return (time.process_time() + t.children_user + t.children_system) * 1000.0


def run_pipeline(handler, pipeline: str, work: Path, pages: int = DEFAULT_PAGES) -> dict:
    """Build a fixture under `work`, run `handler` once inside the measured window, then audit residuals."""
    params = build_fixture(work, pipeline, pages)
    prior = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.environ["CLAUDE_CODE_SESSION_ID"] = _SESSION_ID
    try:
        spawns0 = spawn_counter.spawn_count()
        cpu0, wall0 = _cpu_ms(), time.perf_counter()
        out = handler(params, repo_root=work)
        cpu1, wall1 = _cpu_ms(), time.perf_counter()
        spawns = spawn_counter.spawn_count() - spawns0
    finally:
        if prior is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = prior
    porcelain = run_scoped_porcelain(work)
    peer = (work / "peer.txt").read_text(encoding="utf-8")
    return {
        "pipeline": pipeline,
        "pages": pages,
        "exit_code": out.get("exit_code"),
        "committed": bool((out.get("commit") or {}).get("committed")),
        "process_ms": round(cpu1 - cpu0, 1),
        "wall_ms": round((wall1 - wall0) * 1000.0, 1),
        "spawns": spawns,
        "residual_broken_links": count_broken_links(work),
        "noncanonical_h1": count_noncanonical_h1(work, params["touched"]),
        "stale_index": count_stale_indexes(work),
        "run_scoped_porcelain": len(porcelain),
        "unrelated_dirty_preserved": peer == "peer dirt\n"
        and any(ln[3:] == "peer.txt" for ln in _git(work, "status", "--porcelain").splitlines()),
        "op_residual_advisories": len(out.get("residual") or []),
    }


def verdict(row: dict, spawn_budget: int) -> list[str]:
    """Failed bars for one pipeline row; empty means it holds."""
    failed = []
    if row["exit_code"] != 0 or not row["committed"]:
        failed.append("op did not commit")
    for key in ("residual_broken_links", "noncanonical_h1", "stale_index", "run_scoped_porcelain"):
        if row[key] != 0:
            failed.append(f"{key}={row[key]}")
    if row["process_ms"] >= PROCESS_MS_BAR:
        failed.append(f"process_ms={row['process_ms']} >= {PROCESS_MS_BAR:g}")
    if row["spawns"] != spawn_budget:
        failed.append(f"spawns={row['spawns']} != budget {spawn_budget}")
    if not row["unrelated_dirty_preserved"]:
        failed.append("unrelated dirty file not preserved")
    return failed


def spawn_budget() -> int:
    from coordinator_core.benchmarks.budget import load_manifest

    return sum(load_manifest()["overrides"][OP]["spawn_count_budget"].values())


def run_bench(pages: int = DEFAULT_PAGES) -> dict:
    """Run both pipelines (after a discarded warm-up) and return the full report."""
    from coordinator_core import ipc

    handler = ipc.get_op_handler(OP)
    report: dict = {"op": OP, "base": {"sha": BASE_SHA, "verdict": base_verdict()}}
    if handler is None:
        report["verdict"] = "op-not-registered"
        return report
    budget = spawn_budget()
    rows, failed = [], []
    with tempfile.TemporaryDirectory(prefix="post-run-finish-bench-") as tmp:
        base = Path(tmp)
        run_pipeline(handler, "lessons", base / "warmup", pages)
        for pipeline in PIPELINES:
            row = run_pipeline(handler, pipeline, base / pipeline, pages)
            row["failed"] = verdict(row, budget)
            failed.extend(f"{pipeline}: {f}" for f in row["failed"])
            rows.append(row)
    census = hardcoded_root_census()
    if census:
        failed.extend(f"hardcoded root literal at {c}" for c in census)
    report.update(
        {"spawn_budget": budget, "process_ms_bar": PROCESS_MS_BAR, "rows": rows,
         "hardcoded_root_hits": census, "verdict": "pass" if not failed else "fail", "failed": failed}
    )
    return report


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pages", type=int, default=DEFAULT_PAGES)
    ap.add_argument("--json", action="store_true", help="print the report as JSON only")
    args = ap.parse_args(argv)
    report = run_bench(args.pages)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"base {report['base']['sha']}: {report['base']['verdict']}")
        for row in report.get("rows", []):
            print(
                f"{row['pipeline']:8} process_ms={row['process_ms']} spawns={row['spawns']} "
                f"broken_links={row['residual_broken_links']} h1={row['noncanonical_h1']} "
                f"stale_index={row['stale_index']} porcelain={row['run_scoped_porcelain']} "
                f"peer_dirty_kept={row['unrelated_dirty_preserved']}"
            )
        print(f"verdict: {report['verdict']}")
        for f in report.get("failed", []):
            print(f"  FAIL {f}")
    if report["verdict"] == "op-not-registered":
        return 2
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
