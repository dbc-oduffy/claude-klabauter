"""Driver for the dispatch.land_run full path, measured as a process tree.

Each invocation builds a fresh repo under a caller-given directory (a one-row
plan with a met criterion, one DONE chunk), then drives `land_run._handler`
through the landing: one scoped commit, with the close legs queued to the close
job. `--setup-only` does everything except the handler call, so the cost of the
call is `full - setup-only` (the run-close spike's method).

    python -m coordinator_core.benchmarks.terminal_commit_full_path <dir> [--setup-only] [--kind plan|mise|ask] [--real-shape]

`--real-shape` (ask only) grows the fixture to the incident's tree, corpus, launch dir and a live
run token (`build_real_shape`); the call then carries `run_id` and `run_token` as a field call does.

    python -m coordinator_core.benchmarks.terminal_commit_full_path <dir> --kind ask|fanout --real-shape --build-only
    python -m coordinator_core.benchmarks.terminal_commit_full_path --prebuilt <dir> [--setup-only]

`--build-only` builds the real-shape repo under `<dir>` and exits; `--prebuilt` imports and calls
against it, so the measured process pays no fixture build. `--kind fanout` adds two plans with their
sizings and batons to that repo and drives `land_run`'s fanout mode (the `ask_fanout` relay is
stubbed: it is the next op, not the landing). `--setup-only --prebuilt` is the import-only baseline
the net subtracts, and it never mutates the repo. A full `--prebuilt` run
writes `handler.json` (parent handler process ms) beside the manifest and exits 0 when the path
completed, whatever the time: the caller gates on it.

`--kind mise` rewires the same repo as an inventory-mode run: the emitted script's marker
names a minted spine, whose sibling inventory record maps row `P1.C3` to the source plan.

The argv is re-run k times by `batched_process_time_ms`, so every invocation
takes its own subdirectory; nothing is shared between runs.

The run closes like a plan-run: the judge's criterion carries a completion
summary and one lesson candidate, and the session's claim lookup reports one baton, so every
inline close leg runs on the measured path. The close job is launched through
the same `spawn_detached` call, pointed at a no-op script: the job's own work
runs in the child, which the tree measurement counts, so it is gated by
`test_close_background_process_budget.py` instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.benchmarks import declare_benchmark_origin

SESSION_ID = "11111111-2222-3333-4444-555555555555"
PLAN_ID = "pln-x-123456"
PLAN_REL = "docs/plan.md"
SCRIPT_REL = "run.mjs"

_PLAN = f"""---
title: p
status: executing
plan_id: {PLAN_ID}
prime_exit_criterion:
  text: a.py exists
---

