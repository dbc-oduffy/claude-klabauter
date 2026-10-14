"""A row closed no-op against a later commit is credited to the checkpoint commit that landed it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import delivery_credit
from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.delivery_credit import (
    NOOP_DETAIL_PREFIX,
    coded_row_refs,
    landing_commits,
    rows_backed_before_base,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOOP = NOOP_DETAIL_PREFIX + " the row's files carry no change from this run"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(repo: Path, rel: str, text: str, subject: str, plan: str = "") -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    _git(repo, "add", rel)
    args = ["commit", "-q", "-m", subject]
    if plan:
        args += ["-m", f"Checkpoint-Plan: {plan}", "-m", "Co-Authored-By: t <t@t>"]
    _git(repo, *args)
    return _git(repo, "rev-parse", "HEAD")


def _plan(repo: Path, rows: str) -> str:
    path = repo / "plan.md"
    path.write_text("---\nstatus: draft\n---\n\n# P\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "```\n", encoding="utf-8")
    return str(path)


def _noop_row(rid: str, ref: str, path: str) -> str:
    return (
        f"- id: {rid}\n  disposition: coded\n  disposition_ref: '{ref}'\n"
        f"  disposition_detail: \"{_NOOP}\"\n  writes:\n    - {path}\n"
    )


def _repo(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "seed", "0", "seed")
    return tmp_path


def test_noop_row_rekeys_to_landing_checkpoint_and_is_credited(tmp_path):
    repo = _repo(tmp_path)
    x = _commit(repo, "a/f.py", "1", "checkpoint(wave 1): 1 rows - C1", "plan.md")
    base = _commit(repo, "other", "1", "unrelated")
    y = _commit(repo, "other", "2", "later no-op close")
    plan = _plan(repo, _noop_row("C1", y, "a/f.py"))
    assert coded_row_refs(plan) == {y: ["C1"]}
    assert coded_row_refs(plan, repo_root=repo, before=base) == {x: ["C1"]}
    assert rows_backed_before_base(repo, plan, base) == ["C1"]


def test_trailered_row_commit_beats_newer_unrelated_commit_on_same_path(tmp_path):
    repo = _repo(tmp_path)
    x = _commit(repo, "a/f.py", "1", "checkpoint(wave 1): 2 rows - C1, C10", "plan.md")
    _commit(repo, "a/f.py", "2", "peer tweak")
    _commit(repo, "a/f.py", "3", "checkpoint(wave 2): C1 elsewhere", "other-plan.md")
    _commit(repo, "b/g.py", "1", "peer only")
    head = _git(repo, "rev-parse", "HEAD")
    got = landing_commits(repo, "plan.md", {"C1": ["a/f.py"], "C2": ["b/g.py"], "C3": ["a"]}, head)
    assert got == {"C1": x}


def test_word_boundary_and_directory_paths(tmp_path):
    repo = _repo(tmp_path)
    x = _commit(repo, "d/f.py", "1", "checkpoint(wave 1): C10", "plan.md")
    head = _git(repo, "rev-parse", "HEAD")
    assert landing_commits(repo, "plan.md", {"C1": ["d"]}, head) == {}
    assert landing_commits(repo, "plan.md", {"C10": ["d"]}, head) == {"C10": x}


def test_no_noop_row_makes_no_extra_spawn(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    ref = _commit(repo, "f", "1", "one")
    plan = _plan(repo, f"- id: C1\n  disposition: coded\n  disposition_ref: '{ref}'\n")
    calls = []
    real = delivery_credit.run_git
    monkeypatch.setattr(delivery_credit, "run_git", lambda a, **kw: (calls.append(a[0]), real(a, **kw))[1])
    assert coded_row_refs(plan, repo_root=repo) == {ref: ["C1"]}
    assert calls == []


def test_noop_prefix_heads_terminal_commit_detail():
    assert terminal_commit._NOOP_DETAIL.startswith(NOOP_DETAIL_PREFIX)


def test_credit_note_names_landing_sha(tmp_path):
    repo = _repo(tmp_path)
    x = _commit(repo, "a/f.py", "1", "checkpoint(wave 1): C1", "plan.md")
    base = _commit(repo, "other", "1", "unrelated")
    y = _commit(repo, "other", "2", "no-op close")
    plan = _plan(repo, _noop_row("C1", y, "a/f.py"))
    note = rd._commit_credit_note(repo, plan, base)
    assert "C1 at " + x[:10] in note and "landed before this run's base" in note
