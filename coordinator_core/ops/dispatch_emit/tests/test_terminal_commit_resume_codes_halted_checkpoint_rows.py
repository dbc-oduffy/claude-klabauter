"""A resume close codes the still-open rows a halted run's own checkpoints named, at their checkpoint sha."""

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


def _plan_text(c2_disposition: str = "open") -> str:
    rows = "".join(
        f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {i.lower()}.py\n"
        f"  disposition: {c2_disposition if i == 'C2' else 'open'}\n  deferred: false\n"
        + ("  disposition_ref: " + "d" * 40 + "\n" if i == "C2" and c2_disposition == "coded" else "")
        for i in ("C1", "C2", "C3")
    )
    return f"---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n"


def _checkpoint(root: Path, name: str, subject_tail: str, base: str, plan: str = REL) -> str:
    (root / f"{name}.py").write_text(f"{name}\n")
    _git(root, "add", f"{name}.py")
    _git(
        root, "commit", "-qm", f"checkpoint(wave 0): 2 rows — {subject_tail}",
        "-m", f"Checkpoint-Plan: {plan}\nCheckpoint-Base: {base[:7]}\nCheckpoint-Gate-Owed: C3",
    )
    return _git(root, "rev-parse", "HEAD")


def _build(tmp_path: Path, c2_disposition: str = "open"):
    """Untracked plan; product checkpoint, process half, foreign commit, then the halt commit adding the plan."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs").mkdir()
    (tmp_path / "seed.txt").write_text("s\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / REL).write_text(_plan_text(c2_disposition), encoding="utf-8", newline="\n")
    product = _checkpoint(tmp_path, "c1", "C1, C2, C3", base)
    process = _checkpoint(tmp_path, "proc", "C1, C2, C3 (process files)", base)
    (tmp_path / "peer.txt").write_text("p\n")
    _git(tmp_path, "add", "peer.txt")
    _git(tmp_path, "commit", "-qm", "peer work")
    _git(tmp_path, "add", REL)
    _git(
        tmp_path, "commit", "-qm", "warp: record halted run-1 run artifacts",
        "-m", f"Halt-Plan: {REL}\nHalted-By: ledger-check\nLands-Chunks: ",
    )
    return tmp_path, product, process, _git(tmp_path, "rev-parse", "HEAD"), base


@pytest.fixture
def repo(tmp_path):
    return _build(tmp_path)


def test_halted_checkpoint_rows_map_to_the_product_half_sha(repo):
    root, product, process, halt, _base = repo
    got = terminal_commit._halted_run_checkpoint_rows(root, REL, {"halt_sha": halt})
    assert got == {product: {REL: {"C1", "C2"}}}
    assert process not in got
    assert "C3" not in got[product][REL]


def test_non_resume_close_returns_nothing(repo):
    root, _p, _q, halt, _b = repo
    assert terminal_commit._halted_run_checkpoint_rows(root, REL, None) == {}
    assert terminal_commit._halted_run_checkpoint_rows(root, REL, {"halt_sha": "nope"}) == {}
    assert terminal_commit._halted_run_checkpoint_rows(root, None, {"halt_sha": halt}) == {}
    assert terminal_commit._halted_run_checkpoint_rows(root, REL, {"halt_sha": "a" * 40}) == {}


def test_other_plans_checkpoint_is_not_attributed(tmp_path):
    root, _product, _process, halt, _base = _build(tmp_path)
    assert terminal_commit._halted_run_checkpoint_rows(root, "docs/other.md", {"halt_sha": halt}) == {}


def test_walk_crosses_a_checkpoint_base_and_stops_when_wanted_is_found(repo):
    root, product, process, _halt, _base = repo
    (root / "later.py").write_text("l\n")
    _git(root, "add", "later.py")
    _git(root, "commit", "-qm", "checkpoint(wave 1): 1 rows — C3", "-m",
         f"Checkpoint-Plan: {REL}\nCheckpoint-Base: {process[:7]}")
    nxt = _git(root, "rev-parse", "HEAD")
    got = terminal_commit._halted_run_checkpoint_rows(root, REL, {"halt_sha": nxt})
    assert got == {nxt: {REL: {"C3"}}, product: {REL: {"C1", "C2"}}}


def test_resume_close_codes_halted_checkpoint_rows_and_spares_coded_and_owed(tmp_path, monkeypatch):
    root, product, _process, halt, _base = _build(tmp_path, c2_disposition="coded")
    monkeypatch.setattr(terminal_commit, "_changed_paths", lambda _r, paths: set(paths))
    halted = terminal_commit._halted_run_checkpoint_rows(root, REL, {"halt_sha": halt})
    out = terminal_commit._stamp_coded_commit(
        lambda p, r: {"committed": True, "sha": "f" * 40}, root, root,
        {}, "e" * 40, None, checkpoint_rows=halted,
    )
    assert out["rows_coded"] == {REL: ["C1"]}
    rows = _rows(root)
    assert rows["C1"]["disposition_ref"] == product
    assert rows["C2"]["disposition_ref"] == "d" * 40
    assert _row_disposition(rows["C3"]) == "open"


def test_helper_spawns_nothing(repo, monkeypatch):
    root, _p, _q, halt, _b = repo
    spawned: list = []

    def _boom(*a, **kw):
        spawned.append(a)
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert terminal_commit._halted_run_checkpoint_rows(root, REL, {"halt_sha": halt})
    assert spawned == []


def test_walk_cap_bounds_the_history_read(repo, monkeypatch):
    root, _p, _q, halt, _b = repo
    monkeypatch.setattr(terminal_commit, "_HALTED_WALK_CAP", 1)
    assert terminal_commit._halted_run_checkpoint_rows(root, REL, {"halt_sha": halt}) == {}
