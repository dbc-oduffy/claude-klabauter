"""review_stamp.mint on a null delivery verdict: a zero-claim plan with a met criterion and
passing tests mints; a plan carrying unbacked claims, or a run that gave no benign reason, refuses."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops import review_stamp as m
from coordinator_core.ops.review_mint.roster import DELIVERY_NOT_ROSTERED
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_FLAGS = no_console_creationflags()
_PLAN_ID = "pln-zeroclaim-abc123"
_RUN_REL = ".coordinator-local/subagent-share/s1/run-record.md"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, **_FLAGS
    ).stdout.strip()


def _write_run(repo: Path, data: dict) -> None:
    path = repo / _RUN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in data.items()) + "\n---\n\nbody\n",
        encoding="utf-8",
    )


def _repo(tmp_path: Path, **overrides) -> tuple[Path, str]:
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
            created: 2026-10-16
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
        "recorded_at": "2026-10-16T10:00:00+00:00",
        "prep": {"run_base_sha": terminal, "product_files": 1, "foreign_claims": [], "slice_files": ["a.txt"]},
        "delivery": None,
        "delivery_not_run": DELIVERY_NOT_ROSTERED,
        "tests": {"status": "pass", "run": 3, "failed": 0, "sidecar": None},
        "criterion": {"status": "met", "observation": "held", "sidecar": None},
        "slices": 1,
    }
    run.update(overrides)
    _write_run(repo, run)
    return repo, terminal


def _mint(repo: Path, terminal: str):
    plan = repo / "docs" / "plans" / "example.md"
    data = m._load_sidecar(repo / _RUN_REL)
    return m.mint(plan, repo, build_test_path=None, resolved=(terminal, repo / _RUN_REL, data))


def test_zero_claim_met_passing_mints_without_rejudge_or_build_test(tmp_path):
    repo, terminal = _repo(tmp_path)
    stamp = _mint(repo, terminal)
    assert stamp["terminal_commit_sha"] == terminal
    assert "review_stamp:" in (repo / "docs" / "plans" / "example.md").read_text(encoding="utf-8")


def test_null_verdict_with_unbacked_claims_still_refuses(tmp_path):
    claims = [{"claim": "c", "anchor": "a"}]
    repo, terminal = _repo(tmp_path, delivery={"verdict": None, "claims_unbacked": claims})
    with pytest.raises(m.MintRefusal, match="delivery verdict is None"):
        _mint(repo, terminal)


def test_run_without_a_benign_reason_does_not_take_the_zero_claim_path(tmp_path):
    repo, terminal = _repo(tmp_path, delivery_not_run="rejected: boom")
    with pytest.raises(m.MintRefusal, match="delivery verdict is None") as exc:
        _mint(repo, terminal)
    assert f"--reverify-delivery {_RUN_REL}" in str(exc.value)


def test_unjudged_criterion_refuses_and_hint_names_rejudge_not_the_looping_rerun(tmp_path):
    repo, terminal = _repo(tmp_path, criterion={"status": "not_run", "observation": "", "sidecar": None})
    with pytest.raises(m.MintRefusal, match="delivery verdict is None") as exc:
        _mint(repo, terminal)
    text = str(exc.value)
    assert "--rejudge" in text
    assert "--reverify-delivery" not in text
