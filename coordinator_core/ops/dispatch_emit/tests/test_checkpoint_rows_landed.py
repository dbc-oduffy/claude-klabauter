"""checkpoint_rows_landed: which wanted rows did same-plan checkpoints land, newest first."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import checkpoint_stamp as cs

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "docs/plan.md"
OTHER = "docs/other.md"
ALL = frozenset({"B1", "C2", "B3", "B4", "G1", "G2", "N1", "P1"})
OLD = "2026-01-01T00:00:00+0000"


def _git(root: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date:
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(root: Path, subject: str, trailers: str = "", date: str | None = None) -> str:
    name = f"f{len(list(root.glob('f*.txt')))}.txt"
    (root / name).write_text(subject + "\n")
    _git(root, "add", name)
    args = ["commit", "-qm", subject]
    if trailers:
        args += ["-m", trailers]
    _git(root, *args, date=date)
    return _git(root, "rev-parse", "HEAD")


def _cp(root, ids, plan=PLAN, base="", owed="", process=False, date=None, wave=0) -> str:
    subject = f"checkpoint(wave {wave}): {len(ids)} rows — {', '.join(ids)}"
    if process:
        subject += " (process files)"
    trailers = f"Checkpoint-Plan: {plan}"
    if base:
        trailers += f"\nCheckpoint-Base: {base[:7]}"
    if owed:
        trailers += f"\nCheckpoint-Gate-Owed: {owed}"
    return _commit(root, subject, trailers, date)


class Shape:
    pass


@pytest.fixture
def shape(tmp_path):
    """The example-game-repo history, built oldest first; names are the checkpoint each row lands at."""
    s = Shape()
    _git(tmp_path, "init", "-q")
    s.seed = _commit(tmp_path, "seed")
    s.b3 = _cp(tmp_path, ["B3", "G1", "G2"], base=s.seed, wave=0)
    _commit(tmp_path, "non-run commit")
    s.peer_p1 = _cp(tmp_path, ["P1"], plan=OTHER)
    peer_halt = _commit(tmp_path, "warp: record halted run artifacts", "Halt-Plan: docs/peer.md")
    s.p1 = _cp(tmp_path, ["P1"], base=peer_halt, wave=1)
    s.b = _cp(tmp_path, ["B1", "C2", "G1", "G2", "N1"], base=s.p1, wave=2)
    s.b_proc = _cp(tmp_path, ["B1", "C2", "G1", "G2", "N1"], process=True, wave=2)
    halt_a = _commit(tmp_path, "warp: record halted run artifacts", f"Halt-Plan: {PLAN}")
    s.b4 = _cp(tmp_path, ["B4"], base=halt_a, wave=3)
    _commit(tmp_path, "warp: record halted run artifacts", f"Halt-Plan: {PLAN}")
    s.v1 = _cp(tmp_path, ["V1"], wave=4)
    _cp(tmp_path, ["V1"], process=True, wave=4)
    s.stamp = _commit(
        tmp_path, "checkpoint(wave 4): 1 row — B3",
        f"Checkpoint-Plan: {PLAN}\nCheckpoint-Stamp: {s.v1}",
    )
    _commit(tmp_path, "warp: record halted run artifacts", f"Halt-Plan: {PLAN}")
    s.s1 = _cp(tmp_path, ["S1", "N1", "P1"], base=halt_a, owed="N1, P1", wave=5)
    _commit(tmp_path, "warp: record halted run artifacts", f"Halt-Plan: {PLAN}")
    s.head = _commit(tmp_path, "terminal")
    s.root = tmp_path
    return s


def test_every_wanted_row_lands_at_the_newest_same_plan_product_checkpoint(shape):
    got = cs.checkpoint_rows_landed(shape.root, PLAN, shape.head, ALL)
    expected = {
        shape.b4: {"B4"},
        shape.b: {"B1", "C2", "G1", "G2", "N1"},
        shape.p1: {"P1"},
        shape.b3: {"B3"},
    }
    assert {k: set(v) for k, v in got.landed.items()} == expected
    assert not got.capped


def test_foreign_process_stamp_and_gate_owed_never_contribute(shape):
    got = cs.checkpoint_rows_landed(shape.root, PLAN, shape.head, ALL)
    assert shape.peer_p1 not in got.landed
    assert shape.b_proc not in got.landed
    assert shape.stamp not in got.landed
    assert shape.s1 not in got.landed
    only_owed = cs.checkpoint_rows_landed(shape.root, PLAN, shape.head, frozenset({"S1x"}))
    assert only_owed.landed == {}


def test_a_gate_owed_only_landing_is_not_found(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "seed")
    _cp(tmp_path, ["A1", "A2"], owed="A2")
    head = _commit(tmp_path, "terminal")
    got = cs.checkpoint_rows_landed(tmp_path, PLAN, head, frozenset({"A1", "A2"}))
    assert [set(v) for v in got.landed.values()] == [{"A1"}]


def test_walk_spawns_no_process(shape, monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    got = cs.checkpoint_rows_landed(shape.root, PLAN, shape.head, ALL)
    assert got.landed


def test_walk_stops_as_soon_as_wanted_is_exhausted(shape):
    got = cs.checkpoint_rows_landed(shape.root, PLAN, shape.head, frozenset({"B4"}))
    assert set(got.landed) == {shape.b4}
    assert got.commits_read == 9  # terminal, halt, s1, halt, stamp, v1 process, v1, halt, b4
    walked_all = cs.checkpoint_rows_landed(shape.root, PLAN, shape.head, ALL)
    assert got.commits_read < walked_all.commits_read


def test_cap_ends_the_walk_and_reports_capped(shape):
    got = cs.checkpoint_rows_landed(shape.root, PLAN, shape.head, ALL, cap=3)
    assert got.capped is True
    assert got.commits_read == 3
    assert got.landed == {}


def test_floor_ends_the_walk_before_older_commits(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "seed", date=OLD)
    _cp(tmp_path, ["OLD1"], date=OLD)
    new = _cp(tmp_path, ["NEW1"])
    head = _commit(tmp_path, "terminal")
    floor = 1_790_000_000  # 2026-09-22, after OLD (2026-01-01) and before the fresh commits
    got = cs.checkpoint_rows_landed(tmp_path, PLAN, head, frozenset({"OLD1", "NEW1"}), floor_epoch=floor)
    assert set(got.landed) == {new}
    assert not got.capped
    nofloor = cs.checkpoint_rows_landed(tmp_path, PLAN, head, frozenset({"OLD1", "NEW1"}))
    assert len(nofloor.landed) == 2


def test_empty_wanted_bad_sha_or_plan_return_an_empty_walk(shape):
    assert cs.checkpoint_rows_landed(shape.root, PLAN, shape.head, frozenset()) == ({}, 0, False)
    assert cs.checkpoint_rows_landed(shape.root, PLAN, "nope", ALL) == ({}, 0, False)
    assert cs.checkpoint_rows_landed(shape.root, "", shape.head, ALL) == ({}, 0, False)
    assert cs.checkpoint_rows_landed(shape.root, PLAN, "a" * 40, ALL).landed == {}


def test_plan_created_floor():
    day = 1_760_745_600  # 2025-10-18T00:00:00Z
    assert cs.plan_created_floor("---\ntitle: p\ncreated: 2025-10-18\n---\nbody\n") == day - 86400
    assert cs.plan_created_floor('---\ncreated: "2025-10-18"\r\n---\n') == day - 86400
    assert cs.plan_created_floor("---\ntitle: p\n---\ncreated: 2025-10-18\n") is None
    assert cs.plan_created_floor("---\ncreated: garbage\n---\n") is None
    assert cs.plan_created_floor("---\ncreated: 2025-13-45\n---\n") is None
    assert cs.plan_created_floor("no frontmatter") is None
