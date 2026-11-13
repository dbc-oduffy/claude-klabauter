"""Recall benchmark for the prior-art-checker: planted conflicts, one per corpus kind.

Each case is a tiny repo (shared decoys in `cases/_base/corpus` plus the case's
`plant/` overlay) and a plan with exactly one claim contradicting the planted
file. A run is CAUGHT when the sidecar's Conflicts section cites that file.
The runner repeats every case N times per model and reports catch rate, so a
model flip is decided from a number, never from one trial.

    python -m coordinator_core.prior_art_recall --models sonnet,haiku --n 5

Run output lands under `<repo>/scratch/prior-art-recall/`. Nothing here changes
an agent's model.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from coordinator_core.prior_art_recall.search_log import validate_sidecar

CASES_DIR = Path(__file__).parent / "cases"
BASE_DIR = CASES_DIR / "_base"
DEFAULT_AGENT = Path("C:/dev/coordinator-content-repo/coordinator/agents/prior-art-checker.md")
KINDS = ("project-wiki", "doctrine-wiki", "decision-record", "lesson-only", "improvement-queue", "skill-predicate")

# Run in the workdir; the benchmark corpus roots are overridden by the brief, not the agent's defaults.
BRIEF = """mode: plan
plan: plan.md
sidecar_path: {sidecar}
plan_repo: ledger-sync
corpus_roots:
  project_wikis: docs/wiki
  global_wikis: global-wiki
  doctrine_wiki: doctrine-wiki
  decisions: docs/decisions
  lessons: state/lessons
  improvement_queue: improvement-queue
  skills: skills
Run the prior-art-checker procedure in full and write the sidecar to sidecar_path."""


@dataclass(frozen=True)
class Case:
    case_id: str
    kind: str
    planted_file: str
    dir: Path

    @property
    def plan_text(self) -> str:
        return (self.dir / "plan.md").read_text(encoding="utf-8")


Runner = Callable[[str, Path, Path, str], None]  # model, workdir, sidecar path, brief


def load_cases(only: set[str] | None = None) -> list[Case]:
    cases = []
    for d in sorted(p for p in CASES_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")):
        exp = yaml.safe_load((d / "expected.yaml").read_text(encoding="utf-8"))
        if only and d.name not in only:
            continue
        cases.append(Case(d.name, exp["kind"], exp["planted_file"], d))
    return cases


def build_workdir(case: Case, dest: Path) -> None:
    """Materialise base decoys + the case's plant + the plan into `dest` (must not exist)."""
    shutil.copytree(BASE_DIR / "corpus", dest)
    shutil.copytree(case.dir / "plant" / "corpus", dest, dirs_exist_ok=True)
    shutil.copy(case.dir / "plan.md", dest / "plan.md")


_CONFLICTS_RE = re.compile(r"^###\s+Conflicts\b.*?$(.*?)(?=^###\s|\Z)", re.MULTILINE | re.DOTALL)


def conflicts_section(sidecar_text: str) -> str:
    m = _CONFLICTS_RE.search(sidecar_text)
    return m.group(1) if m else ""


def score_sidecar(case: Case, sidecar_text: str) -> dict:
    """CAUGHT = the Conflicts section names the planted file (path or stem).
    Citing it only as compatible-but-relevant or silent is a miss."""
    section = conflicts_section(sidecar_text)
    stem = Path(case.planted_file).stem
    caught = bool(section) and (case.planted_file.split("corpus/", 1)[-1] in section or stem in section)
    return {"caught": caught, "log_errors": validate_sidecar(sidecar_text, case.plan_text)}


def run_case(model: str, case: Case, run_dir: Path, runner: Runner) -> dict:
    work = run_dir / "work"
    build_workdir(case, work)
    sidecar = work / "sidecar.prior-art-check.md"
    err = None
    try:
        runner(model, work, sidecar, BRIEF.format(sidecar=sidecar.name))
    except Exception as exc:  # a crashed run is a miss, recorded, never skipped
        err = f"{type(exc).__name__}: {exc}"
    if not sidecar.exists():
        return {"caught": False, "log_errors": ["no sidecar written"], "error": err}
    result = score_sidecar(case, sidecar.read_text(encoding="utf-8"))
    result["error"] = err
    return result


