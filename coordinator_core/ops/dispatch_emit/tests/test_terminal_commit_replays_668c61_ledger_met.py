"""Replay of example-game-repo run ask-20261109T003727-668c61: ledger met, clean review, delivery PASS, yet the example-game-repo
terminal commit refused `no-credited-chunk-landed` and stranded C1's six declared files.

The records under fixtures/replay_668c61/ are trimmed copies of the run's own (halted, recovery, manifest, run,
overlay-verdict, and C1's dispatch report). recovery.json marks C1 `blocked` (the fix leg's own report counted as an
outside write) beside a `fixed` remnant pass covering all six files. The manifest and recovery records sit in the record dir beside
run.json, so the ledger gate reads the bound ledger. The credit test is green at the run base too; the release itself
(`credit_gate.recovery_blocks_superseded`) is pinned by the not_met withheld_by pair below, which is red without it.
The `land_run` release is pinned in test_land_run_product.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RECORD_DIR_ROOT, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.testing.git_write import write_seen_by_git
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "replay_668c61"
RUN = "ask-20261109T003727-668c61"
SID = "11111111-2222-3333-4444-555555555555"
_PIPE = "tools/likeness-prototype/likeness/pipeline/"
_TESTS = "tools/likeness-prototype/tests/"
DECLARED = (
    f"{_PIPE}registry_env.py", f"{_PIPE}driver.py", f"{_PIPE}snapshot.py",
    f"{_TESTS}test_likeness_registry_env.py", f"{_TESTS}test_likeness_pipeline_driver.py",
    f"{_TESTS}test_likeness_pipeline_snapshot.py",
)
CHANGED = f"{_TESTS}test_likeness_pipeline_snapshot.py"
REPORT = ".coordinator-local/subagent-share/dispatch-reports/2026-11-09-clone-inputs-preflight-wiring-668c61/C1.md"
_VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _copy(root: Path, name: str, rel: str) -> None:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / name, dest)


def _review() -> dict:
    return reviewed(
        prep={"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": [CHANGED]},
        delivery={"verdict": "PASS", "product_files": 1},
        integration={"sidecar": "s", "unresolved": [], "confinement_violations": 0},
    )


@pytest.fixture
def replay(tmp_path, monkeypatch):
    """Base commit, a halt commit of run artifacts only, a checkpoint of two sibling test files, then C1's six
    declared files with the snapshot test dirty."""
    root = tmp_path / "repo"
    root.mkdir()
    for cmd in (("init", "-q"), ("config", "user.email", "t@example.com"), ("config", "user.name", "t"),
                ("config", "commit.gpgsign", "false")):
        _git(root, *cmd)
    sizing = json.loads((FIXTURES / "run.json").read_text(encoding="utf-8"))["sizing_rel"]
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _write(
        root, sizing,
        "schema: sizing-object\nintent: Test intent, verbatim.\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: wire the preflight\n",
    )
    for p in DECLARED:
        _write(root, p, f"# {p} base\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")

    rec = f"{RECORD_DIR_ROOT}/{RUN}"
    for name in ("halted.json", "run.json", "overlay-verdict.json"):
        _copy(root, name, f"{rec}/{name}")
    _git(root, "add", "-f", rec)
    _git(root, "commit", "-q", "-m", "warp: record halted run artifacts")

    for p in (DECLARED[3], DECLARED[4]):
        _write(root, p, f"# {p} checkpointed\n")
    _git(root, "add", DECLARED[3], DECLARED[4])
    _git(root, "commit", "-q", "-m", "checkpoint(wave 0): 1 row - C1")

    run_dir = f"{RUN_DIR_ROOT}/{RUN}"
    _copy(root, "manifest.json", f"{rec}/manifest.json")
    _copy(root, "recovery.json", f"{rec}/recovery.json")
    _copy(root, "C1.md", REPORT)
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="wire the preflight", paths=DECLARED, report=REPORT),))
    _write(root, f"{run_dir}/commit-request.txt", render_marker(request) + "\n")
    _write(root, "run.mjs", f"// emitted\n{ASK_MANIFEST_MARKER}{rec}/manifest.json\n")
    write_seen_by_git(root / CHANGED, "# hermetic snapshot tests\n", newline="\n")
    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, sizing)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        "coordinator_core.session.claims.list_claims_by_session_checked", lambda sid, cwd=None: ([], [])
    )
    return root


def _terminal(root: Path, **extra) -> dict:
    return tc._handler(
        {
            "script_path": "run.mjs", "run_id": RUN, "incomplete_chunks": ["C1"], "recovery_withheld": ["C1"],
            "inline_review": _review(), "session_id": SID, "ledger_verdict": _VERDICT, **extra,
        },
        repo_root=root / ".git",
    )


def test_ledger_met_run_with_c1_recovery_blocked_credits_c1_and_lands_its_changed_file(replay):
    out = _terminal(replay)
    assert out.get("refused") != "no-credited-chunk-landed", out
    assert out["committed"] is True and out["partial_committed"] == ["C1"], out
    landed = _git(replay, "log", "--name-only", "--format=", "-3").split()
    assert CHANGED in landed


_WITHHELD_ROW = {"items": [{"id": "R1", "status": "not_met", "essential": True, "withheld_by": "C1"}]}


def test_a_blocked_chunk_a_fixed_remnant_covers_no_longer_excuses_its_not_met_row(replay):
    out = _terminal(replay, ledger_verdict=_WITHHELD_ROW)
    assert out["refused"] == "ledger-unmet", out
    assert "withheld_by C1 names no gated chunk" in out["error"], out


def test_a_blocked_chunk_no_fixed_remnant_covers_still_excuses_its_not_met_row(replay):
    rec = replay / RECORD_DIR_ROOT / RUN / "recovery.json"
    data = json.loads(rec.read_text(encoding="utf-8"))
    data["remnant"]["status"] = "open"
    rec.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    out = _terminal(replay, ledger_verdict=_WITHHELD_ROW)
    assert out.get("refused") is None and out["committed"] is True, out


def test_a_stranded_chunk_with_no_delivered_report_is_never_credited_by_the_ledger_alone(replay):
    (replay / REPORT).unlink()
    doc = "docs/delivery/2026-11/ask-20261109T003727-668c61.md"
    _write(replay, doc, "# delivered\n")
    request = CommitRequest(chunks=(
        ChunkCommit(id="C1", title="wire the preflight", paths=DECLARED, report=REPORT),
        ChunkCommit(id="docs", title="run docs", paths=(doc,)),
    ))
    _write(replay, f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt", render_marker(request) + "\n")
    out = _terminal(replay)
    assert out.get("refused") == "no-credited-chunk-landed", out
    assert _git(replay, "status", "--porcelain", "--", CHANGED).startswith(" M")
