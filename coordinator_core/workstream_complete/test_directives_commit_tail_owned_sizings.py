"""The close commit folds the session's own dirty sizings into its stage paths.

Real git fixture repo; the claim surface is supplied as a `ResidueSnapshot`
and the peer-claim readers are patched, so no session store is needed.

Run: python3 -m pytest coordinator_core/workstream_complete/test_directives_commit_tail_owned_sizings.py -q
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.session import record_homes
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete import directives_commit_tail as tail
from coordinator_core.workstream_complete.close_residue import ResidueSnapshot

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SID = "sid-owned-sizings"
_HOME = record_homes.home_dir("", "sizings").replace("\\", "/")
_OWNED = f"{_HOME}/owned.yaml"
_PEER = f"{_HOME}/peer.yaml"
_UNCLAIMED = f"{_HOME}/unclaimed.yaml"
_OTHER = "elsewhere/claimed.txt"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    (root / _HOME).mkdir(parents=True)
    (root / "elsewhere").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for rel in (_OWNED, _PEER, _UNCLAIMED, _OTHER):
        (root / rel).write_text("v1\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    if _git("commit", "-qm", "seed", cwd=root).returncode != 0:
        pytest.skip("git unavailable")
    for rel in (_OWNED, _PEER, _UNCLAIMED, _OTHER, "seed.txt"):
        (root / rel).write_text("v2\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _peers(monkeypatch):
    monkeypatch.setattr(tail, "_enumerate_peer_session_ids", lambda *_a: (["peer"], True))
    monkeypatch.setattr(tail, "_session_live_conservative", lambda *_a: True)
    monkeypatch.setattr(tail, "_peer_touch_claimed_paths", lambda *_a: {_PEER})


def _close(root, owned, **kw):
    snap = ResidueSnapshot(tuple(owned), (), False)
    return tail.run_close_commit_and_release_claims(
        root, session_id=_SID, subject="close", stage_paths=["seed.txt"],
        residue_snapshot=snap, **kw,
    )


def _committed(root, sha):
    return set(_git("show", "--name-only", "--format=", sha, cwd=root).stdout.split())


def test_claimed_dirty_sizing_outside_stage_paths_lands(repo):
    result = _close(repo, [_OWNED])

    assert result.committed_sha, result.diagnostics
    assert _OWNED in _committed(repo, result.committed_sha)
    assert f"owned sizing staged: {_OWNED}" in result.diagnostics


def test_sizing_a_live_peer_also_claims_stays_out(repo):
    result = _close(repo, [_OWNED, _PEER])

    assert _PEER not in _committed(repo, result.committed_sha)
    assert _OWNED in _committed(repo, result.committed_sha)


def test_unclaimed_dirty_sizing_stays_out(repo):
    result = _close(repo, [_OWNED])

    assert _UNCLAIMED not in _committed(repo, result.committed_sha)


def test_non_sizing_claimed_dirty_file_is_not_added(repo):
    result = _close(repo, [_OTHER])

    assert _OTHER not in _committed(repo, result.committed_sha)
    assert not [d for d in result.diagnostics if d.startswith("owned sizing staged")]


def test_falsy_session_id_folds_nothing(repo):
    paths, added, orphans, refused = tail._fold_owned_dirty_sizings(
        repo, "", ["seed.txt"], ResidueSnapshot((_OWNED,), (), False)
    )

    assert paths == ["seed.txt"] and added == [] and orphans == [] and refused == []


_VALID_SIZING = """schema: sizing-object
name: "x"
intent: "an intent"
estimate:
  tshirt: S
  provisional: true
route: spec-dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: read
  evidence: "read"
"""


def _orphan(root, name, text=_VALID_SIZING, age_s=7200):
    rel = f"{_HOME}/{name}"
    path = root / rel
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return rel


def test_untracked_orphan_sizing_is_swept_into_the_close_commit(repo):
    orphan = _orphan(repo, "orphan.yaml")

    result = _close(repo, [])

    assert result.committed_sha, result.diagnostics
    assert orphan in _committed(repo, result.committed_sha)
    assert f"orphan sizing staged: {orphan}" in result.diagnostics


def test_peer_claimed_untracked_sizing_stays_out(repo, monkeypatch):
    orphan = _orphan(repo, "orphan.yaml")
    monkeypatch.setattr(tail, "_peer_touch_claimed_paths", lambda *_a: {_PEER, orphan})

    result = _close(repo, [])

    assert orphan not in _committed(repo, result.committed_sha)


def test_schema_invalid_orphan_is_refused_not_committed(repo):
    bad = _orphan(repo, "bad.yaml", "a: 1\n")

    result = _close(repo, [])

    assert bad not in _committed(repo, result.committed_sha)
    assert any(d.startswith(f"orphan sizing refused: {bad}: ") for d in result.diagnostics)
    assert (repo / bad).exists()
