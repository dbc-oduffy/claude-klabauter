"""A refire under a fresh run id baselines from the first pass, so review prep slices its earlier edit."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit import ask_stage, write_baseline
from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow, record_dir
from coordinator_core.ops.dispatch_emit.kept_work import fingerprint
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

TARGET = "pkg/a.py"
FIRST, REFIRE = "ask-first", "warp-refire"
ROWS = [ManifestRow("C1", "executor", "sonnet", "b.md", (TARGET,), 0)]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "pkg").mkdir()
    (tmp_path / TARGET).write_text("x = 1\n", encoding="utf-8", newline="\n")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8", newline="\n")


def _halted_first_pass(repo: Path) -> str:
    """Stage records the clean baseline, the executor edits the file, the run halts before review."""
    head = _git(repo, "rev-parse", "HEAD").strip()
    rec = record_dir(repo, FIRST)
    clean = ask_stage._stage_write_baseline(repo, ROWS)
    _dump(rec / "manifest.json", {"review_declared_paths": [TARGET], "write_baseline": clean})
    (repo / TARGET).write_text("x = 2\n", encoding="utf-8", newline="\n")
    _dump(rec / "halted.json", {"status": "resumed", "halted": "x", "run_base_sha": head})
    return head


def _refire_record(repo: Path) -> Path:
    rec = record_dir(repo, REFIRE)
    _dump(rec / "run.json", {"resumed_from": FIRST})
    return rec


def test_refire_review_prep_slices_the_first_passes_uncommitted_edit(repo):
    head = _halted_first_pass(repo)
    rec = _refire_record(repo)

    baseline = ask_stage._stage_write_baseline(repo, ROWS, rec)
    assert baseline[TARGET] != fingerprint(repo, TARGET)
    assert write_baseline.unwritten(repo, baseline, [TARGET]) == []

    _dump(rec / "manifest.json", {"review_declared_paths": [TARGET], "write_baseline": baseline})
    reply = rp._handler(
        {
            "base": head, "head": head, "slice_prefix": "prep-r", "plan_path": "docs/plans/p.md",
            "session_id": "sess-own", "run_id": REFIRE,
        },
        repo,
    )
    assert reply["error"] is None
    assert [f for s in reply["slices"] for f in s["files"]] == [TARGET], reply


def test_oldest_predecessor_baseline_outranks_a_polluted_nearer_one(repo):
    _halted_first_pass(repo)
    mid = "ask-mid"
    _dump(record_dir(repo, mid) / "run.json", {"resumed_from": FIRST})
    _dump(record_dir(repo, mid) / "halted.json", {"run_base_sha": "b" * 40})
    _dump(record_dir(repo, mid) / "manifest.json", {"write_baseline": {TARGET: fingerprint(repo, TARGET)}})
    rec = record_dir(repo, REFIRE)
    _dump(rec / "run.json", {"resumed_from": mid})

    original = json.loads((record_dir(repo, FIRST) / "manifest.json").read_text(encoding="utf-8"))["write_baseline"]
    assert ask_stage._stage_write_baseline(repo, ROWS, rec) == original


def test_unlinked_run_still_captures_the_live_tree(repo):
    _halted_first_pass(repo)
    rec = record_dir(repo, "ask-other")
    assert ask_stage._stage_write_baseline(repo, ROWS, rec) == {TARGET: fingerprint(repo, TARGET)}