# Plan

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
```
"""

INLINE_REVIEW = {
    "integration_stem": f"{PLAN_ID}.review-wave-bookkeeping",
    "slices": 1,
    "fixes": 0,
    "plan_id": PLAN_ID,
    "prep_sidecar": None,
    "wave_sidecar_paths": [],
    "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
    "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
    "tests": {"status": "not_run", "run": 0, "failed": 0, "sidecar": None},
    "criterion": {
        "status": "met",
        "observation": "a.py exists at HEAD",
        "sidecar": "j.md",
        "completion_summary": "Shipped a.py. It makes the fixture plan's criterion true.",
        "lesson_candidates": [
            {
                "title": "Fixture lesson",
                "body": "A fixture lesson body.",
                "change_kind": "wiki-append",
                "target_wiki": "coordinator/docs/wiki/fixture.md",
            }
        ],
        "authored_by": "terminal-judge",
    },
}

BATON_REL = "state/handoffs/b.md"
_BATON = f"""---
title: "Fixture baton"
status: claimed
claimed_at: 2026-07-05T12:00:00Z
authoring_session: {SESSION_ID}
deployment_state: in_flight
claimed_by: {SESSION_ID}
created: 2026-07-05
branch: main
predecessor: none
category: infra
summary: fixture baton
---

## What this covers

A realistically sized section standing in for the prose ahead of the ledger block, so the
ledger append's disambiguation window stays far under the body-size ratio bound.

## Session Ledger

<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary> -->
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def build_fixture(parent: Path) -> tuple[Path, str]:
    """A fresh repo under `parent` with the plan committed and `a.py` written
    but unstaged; returns the repo root and the emitted script's repo-relative
    path."""
    from coordinator_core.ops.dispatch_emit.commit_request import (
        ChunkCommit,
        CommitRequest,
        render_marker,
    )

    repo = parent / uuid.uuid4().hex[:12]
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    (repo / "docs").mkdir()
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / BATON_REL).write_text(_BATON, encoding="utf-8")
    (repo / PLAN_REL).write_text(_PLAN, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "plan")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),), plan_path=PLAN_REL
    )
    script = SCRIPT_REL
    (repo / script).write_text("// emitted script\n" + render_marker(request) + "\n", encoding="utf-8")
    return repo, script


SPINE_REL = "state/mise-inventory/run.spine.md"
INVENTORY_REL = "state/mise-inventory/run.md"


def build_mise_fixture(parent: Path) -> tuple[Path, str]:
    """`build_fixture` plus a committed inventory record and minted spine; the script's
    marker names the spine and chunk `P1.C3`."""
    from coordinator_core.ops.dispatch_emit.commit_request import (
        ChunkCommit,
        CommitRequest,
        render_marker,
    )

    repo, script = build_fixture(parent)
    (repo / "state" / "mise-inventory").mkdir(parents=True)
    (repo / INVENTORY_REL).write_text(
        "---\nrun_id: run\n---\n\n## Chunk table\n\n"
        "| ID | Spec path | Summary | Footprint | Deps | Verification | Complexity | Disposition |\n"
        "|---|---|---|---|---|---|---|---|\n"
        f"| P1 | `{PLAN_REL}` | s | `a.py` | - | v | S | pending |\n",
        encoding="utf-8",
    )
    (repo / SPINE_REL).write_text(
        "---\nrun_id: run\nderived_from: mise inventory record\n---\n\n## Tasks\n", encoding="utf-8"
    )
    _git(repo, "add", "state/mise-inventory")
    _git(repo, "commit", "-q", "-m", "inventory")
    request = CommitRequest(chunks=(ChunkCommit(id="P1.C3", title="t3", paths=("a.py",)),), plan_path=SPINE_REL)
    (repo / script).write_text("// emitted script\n" + render_marker(request) + "\n", encoding="utf-8")
    return repo, script


ASK_RUN = "run1"
ASK_DLV = "dlv-alpha-aaaaaa"
ASK_SIZING_REL = "state/sizings/20260101-a.yaml"
ASK_PLAN_REL = "docs/plans/p.md"
ASK_DELIVERY_REL = f"docs/delivery/{ASK_RUN}.md"
_ASK_SIZING = (
    "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
    "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
    "premise:\n  provenance: read\n  evidence: fixture\n"
)
_ASK_PLAN = f"""---
title: p
status: executing
plan_id: {PLAN_ID}
deliverable_id: {ASK_DLV}
sizing_object: {ASK_SIZING_REL}
prime_exit_criterion:
  text: a.py exists
  statement: done
  falsifier:
    how: pytest tests/test_a.py::test_x -q
    expected_when_true: passes
---

# Plan

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
```
"""


def build_ask_fixture(parent: Path) -> tuple[Path, str]:
    """`build_fixture` rewired as a warp ask run: sizing committed, in-run plan on disk and
    uncommitted, a run directory with marker and manifest, an ask-manifest script, a seeded
    requirements ledger, a delivery note and one product file."""
    import json

    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit.ask_contract import (
        ASK_MANIFEST_MARKER,
        RUN_DIR_ROOT,
        StageManifest,
    )
    from coordinator_core.ops.dispatch_emit.commit_request import (
        ChunkCommit,
        CommitRequest,
        render_marker,
    )

    repo, script = build_fixture(parent)
    (repo / ".gitignore").write_text("scratch/\n.coordinator-local/\n", encoding="utf-8")
    (repo / ASK_SIZING_REL).parent.mkdir(parents=True)
    (repo / ASK_SIZING_REL).write_text(_ASK_SIZING, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "ask seed")
    (repo / ASK_PLAN_REL).parent.mkdir(parents=True)
    (repo / ASK_PLAN_REL).write_text(_ASK_PLAN, encoding="utf-8")
    run_rel = f"{RUN_DIR_ROOT}/{ASK_RUN}"
    marker_rel = f"{run_rel}/commit-request.txt"
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),),
        deliverable_id=ASK_DLV,
        plan_path=ASK_PLAN_REL,
    )
    (repo / run_rel).mkdir(parents=True)
    (repo / marker_rel).write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=run_rel, rows=(), review_declared_paths=(), marker_path=marker_rel
    )
    (repo / run_rel / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    (repo / script).write_text(
        f"// emitted script\n{ASK_MANIFEST_MARKER}{run_rel}/manifest.json\n", encoding="utf-8"
    )
    rl.start(repo, ASK_RUN, all_of_pm_ask=False)
    rl.bind(repo, ASK_RUN, ASK_SIZING_REL, ask_text="ship a.py")
    (repo / ASK_DELIVERY_REL).parent.mkdir(parents=True)
    (repo / ASK_DELIVERY_REL).write_text("# delivered\n", encoding="utf-8")
    return repo, script


REAL_TREE_FILES = 41_500
REAL_SIZING_RECORDS = 1_250
REAL_DELIVERY_RECORDS = 120
REAL_STOPPED_RUNS = 80
REAL_TRANSCRIPT_MB = 34
REAL_TRANSCRIPT_LINE_BYTES = 1_000_000
# A field sizing record parses in ~1.6ms (the live corpus, whole-document yaml); a bare fixture
# record parses in ~0.3ms, so the cascade's whole-corpus read under-pays by 5x without a log.
REAL_SIZING_LOG_ENTRIES = 14
REAL_LAUNCH_ENTRIES = 3_000
# Setup at real shape costs ~100s of tree process time, so the setup-only subtraction swings by
# seconds. The handler's own (parent) process time carries no subtraction: over this bar the
# driver exits 3, which the budget test reads as red whatever the net figure says.
REAL_SHAPE_PARENT_BAR_MS = 500.0


def _write_tree(repo: Path, files: int) -> None:
    for i in range(files):
        d = repo / "tree" / f"d{i // 100:03d}"
        if i % 100 == 0:
            d.mkdir(parents=True)
        (d / f"f{i:05d}.txt").write_text(f"{i}\n", encoding="utf-8")


def build_real_shape(repo: Path) -> str:
    """Grow an ask fixture to the shape the 2026-10-21 incident ran at, sizes from
    docs/research/2026-10-21-terminal-commit-real-shape-attribution.md: a tracked tree, a
    `state/sizings` corpus, delivery-verdict records, stopped in-window own runs whose
    submissions point at one large transcript that holds each one's refused submit ahead of
    its tail window, a populated launch dir, and a live token for the fixture run itself.
    Everything lives under `repo` or its parent. Returns the run token a field call carries
    as `run_token`."""
    import json
    import time
    from calendar import timegm

    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.run_token import TTL_SECONDS

    _write_tree(repo, REAL_TREE_FILES)
    sizings = repo / "state" / "sizings"
    for i in range(REAL_SIZING_RECORDS):
        log = "".join(
            f"  - at: 2026-01-02T00:{k % 60:02d}:00Z\n    by: sizing-agent\n"
            f"    note: Premise check {k} on record {i} against the plan evidence trail.\n"
            for k in range(REAL_SIZING_LOG_ENTRIES)
        )
        (sizings / f"20260102-s{i:05d}.yaml").write_text(
            _ASK_SIZING.replace("Test intent", f"Intent {i}")
            + f"deliverable_id: dlv-s{i:05d}-aaaaaa\nevidence_log:\n{log}",
            encoding="utf-8",
        )
    verdicts = repo / "state" / "delivery-verdicts" / "plan-x"
    verdicts.mkdir(parents=True)
    for i in range(REAL_DELIVERY_RECORDS):
        (verdicts / f"v{i:04d}.md").write_text(
            f"---\nkind: delivery-verdict\nsupersedes: docs/other/r{i}.md\nrecorded_at: 2026-01-01T00:00:00Z\n"
            "delivery:\n  verdict: PASS\n---\n\nbody\n",
            encoding="utf-8",
        )
    _git(repo, "add", "tree", "state/sizings", "state/delivery-verdicts")
    _git(repo, "commit", "-q", "-m", "real-shape corpus")

    transcript = repo.parent / f"{repo.name}-transcript.jsonl"
    line = json.dumps({"type": "assistant", "text": "x" * REAL_TRANSCRIPT_LINE_BYTES}) + "\n"
    with open(transcript, "w", encoding="utf-8", newline="\n") as handle:
        # Each run's refused-submit result sits ahead of the tail window, so the run reads as
        # ended only after `_earlier_lines` streams the head window for its tool_use_id.
        for i in range(REAL_STOPPED_RUNS):
            handle.write(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {"type": "tool_result", "tool_use_id": f"toolu_absent_{i:06d}", "content": "refused"}
                            ]
                        },
                        "toolUseResult": "refused",
                    }
                )
                + "\n"
            )
        for _ in range(REAL_TRANSCRIPT_MB * 1024 * 1024 // len(line)):
            handle.write(line)
    now = time.time()
    for i in range(REAL_STOPPED_RUNS):
        t = time.gmtime(now - 60 - i)
        run_id = time.strftime("%Y%m%dT%H%M%S", t) + f"-{i:06x}"
        assert now - timegm(t) < TTL_SECONDS
        sub = warp_live._submission_path(repo, run_id)
        sub.parent.mkdir(parents=True)
        sub.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "session_id": SESSION_ID,
                    "transcript_path": str(transcript),
                    "script_path": f"{RUN_DIR_ROOT}/{run_id}/run.mjs",
                    "sizing_rel": ASK_SIZING_REL,
                    "tool_use_id": f"toolu_absent_{i:06d}",
                    "submitted_at": now,
                }
            ),
            encoding="utf-8",
        )
        (repo / "state" / "warp" / run_id).mkdir(parents=True)

    launch = repo.parent / f"{repo.name}-tmp"
    launch.mkdir()
    for i in range(REAL_LAUNCH_ENTRIES):
        (launch / f"launch-{i:05d}.txt").write_text("", encoding="utf-8")
    point_temp_at(launch)
    return run_token.mint(repo, ASK_RUN, SCRIPT_REL, session_id=SESSION_ID, sizing_rel=ASK_SIZING_REL)


def point_temp_at(launch: Path) -> None:
    import tempfile

    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = str(launch)
    tempfile.tempdir = str(launch)


def _prepare_close(repo: Path) -> None:
    """Point the lessons outbox and the close job at the fixture, and stub the session's baton claim.

    Runs in both modes, so its cost cancels out of the net figure. The outbox is per
    fixture repo: a shared one would answer a later invocation `discharged`.
    Trap: `land_run._launch_close_job` is replaced here by a launch of the no-op script; renaming
    it would silently measure the real close job.
    """
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.session import claims

    outbox = repo.parent / f"{repo.name}-lessons-outbox"
    outbox.mkdir(exist_ok=True)
    os.environ["LESSON_PROMOTE_OUTBOX_ROOT"] = str(outbox)
    os.environ["COORDINATOR_SESSION_ID"] = SESSION_ID
    noop = repo.parent / "close_job_noop.py"
    noop.write_text("pass\n", encoding="utf-8")
    if not hasattr(land_run, "_launch_close_job"):
        raise RuntimeError("land_run._launch_close_job renamed: this driver would measure the real close job")

    engine = Path(__file__).resolve().parents[2]

    def launch_noop(root: Path, key: str, batons: list, memo: Optional[str]) -> bool:
        return spawn_detached(str(engine), str(noop), [key, "--root", str(root)], site_free=True)

    land_run._launch_close_job = launch_noop
    claims.list_claims_by_session_checked = lambda sid, cwd=None: ([("handoff", "b.md")], [])


def _mise_close_failure(out: dict) -> str:
    close = out.get("close") or {}
    legs = close.get("legs") or {}
    wrong = {
        leg: (legs.get(leg) or {}).get("status")
        for leg, want in (
            ("plan-reconciliation", "not-applicable"),
            ("memo-lifecycle", "not-applicable"),
            ("review", "queued"),
        )
        if (legs.get(leg) or {}).get("status") != want
    }
    if close.get("kind") != "mise-run" or wrong:
        return f"mise close block wrong: kind={close.get('kind')!r} legs={wrong!r}"
    return ""


def _close_failure(out: dict) -> str:
    """Every leg the landing defers is queued to the close job; none ran inline."""
    close = out.get("close") or {}
    legs = close.get("legs") or {}
    owed = (
        "plan-reconciliation", "lessons", "completion-entry", "baton-ship",
        "review", "baton-drain", "orientation-refresh", "post-close",
    )
    not_queued = {leg: legs.get(leg) for leg in owed if (legs.get(leg) or {}).get("status") != "queued"}
    if not_queued or close.get("outstanding"):
        return f"close legs not queued: {not_queued!r} outstanding={close.get('outstanding')!r}"
    return ""


FANOUT_PLANS = ("docs/plans/2026-10-21-fan-a.md", "docs/plans/2026-10-21-fan-b.md")


def build_fanout(repo: Path) -> None:
    """Two plans on a real-shape ask repo, each citing a sizing that cites a baton, plus the
    fanout run's two record files: the dirt `land_run`'s fanout mode lands."""
    import yaml

    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.ops.sizing_intake_contract import intake_record

    for name, plan_rel in zip(("a", "b"), FANOUT_PLANS):
        sizing = f"state/sizings/2026-10-21-fan-{name}.yaml"
        baton = f"state/handoffs/2026-10-21-fan-{name}.md"
        (repo / plan_rel).write_text(
            f"---\nsizing_object: {sizing}\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
            f"- id: {name.upper()}1\n  title: T\n  disposition: open\n  writes:\n    - pkg/{name}.py\n```\n",
            encoding="utf-8", newline="\n",
        )
        rec = intake_record(name=name, turn={"text": "do the thing", "turn": 1}, today="2026-10-21")
        rec["plan"] = plan_rel
        rec["baton"] = baton
        rec.setdefault("interaction_mode", "pm")
        (repo / sizing).write_text(yaml.safe_dump(rec, sort_keys=False), encoding="utf-8", newline="\n")
        (repo / baton).write_text(
            f"---\ntitle: b\nstatus: claimed\nkind: session-handoff\n---\n\n# baton {name}\n", encoding="utf-8"
        )
    record = repo / RECORD_DIR_ROOT / ASK_RUN
    record.mkdir(parents=True, exist_ok=True)
    for fname in ("ask.md", "run.json"):
        (record / fname).write_text(f"{fname}\n", encoding="utf-8")


def _stub_fanout_relay() -> None:
    """The `ask_fanout` relay is the next op, not the landing: it answers ready and fires nothing."""
    from coordinator_core.ops.dispatch_emit import ask_fanout

    ask_fanout.fanout = lambda params, root: {"exit_code": 0, "ready": [], "runs": []}


def _run_full_path(repo: Path, script: str, kind: str = "plan", run_token: Optional[str] = None) -> dict:
    from coordinator_core.ops.dispatch_emit import land_run

    if kind == "fanout":
        _stub_fanout_relay()
        return land_run._handler(
            {
                "script_path": script, "run_id": ASK_RUN, "run_token": run_token, "session_id": SESSION_ID,
                "fanout": {"plans": list(FANOUT_PLANS)},
            },
            repo_root=repo / ".git",
        )
    params = {
        "script_path": script,
        "incomplete_chunks": [],
        "session_id": SESSION_ID,
        "inline_review": INLINE_REVIEW,
    }
    if kind == "ask":
        params["ledger_verdict"] = {"items": [{"id": "R1", "status": "met"}]}
    if run_token is not None:
        params["run_id"] = ASK_RUN
        params["run_token"] = run_token
    return land_run._handler(params, repo_root=repo / ".git")


PREBUILT_MANIFEST = "prebuilt.json"
HANDLER_RESULT = "handler.json"


def _build_only(parent: Path, kind: str) -> int:
    """Build the real-shape repo (`ask`, or `fanout` with its two plans) under `parent` and record
    what the measured process needs."""
    import json

    repo, script = build_ask_fixture(parent)
    token = build_real_shape(repo)
    if kind == "fanout":
        build_fanout(repo)
    launch = repo.parent / f"{repo.name}-tmp"
    (parent / PREBUILT_MANIFEST).write_text(
        json.dumps(
            {"repo": str(repo), "script": script, "launch": str(launch), "run_token": token, "kind": kind}
        ),
        encoding="utf-8",
    )
    return 0


def _landed_failure(out: dict, kind: str) -> str:
    """Why `out` is not a landing that queued its close legs, else the empty string."""
    if out.get("committed") is not True or not out.get("sha") or out.get("error"):
        return f"land_run did not land: {out!r}"
    if kind == "fanout":
        fan = out.get("fanout") or {}
        return "" if fan.get("exit_code") == 0 else f"fanout relay not reached: {out!r}"
    if out.get("review_stamp") != "deferred":
        return f"review stamp not deferred to the close job: {out.get('review_stamp')!r}"
    return ""


def main(argv: list[str]) -> int:
    declare_benchmark_origin()
    argv = list(argv)
    kind = "plan"
    if "--kind" in argv:
        at = argv.index("--kind")
        kind = argv[at + 1] if at + 1 < len(argv) else ""
        del argv[at : at + 2]
    prebuilt: Optional[str] = None
    if "--prebuilt" in argv:
        at = argv.index("--prebuilt")
        prebuilt = argv[at + 1] if at + 1 < len(argv) else ""
        del argv[at : at + 2]
    real_shape = "--real-shape" in argv
    args = [a for a in argv if not a.startswith("--")]
    flags = ("--setup-only", "--real-shape", "--build-only")
    unknown = [a for a in argv if a.startswith("--") and a not in flags]
    builders = {"plan": build_fixture, "mise": build_mise_fixture, "ask": build_ask_fixture}
    build_only = "--build-only" in argv
    bad_shape = (build_only and not (real_shape and kind in ("ask", "fanout"))) or (
        real_shape and not build_only and kind != "ask"
    )
    if (
        unknown
        or (kind not in builders and kind != "fanout")
        or (kind == "fanout" and not build_only and prebuilt is None)
        or bad_shape
        or prebuilt == ""
        or len(args) != (0 if prebuilt is not None else 1)
        or (prebuilt is not None and (build_only or real_shape))
    ):
        print(
            "usage: terminal_commit_full_path <dir> [--setup-only] [--kind plan|mise|ask] "
            "[--real-shape (ask only)] [--build-only (with --kind ask|fanout --real-shape)]\n"
            "       terminal_commit_full_path --prebuilt <dir> [--setup-only]",
            file=sys.stderr,
        )
        return 2
    if build_only:
        return _build_only(Path(args[0]), kind)
    token: Optional[str] = None
    if prebuilt is not None:
        import json

        manifest = json.loads((Path(prebuilt) / PREBUILT_MANIFEST).read_text(encoding="utf-8"))
        repo, script, token = Path(manifest["repo"]), manifest["script"], manifest["run_token"]
        kind, real_shape = manifest.get("kind", "ask"), True
        point_temp_at(Path(manifest["launch"]))
    else:
        repo, script = builders[kind](Path(args[0]))
        if real_shape:
            token = build_real_shape(repo)
    from coordinator_core.ops.dispatch_emit import land_run  # noqa: F401  (loaded in both modes)

    _prepare_close(repo)
    if "--setup-only" in argv:
        return 0
    import time

    started = time.process_time()
    out = _run_full_path(repo, script, kind, token)
    handler_ms = (time.process_time() - started) * 1000.0
    if prebuilt is not None:
        import json

        (Path(prebuilt) / HANDLER_RESULT).write_text(json.dumps({"handler_ms": handler_ms}), encoding="utf-8")
    elif real_shape and handler_ms >= REAL_SHAPE_PARENT_BAR_MS:
        print(
            f"real-shape handler parent process time {handler_ms:.0f}ms is over the "
            f"{REAL_SHAPE_PARENT_BAR_MS:.0f}ms bar",
            file=sys.stderr,
        )
        return 3
    failure = _landed_failure(out, kind)
    if not failure and kind == "mise":
        failure = _mise_close_failure(out)
    elif not failure and kind == "plan":
        failure = _close_failure(out)
    if failure:
        print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
