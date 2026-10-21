"""commit_halted_record persists run-outcome.json and writes a commit-halt pointer for an all-landed commit refusal."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RUN_OUTCOME_FILE,
    ledger_halt_path,
    read_ledger_halt,
    read_run_outcome,
)
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261015T000000-aaaaaa"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    return root


def _write_record(root: Path, run_id: str, names=("ask.md", "pm-intent.md", "run.json")) -> Path:
    rec = root / RECORD_DIR_ROOT / run_id
    rec.mkdir(parents=True, exist_ok=True)
    for name in names:
        (rec / name).write_text(f"{name}\n", encoding="utf-8")
    return rec


PLAN = "docs/plans/2026-10-16-demo.md"
SIZING = "state/sizings/demo.yaml"
COMMIT_HALT = {"halted": "commit", "plan": PLAN, "sizing": SIZING}
REVIEW = {"integration_stem": "s", "slices": 1, "fixes": 1}


def _outcome(**extra) -> dict:
    return {"inline_review": REVIEW, "incomplete_chunks": [], "landed_chunks": ["C1"], **extra}


def _call(root, halted, **extra) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: halted, **extra}
    return land_run._handler(params, repo_root=root)


def _with_sizing(root):
    rec = _write_record(root, RUN)
    (rec / "run.json").write_text(f'{{"sizing_rel": "{SIZING}"}}\n', encoding="utf-8")
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / SIZING).write_text("a: 1\n", encoding="utf-8")


def test_commit_halt_with_outcome_lands_outcome_and_pointer(repo):
    _with_sizing(repo)

    out = _call(repo, COMMIT_HALT, run_outcome=_outcome())

    assert out["committed"] is True, out
    rel = f"{RECORD_DIR_ROOT}/{RUN}/{RUN_OUTCOME_FILE}"
    assert rel in _git(["show", "--name-only", "--format=", "HEAD"], repo)
    assert read_run_outcome(repo, RUN)["inline_review"] == REVIEW
    halt = read_ledger_halt(ledger_halt_path(repo, PLAN))
    assert halt is not None
    assert (halt.run_id, halt.plan_rel, halt.sizing_rel, halt.blocking) == (RUN, PLAN, SIZING, ())
    assert (halt.halted, halt.outcome) == ("commit", rel)


def test_commit_halt_with_incomplete_chunk_writes_neither(repo):
    _with_sizing(repo)

    _call(repo, COMMIT_HALT, run_outcome=_outcome(incomplete_chunks=["C2"]))

    assert not (repo / RECORD_DIR_ROOT / RUN / RUN_OUTCOME_FILE).exists()
    assert not ledger_halt_path(repo, PLAN).exists()


def test_commit_halt_without_outcome_unlinks_stale_pointer(repo):
    _with_sizing(repo)
    _call(repo, COMMIT_HALT, run_outcome=_outcome())
    assert ledger_halt_path(repo, PLAN).is_file()

    _call(repo, COMMIT_HALT)

    assert not ledger_halt_path(repo, PLAN).exists()


def test_ledger_check_pointer_bytes_unchanged(repo):
    _with_sizing(repo)

    _call(repo, {"halted": "ledger-check", "plan": PLAN, "items": [{"id": "R1"}]})

    data = json.loads(ledger_halt_path(repo, PLAN).read_text(encoding="utf-8"))
    assert data == {"run_id": RUN, "plan": PLAN, "sizing": SIZING, "blocking": ["R1"]}
