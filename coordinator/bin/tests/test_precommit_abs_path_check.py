"""Behavior tests for coordinator-precommit-abs-path-check over real tmp git repos."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SCRIPT = Path(__file__).resolve().parents[1] / "coordinator-precommit-abs-path-check"
# Built from parts so this test file carries no literal machine path itself.
BAD = "E" + ":/ptbase/x"
PLAN = "docs/plans/x.md"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _run(repo: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=repo, capture_output=True, text=True, env=dict(os.environ)
    )


def test_new_citation_in_staged_plan_is_refused_with_file_and_line(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, PLAN, f"# t\n\nsee {BAD} here\n")
    _git(repo, "add", PLAN)
    proc = _run(repo)
    assert proc.returncode == 1
    assert f"{PLAN}:3" in proc.stderr
    assert "drive-letter" in proc.stderr
    assert f"fix-concrete-path-citations --apply {PLAN}" in proc.stderr


def test_marker_with_reason_discharges_but_bare_marker_does_not(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, PLAN, f"see {BAD} <!-- abs-path-ok: PM verbatim -->\n")
    _git(repo, "add", PLAN)
    assert _run(repo).returncode == 0
    _write(repo, PLAN, f"see {BAD} abs-path-ok:\n")
    _git(repo, "add", PLAN)
    assert _run(repo).returncode == 1


def test_carve_out_site_discharges_a_code_surface_line(tmp_path):
    repo = _repo(tmp_path)
    line = f'X = "{BAD}"  # abs-path-ok: fixture'
    _write(repo, "tool.py", line + "\n")
    _git(repo, "add", "tool.py")
    assert _run(repo).returncode == 1  # reason alone never discharges code
    escaped = line.replace("\\", "\\\\").replace('"', '\\"')
    _write(
        repo,
        ".abs-path-carve-outs.toml",
        '[[carve_out]]\nid = "fx"\nreason = "fixture"\n'
        f'sites = [{{ path = "tool.py", line = "{escaped}" }}]\n',
    )
    assert _run(repo).returncode == 0


def test_preexisting_citation_untouched_by_the_diff_passes(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, PLAN, f"see {BAD}\n")
    _git(repo, "add", PLAN)
    _git(repo, "commit", "-q", "-m", "seed")
    _write(repo, PLAN, f"see {BAD}\n\nan unrelated added line\n")
    _git(repo, "add", PLAN)
    assert _run(repo).returncode == 0


def test_duplicating_a_preexisting_citation_is_refused(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, PLAN, f"see {BAD}\n")
    _git(repo, "add", PLAN)
    _git(repo, "commit", "-q", "-m", "seed")
    _write(repo, PLAN, f"see {BAD}\nagain {BAD}\n")
    _git(repo, "add", PLAN)
    assert _run(repo).returncode == 1


def test_nothing_staged_and_clean_staged_exit_zero(tmp_path):
    repo = _repo(tmp_path)
    assert _run(repo).returncode == 0
    _write(repo, PLAN, "# clean\n")
    _git(repo, "add", PLAN)
    assert _run(repo).returncode == 0


def test_posix_home_shape_is_refused_too(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, PLAN, "see " + "/home" + "/m7k2zq/work/x\n")
    _git(repo, "add", PLAN)
    proc = _run(repo)
    assert proc.returncode == 1
    assert f"{PLAN}:1" in proc.stderr


def _merge_in_progress(tmp_path: Path, side_text: str, main_text: str = "# main\n") -> Path:
    """Repo mid-merge: side branch adds PLAN with `side_text`; main added OTHER."""
    repo = _repo(tmp_path)
    _write(repo, "seed.md", "seed\n")
    _git(repo, "add", "seed.md")
    _git(repo, "commit", "-q", "-m", "seed")
    _git(repo, "checkout", "-q", "-b", "side")
    _write(repo, PLAN, side_text)
    _git(repo, "add", PLAN)
    _git(repo, "commit", "-q", "-m", "side")
    _git(repo, "checkout", "-q", "-")
    _write(repo, "other.md", main_text)
    _git(repo, "add", "other.md")
    _git(repo, "commit", "-q", "-m", "main")
    _git(repo, "merge", "--no-commit", "--no-ff", "side")
    assert (repo / ".git" / "MERGE_HEAD").exists()
    return repo


def test_merge_commit_passes_citations_already_on_the_merged_in_parent(tmp_path):
    repo = _merge_in_progress(tmp_path, f"see {BAD}\n")
    assert _run(repo).returncode == 0


def test_merge_commit_still_refuses_a_citation_absent_from_both_parents(tmp_path):
    repo = _merge_in_progress(tmp_path, f"see {BAD}\n")
    _write(repo, PLAN, f"see {BAD}\nfresh {BAD}/new\n")
    _git(repo, "add", PLAN)
    proc = _run(repo)
    assert proc.returncode == 1
    assert f"{PLAN}:2" in proc.stderr


def test_non_merge_commit_of_the_same_content_is_still_refused(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, PLAN, f"see {BAD}\n")
    _git(repo, "add", PLAN)
    assert _run(repo).returncode == 1


def test_runs_in_any_repo_not_only_the_meta_repo(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, PLAN, f"see {BAD}\n")
    _git(repo, "add", PLAN)
    assert _run(repo).returncode == 1