def run_benchmark(models: list[str], n: int, runner: Runner, out_dir: Path, cases: list[Case] | None = None) -> dict:
    cases = cases if cases is not None else load_cases()
    report: dict = {"n": n, "cases": [c.case_id for c in cases], "models": {}}
    for model in models:
        rows = []
        for case in cases:
            for i in range(n):
                run_dir = out_dir / model / case.case_id / f"run{i}"
                if run_dir.exists():
                    shutil.rmtree(run_dir)
                run_dir.mkdir(parents=True)
                rows.append({"case": case.case_id, "kind": case.kind, "run": i, **run_case(model, case, run_dir, runner)})
        report["models"][model] = summarize(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def summarize(rows: list[dict]) -> dict:
    kinds: dict[str, list[bool]] = {}
    for r in rows:
        kinds.setdefault(r["kind"], []).append(r["caught"])
    total = len(rows)
    caught = sum(r["caught"] for r in rows)
    valid = sum(1 for r in rows if not r["log_errors"])
    return {
        "runs": total,
        "caught": caught,
        "catch_rate": round(caught / total, 4) if total else 0.0,
        "log_valid_rate": round(valid / total, 4) if total else 0.0,
        "per_kind": {k: round(sum(v) / len(v), 4) for k, v in sorted(kinds.items())},
        "rows": rows,
    }


def format_report(report: dict, baseline: str = "sonnet", candidate: str = "haiku") -> str:
    lines = [f"prior-art recall benchmark: {len(report['cases'])} cases x N={report['n']} per model"]
    for model, s in report["models"].items():
        kinds = " ".join(f"{k}={v:.2f}" for k, v in s["per_kind"].items())
        lines.append(f"{model}: catch_rate={s['catch_rate']:.4f} ({s['caught']}/{s['runs']}) log_valid={s['log_valid_rate']:.4f} | {kinds}")
    m = report["models"]
    if baseline in m and candidate in m:
        delta = m[candidate]["catch_rate"] - m[baseline]["catch_rate"]
        lines.append(f"{candidate} minus {baseline}: {delta:+.4f} (a number for the PM/EM; this tool flips nothing)")
    return "\n".join(lines)


def claude_cli_runner(agent_file: Path = DEFAULT_AGENT, timeout_s: int = 900) -> Runner:
    """Drive the real agent through `claude -p`, its body as the system prompt, cwd = the case repo."""
    body = agent_file.read_text(encoding="utf-8")
    body = re.sub(r"\A---\n.*?\n---\n", "", body, count=1, flags=re.DOTALL)

    def run(model: str, workdir: Path, sidecar: Path, brief: str) -> None:
        subprocess.run(
            ["claude", "-p", "--model", model, "--permission-mode", "bypassPermissions", "--append-system-prompt", body, brief],
            cwd=workdir, check=True, timeout=timeout_s, capture_output=True, text=True,
        )

    return run


def self_check(cases: list[Case]) -> list[str]:
    """Fixture integrity: kinds covered once each, plant exists, plan exists."""
    problems = []
    kinds = sorted(c.kind for c in cases)
    if kinds != sorted(KINDS):
        problems.append(f"kinds {kinds} != {sorted(KINDS)}")
    for c in cases:
        if not (c.dir / "plant" / c.planted_file).is_file():
            problems.append(f"{c.case_id}: planted file missing")
        if not (c.dir / "plan.md").is_file():
            problems.append(f"{c.case_id}: plan missing")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="prior_art_recall")
    ap.add_argument("--models", default="sonnet,haiku")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--cases", default="", help="comma-separated case ids; default all")
    ap.add_argument("--out", default="", help="default <repo>/scratch/prior-art-recall")
    ap.add_argument("--agent-file", default=str(DEFAULT_AGENT))
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args(argv)
    cases = load_cases(set(filter(None, a.cases.split(","))) or None)
    if a.self_check:
        problems = self_check(cases)
        print("\n".join(problems) or f"ok: {len(cases)} cases")
        return 1 if problems else 0
    out = Path(a.out) if a.out else Path(__file__).resolve().parents[2] / "scratch" / "prior-art-recall"
    models = [m for m in a.models.split(",") if m]
    report = run_benchmark(models, a.n, claude_cli_runner(Path(a.agent_file)), out, cases)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
