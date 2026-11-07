"""A merge commit or another deliverable's commit touching a row's writes is never that row's landing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.plan_row_coding import rows_landed_outside_executor

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REL = "docs/plans/p.md"
_OWN = "dlv-own-111111"
_ROW = [{"id": "C1", "writes": ["c1.py"]}]
_PLAN = (
    f"---\ntitle: p\ndeliverable_id: \"{_OWN}\"\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: c1.py\n  writes: ['c1.py']\n"
    "  disposition: open\n  deferred: false\n```\n"
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(root: Path, files: dict, message: str) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


def _repo(root: Path) -> Path:
    _git(root, "init", "-q", "-b", "main")
    _commit(root, {"seed": "0", "c1.py": "0"}, "seed")
    _commit(root, {_REL: _PLAN}, "plan")
    return root


def test_merge_commit_touching_every_write_credits_nothing(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "other")
    _commit(repo, {"c1.py": "other side"}, "other side edit")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"unrelated": "1"}, "main side")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge other", "other")
    assert len(_git(repo, "rev-list", "--parents", "-n1", "HEAD").split()) == 3
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {}


def test_foreign_deliverable_commit_credits_nothing(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, {"c1.py": "1"}, "other work\n\nDeliverable-Id: dlv-someone-else-222222")
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {}


def test_commit_naming_own_deliverable_is_credited(tmp_path):
    repo = _repo(tmp_path)
    landed = _commit(repo, {"c1.py": "1"}, f"own work\n\nDeliverable-Id: {_OWN}")
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {"C1": landed}


def test_commit_naming_own_among_several_deliverables_is_credited(tmp_path):
    repo = _repo(tmp_path)
    landed = _commit(repo, {"c1.py": "1"}, f"both\n\nDeliverable-Id: dlv-x-333333\nDeliverable-Id: {_OWN}")
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {"C1": landed}


def test_credit_skips_foreign_and_merge_to_reach_the_genuine_landing(tmp_path):
    repo = _repo(tmp_path)
    landed = _commit(repo, {"c1.py": "1"}, f"own work\n\nDeliverable-Id: {_OWN}")
    _commit(repo, {"c1.py": "2"}, "other work\n\nDeliverable-Id: dlv-someone-else-222222")
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {"C1": landed}


def test_trailerless_commit_touching_every_write_credits_nothing(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, {"c1.py": "1"}, "hand edit")
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {}


def test_trailerless_commit_is_stepped_over_to_reach_the_genuine_landing(tmp_path):
    repo = _repo(tmp_path)
    landed = _commit(repo, {"c1.py": "1"}, f"own work\n\nDeliverable-Id: {_OWN}")
    _commit(repo, {"c1.py": "2"}, "hand edit")
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {"C1": landed}


def test_own_trailer_commit_withholding_the_row_write_credits_nothing(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, {"c1.py": "1"}, f"checkpoint\n\nDeliverable-Id: {_OWN}\nCheckpoint-Withheld: c1.py")
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {}


def test_commit_naming_this_plan_by_halt_plan_is_credited(tmp_path):
    repo = _repo(tmp_path)
    landed = _commit(repo, {"c1.py": "1"}, f"halt record\n\nHalt-Plan: {_REL}")
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {"C1": landed}
