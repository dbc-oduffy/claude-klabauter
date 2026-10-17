"""A resume close codes the rows the halted predecessor landed at the halt sha; a non-resume close codes none."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.execute_plan_assemble.row_spans import _row_disposition
from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops.dispatch_emit import terminal_commit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REL = "docs/plan.md"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _rows(root: Path) -> dict:
    text = (root / REL).read_text(encoding="utf-8")
    return {r["id"]: r for r in yaml.safe_load(locate_fenced_block(text).body)}


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs").mkdir()
    body = "".join(
        f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {i.lower()}.py\n  disposition: open\n  deferred: false\n"
        for i in ("C1", "C2", "C3")
    )
    (tmp_path / REL).write_text(f"---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "c1.py").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(
        tmp_path, "commit", "-qm", "warp: record halted run-1 run artifacts",
        "-m", f"Halt-Plan: {REL}\nHalted-By: ledger-check\nLands-Chunks: C1, C2",
    )
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def test_resume_close_codes_predecessor_rows_at_the_halt_sha(repo, monkeypatch):
    root, halt = repo
    monkeypatch.setattr(terminal_commit, "_changed_paths", lambda _r, paths: set(paths))
    predecessor = terminal_commit._predecessor_rows(root, REL, {"halt_sha": halt})
    assert predecessor == {halt: {REL: {"C1", "C2"}}}

    out = terminal_commit._stamp_coded_commit(
        lambda p, r: {"committed": True, "sha": "f" * 40}, root, root,
        {REL: {"C3"}}, "e" * 40, None, checkpoint_rows=predecessor,
    )
    assert out["rows_coded"] == {REL: ["C1", "C2", "C3"]}
    rows = _rows(root)
    assert rows["C1"]["disposition_ref"] == rows["C2"]["disposition_ref"] == halt
    assert rows["C3"]["disposition_ref"] == "e" * 40
    assert not any(_row_disposition(r) == "open" for r in rows.values())


def test_non_resume_close_codes_no_predecessor_rows(repo):
    root, _halt = repo
    assert terminal_commit._predecessor_rows(root, REL, None) == {}
    assert terminal_commit._predecessor_rows(root, REL, {"halt_sha": "not-a-sha"}) == {}
    assert terminal_commit._predecessor_rows(root, None, {"halt_sha": "a" * 40}) == {}


def test_rows_the_halt_commit_did_not_name_are_not_coded(repo):
    root, halt = repo
    assert "C3" not in terminal_commit._predecessor_rows(root, REL, {"halt_sha": halt})[halt][REL]


def test_unreadable_halt_commit_yields_no_rows(repo):
    root, _halt = repo
    assert terminal_commit._predecessor_rows(root, REL, {"halt_sha": "a" * 40}) == {}
