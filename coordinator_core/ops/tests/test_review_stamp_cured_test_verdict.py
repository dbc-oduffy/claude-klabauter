"""review_stamp.mint reads a baseline-cleared sidecar and a later plan-bound tests record over a stale run-record fail."""

from __future__ import annotations

import json
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.completion_receipts.test_verdict import record_test_verdict
from coordinator_core.ops import review_stamp as m
from coordinator_core.ops.dispatch_emit.verdict_supersession import record_tests_supersession
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_FLAGS = no_console_creationflags()
_PLAN_ID = "pln-example-abc123"
_FAIL = {"status": "fail", "run": 5, "failed": 2, "sidecar": None}
_PASS = {"status": "pass", "run": 5, "failed": 0, "sidecar": None}


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


def _record(repo: Path, head: str, *, tests: dict, recorded_at: str = "2000-01-01T00:00:00Z") -> Path:
    data = {
        "kind": "superseding-review",
        "plan_id": _PLAN_ID,
        "recorded_at": recorded_at,
        "commit_range": {"base": head, "head": head},
        "supersedes": None,
        "prep_sidecar": None,
        "unresolved": [],
        "confinement_violations": 0,
        "fixes_applied": 0,
        "slices": 1,
        "brief_conformance": {"items": 0, "met": 0, "unmet": 0},
        "prep": {"run_base_sha": head, "product_files": 1, "foreign_claims": [], "slice_files": ["a.txt"]},
        "delivery": {"verdict": "PASS", "product_files": 1},
        "tests": tests,
        "criterion": {"status": "met", "observation": "the criterion held", "sidecar": None},
    }
    path = repo / "state" / "superseding-reviews" / "2026-10" / "rec.md"
    _frontmatter(path, data)
    return path


def _plan(repo: Path) -> Path:
    return repo / "docs" / "plans" / "example.md"


def _mint(repo: Path, record: Path, build_test_path=None):
    return m.mint(_plan(repo), repo, build_test_path=build_test_path, superseding_record=record)


def _tests_record(repo: Path, tests: dict, plan_id: str = _PLAN_ID) -> str:
    time.sleep(0.01)
    return record_tests_supersession(repo_root=repo, plan_id=plan_id, tests=tests)


def test_a_build_test_sidecar_of_all_pre_existing_failures_mints(tmp_path):
    repo, head = _repo(tmp_path)
    sidecar = repo / ".coordinator-local" / "subagent-share" / "s1" / "test-runner.md"
    _frontmatter(sidecar, {"status": "open", "agent_type": "test-runner"})
    record_test_verdict(
        repo,
        {
            "status": "fail",
            "tests_run": 5,
            "tests_failed": 2,
            "sidecar_path": str(sidecar),
            "baseline_method": "export",
            "baseline": {"pre_existing": ["a", "b"], "caused": [], "unverified": []},
        },
    )
    stamp = _mint(repo, _record(repo, head, tests=_FAIL), build_test_path=str(sidecar))
    assert stamp["build_test"]["verdict"] == "pass"


def test_a_later_plan_bound_pass_cures_a_stale_run_record_fail(tmp_path):
    repo, head = _repo(tmp_path)
    record = _record(repo, head, tests=_FAIL)
    with pytest.raises(m.MintRefusal, match="build/test verdict is 'fail'"):
        _mint(repo, record)
    rel = _tests_record(repo, _PASS)
    stamp = _mint(repo, record)
    assert stamp["build_test"]["verdict"] == "pass"
    assert stamp["build_test"]["sidecar"] == rel


def test_a_tests_record_older_than_the_run_record_cures_nothing(tmp_path):
    repo, head = _repo(tmp_path)
    _tests_record(repo, _PASS)
    record = _record(repo, head, tests=_FAIL, recorded_at="2999-01-01T00:00:00Z")
    with pytest.raises(m.MintRefusal, match="build/test verdict is 'fail'"):
        _mint(repo, record)


def test_a_newer_fail_record_displaces_the_pass(tmp_path):
    repo, head = _repo(tmp_path)
    record = _record(repo, head, tests=_FAIL)
    _tests_record(repo, _PASS)
    _tests_record(repo, _FAIL)
    with pytest.raises(m.MintRefusal, match="build/test verdict is 'fail'"):
        _mint(repo, record)


def test_a_pass_bound_to_another_plan_cures_nothing(tmp_path):
    repo, head = _repo(tmp_path)
    record = _record(repo, head, tests=_FAIL)
    _tests_record(repo, _PASS, plan_id="pln-other-ffffff")
    with pytest.raises(m.MintRefusal, match="build/test verdict is 'fail'"):
        _mint(repo, record)


def test_a_caused_failure_with_no_record_still_refuses(tmp_path):
    repo, head = _repo(tmp_path)
    caused = {
        **_FAIL,
        "baseline_method": "export",
        "baseline": {"pre_existing": ["a"], "caused": ["b"], "unverified": []},
    }
    with pytest.raises(m.MintRefusal, match="build/test verdict is 'fail'"):
        _mint(repo, _record(repo, head, tests=caused))
