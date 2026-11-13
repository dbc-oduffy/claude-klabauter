"""co_declared_settle: same-parent siblings and per-run artifacts settle at hunk level; the rest hold."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import co_declared_settle as cds
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit.peer_hunk_gate import UNREADABLE_PEER_CLAIMS

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

ME = "ask-20261027T000000-aaaaaa"
SIB = "ask-20261027T000000-bbbbbb"
PARENT = "ask-parent-20261027T000000-111111"
SRC = "src/target.py"
DELIVERY = "docs/delivery/shared.md"


def _git(root, *args):
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _text(n, edits=None):
    edits = edits or {}
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, n + 1))


@pytest.fixture
def world(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "docs" / "delivery").mkdir(parents=True)
    (tmp_path / SRC).write_bytes(_text(10).encode())
    (tmp_path / DELIVERY).write_bytes("head1\nhead2\n".encode())
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    baselines: dict = {}
    monkeypatch.setattr(hha, "_baseline", lambda root, run: baselines.get(run))

    class World:
        root = tmp_path

        @staticmethod
        def baseline(run, path, text):
            sha = git_objects.write_object(resolve_git_common_dir(tmp_path), b"blob", text.encode())
            baselines.setdefault(run, {})[path] = sha

        @staticmethod
        def parent(run, parent=PARENT):
            rel = tmp_path / contract.run_parent_copy_rel(run)
            rel.parent.mkdir(parents=True, exist_ok=True)
            rel.write_text(json.dumps({"parent_id": parent}))

    return World


def test_per_run_artifact_names():
    assert cds.is_per_run_artifact("DELIVERY.md")
    assert cds.is_per_run_artifact("sub/DELIVERY.md")
    assert cds.is_per_run_artifact("docs/delivery/ask-1.md")
    assert not cds.is_per_run_artifact("docs/delivery.md")
    assert not cds.is_per_run_artifact(SRC)


def test_same_parent_sibling_with_disjoint_hunks_stages_only_own(world):
    world.parent(ME)
    world.parent(SIB)
    (world.root / SRC).write_bytes(_text(10, {2: "mine", 9: "theirs"}).encode())
    world.baseline(ME, SRC, _text(10, {9: "theirs"}))
    world.baseline(SIB, SRC, _text(10, {2: "mine"}))
    stage, still = cds.settle(world.root, ME, [(SRC, SIB)])
    assert list(stage) == [SRC]
    assert [(h.old_start, h.old_len) for h in stage[SRC]] == [(2, 1)]
    assert still == []


def test_other_parent_sibling_still_holds(world):
    world.parent(ME)
    world.parent(SIB, "ask-parent-other")
    (world.root / SRC).write_bytes(_text(10, {2: "mine", 9: "theirs"}).encode())
    world.baseline(ME, SRC, _text(10, {9: "theirs"}))
    world.baseline(SIB, SRC, _text(10, {2: "mine"}))
    assert cds.settle(world.root, ME, [(SRC, SIB)]) == ({}, [(SRC, SIB)])


def test_overlapping_hunks_of_same_parent_sibling_hold(world):
    world.parent(ME)
    world.parent(SIB)
    (world.root / SRC).write_bytes(_text(10, {3: "both"}).encode())
    world.baseline(ME, SRC, _text(10))
    world.baseline(SIB, SRC, _text(10))
    assert cds.settle(world.root, ME, [(SRC, SIB)]) == ({}, [(SRC, SIB)])


def test_unreadable_peer_claims_never_settle_even_a_per_run_artifact(world):
    (world.root / DELIVERY).write_bytes("head1\nhead2\nmine\n".encode())
    world.baseline(ME, DELIVERY, "head1\nhead2\n")
    holds = [(DELIVERY, UNREADABLE_PEER_CLAIMS)]
    assert cds.settle(world.root, ME, holds) == ({}, holds)


def test_per_run_artifact_with_a_foreign_parent_sibling_settles_overlapping_appends(world):
    (world.root / DELIVERY).write_bytes("head1\nhead2\nmine1\nmine2\ntheirs1\n".encode())
    world.baseline(ME, DELIVERY, "head1\nhead2\n")
    world.baseline(SIB, DELIVERY, "head1\nhead2\nmine1\nmine2\n")
    stage, still = cds.settle(world.root, ME, [(DELIVERY, SIB)])
    assert still == []
    assert [(h.old_start, h.old_len, h.new_start, h.new_len) for h in stage[DELIVERY]] == [(2, 0, 3, 2)]


def test_settled_blob_carries_only_the_own_inserted_lines(world):
    (world.root / DELIVERY).write_bytes("head1\nhead2\nmine1\nmine2\ntheirs1\n".encode())
    world.baseline(ME, DELIVERY, "head1\nhead2\n")
    world.baseline(SIB, DELIVERY, "head1\nhead2\nmine1\nmine2\n")
    stage, _still = cds.settle(world.root, ME, [(DELIVERY, SIB)])
    head = b"head1\nhead2\n"
    sha = cds.settled_blob(world.root, DELIVERY, head, stage[DELIVERY])
    kind, data = git_objects.read_object(resolve_git_common_dir(world.root), sha)
    assert kind == "blob" and data == b"head1\nhead2\nmine1\nmine2\n"


def test_a_line_both_runs_changed_in_a_per_run_artifact_still_holds(world):
    (world.root / DELIVERY).write_bytes("both\nhead2\n".encode())
    world.baseline(ME, DELIVERY, "head1\nhead2\n")
    world.baseline(SIB, DELIVERY, "head1\nhead2\n")
    holds = [(DELIVERY, SIB)]
    assert cds.settle(world.root, ME, holds) == ({}, holds)


def test_an_unattributed_hunk_in_a_per_run_artifact_holds(world):
    (world.root / DELIVERY).write_bytes("head1\nhead2\nstray\n".encode())
    world.baseline(ME, DELIVERY, "head1\nhead2\nstray\n")
    world.baseline(SIB, DELIVERY, "head1\nhead2\nstray\n")
    holds = [(DELIVERY, SIB)]
    assert cds.settle(world.root, ME, holds) == ({}, holds)


def test_no_run_id_or_no_holds_is_a_no_op(world):
    assert cds.settle(world.root, None, [(SRC, SIB)]) == ({}, [(SRC, SIB)])
    assert cds.settle(world.root, ME, []) == ({}, [])


@pytest.fixture
def holds_of(monkeypatch):
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate

    def install(pairs):
        monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: list(pairs))

    return install


def _eligible(world, paths=(SRC,)):
    return cds.settle_eligible(world.root, ME, list(paths), own_declared=list(paths))


def test_eligible_when_every_hold_is_a_live_same_parent_sibling(world, holds_of):
    world.parent(ME)
    world.parent(SIB)
    holds_of([(SRC, SIB)])
    assert _eligible(world) == frozenset({SRC})


def test_halted_sibling_is_not_eligible(world, holds_of, monkeypatch):
    from coordinator_core.ops import warp_run_deliverable

    world.parent(ME)
    world.parent(SIB)
    monkeypatch.setattr(warp_run_deliverable, "_halted", lambda root, run: True)
    holds_of([(SRC, SIB)])
    assert _eligible(world) == frozenset()


def test_different_parent_sibling_is_not_eligible(world, holds_of):
    world.parent(ME)
    world.parent(SIB, "ask-parent-other")
    holds_of([(SRC, SIB)])
    assert _eligible(world) == frozenset()


def test_unreadable_peer_claims_is_not_eligible(world, holds_of):
    holds_of([(DELIVERY, UNREADABLE_PEER_CLAIMS)])
    assert _eligible(world, [DELIVERY]) == frozenset()


def test_one_disqualifying_sibling_among_live_ones_disqualifies(world, holds_of):
    other = "ask-20261027T000000-cccccc"
    world.parent(ME)
    world.parent(SIB)
    world.parent(other, "ask-parent-other")
    holds_of([(SRC, SIB), (SRC, other)])
    assert _eligible(world) == frozenset()


def test_per_run_artifact_is_eligible_across_parents_and_unheld_path_is_not(world, holds_of):
    holds_of([(DELIVERY, SIB)])
    assert _eligible(world, [DELIVERY, SRC]) == frozenset({DELIVERY})


def test_settle_eligible_never_raises(world, monkeypatch):
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", boom)
    assert _eligible(world) == frozenset()
    assert cds.settle_eligible(world.root, None, [SRC], own_declared=[SRC]) == frozenset()


REGISTRY = "coordinator_core/ops/_registry_map.py"
REG_HEAD = "head1\nhead2\nhead3\n"


def _registry_world(world, path=REGISTRY):
    (world.root / path).parent.mkdir(parents=True, exist_ok=True)
    (world.root / path).write_bytes(REG_HEAD.encode())
    _git(world.root, "add", path)
    _git(world.root, "commit", "-q", "-m", "registry seed")


def test_additive_registry_names():
    assert cds.is_additive_registry("coordinator_core/ops/_registry_map.py")
    assert cds.is_additive_registry("./coordinator_core\\ops\\__init__.py")
    assert not cds.is_additive_registry("coordinator_core/ops/ceremony/__init__.py")
    assert not cds.is_additive_registry(SRC)


@pytest.mark.parametrize("path", sorted(cds.ADDITIVE_REGISTRY_FILES))
def test_registry_rows_appended_at_the_same_anchor_by_an_unrelated_run_settle_to_own_lines(world, path):
    _registry_world(world, path)
    world.parent(ME)
    world.parent(SIB, "ask-parent-other")
    (world.root / path).write_bytes((REG_HEAD + "mine\ntheirs\n").encode())
    world.baseline(ME, path, REG_HEAD)
    world.baseline(SIB, path, REG_HEAD + "mine\n")
    stage, still = cds.settle(world.root, ME, [(path, SIB)])
    assert still == []
    assert [(h.old_start, h.old_len, h.new_start, h.new_len) for h in stage[path]] == [(3, 0, 4, 1)]
    sha = cds.settled_blob(world.root, path, REG_HEAD.encode(), stage[path])
    assert git_objects.read_object(resolve_git_common_dir(world.root), sha)[1] == (REG_HEAD + "mine\n").encode()


def test_registry_rows_at_different_anchors_each_settle_to_own_row(world):
    _registry_world(world)
    world.parent(ME)
    world.parent(SIB, "ask-parent-other")
    (world.root / REGISTRY).write_bytes("head1\nmine\nhead2\nhead3\ntheirs\n".encode())
    world.baseline(ME, REGISTRY, "head1\nhead2\nhead3\ntheirs\n")
    world.baseline(SIB, REGISTRY, "head1\nmine\nhead2\nhead3\n")
    stage, still = cds.settle(world.root, ME, [(REGISTRY, SIB)])
    assert still == []
    sha = cds.settled_blob(world.root, REGISTRY, REG_HEAD.encode(), stage[REGISTRY])
    assert git_objects.read_object(resolve_git_common_dir(world.root), sha)[1] == b"head1\nmine\nhead2\nhead3\n"


def test_a_registry_line_modified_by_the_own_run_still_holds(world):
    _registry_world(world)
    world.parent(ME)
    world.parent(SIB, "ask-parent-other")
    (world.root / REGISTRY).write_bytes("head1\nedited\nhead3\ntheirs\n".encode())
    world.baseline(ME, REGISTRY, "head1\nhead2\nhead3\ntheirs\n")
    world.baseline(SIB, REGISTRY, "head1\nedited\nhead3\n")
    holds = [(REGISTRY, SIB)]
    assert cds.settle(world.root, ME, holds) == ({}, holds)


def test_a_registry_line_deleted_by_the_own_run_still_holds(world):
    _registry_world(world)
    world.parent(ME)
    world.parent(SIB, "ask-parent-other")
    (world.root / REGISTRY).write_bytes("head1\nhead3\ntheirs\n".encode())
    world.baseline(ME, REGISTRY, "head1\nhead2\nhead3\ntheirs\n")
    world.baseline(SIB, REGISTRY, "head1\nhead3\n")
    holds = [(REGISTRY, SIB)]
    assert cds.settle(world.root, ME, holds) == ({}, holds)


def test_a_registry_insertion_touching_a_line_the_peer_changed_still_holds(world):
    _registry_world(world)
    world.parent(ME)
    world.parent(SIB, "ask-parent-other")
    (world.root / REGISTRY).write_bytes("head1\nmine\npeer2\nhead3\n".encode())
    world.baseline(ME, REGISTRY, "head1\npeer2\nhead3\n")
    world.baseline(SIB, REGISTRY, "head1\nhead2\nhead3\n")
    holds = [(REGISTRY, SIB)]
    assert cds.settle(world.root, ME, holds) == ({}, holds)


def test_an_unattributed_registry_hunk_still_holds(world):
    _registry_world(world)
    (world.root / REGISTRY).write_bytes((REG_HEAD + "stray\n").encode())
    world.baseline(ME, REGISTRY, REG_HEAD + "stray\n")
    world.baseline(SIB, REGISTRY, REG_HEAD + "stray\n")
    holds = [(REGISTRY, SIB)]
    assert cds.settle(world.root, ME, holds) == ({}, holds)


def test_registry_with_unreadable_peer_claims_is_not_eligible_and_never_settles(world, holds_of):
    _registry_world(world)
    (world.root / REGISTRY).write_bytes((REG_HEAD + "mine\n").encode())
    world.baseline(ME, REGISTRY, REG_HEAD)
    holds = [(REGISTRY, UNREADABLE_PEER_CLAIMS)]
    assert cds.settle(world.root, ME, holds) == ({}, holds)
    holds_of(holds)
    assert _eligible(world, [REGISTRY]) == frozenset()


def test_registry_is_eligible_across_parents_while_a_plain_path_is_not(world, holds_of):
    world.parent(ME)
    world.parent(SIB, "ask-parent-other")
    holds_of([(REGISTRY, SIB), (SRC, SIB)])
    assert _eligible(world, [REGISTRY, SRC]) == frozenset({REGISTRY})


def _junior_world(world, sib_baseline):
    world.parent(ME)
    world.parent(SIB)
    (world.root / SRC).write_bytes(_text(10, {2: "mine", 9: "theirs"}).encode())
    world.baseline(ME, SRC, _text(10))
    world.baseline(SIB, SRC, sib_baseline)
    return cds.settle(world.root, ME, [(SRC, SIB)])


def test_junior_sibling_whose_baseline_is_head_plus_own_holds_the_unattributed_hunk(world):
    stage, still = _junior_world(world, _text(10, {2: "mine"}))
    assert still == []
    assert [(h.old_start, h.old_len) for h in stage[SRC]] == [(2, 1)]


def test_junior_sibling_with_a_baseline_that_is_not_head_plus_own_still_holds(world):
    assert _junior_world(world, _text(10)) == ({}, [(SRC, SIB)])


def _sibling_delta_after_split(monkeypatch, value):
    """Make the junior-hold probe (which runs after `own_hunks`) see `value` as the sibling's delta."""
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    real_delta, real_split, split_done = rhc._claimant_delta, rhc.own_hunks, []

    def split(*a, **k):
        out = real_split(*a, **k)
        split_done.append(True)
        return out

    monkeypatch.setattr(rhc, "own_hunks", split)
    monkeypatch.setattr(
        rhc, "_claimant_delta",
        lambda root, common, run, *a, **k: value if split_done and run == SIB else real_delta(root, common, run, *a, **k),
    )


def test_junior_sibling_with_no_readable_delta_still_holds(world, monkeypatch):
    _sibling_delta_after_split(monkeypatch, None)
    assert _junior_world(world, _text(10, {2: "mine"})) == ({}, [(SRC, SIB)])


def test_junior_sibling_delta_not_covering_the_hunk_still_holds(world, monkeypatch):
    _sibling_delta_after_split(monkeypatch, [])
    assert _junior_world(world, _text(10, {2: "mine"})) == ({}, [(SRC, SIB)])
