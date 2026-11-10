"""A refire credits its same-sizing predecessor's withheld paths after a peer edited them (the 655e5b shape)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index, land_run
from coordinator_core.ops.dispatch_emit.kept_work import fingerprint
from coordinator_core.ops.dispatch_emit.tests._git_fixture import _git, seed_repo
from coordinator_core.ops.dispatch_emit.write_baseline import (
    capture,
    common_dir_for,
    exclude_halted_withheld,
    persist_raw,
    pre_existing_dirt,
    predecessor_withheld,
    unwritten,
)

pytestmark = pytest.mark.spawns_process

SIZING = "state/sizings/2026-11/a.yaml"
PATH = "pkg/mod.py"
BASE = "alpha\nbeta\n"
OWN_HUNK = "alpha\nPREDECESSOR newline pin\nbeta\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = seed_repo(tmp_path / "repo")
    (root / "pkg").mkdir()
    (root / PATH).write_text(BASE, encoding="utf-8")
    _git(["add", PATH], root)
    _git(["commit", "-q", "-m", "base"], root)
    return root


def _halt(root: Path, *, sizing: str = SIZING, persist: bool = True) -> str:
    """The predecessor's halt: its hunk is on disk, its print recorded and (optionally) its blob persisted."""
    (root / PATH).write_text(OWN_HUNK, encoding="utf-8", newline="\n")
    print_ = fingerprint(root, PATH)
    if persist:
        persist_raw(common_dir_for(root), (root / PATH).read_bytes())
    halted_withheld_index.add_run(root, "pred", [PATH], sizing_rel=sizing, prints={PATH: print_}, at_ns=1)
    (root / halted_withheld_index.INDEX_DIR / halted_withheld_index.SENTINEL).write_bytes(b"")
    return print_


def _peer_edit(root: Path, text: str) -> None:
    (root / PATH).write_text(text, encoding="utf-8", newline="\n")


def test_peer_hunk_beside_withheld_hunk_still_credits(repo):
    _halt(repo)
    _peer_edit(repo, OWN_HUNK + "PEER hunk\n")
    assert predecessor_withheld(repo, SIZING, repo / "refire") == frozenset({PATH})


def test_peer_hunk_does_not_turn_withheld_work_into_dirt(repo):
    _halt(repo)
    _peer_edit(repo, OWN_HUNK + "PEER hunk\n")
    captured = capture(repo, [PATH])
    baseline = exclude_halted_withheld(repo, captured, SIZING, "refire")
    assert PATH not in unwritten(repo, baseline, [PATH])
    assert pre_existing_dirt(repo, baseline, [PATH]) == []


def test_foreign_rewrite_dropping_the_run_hunk_stays_uncredited(repo):
    _halt(repo)
    _peer_edit(repo, BASE + "PEER hunk\n")
    assert predecessor_withheld(repo, SIZING, repo / "refire") == frozenset()
    baseline = exclude_halted_withheld(repo, capture(repo, [PATH]), SIZING, "refire")
    assert baseline[PATH] == fingerprint(repo, PATH)
    assert pre_existing_dirt(repo, baseline, [PATH]) == [PATH]


def test_unpersisted_print_credits_nothing_on_mismatch(repo):
    _halt(repo, persist=False)
    _peer_edit(repo, OWN_HUNK + "PEER hunk\n")
    assert predecessor_withheld(repo, SIZING, repo / "refire") == frozenset()


def test_foreign_sizing_is_never_credited_after_a_peer_edit(repo):
    _halt(repo, sizing="state/sizings/2026-11/other.yaml")
    _peer_edit(repo, OWN_HUNK + "PEER hunk\n")
    assert predecessor_withheld(repo, SIZING, repo / "refire") == frozenset()
    baseline = exclude_halted_withheld(repo, capture(repo, [PATH]), SIZING, "refire")
    assert baseline[PATH] == fingerprint(repo, PATH)


def test_footprint_fence_allows_the_predecessor_withheld_path(repo, monkeypatch):
    _halt(repo)
    _peer_edit(repo, OWN_HUNK + "PEER hunk\n")
    monkeypatch.setattr(land_run, "_leg_footprint_writes", lambda root, run_dir: ["other.py"])
    monkeypatch.setattr(land_run, "_manifest_declared", lambda p: ([PATH, "other.py"], "head"))
    monkeypatch.setattr(land_run, "_halt_base", lambda *a: ("base", "src"))
    monkeypatch.setattr(land_run, "_path_changed", lambda common, root, base, p: True)
    monkeypatch.setattr(land_run, "_cited_sizing", lambda root, plan: SIZING)
    fence = land_run._footprint_fence(repo, repo / "state" / "warp" / "refire", {"blocking": []}, None, None)
    assert fence == {"recorded": True, "breach": []}


def test_footprint_fence_still_breaches_an_unrelated_path(repo, monkeypatch):
    _halt(repo)
    monkeypatch.setattr(land_run, "_leg_footprint_writes", lambda root, run_dir: ["other.py"])
    monkeypatch.setattr(land_run, "_manifest_declared", lambda p: (["stray.py"], "head"))
    monkeypatch.setattr(land_run, "_halt_base", lambda *a: ("base", "src"))
    monkeypatch.setattr(land_run, "_path_changed", lambda common, root, base, p: True)
    monkeypatch.setattr(land_run, "_cited_sizing", lambda root, plan: SIZING)
    fence = land_run._footprint_fence(repo, repo / "state" / "warp" / "refire", {"blocking": []}, None, None)
    assert fence == {"recorded": True, "breach": ["stray.py"]}
