"""Driver for the halted-run record-only commit, measured as a process tree.

Each invocation builds a fresh repo under a caller-given directory: an untracked
`state/warp/<run>/` holding the five record files a staged run writes, beside other dirty paths
(a modified tracked file a finished chunk declares, an untracked sizing, a second run's record
dir). It then calls `dispatch.land_run`'s handler with `halted_by` params (no plan, no
incomplete_chunks); the run script's marker supplies the finished chunk.
The halt carries no test verdict, so the commit lands the record only and withholds `product.py`
(`halt-record-only`); the driver asserts that withhold.
`--setup-only` does everything except that call, so the cost of the call is `full - setup-only`.

`--incident-shape` swaps the fixture for a 3-chunk run with C1 committed, C2/C3 files dirty, a
`run_outcome` carrying incomplete_chunks and landed_chunks, and a peer run's record dir; the repo is
unsigned, since a signing stall is wall-clock waiting and this measures work.

`--refire-withheld` is the #60 case: an ask run is halted (the halt call runs in setup, in both
modes), HEAD then moves past the halt commit and the withheld product is left dirty with the halt
commit's own bytes. The measured call is the refire's product landing with a `resume_ledger`, which
must land that product instead of refusing "no declared product path changed since the halt commit".

    python -m coordinator_core.benchmarks.halted_record_commit_full_path <dir> [--setup-only] [--incident-shape | --refire-withheld]
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.git.run import run_git

RUN_ID = "ask-bench"
OTHER_RUN_ID = "ask-other"
RECORD_FILES = ("ask.md", "pm-intent.md", "run.json", "requirements.md", "manifest.json")
REFIRE_RUN_ID = "run2"
REFIRE_SCRIPT = "run2.mjs"


def _git(repo: Path, *args: str) -> str:
    done = run_git(list(args), cwd=str(repo))
    if not done.ok:
        raise subprocess.CalledProcessError(done.returncode, ["git", *args], done.stdout, done.stderr)
    return done.stdout


def build_incident_fixture(parent: Path) -> Path:
    """The incident shape: a 3-chunk run whose C1 is committed, C2/C3 declared files dirty, beside a
    peer run's record dir; returns the repo root."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    repo = parent / uuid.uuid4().hex[:12]
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    for name in ("c1.py", "c2.py", "c3.py"):
        (repo / name).write_text("a = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    (repo / "reports").mkdir()
    chunks = []
    for cid in ("C1", "C2", "C3"):
        (repo / "reports" / f"{cid}.md").write_text(f"DONE: {cid}\n", encoding="utf-8")
        chunks.append(ChunkCommit(id=cid, title=cid, paths=(f"{cid.lower()}.py",), report=f"reports/{cid}.md"))
    (repo / "c1.py").write_text("a = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "C1 landed")
    (repo / "c2.py").write_text("a = 2\n", encoding="utf-8")
    (repo / "c3.py").write_text("a = 2\n", encoding="utf-8")
    marker = render_marker(CommitRequest(chunks=tuple(chunks), session_id=None, repo_root=None, plan_path=None))
    (repo / "run.mjs").write_text(f"{marker}\n", encoding="utf-8")
    record = repo / RECORD_DIR_ROOT / RUN_ID
    record.mkdir(parents=True)
    for name in RECORD_FILES:
        (record / name).write_text(f"{name}\n", encoding="utf-8")
    other = repo / RECORD_DIR_ROOT / OTHER_RUN_ID
    other.mkdir(parents=True)
    (other / "ask.md").write_text("other\n", encoding="utf-8")
    return repo


def build_fixture(parent: Path) -> Path:
    """A fresh repo with an untracked record dir plus other dirty paths; returns the repo root."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    repo = parent / uuid.uuid4().hex[:12]
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    (repo / "product.py").write_text("a = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    (repo / "product.py").write_text("a = 2\n", encoding="utf-8")
    (repo / "state" / "sizings").mkdir(parents=True)
    (repo / "state" / "sizings" / "x.yaml").write_text("schema: sizing-object\n", encoding="utf-8")
    record = repo / RECORD_DIR_ROOT / RUN_ID
    record.mkdir(parents=True)
    for name in RECORD_FILES:
        (record / name).write_text(f"{name}\n", encoding="utf-8")
    (repo / "reports").mkdir()
    (repo / "reports" / "C1.md").write_text("DONE: C1\n", encoding="utf-8")
    chunk = ChunkCommit(id="C1", title="C1", paths=("product.py",), report="reports/C1.md")
    marker = render_marker(CommitRequest(chunks=(chunk,), session_id=None, repo_root=None, plan_path=None))
    (repo / "run.mjs").write_text(f"{marker}\n", encoding="utf-8")
    other = repo / RECORD_DIR_ROOT / OTHER_RUN_ID
    other.mkdir(parents=True)
    (other / "ask.md").write_text("other\n", encoding="utf-8")
    return repo


def build_refire_fixture(parent: Path) -> tuple[Path, str]:
    """An ask run halted at `ledger-check` with `a.py` withheld, then a second run staged as its
    refire; returns the repo root and the halt commit's sha.

    The halt commit holds `a.py` as "a = 1"; a later commit moves it to "a = 3" and the worktree is
    left holding the halt commit's "a = 1": unchanged against the halt commit, dirty against HEAD."""
    import json

    from coordinator_core.benchmarks import terminal_commit_full_path as tcfp
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import (
        ASK_MANIFEST_MARKER,
        HALTED_RECORD_PARAM,
        RECORD_DIR_ROOT,
        RUN_DIR_ROOT,
        ManifestRow,
        StageManifest,
    )
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    repo, script = tcfp.build_ask_fixture(parent)
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "product as the halt sees it")
    (repo / "a.py").write_text("a = 2\n", encoding="utf-8")
    (repo / "reports").mkdir()
    (repo / "reports" / "C3.md").write_text("DONE: C3\n", encoding="utf-8")
    finished = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",), report="reports/C3.md"),),
        deliverable_id=tcfp.ASK_DLV, plan_path=tcfp.ASK_PLAN_REL,
    )
    (repo / RUN_DIR_ROOT / tcfp.ASK_RUN / "commit-request.txt").write_text(
        render_marker(finished) + "\n", encoding="utf-8"
    )
    record = repo / RECORD_DIR_ROOT / tcfp.ASK_RUN
    record.mkdir(parents=True, exist_ok=True)
    for name in RECORD_FILES:
        (record / name).write_text(f"{name}\n", encoding="utf-8")
    tcfp._prepare_close(repo)
    halt = land_run._handler(
        {
            "script_path": script, "run_id": tcfp.ASK_RUN,
            HALTED_RECORD_PARAM: {"halted": "ledger-check", "reason": "row R1 open"},
        },
        repo_root=repo / ".git",
    )
    if halt.get("committed") is not True:
        raise RuntimeError(f"refire fixture: the halt did not land: {halt!r}")
    halt_sha = halt["sha"]
    (repo / "a.py").write_text("a = 3\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "checkpoint moves the product on")
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")

    run_rel = f"{RUN_DIR_ROOT}/{REFIRE_RUN_ID}"
    (repo / run_rel).mkdir(parents=True)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),),
        deliverable_id=tcfp.ASK_DLV, plan_path=tcfp.ASK_PLAN_REL,
    )
    (repo / run_rel / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    row = ManifestRow(id="C3", agent_type="x", model="m", brief_path="briefs/C3.md", writes=("a.py",), wave=0)
    manifest = StageManifest(
        run_dir=run_rel, rows=(row,), review_declared_paths=(), marker_path=f"{run_rel}/commit-request.txt"
    ).to_json()
    manifest["head_sha"] = halt_sha
    (repo / run_rel / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (repo / REFIRE_SCRIPT).write_text(
        f"// emitted script\n{ASK_MANIFEST_MARKER}{run_rel}/manifest.json\n", encoding="utf-8"
    )
    rl.start(repo, REFIRE_RUN_ID, all_of_pm_ask=False)
    rl.bind(repo, REFIRE_RUN_ID, tcfp.ASK_SIZING_REL, ask_text="ship a.py")
    token = run_token.mint(
        repo, REFIRE_RUN_ID, REFIRE_SCRIPT, session_id=tcfp.SESSION_ID, sizing_rel=tcfp.ASK_SIZING_REL
    )
    (parent / "refire.json").write_text(json.dumps({"token": token}), encoding="utf-8")
    return repo, halt_sha


def _refire_failure(out: dict) -> str:
    # Trap: checks the reply only, not the commit's tree; a git read here would be counted in the
    # measured process tree, so the tree check belongs in the test that owns the fixture.
    if out.get("committed") is True and out.get("sha") and not out.get("error") and not out.get("refused"):
        return ""
    return f"refire did not land the withheld product: {out!r}"


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    flags = ("--setup-only", "--incident-shape", "--refire-withheld")
    unknown = [a for a in argv if a.startswith("--") and a not in flags]
    if len(args) != 1 or unknown or ("--incident-shape" in argv and "--refire-withheld" in argv):
        print(
            "usage: halted_record_commit_full_path <dir> [--setup-only] [--incident-shape | --refire-withheld]",
            file=sys.stderr,
        )
        return 2
    declare_benchmark_origin()
    incident = "--incident-shape" in argv
    refire = "--refire-withheld" in argv
    if refire:
        return _main_refire(Path(args[0]), "--setup-only" in argv)
    repo = (build_incident_fixture if incident else build_fixture)(Path(args[0]))
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    if "--setup-only" in argv:
        return 0
    params = {
        "script_path": "run.mjs", "run_id": RUN_ID,
        "halted_by": {"halted": "ledger-check", "reason": "sizing refused: route is not spec-dispatch"},
    }
    expected = {f"{RECORD_DIR_ROOT}/{RUN_ID}/{n}" for n in (*RECORD_FILES, "halted.json")}
    if incident:
        params["incomplete_chunks"] = ["C2", "C3"]
        params["landed_chunks"] = ["C1"]
        params["run_outcome"] = {"incomplete_chunks": ["C2", "C3"], "landed_chunks": ["C1"]}
    out = land_run._handler(params, repo_root=repo / ".git")
    if not incident and (out.get("withheld") or {}).get("product.py") != "halt-record-only":
        print(f"halted-record commit did not withhold product.py: {out!r}", file=sys.stderr)
        return 1
    if out.get("committed") is not True or set(out.get("paths") or []) != expected:
        print(f"halted-record commit did not complete: {out!r}", file=sys.stderr)
        return 1
    return 0


def _main_refire(parent: Path, setup_only: bool) -> int:
    import json

    from coordinator_core.benchmarks import terminal_commit_full_path as tcfp
    from coordinator_core.ops.dispatch_emit import land_run

    repo, halt_sha = build_refire_fixture(parent)
    if setup_only:
        return 0
    token = json.loads((parent / "refire.json").read_text(encoding="utf-8"))["token"]
    out = land_run._handler(
        {
            "script_path": REFIRE_SCRIPT, "run_id": REFIRE_RUN_ID, "run_token": token,
            "incomplete_chunks": [], "session_id": tcfp.SESSION_ID, "inline_review": tcfp.INLINE_REVIEW,
            "ledger_verdict": {"items": [{"id": "R1", "status": "met"}]},
            "resume_ledger": {"run_id": tcfp.ASK_RUN, "halt_sha": halt_sha, "blocking": ["R1"]},
        },
        repo_root=repo / ".git",
    )
    failure = _refire_failure(out)
    if failure:
        print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
