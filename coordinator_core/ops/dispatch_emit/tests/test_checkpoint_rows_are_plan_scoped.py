"""A checkpoint commit belongs to the plan its `Checkpoint-Plan:` trailer names: another
plan's checkpoint naming the same row id neither matches nor lands this plan's row."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.chunk_commits import resolve_chunk_commits
from coordinator_core.ops.dispatch_emit.emit import checkpoint_landed_rows, emit_script
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN_A = "docs/plans/a.md"
_PLAN_B = "docs/plans/b.md"


def _plan(ids) -> str:
    rows = "".join(
        f"- id: {i}\n  title: t{i}\n  change_kind: doc-edit\n  surface: docs/x/{i}.md\n"
        f"  writes:\n    - docs/x/{i}.md\n  queue_scope: project\n  disposition: open\n"
        f"  body: |\n    Do {i}.\n"
        for i in ids
    )
    return f'---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n'


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags()
    )


def _commit(repo: Path, rel: str, text: str, *messages: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)
    args = ["commit", "-q"]
    for m in messages:
        args += ["-m", m]
    _git(repo, *args)


def _checkpoint(repo: Path, rel: str, ids: str, plan: str) -> None:
    _commit(
        repo, rel, f"{ids}\n",
        f"checkpoint(wave 1): 2 rows — {ids}",
        f"Checkpoint-Plan: {plan}\nCheckpoint-Base: abc1234",
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.example")
    _git(r, "config", "user.name", "t")
    _commit(r, _PLAN_A, _plan(["C9", "C10", "C13", "C14"]), "add plan a")
    _commit(r, _PLAN_B, _plan(["C9", "C10", "C13"]), "add plan b")
    return r


def test_other_plans_checkpoint_does_not_match_this_plans_row(repo):
    _checkpoint(repo, "docs/x/b.txt", "C9, C10", _PLAN_B)

    assert resolve_chunk_commits(repo, _PLAN_A, "C10") == []
    hit = resolve_chunk_commits(repo, _PLAN_B, "C10")
    assert [c["subject"] for c in hit] == ["checkpoint(wave 1): 2 rows — C9, C10"]


def test_repo_key_prefix_on_the_trailer_still_names_the_plan(repo):
    _checkpoint(repo, "docs/x/a.txt", "C9, C10", f"somerepo:{_PLAN_A}")

    assert len(resolve_chunk_commits(repo, _PLAN_A, "C9")) == 1
    assert resolve_chunk_commits(repo, _PLAN_B, "C9") == []


def test_plain_chunk_subject_keeps_its_rule(repo):
    _commit(repo, "docs/x/c.txt", "x\n", "C10: build it")

    assert [c["subject"] for c in resolve_chunk_commits(repo, _PLAN_A, "C10")] == ["C10: build it"]


def test_checkpoint_landed_rows_reads_only_this_plans_trailer(repo):
    _checkpoint(repo, "docs/x/a.txt", "C9, C10", _PLAN_A)
    _checkpoint(repo, "docs/x/b.txt", "C13", _PLAN_B)

    assert checkpoint_landed_rows(repo, _PLAN_A) == {"C9", "C10"}
    assert checkpoint_landed_rows(repo, _PLAN_B) == {"C13"}


def test_only_incomplete_excludes_rows_a_checkpoint_of_this_plan_landed(repo):
    _checkpoint(repo, "docs/x/a.txt", "C9, C10", _PLAN_A)
    _checkpoint(repo, "docs/x/b.txt", "C13", _PLAN_B)

    script = emit_script(
        repo / _PLAN_A, repo_root=repo, landed_rows=frozenset({"C14"}), **REVIEW_KW
    )

    assert "C13" in script
    assert "C9" not in script and "C10" not in script and "C14" not in script


def test_only_incomplete_with_a_run_text_naming_no_rows_still_excludes_checkpointed_rows(repo):
    _checkpoint(repo, "docs/x/a.txt", "C9, C10", _PLAN_A)

    script = emit_script(repo / _PLAN_A, repo_root=repo, landed_rows=frozenset(), **REVIEW_KW)

    assert "C13" in script and "C14" in script
    assert "C9" not in script and "C10" not in script
