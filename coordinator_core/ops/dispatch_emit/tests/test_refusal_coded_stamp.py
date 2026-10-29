"""stamp_landed_rows_on_refusal codes only commit-proven landings, at the landing sha."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.execute_plan_assemble.row_spans import _row_disposition
from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops.dispatch_emit import refusal_coded_stamp, terminal_commit

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


def _plan_text() -> str:
    rows = "".join(
        f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {i.lower()}.py\n"
        f"  writes:\n    - {i.lower()}.py\n  disposition: open\n  deferred: false\n"
        for i in ("A", "B", "C")
    )
    return f"---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n"


def _build(tmp_path: Path, halt_lands: str = "a.py"):
    """Plan committed first; the halt record commit then lands ``halt_lands`` only."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs").mkdir()
    (tmp_path / REL).write_text(_plan_text(), encoding="utf-8", newline="\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed plan")
    (tmp_path / halt_lands).write_text("work\n")
    _git(tmp_path, "add", halt_lands)
    _git(
        tmp_path, "commit", "-qm", "warp: record halted run-1 run artifacts",
        "-m", f"Halt-Plan: {REL}\nHalted-By: ledger-check\nLands-Chunks: ",
    )
    return _git(tmp_path, "rev-parse", "HEAD")


@pytest.fixture
def fake_commit(monkeypatch):
    calls: list = []

    def _commit(params, root):
        calls.append(params)
        return {"committed": True, "sha": "f" * 40}

    monkeypatch.setattr(refusal_coded_stamp, "_commit_v2", _commit)
    monkeypatch.setattr(terminal_commit, "_changed_paths", lambda _r, paths: set(paths))
    return calls


def test_row_landed_only_by_the_halt_record_commit_codes_at_the_halt_sha(tmp_path, fake_commit):
    halt = _build(tmp_path)
    out = refusal_coded_stamp.stamp_landed_rows_on_refusal(
        tmp_path, tmp_path, {"plan_path": REL}, {"refused": "halted-run"}, None,
    )
    assert out == {"refusal_rows_coded": {REL: ["A"]}, "refusal_coded_sha": "f" * 40}
    rows = _rows(tmp_path)
    assert rows["A"]["disposition"] == "coded"
    assert rows["A"]["disposition_ref"] == halt
    assert _row_disposition(rows["B"]) == "open"
    assert _row_disposition(rows["C"]) == "open"
    assert [c["paths"] for c in fake_commit] == [[REL]]


def test_outside_commit_above_the_halt_codes_at_its_own_sha(tmp_path, fake_commit):
    halt = _build(tmp_path)
    (tmp_path / "b.py").write_text("later\n")
    _git(tmp_path, "add", "b.py")
    _git(tmp_path, "commit", "-qm", "later scoped commit")
    later = _git(tmp_path, "rev-parse", "HEAD")
    params = {"plan_path": REL, terminal_commit.TERMINAL_RESUME_LEDGER_PARAM: {"halt_sha": halt}}
    out = refusal_coded_stamp.stamp_landed_rows_on_refusal(tmp_path, tmp_path, params, {}, None)
    assert out["refusal_rows_coded"] == {REL: ["A", "B"]}
    rows = _rows(tmp_path)
    assert rows["A"]["disposition_ref"] == halt
    assert rows["B"]["disposition_ref"] == later
    assert _row_disposition(rows["C"]) == "open"


def test_checkpoint_landed_row_codes_at_the_checkpoint_sha(tmp_path, fake_commit):
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs").mkdir()
    (tmp_path / REL).write_text(_plan_text(), encoding="utf-8", newline="\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed plan")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "b.py").write_text("b\n")
    _git(tmp_path, "add", "b.py")
    _git(tmp_path, "commit", "-qm", "checkpoint(wave 0): 1 rows — B",
         "-m", f"Checkpoint-Plan: {REL}\nCheckpoint-Base: {base[:7]}")
    checkpoint = _git(tmp_path, "rev-parse", "HEAD")
    out = refusal_coded_stamp.stamp_landed_rows_on_refusal(tmp_path, tmp_path, {"plan_path": REL}, {}, None)
    assert out["refusal_rows_coded"] == {REL: ["B"]}
    assert _rows(tmp_path)["B"]["disposition_ref"] == checkpoint


def test_no_plan_path_commits_nothing_and_spawns_no_git(tmp_path, fake_commit, monkeypatch):
    _build(tmp_path)

    def _boom(*a, **kw):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert refusal_coded_stamp.stamp_landed_rows_on_refusal(tmp_path, tmp_path, {}, {}, None) == {}
    assert fake_commit == []


def test_no_landed_row_commits_nothing_and_spawns_no_git(tmp_path, fake_commit, monkeypatch):
    _build(tmp_path, halt_lands="unrelated.py")

    def _boom(*a, **kw):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    out = refusal_coded_stamp.stamp_landed_rows_on_refusal(tmp_path, tmp_path, {"plan_path": REL}, {}, None)
    assert out == {}
    assert fake_commit == []
    assert all(_row_disposition(r) == "open" for r in _rows(tmp_path).values())


def test_halt_record_of_another_plan_codes_nothing(tmp_path, fake_commit):
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs").mkdir()
    (tmp_path / REL).write_text(_plan_text(), encoding="utf-8", newline="\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed plan")
    (tmp_path / "a.py").write_text("work\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-qm", "warp: record halted run-9 run artifacts",
         "-m", "Halt-Plan: docs/other.md\nHalted-By: ledger-check\nLands-Chunks: ")
    out = refusal_coded_stamp.stamp_landed_rows_on_refusal(tmp_path, tmp_path, {"plan_path": REL}, {}, None)
    assert out == {}
    assert fake_commit == []
