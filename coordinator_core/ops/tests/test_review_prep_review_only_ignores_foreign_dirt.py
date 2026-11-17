"""review.prep with run_id: a run whose declared paths are clean adopts no foreign dirt.

Reproduces run 32aa8c: a review-only refire declared files already committed and unchanged; a live
peer's uncommitted edits elsewhere must not become the review slice. A run with an edit of its own
still adopts an undeclared edit.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.tests.conftest import stub_own_write_claims
from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot as snap
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
RUN = "ask-20261111T042347-32aa8c"
DECLARED = "control/server/nanite.ts"
FOREIGN = "tools/likeness/proc_tree.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    for rel, text in (
        (DECLARED, "export const a = 1;\n"),
        (FOREIGN, "x = 1\n"),
        ("docs/plans/p.md", "---\nplan_id: pln-test-1\n---\n\n# p\n"),
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    record = record_dir(tmp_path, RUN)
    record.mkdir(parents=True, exist_ok=True)
    (record / "manifest.json").write_text(json.dumps({"review_declared_paths": [DECLARED]}), encoding="utf-8")
    snap.write_snapshot(tmp_path, record)
    time.sleep(0.05)
    return tmp_path


def _prep(repo: Path) -> dict:
    return rp._handler(
        {
            "run_id": RUN,
            "base": _git(repo, "rev-parse", "HEAD").strip(),
            "worktree": True,
            "plan_path": "docs/plans/p.md",
            "session_id": SESSION,
        },
        repo,
    )


def test_clean_declared_paths_adopt_no_foreign_dirt(repo):
    (repo / FOREIGN).write_text("x = 2  # a peer run's live edit\n", encoding="utf-8")
    reply = _prep(repo)
    assert reply["error"] is None
    assert reply["undeclared_edits"] == []
    assert FOREIGN not in reply["reviewed_sha256"]
    assert FOREIGN not in json.dumps(reply["slices"])
    assert FOREIGN not in Path(reply["whole_diff_path"]).read_text(encoding="utf-8")


def test_unit_clean_declared_returns_nothing_where_the_scan_found_foreign_dirt(repo):
    (repo / FOREIGN).write_text("x = 2\n", encoding="utf-8")
    assert snap.undeclared_edits(repo, record_dir(repo, RUN), [DECLARED]) == [FOREIGN]
    assert rp.undeclared_edits_for_run(repo, RUN, [DECLARED], SESSION) == []


@pytest.fixture
def own_session_claims(monkeypatch):
    stub_own_write_claims(monkeypatch, SESSION)


def test_own_declared_edit_still_adopts_an_undeclared_edit(repo, own_session_claims):
    (repo / DECLARED).write_text("export const a = 2;\n", encoding="utf-8")
    (repo / FOREIGN).write_text("x = 2\n", encoding="utf-8")
    reply = _prep(repo)
    assert reply["error"] is None
    assert reply["undeclared_edits"] == [FOREIGN]
    assert FOREIGN in reply["reviewed_sha256"]


def test_non_product_declared_path_is_not_proof_of_clean(repo):
    (repo / "state").mkdir(exist_ok=True)
    (repo / "state" / "own.md").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "state/own.md")
    assert rp._declared_provably_clean(repo, ["state/own.md"]) is False


def test_untracked_declared_path_is_not_proof_of_clean(repo, own_session_claims):
    (repo / "new").mkdir()
    (repo / "new" / "own.py").write_text("y = 1\n", encoding="utf-8")
    (repo / FOREIGN).write_text("x = 2\n", encoding="utf-8")
    assert rp.undeclared_edits_for_run(repo, RUN, ["new/own.py"], SESSION) == [FOREIGN]
