"""dispatch.record_halted_run_landed ``in_run``: a running run closes as already delivered."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SOLO = "warp-2026-10-20-solo-20261020T000000-abcdef"
NO_RECORD = {"status": "not-applicable", "detail": "no halt record"}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def launches(monkeypatch):
    calls: list = []
    monkeypatch.setattr(op, "_launch", lambda root, run_id: calls.append(run_id) or True)
    return calls


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    first = _commit(tmp_path, "one")
    second = _commit(tmp_path, "two")
    return tmp_path, first, second


def _manifest(root: Path, rid: str, base: str, paths: list) -> Path:
    d = root / "state" / "warp" / rid
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"head_sha": base, "review_declared_paths": paths}), encoding="utf-8")
    return d


def _status(root: Path, rid: str, status: str) -> None:
    st = root / c.status_rel(rid)
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps({"parent_id": "P", "status": status}), encoding="utf-8")


def _tree(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in (root / "state").rglob("*") if p.is_file()}


def test_solo_markerless_running_run_closes_landed_and_writes_nothing(repo):
    root, first, second = repo
    _manifest(root, SOLO, first, ["one"])
    before = _tree(root)
    out = op._handler({"run_id": SOLO, "sha": second, "in_run": True}, root)
    assert out["ok"] and out["written"] is True and out["sha"] == second, out
    assert out["close"] == NO_RECORD and "recorded" not in out
    assert _tree(root) == before
    assert not (root / "state" / "warp" / SOLO / "halted.json").exists()


def test_solo_markerless_without_in_run_still_refuses(repo):
    root, first, second = repo
    _manifest(root, SOLO, first, ["one"])
    out = op._handler({"run_id": SOLO, "sha": second}, root)
    assert not out["ok"] and "never ran" in out["error"]


def test_dirty_write_set_refuses(repo):
    root, first, second = repo
    _manifest(root, SOLO, first, ["one", "two"])
    out = op._handler({"run_id": SOLO, "sha": second, "in_run": True}, root)
    assert not out["ok"] and "not clean" in out["error"] and "two" in out["error"]


def test_uncommitted_worktree_edit_refuses(repo):
    root, first, second = repo
    _manifest(root, SOLO, first, ["one"])
    (root / "one").write_text("edited", encoding="utf-8")
    out = op._handler({"run_id": SOLO, "sha": second, "in_run": True}, root)
    assert not out["ok"] and "not clean" in out["error"]


def test_sha_not_ancestor_of_head_refuses(repo):
    root, first, _ = repo
    _manifest(root, SOLO, first, ["one"])
    _git(root, "checkout", "-q", "-b", "side", first)
    side = _commit(root, "side")
    _git(root, "checkout", "-q", "-")
    out = op._handler({"run_id": SOLO, "sha": side, "in_run": True}, root)
    assert not out["ok"] and "not an ancestor" in out["error"]


@pytest.mark.parametrize("doc", [None, {"head_sha": "x"}, {"review_declared_paths": ["one"]}, {"head_sha": "a" * 40, "review_declared_paths": []}])
def test_missing_or_incomplete_manifest_refuses(repo, doc):
    root, _, second = repo
    if doc is not None:
        d = root / "state" / "warp" / SOLO
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps(doc), encoding="utf-8")
    out = op._handler({"run_id": SOLO, "sha": second, "in_run": True}, root)
    assert not out["ok"]


def test_in_run_must_be_boolean(repo):
    root, _, second = repo
    out = op._handler({"run_id": SOLO, "sha": second, "in_run": "yes"}, root)
    assert not out["ok"] and "boolean" in out["error"]


def test_sibling_running_status_admitted_only_with_in_run(repo):
    root, first, second = repo
    _manifest(root, "A", first, ["one"])
    _status(root, "A", "running")
    from coordinator_core.ops.dispatch_emit import run_token

    run_token.mint(root, "A", "scratch/x.py")
    refused = op._handler({"run_id": "A", "sha": second}, root)
    assert not refused["ok"] and "not halted" in refused["error"]
    out = op._handler({"run_id": "A", "sha": second, "in_run": True}, root)
    assert out["ok"] and out["written"] is True and out["sha"] == second, out
    assert json.loads((root / c.status_rel("A")).read_text(encoding="utf-8"))["status"] == "landed"


def test_sibling_dirty_write_set_leaves_running_status(repo):
    root, first, second = repo
    _manifest(root, "A", first, ["two"])
    _status(root, "A", "running")
    out = op._handler({"run_id": "A", "sha": second, "in_run": True}, root)
    assert not out["ok"]
    assert json.loads((root / c.status_rel("A")).read_text(encoding="utf-8"))["status"] == "running"


def test_in_run_spawns_no_more_than_halted_mode(repo, monkeypatch):
    root, first, second = repo
    _manifest(root, SOLO, first, ["one"])
    with _count_spawns_attributed(monkeypatch) as in_run:
        assert op._handler({"run_id": SOLO, "sha": second, "in_run": True}, root)["ok"]
    halted_id = "warp-2026-10-21-halted-20261021T000000-abcdef"
    d = root / "state" / "warp" / halted_id
    d.mkdir(parents=True)
    (d / "halted.json").write_text(json.dumps({"status": "halted", "halted": "x", "sizing_rel": None}), encoding="utf-8")
    with _count_spawns_attributed(monkeypatch) as halted:
        assert op._handler({"run_id": halted_id, "sha": second}, root)["ok"]
    assert len(in_run) <= len(halted)
