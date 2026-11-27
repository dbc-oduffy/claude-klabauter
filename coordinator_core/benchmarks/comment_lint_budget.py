"""Process time and spawn budget of the executor comment-lint guard and the land-time check; `--expect baseline --root .` in a baseline export."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GUARD_BUDGET_MS = 50.0
LAND_BUDGET_MS = 500.0
LAND_SPAWN_BUDGET = 2
WARM_RUNS = 20
PREFIX = "budget-t"
AGENT = "a1b2c3d4e5f60718"
EM_SESSION = "em-session-budget"
REVIEW_SESSION = "review-session-budget"
AGENT_SESSION = "11111111-2222-3333-4444-555555555555"
COMMENT = "# because X"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, check=True, creationflags=_NO_WINDOW,
    )
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _commit(repo: Path, rel: str, text: str, message: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class _SpawnCounter:
    """Counts every `subprocess.Popen` construction while active."""

    def __init__(self) -> None:
        self.count = 0
        self._orig = subprocess.Popen.__init__

    def __enter__(self) -> "_SpawnCounter":
        counter, orig = self, self._orig

        def counted(popen, *a, **k) -> None:
            counter.count += 1
            orig(popen, *a, **k)

        setattr(subprocess.Popen, "__init__", counted)
        return self

    def __exit__(self, *exc) -> None:
        setattr(subprocess.Popen, "__init__", self._orig)


def _measure(fn, runs: int) -> tuple:
    """Mean process ms per call, max spawns per call, last result."""
    spawns, result = [], None
    t0 = time.process_time()
    for _ in range(runs):
        with _SpawnCounter() as sc:
            result = fn()
        spawns.append(sc.count)
    return (time.process_time() - t0) * 1000.0 / runs, max(spawns), result


def _seed_executor(repo: Path) -> None:
    hub = repo / ".coordinator-local" / "coordinator-sessions"
    (hub / ".agents" / AGENT).mkdir(parents=True)
    (hub / ".agents" / AGENT / "em-session-id.txt").write_text(EM_SESSION + "\n", encoding="utf-8")
    (hub / EM_SESSION).mkdir(parents=True, exist_ok=True)
    (hub / EM_SESSION / "dispatched-agents.txt").write_text(
        f"{AGENT}\tx\tcoordinator:executor\n", encoding="utf-8"
    )


def _seed_snapshot(repo: Path, base: str, rel: str, content: bytes) -> None:
    import hashlib

    from coordinator_core.ops.pre_review_dir import pre_review_dir
    from coordinator_core.session.machinery_paths import share_dir

    snap = pre_review_dir(Path(share_dir(str(repo), REVIEW_SESSION)), PREFIX)
    snap.mkdir(parents=True)
    digest = hashlib.sha256(content).hexdigest()
    (snap / digest).write_bytes(content)
    (snap / "manifest.json").write_text(
        json.dumps({
            "base": base, "recorded_at": "2026-11-26T00:00:00Z",
            "paths": [rel], "files": {rel: digest},
        }),
        encoding="utf-8",
    )


def _line_counts(root: Path) -> dict:
    rels = [
        "coordinator_core/commenting/added_types.py",
        "coordinator_core/commenting/added.py",
        "coordinator_core/commenting/allow.py",
        "coordinator_core/commenting/authorship.py",
        "coordinator_core/write_guards/nudge_executor_added_comment.py",
    ]
    out = {}
    for rel in rels:
        try:
            out[rel] = len((root / rel).read_text(encoding="utf-8").splitlines())
        except OSError:
            out[rel] = 0
    return out


def run(expect: str, engine_root: Path) -> dict:
    sys.path.insert(0, str(engine_root))
    from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home

    declare_benchmark_origin()
    work = Path(tempfile.mkdtemp(prefix="comment-lint-budget-")).resolve()
    try:
        with fixture_settings_home(work):
            return _run_in(expect, engine_root, work / "repo")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_in(expect: str, engine_root: Path, repo: Path) -> dict:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    base_text = "VALUE = 1\n"
    _commit(repo, "pkg/mod.py", base_text, "base")
    plan_sha = _commit(repo, "docs/plans/p.md", "# plan\n", "plan")
    governed = f"Governed-By: {plan_sha} 2026-11-26T00:00:00+00:00 docs/plans/p.md"
    agent_sha = _commit(
        repo, "pkg/mod.py", f"{base_text}{COMMENT}\n",
        f"agent edit\n\nSession-Id: {AGENT_SESSION}\n{governed}\n",
    )
    human_sha = _commit(repo, "pkg/human.py", "X = 1\n# human note\n", "human edit")

    _seed_executor(repo)
    _seed_snapshot(repo, plan_sha, "pkg/mod.py", base_text.encode())

    from coordinator_core.write_guards import engine

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(repo / "tools" / "new_mod.py"), "content": f"x = 1\n{COMMENT}\n"},
        "session_id": EM_SESSION,
        "agent_id": AGENT,
        "cwd": str(repo),
    }
    engine.evaluate(payload)
    guard_ms, guard_spawns, advisory = _measure(lambda: engine.evaluate(payload), WARM_RUNS)
    context = ""
    if isinstance(advisory, dict):
        context = (advisory.get("hookSpecificOutput") or {}).get("additionalContext", "")

    from coordinator_core.ops import review_reverify as rr

    def review() -> dict:
        return rr.review_reverify(repo, base=plan_sha, slice_prefix=PREFIX, session_id=REVIEW_SESSION)

    land_fn = getattr(rr, "_land_findings", None)
    land = {}
    for label, head in (("all_agent", agent_sha), ("mixed", human_sha)):
        _git(repo, "checkout", "-q", head)
        land_check = _measure(lambda: land_fn(repo, plan_sha), WARM_RUNS) if land_fn else (None, None, None)
        land[label] = {"land_check": land_check, "review_reverify": _measure(review, WARM_RUNS)}

    mixed = land["mixed"]["review_reverify"][2] or {}
    all_agent = land["all_agent"]["review_reverify"][2] or {}
    rows = mixed.get("comment_findings") or []
    result = {
        "guard": {
            "process_ms": round(guard_ms, 2), "spawns": guard_spawns,
            "advisory": bool(context), "names_comment": "because X" in context,
        },
        "land": {
            label: {
                "land_check_process_ms": None if v["land_check"][0] is None else round(v["land_check"][0], 2),
                "land_check_spawns": v["land_check"][1],
                "review_reverify_process_ms": round(v["review_reverify"][0] or 0.0, 2),
                "review_reverify_spawns": v["review_reverify"][1],
            }
            for label, v in land.items()
        },
        "comment_findings_key_present": "comment_findings" in mixed,
        "mixed_comment_findings": [
            {"path": r["path"], "line": r["line"], "severity": r["severity"]} for r in rows
        ],
        "all_agent_provenance_findings": len(all_agent.get("provenance_findings") or []),
        "all_agent_comment_findings": len(all_agent.get("comment_findings") or []),
        "review_errors": [e for e in (all_agent.get("error"), mixed.get("error")) if e],
        "lines_added": _line_counts(engine_root),
    }
    result["lines_added_total"] = sum(result["lines_added"].values())
    result["pass"] = _verdict(expect, result)
    return result


def _verdict(expect: str, r: dict) -> bool:
    if expect == "baseline":
        return not r["guard"]["advisory"] and not r["comment_findings_key_present"]
    land = r["land"]["all_agent"]
    spawns, ms = land["land_check_spawns"], land["land_check_process_ms"]
    found = r["mixed_comment_findings"]
    return (
        r["guard"]["advisory"] and r["guard"]["names_comment"]
        and r["guard"]["process_ms"] < GUARD_BUDGET_MS and r["guard"]["spawns"] == 0
        and len(found) == 1 and found[0]["path"] == "pkg/mod.py" and found[0]["severity"] == "major"
        and r["all_agent_comment_findings"] == 1 and r["all_agent_provenance_findings"] == 0
        and not r["review_errors"]
        and spawns is not None and spawns <= LAND_SPAWN_BUDGET
        and ms is not None and ms < LAND_BUDGET_MS
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="comment_lint_budget")
    parser.add_argument("--expect", choices=("end", "baseline"), default="end")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args(argv)
    result = run(args.expect, Path(args.root).resolve())
    print(json.dumps(result, indent=2))
    g, a = result["guard"], result["land"]["all_agent"]
    print(
        f"guard process ms {g['process_ms']} spawns {g['spawns']}; "
        f"land check (all-agent) spawns {a['land_check_spawns']} process ms {a['land_check_process_ms']}; "
        f"expect={args.expect} pass={result['pass']}"
    )
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
