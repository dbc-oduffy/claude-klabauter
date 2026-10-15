"""review_stamp.mint on a run whose delivery slot is null: a sha-bound re-run record clears it."""

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
_PLAN_ID = "pln-nulldelivery-abc123"
_RUN_REL = ".coordinator-local/subagent-share/s1/run-record.md"


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


def _repo(tmp_path: Path) -> tuple[Path, str, dict]:
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
            created: 2026-10-15
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
    _git(repo, "commit", "-q", "-m", "work")
    terminal = _git(repo, "rev-parse", "HEAD")
    run = {
        "recorded_at": "2026-10-15T10:00:00+00:00",
        "prep": {"run_base_sha": terminal, "product_files": 1, "foreign_claims": [], "slice_files": ["a.txt"]},
        "delivery": None,
        "tests": {"status": "pass", "run": 3, "failed": 0, "sidecar": None},
        "criterion": {"status": "met", "observation": "held", "sidecar": None},
        "slices": 1,
    }
    _frontmatter(repo / _RUN_REL, run)
    return repo, terminal, run


def _rerun_record(repo: Path, **overrides) -> Path:
    data = {
        "kind": "delivery-verdict",
        "supersedes": _RUN_REL,
        "recorded_at": "2026-10-15T11:00:00+00:00",
        "delivery": {"verdict": "PASS", "product_files": 1},
    }
    data.update(overrides)
    path = repo / "state" / "delivery-verdicts" / "2026-10" / "rerun.md"
    _frontmatter(path, data)
    return path


def _plan(repo: Path) -> Path:
    return repo / "docs" / "plans" / "example.md"


def _mint(repo: Path, terminal: str):
    return m.mint(_plan(repo), repo, build_test_path=None, resolved=(terminal, repo / _RUN_REL, _run_data(repo)))


def _run_data(repo: Path) -> dict:
    return m._load_sidecar(repo / _RUN_REL)


def test_same_sha_record_mints_at_run_terminal(tmp_path):
    repo, terminal, _ = _repo(tmp_path)
    (repo / "b.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "later")
    _rerun_record(repo, diff_sha=terminal, head_sha=_git(repo, "rev-parse", "HEAD"))
    stamp = _mint(repo, terminal)
    assert stamp["delivery"]["verdict"] == "PASS"
    assert stamp["terminal_commit_sha"] == terminal


def test_different_sha_record_refuses(tmp_path):
    repo, terminal, _ = _repo(tmp_path)
    _rerun_record(repo, diff_sha="0" * 40)
    with pytest.raises(m.MintRefusal, match="re-verify the run's terminal commit") as exc:
        _mint(repo, terminal)
    assert "0" * 40 in str(exc.value) and terminal in str(exc.value) and "rerun.md" in str(exc.value)
    assert "review_stamp:" not in _plan(repo).read_text(encoding="utf-8")


def test_record_without_diff_sha_refuses(tmp_path):
    repo, terminal, _ = _repo(tmp_path)
    _rerun_record(repo)
    with pytest.raises(m.MintRefusal, match="carries no diff_sha") as exc:
        _mint(repo, terminal)
    assert "rerun.md" in str(exc.value)


def test_no_record_refusal_names_reason_and_route(tmp_path):
    repo, terminal, run = _repo(tmp_path)
    _frontmatter(repo / _RUN_REL, {**run, "delivery_not_run": "rejected: boom"})
    with pytest.raises(m.MintRefusal) as exc:
        _mint(repo, terminal)
    text = str(exc.value)
    assert "delivery not run: rejected: boom" in text
    assert f"--reverify-delivery {_RUN_REL}" in text
