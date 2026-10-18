"""A non-resume terminal close codes still-open rows an earlier fire's checkpoints landed, at those shas."""

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
IDS = ("B1", "B2", "C1")


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
        f"  disposition: open\n  deferred: false\n"
        for i in IDS
    )
    return f"---\ntitle: p\ncreated: 2020-01-01\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n"


def _checkpoint(root: Path, name: str, rows: str, base: str, plan: str = REL) -> str:
    (root / f"{name}.py").write_text(f"{name}\n")
    _git(root, "add", f"{name}.py")
    _git(
        root, "commit", "-qm", f"checkpoint(wave 0): 1 rows — {rows}",
        "-m", f"Checkpoint-Plan: {plan}\nCheckpoint-Base: {base[:7]}",
    )
    return _git(root, "rev-parse", "HEAD")


def _build(tmp_path: Path):
    """Two fires: fire 1 checkpoints B1; a halt record; fire 2 (own base) checkpoints B2; terminal."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs").mkdir()
    (tmp_path / "seed.txt").write_text("s\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    seed = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / REL).write_text(_plan_text(), encoding="utf-8", newline="\n")
    first = _checkpoint(tmp_path, "b1", "B1", seed)
    (tmp_path / "peer.txt").write_text("p\n")
    _git(tmp_path, "add", "peer.txt")
    _git(tmp_path, "commit", "-qm", "peer halt record")
    peer = _git(tmp_path, "rev-parse", "HEAD")
    second = _checkpoint(tmp_path, "b2", "B2", peer)
    (tmp_path / "term.txt").write_text("t\n")
    _git(tmp_path, "add", "term.txt")
    _git(tmp_path, "commit", "-qm", "terminal")
    return tmp_path, first, second, _git(tmp_path, "rev-parse", "HEAD")


def test_non_resume_helper_maps_each_row_to_its_checkpoint_across_a_base(tmp_path):
    root, first, second, terminal = _build(tmp_path)
    got = terminal_commit._earlier_fire_checkpoint_rows(root, REL, terminal)
    assert got == {second: {REL: {"B2"}}, first: {REL: {"B1"}}}


def test_exclude_keeps_this_closes_own_rows_out(tmp_path):
    root, first, _second, terminal = _build(tmp_path)
    got = terminal_commit._earlier_fire_checkpoint_rows(root, REL, terminal, frozenset({"B2"}))
    assert got == {first: {REL: {"B1"}}}


def test_non_resume_close_codes_earlier_fire_rows_at_their_checkpoint_sha(tmp_path, monkeypatch):
    root, first, second, terminal = _build(tmp_path)
    monkeypatch.setattr(terminal_commit, "_changed_paths", lambda _r, paths: set(paths))
    earlier = terminal_commit._earlier_fire_checkpoint_rows(root, REL, terminal)
    out = terminal_commit._stamp_coded_commit(
        lambda p, r: {"committed": True, "sha": "f" * 40}, root, root,
        {}, "e" * 40, None, checkpoint_rows=earlier,
    )
    assert out["rows_coded"] == {REL: ["B1", "B2"]}
    rows = _rows(root)
    assert rows["B1"]["disposition_ref"] == first
    assert rows["B2"]["disposition_ref"] == second
    assert _row_disposition(rows["C1"]) == "open"


def test_helper_spawns_nothing(tmp_path, monkeypatch):
    root, _a, _b, terminal = _build(tmp_path)

    def _boom(*a, **kw):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert terminal_commit._earlier_fire_checkpoint_rows(root, REL, terminal)


def test_root_terminal_or_bad_sha_returns_nothing(tmp_path):
    root, _a, _b, terminal = _build(tmp_path)
    assert terminal_commit._earlier_fire_checkpoint_rows(root, REL, "a" * 40) == {}
    assert terminal_commit._earlier_fire_checkpoint_rows(root, None, terminal) == {}
