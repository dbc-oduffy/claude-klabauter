"""review_stamp.mint(superseding_record=...) with a test-runner --build-test sidecar and no delivery stage."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops import review_stamp as m
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_FLAGS = no_console_creationflags()
_PLAN_ID = "pln-example-abc123"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, **_FLAGS
    ).stdout.strip()


def _frontmatter(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in data.items()) + "\n---\n\nbody\n",
        encoding="utf-8",
    )


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "docs" / "plans" / "example.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            title: Example
            created: 2026-09-27
            author: test
            status: executing
            plan_id: {_PLAN_ID}
            scope:
              - docs/plans/example.md
            ---

            # Example
            """
        ),
        encoding="utf-8",
    )
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "stranded work")
    return repo, _git(repo, "rev-parse", "HEAD")


def _record(repo: Path, head: str, **overrides) -> Path:
    data = {
        "kind": "superseding-review",
        "plan_id": _PLAN_ID,
        "commit_range": {"base": head, "head": head},
        "supersedes": None,
        "prep_sidecar": None,
        "unresolved": [],
        "confinement_violations": 0,
        "fixes_applied": 0,
        "slices": 1,
        "brief_conformance": {"items": 0, "met": 0, "unmet": 0},
        "prep": {"run_base_sha": head, "product_files": 1, "foreign_claims": [], "slice_files": ["a.txt"]},
        "criterion": {"status": "met", "observation": "the criterion held", "sidecar": None},
    }
    data.update(overrides)
    path = repo / "state" / "superseding-reviews" / "2026-10" / "rec.md"
    _frontmatter(path, data)
    return path


def _test_runner(repo: Path) -> Path:
    path = repo / ".coordinator-local" / "subagent-share" / "s1" / "test-runner.md"
    _frontmatter(path, {"status": "pass", "run": 7, "failed": 0})
    return path


def _plan(repo: Path) -> Path:
    return repo / "docs" / "plans" / "example.md"


def test_record_with_no_delivery_stage_mints_pass_off_a_met_criterion(tmp_path):
    repo, head = _repo(tmp_path)
    stamp = m.mint(
        _plan(repo), repo, build_test_path=str(_test_runner(repo)), superseding_record=_record(repo, head)
    )
    assert stamp["delivery"]["verdict"] == "PASS"
    assert stamp["build_test"]["verdict"] == "pass"
    assert m.check(_plan(repo), repo) is None


def test_recorded_fail_delivery_still_refuses(tmp_path):
    repo, head = _repo(tmp_path)
    record = _record(repo, head, delivery={"verdict": "FAIL", "product_files": 1})
    with pytest.raises(m.MintRefusal, match="delivery verdict is 'FAIL'"):
        m.mint(_plan(repo), repo, build_test_path=str(_test_runner(repo)), superseding_record=record)


@pytest.mark.parametrize(
    "criterion",
    [
        None,
        {"status": "indeterminate", "observation": "unsure", "sidecar": None},
        {"status": "met", "observation": "  ", "sidecar": None},
    ],
)
def test_no_delivery_without_a_judged_criterion_still_refuses(tmp_path, criterion):
    repo, head = _repo(tmp_path)
    record = _record(repo, head, criterion=criterion)
    with pytest.raises(m.MintRefusal, match="delivery verdict is None, not PASS"):
        m.mint(_plan(repo), repo, build_test_path=str(_test_runner(repo)), superseding_record=record)
    assert "review_stamp:" not in _plan(repo).read_text(encoding="utf-8")
