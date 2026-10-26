"""The XS and M mints append the post-commit clause only when a statement names an existing bug row."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import bug_close
from coordinator_core.ops.dispatch_emit.sizing_m_mint import mint_m_spine
from coordinator_core.ops.dispatch_emit.sizing_xs_mint import mint_xs_spine

BUG = "f8207b1c93de"
GHOST = "0123456789ab"
CLAUSE_HEAD = "Closing bug row(s)"


def _repo(tmp_path: Path) -> Path:
    home = tmp_path / "state" / "bug-backlog"
    home.mkdir(parents=True)
    (home / f"2026-10-26-x-{BUG}.yaml").write_text("status: open\n", encoding="utf-8")
    return tmp_path


def _xs(statement: str, repo_root):
    sizing = {"exit_criterion": {"statement": statement}, "intent": "Fix the thing."}
    text, _ = mint_xs_spine(
        sizing, sizing_rel="state/sizings/s.yaml", writes=["pkg/mod.py"], out_dir=Path("out"), repo_root=repo_root
    )
    return text


def test_xs_named_existing_bug_gets_clause(tmp_path):
    text = _xs(f"Tests pass and {BUG} is closed citing the landing sha", _repo(tmp_path))
    assert CLAUSE_HEAD in text
    assert bug_close.post_commit_clause([BUG]) in text


def test_xs_unknown_id_mints_no_clause(tmp_path):
    text = _xs(f"Tests pass and {GHOST} is closed citing the landing sha", _repo(tmp_path))
    assert CLAUSE_HEAD not in text


def test_xs_without_repo_root_mints_no_clause(tmp_path):
    _repo(tmp_path)
    text = _xs(f"Tests pass and {BUG} is closed citing the landing sha", None)
    assert CLAUSE_HEAD not in text


def test_m_clause_only_on_naming_row(tmp_path):
    repo = _repo(tmp_path)
    rows = [
        {"statement": f"Close {BUG} with the landing sha", "check": "pytest a", "writes": ["pkg/a.py"]},
        {"statement": "Other work", "check": "pytest b", "writes": ["pkg/b.py"]},
    ]
    text, _ = mint_m_spine({}, sizing_rel="state/sizings/s.yaml", rows=rows, repo_root=repo)
    first, second = text.split("- id: M2")
    assert bug_close.post_commit_clause([BUG]) in first
    assert CLAUSE_HEAD not in second
