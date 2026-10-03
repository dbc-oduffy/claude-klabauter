"""review_stamp.mint(superseding_record=...) — a stranded run with no Inline-Review trailer."""

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
_NOT_RUN = {"status": "not_run", "run": None, "failed": None, "sidecar": None}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, **_FLAGS
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
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
    (repo / "state" / "lessons").mkdir(parents=True)
    (repo / "state" / "lessons" / "a.md").write_text("lesson\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "stranded work with no trailer")
    return repo


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
        "prep": {"run_base_sha": head, "product_files": 0, "foreign_claims": [],
                 "slice_files": ["state/lessons/a.md"]},
        "delivery": {"verdict": "PASS", "product_files": 0, "claims_unbacked": 0},
        "tests": _NOT_RUN,
        "criterion": {"status": "met", "observation": "ok", "sidecar": None},
    }
    data.update(overrides)
    path = repo / "state" / "superseding-reviews" / "2026-10" / "rec.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in data.items()) + "\n---\n\nbody\n",
        encoding="utf-8",
    )
    return path


def _plan(repo: Path) -> Path:
    return repo / "docs" / "plans" / "example.md"


def test_mint_accepts_a_superseding_record_with_no_trailer_anywhere(tmp_path):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    record = _record(repo, head)
    stamp = m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    assert stamp["terminal_commit_sha"] == head
    assert stamp["superseding_record"] == "state/superseding-reviews/2026-10/rec.md"
    assert m.check(_plan(repo), repo) is None


def test_mint_refuses_a_plan_id_mismatch(tmp_path):
    repo = _repo(tmp_path)
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), plan_id="pln-other")
    with pytest.raises(m.MintRefusal, match="does not match"):
        m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    assert "review_stamp:" not in _plan(repo).read_text(encoding="utf-8")


def test_mint_refuses_a_record_of_the_wrong_kind(tmp_path):
    repo = _repo(tmp_path)
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), kind="something-else")
    with pytest.raises(m.MintRefusal, match="not a superseding-review record"):
        m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)


def test_mint_refuses_a_fail_delivery_with_mints_exact_message(tmp_path):
    repo = _repo(tmp_path)
    record = _record(
        repo, _git(repo, "rev-parse", "HEAD"), delivery={"verdict": "FAIL", "product_files": 0}
    )
    with pytest.raises(m.MintRefusal) as exc:
        m.mint(_plan(repo), repo, build_test_path=None, superseding_record=record)
    assert str(exc.value).startswith("review-stamp: refusing to mint: delivery verdict is 'FAIL', not PASS;")
    assert "--reverify-delivery" in str(exc.value)


def _count_spawns(monkeypatch) -> list:
    calls: list = []
    real = subprocess.run

    def counting(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(subprocess, "run", counting)
    return calls


def test_spawn_count_matches_the_resolved_path(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    record = _record(repo, head)
    data = m._load_sidecar(record)

    plan = _plan(repo)
    original = plan.read_text(encoding="utf-8")
    calls = _count_spawns(monkeypatch)
    m.mint(plan, repo, build_test_path=None, resolved=(head, record, data))
    resolved_spawns = len(calls)

    plan.write_text(original, encoding="utf-8")
    calls.clear()
    m.mint(plan, repo, build_test_path=None, superseding_record=record)
    assert len(calls) == resolved_spawns
