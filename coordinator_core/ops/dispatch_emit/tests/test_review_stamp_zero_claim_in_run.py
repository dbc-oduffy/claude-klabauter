"""The in-run mint (`criterion_open_ok=True`, as `terminal_commit._mint_review_stamp` calls it) of a
zero-claim run whose roster names no delivery-verifier: the delivery exemption holds whatever
criterion status the stamp carries; unbacked claims, an unbenign reason or a failing build/test
record still refuse, and the non-in-run mint still needs a judged criterion."""

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
_RUN_REL = ".coordinator-local/subagent-share/s1/run-record.md"
_NOT_MET = {"status": "not_met", "observation": "register row T10 open", "sidecar": None}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, **_FLAGS
    ).stdout.strip()


def _repo(tmp_path: Path, **overrides) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "docs" / "plans" / "example.md").write_text(
        textwrap.dedent(
            """\
            ---
            title: Example
            created: 2026-10-16
            author: test
            status: executing
            plan_id: pln-inrun-abc123
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
        "criterion": _NOT_MET,
        "slices": 1,
    }
    run.update(overrides)
    path = repo / _RUN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in run.items()) + "\n---\n\nbody\n",
        encoding="utf-8",
    )
    return repo, terminal


def _mint(repo: Path, terminal: str, *, in_run: bool):
    plan = repo / "docs" / "plans" / "example.md"
    data = m._load_sidecar(repo / _RUN_REL)
    return m.mint(
        plan, repo, build_test_path=None, resolved=(terminal, repo / _RUN_REL, data), criterion_open_ok=in_run
    )


@pytest.mark.parametrize(
    "criterion",
    [
        _NOT_MET,
        {"status": "indeterminate", "observation": "falsifier fail", "sidecar": None},
        {"status": "met", "observation": "held", "sidecar": None},
    ],
)
def test_in_run_zero_claim_mints_whatever_criterion_the_stamp_carries(tmp_path, criterion):
    repo, terminal = _repo(tmp_path, criterion=criterion)
    stamp = _mint(repo, terminal, in_run=True)
    assert stamp["terminal_commit_sha"] == terminal
    assert stamp["criterion"]["status"] == criterion["status"]


def test_in_run_unbacked_claims_still_refuse(tmp_path):
    repo, terminal = _repo(tmp_path, delivery={"verdict": None, "claims_unbacked": [{"claim": "c", "anchor": "a"}]})
    with pytest.raises(m.MintRefusal, match="delivery verdict is None"):
        _mint(repo, terminal, in_run=True)


def test_in_run_unbenign_reason_still_refuses(tmp_path):
    repo, terminal = _repo(tmp_path, delivery_not_run="rejected: boom")
    with pytest.raises(m.MintRefusal, match="delivery verdict is None"):
        _mint(repo, terminal, in_run=True)


def test_in_run_failing_tests_still_refuse(tmp_path):
    repo, terminal = _repo(tmp_path, tests={"status": "fail", "run": 3, "failed": 1, "sidecar": None})
    with pytest.raises(m.MintRefusal, match="delivery verdict is None|build/test verdict"):
        _mint(repo, terminal, in_run=True)


def test_non_in_run_mint_with_unmet_criterion_still_refuses(tmp_path):
    repo, terminal = _repo(tmp_path)
    with pytest.raises(m.MintRefusal, match="delivery verdict is None"):
        _mint(repo, terminal, in_run=False)
