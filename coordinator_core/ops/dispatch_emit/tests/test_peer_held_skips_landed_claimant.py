"""Both _peer_held sites skip a live peer whose last-seen touch hash is the HEAD blob; one batched read, fail closed."""
import subprocess

import pytest

from coordinator_core.git.content_hash import eol_folded_sha256
from coordinator_core.ops.dispatch_emit import halted_hold_release, land_run, terminal_commit_refusals
from coordinator_core.session import claim_index, liveness, touch_record
from pathlib import Path

BODY = b"landed\r\nbytes\n"
PATH = "t/test_x.py"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    def git(*a) -> None:
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "a@b.c")
    git("config", "user.name", "n")
    (tmp_path / "t").mkdir()
    (tmp_path / PATH).write_bytes(BODY)
    git("add", ".")
    git("commit", "-qm", "c")
    return tmp_path


@pytest.fixture
def peer(monkeypatch: pytest.MonkeyPatch):
    """A live peer ``peer1`` holding PATH; ``state['hash']`` is its newest recorded hash."""
    state = {"hash": None, "reads": 0}
    monkeypatch.setattr(claim_index, "lookup", lambda paths, cwd=None: {p: ["peer1"] for p in paths})
    monkeypatch.setattr(touch_record, "kind_blocks_a_peer_commit", lambda kind: True)
    monkeypatch.setattr(liveness, "session_live", lambda sid, root: True)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, root: False)
    monkeypatch.setattr(touch_record, "last_seen_hashes", lambda sid, paths, root: {p: state["hash"] for p in paths})
    monkeypatch.setattr(
        claim_index,
        "classify_paths",
        lambda sid, paths, **k: claim_index.OwnershipAnswer(
            by_path={p: claim_index.PathOwnership(claim_index.OWNERSHIP_PEER, ["peer1"]) for p in paths}, complete=True
        ),
    )
    real = halted_hold_release._blob_fetch

    def counted(root, specs):
        state["reads"] += 1
        return real(root, specs)

    monkeypatch.setattr(halted_hold_release, "_blob_fetch", counted)
    return state


def test_a_claimant_whose_hash_is_the_head_blob_holds_nothing(repo, peer) -> None:
    peer["hash"] = eol_folded_sha256(BODY)
    assert land_run._peer_held(repo, [PATH], "me", None, None) == {}
    assert terminal_commit_refusals._peer_held(repo, "me", [PATH]) == set()


@pytest.mark.parametrize("recorded", [None, "0" * 64])
def test_a_hashless_or_differing_claimant_still_holds(repo, peer, recorded) -> None:
    peer["hash"] = recorded
    assert land_run._peer_held(repo, [PATH], "me", None, None) == {PATH: ["peer1"]}
    assert terminal_commit_refusals._peer_held(repo, "me", [PATH]) == {PATH}


def test_a_path_absent_from_head_still_holds(repo, peer) -> None:
    peer["hash"] = eol_folded_sha256(BODY)
    assert land_run._peer_held(repo, ["t/new.py"], "me", None, None) == {"t/new.py": ["peer1"]}
    assert terminal_commit_refusals._peer_held(repo, "me", ["t/new.py"]) == {"t/new.py"}


def test_a_failed_blob_read_keeps_the_hold(repo, peer, monkeypatch: pytest.MonkeyPatch) -> None:
    peer["hash"] = eol_folded_sha256(BODY)
    monkeypatch.setattr(halted_hold_release, "_blob_fetch", lambda root, specs: None)
    assert land_run._peer_held(repo, [PATH], "me", None, None) == {PATH: ["peer1"]}
    assert terminal_commit_refusals._peer_held(repo, "me", [PATH]) == {PATH}


def test_one_batched_read_over_many_held_paths_and_none_when_nothing_is_held(repo, peer, monkeypatch: pytest.MonkeyPatch) -> None:
    peer["hash"] = "0" * 64
    land_run._peer_held(repo, [PATH, "t/other.py"], "me", None, None)
    assert peer["reads"] == 1
    peer["reads"] = 0
    monkeypatch.setattr(claim_index, "lookup", lambda paths, cwd=None: {})
    assert land_run._peer_held(repo, [PATH], "me", None, None) == {}
    assert peer["reads"] == 0
    assert land_run._without_landed_claimants(repo, {}) == {}
    assert peer["reads"] == 0
