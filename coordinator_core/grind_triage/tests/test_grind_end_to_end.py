"""End-to-end exit criterion for a grinder run, driven through the default entry point (`emit_queue_script`,
no budget override) over one tmp git repo: single mint, flake and not-reproduced rows closed before any
agent, one agent per failure-signature cluster, row-scaled default budget, 25/50/75 checkpoints, and one
scoped commit per green row. The workflow script itself is JS the engine never runs, so the stub agent
consumes the emitted `BATCHES` const exactly as the runtime would.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.bug_blitz.contract import FailingTest, reap_record_rel
from coordinator_core.bug_blitz.mint import mint_tf_rows
from coordinator_core.grind_triage.checkpoint import PROGRESS_FILE, emit_checkpoints
from coordinator_core.grind_triage.contract import CHECKPOINT_PERCENTS, default_budget
from coordinator_core.ops.dispatch_emit import land_run_grind
from coordinator_core.ops.dispatch_emit.grind_admission import NOT_ADMITTED_EXTRA_KEY
from coordinator_core.ops.dispatch_emit.queue_emit import emit_queue_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.queue_append import append_queue_entry
from coordinator_core.testing.git_write import write_seen_by_git
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO = Path(__file__).resolve().parents[3]
_FIXTURE_PROFILE = _REPO / "coordinator_core/ops/dispatch_emit/tests/fixtures/queue-profiles/fixture.yaml"
_SCHEMA_REL = "coordinator_core/frontmatter/schemas/bug-backlog.schema.json"
_RUN_STAMP = "20261115T010203Z"
_MINT_RUNS = ("20261115T000001Z", "20261115T000002Z")

# (file stem, test name, source file the test reads, red-message). Two rows share one failure signature
# (numbers differ), one has its own, and one passes in isolation.
_FLAKY = ("test_c0_flaky", "test_ok")
_SHARED = (("test_c1_xa", "xa", "AssertionError: expected 2 got 1"), ("test_c3_xb", "xb", "AssertionError: expected 2 got 7"))
_LONE = ("test_c2_fix", "fixc", "KeyError: 'widget'")
_GONE_SURFACE = "pkg/gone_at_head.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


def _red_test(src: str) -> str:
    return (
        "from pathlib import Path\n\n\n"
        "def test_x():\n"
        f"    assert (Path(__file__).parents[1] / 'src' / '{src}.py').read_text().strip() == 'VALUE = 2'\n"
    )


@pytest.fixture
def grind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "src").mkdir()
    (root / _SCHEMA_REL).parent.mkdir(parents=True)
    shutil.copy(_REPO / _SCHEMA_REL, root / _SCHEMA_REL)
    (root / f"tests/{_FLAKY[0]}.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    for stem, src, _ in (*_SHARED, _LONE):
        (root / f"tests/{stem}.py").write_text(_red_test(src), encoding="utf-8")
        (root / f"src/{src}.py").write_text("VALUE = 1\n", encoding="utf-8")

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    text = _FIXTURE_PROFILE.read_text(encoding="utf-8")
    narrow = "    batch_size: {default: 4}\n    triage_depth: {default: standard}\n    window: 2"
    assert narrow in text
    (profile_dir / "bug.yaml").write_text(text.replace(narrow, narrow.replace("default: 4", "default: 2"), 1), encoding="utf-8")

    failing = (
        FailingTest(file=f"tests/{_FLAKY[0]}.py", name=_FLAKY[1], message="AssertionError: once"),
        *(FailingTest(file=f"tests/{s}.py", name="test_x", message=m) for s, _, m in _SHARED),
        FailingTest(file=f"tests/{_LONE[0]}.py", name="test_x", message=_LONE[2]),
    )
    minted = [mint_tf_rows(root, run_id, failing, target_ref="redset.txt") for run_id in _MINT_RUNS]
    debt = append_queue_entry(
        "bug-backlog", caller_worktree=root, title="debt: stale cite", body="cites a file removed since",
        status="open", surface=_GONE_SURFACE, severity="P1", tags=["debt"], evidence="audit.md",
        repro_steps="read the cited file",
    )["out_path"]
    reap = root / reap_record_rel()
    reap.parent.mkdir(parents=True)
    reap.write_text(
        json.dumps({"head_sha": "f" * 40, "ran_at": "2026-11-15T00:00:00+00:00", "red": [Path(i).stem for i in minted[0]]}),
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    # On disk (so admission's liveness gate passes) but absent from HEAD.
    (root / _GONE_SURFACE).parent.mkdir()
    (root / _GONE_SURFACE).write_text("pending = True\n", encoding="utf-8")
    return {"root": root, "profile_dir": profile_dir, "minted": minted, "debt_id": Path(debt).stem}


def _rows_by_id(root: Path) -> dict[str, dict]:
    return {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in (root / "state/bug-backlog").rglob("*.yaml")}


def _batches(script: str) -> list[dict]:
    line = next(l for l in script.splitlines() if l.startswith("const BATCHES = "))
    return json.loads(line[len("const BATCHES = "):-1])


def _tf_id(grind: dict, stem: str) -> str:
    ids = [i for i in grind["minted"][0] if stem.replace("_", "-") in i]
    assert len(ids) == 1, (stem, grind["minted"][0])
    return ids[0]


def test_a_grinder_run_meets_the_whole_exit_criterion(grind) -> None:
    root = grind["root"]

    # Mints each row once: the second run id reuses the first run's rows.
    assert grind["minted"][0] == grind["minted"][1]
    assert len(set(grind["minted"][0])) == 4
    queue_rows = _rows_by_id(root)
    assert len(queue_rows) == 5

    run_dir = root / "state" / "queue-grind" / "bug" / f"run-{_RUN_STAMP}"
    run_dir.mkdir(parents=True)
    emission = emit_queue_script(
        "bug", "standard", None,  # default entry point: no budget override
        queue=[root / "state" / "bug-backlog"], profile_dir=grind["profile_dir"], repo_root=root,
        run_dir=run_dir, session_id="sess1", **REVIEW_KW,
    )

    # Row-scaled default budget, recorded as the default.
    arithmetic = emission.receipt_extras[NOT_ADMITTED_EXTRA_KEY]["arithmetic"]
    assert arithmetic["budget_source"] == "default"
    assert arithmetic["budget_tokens"] == default_budget(5)
    assert emission.receipt_extras["admitted_count"] == 5

    # The flake and the gone-at-HEAD debt row close before dispatch; only survivors are batched.
    flaky = _tf_id(grind, _FLAKY[0])
    after = _rows_by_id(root)
    for closed_id, closed_by in ((flaky, "flake"), (grind["debt_id"], "already-green")):
        assert after[closed_id]["status"] != "open", closed_id
        assert after[closed_id]["closed_by"] == closed_by, closed_id
    shared = [_tf_id(grind, s) for s, _, _ in _SHARED]
    lone = _tf_id(grind, _LONE[0])
    survivors = {*shared, lone}
    assert {i for i, r in after.items() if r["status"] == "open"} == survivors

    batches = _batches(emission.script)
    assert {r for b in batches for r in b["rows"]} == survivors
    assert sum(len(b["rows"]) for b in batches) == len(survivors)

    # One agent per failure-signature cluster: the two rows sharing a signature ride one batch.
    shared_batches = {i for i, b in enumerate(batches) if set(b["rows"]) & set(shared)}
    assert len(shared_batches) == 1
    assert len(batches) == 2

    # The emitted script reports to the EM at 25/50/75 percent.
    assert f"const CHECKPOINT_PERCENTS = {json.dumps(list(CHECKPOINT_PERCENTS))};" in emission.script
    assert "_checkpoint();" in emission.script

    # Stub agent: one call per batch, fixes exactly the rows it is handed.
    handed: list[list[str]] = []
    green: list[dict] = []
    events = []
    sources = {_tf_id(grind, s): src for s, src, _ in (*_SHARED, _LONE)}
    settled = 0
    for batch in batches:
        handed.append(list(batch["rows"]))
        for row_id in batch["rows"]:
            write_seen_by_git(root / "src" / f"{sources[row_id]}.py", "VALUE = 2\n")
            green.append({"row_id": row_id, "outcome": "fixed", "paths": [f"src/{sources[row_id]}.py"], "deleted_paths": [], "closed_by": "fix"})
        settled += len(batch["rows"])
        events += emit_checkpoints(run_dir, settled, len(survivors), run_id=run_dir.name)
    assert not ({flaky, grind["debt_id"]} & {r for h in handed for r in h})
    assert [e.percent for e in events] == [25, 50, 75]
    progress = [json.loads(l) for l in (run_dir / PROGRESS_FILE).read_text(encoding="utf-8").splitlines()]
    assert [p["percent"] for p in progress] == [25, 50, 75]

    # One scoped commit per green row; a fix claim with no landed commit is refused.
    before = int(_git(root, "rev-list", "--count", "HEAD"))
    orphan = {"row_id": "orphan", "outcome": "fixed", "paths": [], "deleted_paths": [], "closed_by": "fix"}
    out = land_run_grind.land_green_rows(
        root, root / ".git", {"run_id": _RUN_STAMP, "grind": {"profile": "bug", "rows": [*green, orphan]}}
    )
    assert [c["row_id"] for c in out["commits"]] == [g["row_id"] for g in green]
    assert int(_git(root, "rev-list", "--count", "HEAD")) == before + len(green)
    for commit in out["commits"]:
        files = _git(root, "show", "--name-only", "--format=", commit["sha"]).split()
        assert files == [f"src/{sources[commit['row_id']]}.py"]
    assert [r["row_id"] for r in out["close_refused"]] == ["orphan"]
    assert not [l for l in _git(root, "status", "--porcelain", "--", "src").splitlines() if l.strip()]

    # Tests covering the touched files pass.
    tests = [f"tests/{s}.py" for s, _, _ in (*_SHARED, _LONE)]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
        cwd=root, capture_output=True, text=True, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
