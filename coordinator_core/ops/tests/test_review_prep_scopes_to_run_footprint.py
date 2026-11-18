"""review.prep at default arguments freezes the run's own footprint among hundreds of peer-dirty files.

Reproduces the 794-file shape at small scale: a run with declared writes, many peer files edited
after the stage marker by sessions the claim index does not hold, and the run's own writes. The
frozen product set is the declared footprint and no declared path with a delta is `no-delta`.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.tests.conftest import stub_own_write_claims
from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot as snap
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
RUN = "warp-2026-11-12-scope-aaaaaa-20261112T000000-bbbbbb"
OWN = [f"own/mod_{i}.txt" for i in range(5)]
PEERS = [f"peer/dir_{i % 5}/file_{i}.txt" for i in range(50)]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    files = {rel: "v1\n" for rel in [*OWN, *PEERS]}
    files["docs/plans/p.md"] = "---\nplan_id: pln-test-1\n---\n\n# p\n"
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    record = record_dir(tmp_path, RUN)
    record.mkdir(parents=True, exist_ok=True)
    (record / "manifest.json").write_text(json.dumps({"review_declared_paths": OWN}), encoding="utf-8")
    snap.write_snapshot(tmp_path, record)
    time.sleep(0.05)
    for rel in OWN:
        (tmp_path / rel).write_text("own v2\n", encoding="utf-8")
    for rel in PEERS:
        (tmp_path / rel).write_text("peer v2\n", encoding="utf-8")
    return tmp_path


def _prep(repo: Path) -> dict:
    return rp._handler(
        {
            "run_id": RUN,
            "base": _git(repo, "rev-parse", "HEAD").strip(),
            "worktree": True,
            "plan_path": "docs/plans/p.md",
            "session_id": SESSION,
        },
        repo,
    )


def test_default_prep_freezes_only_the_run_footprint_among_peer_dirt(repo):
    reply = _prep(repo)
    assert reply["error"] is None
    assert set(reply["reviewed_sha256"]) == set(OWN)
    assert reply["product_files"] == len(OWN)
    diff = Path(reply["whole_diff_path"]).read_text(encoding="utf-8")
    assert not any(p in diff for p in PEERS)
    assert all(f"diff --git a/{p} b/{p}" in diff for p in OWN)
    assert [e for e in reply["excluded"] if e["path"] in OWN] == []
    assert sorted(f for s in reply["slices"] for f in s["files"]) == sorted(OWN)


def test_peer_dirt_adopted_nothing_when_run_edits_are_all_declared(repo):
    assert _prep(repo)["undeclared_edits"] == []


@pytest.fixture
def own_session_claims(monkeypatch):
    stub_own_write_claims(monkeypatch, SESSION)


def test_a_few_stray_edits_are_still_adopted(repo, own_session_claims):
    stray = PEERS[:3]
    for rel in PEERS:
        if rel not in stray:
            (repo / rel).write_text("v1\n", encoding="utf-8")
    reply = _prep(repo)
    assert reply["undeclared_edits"] == sorted(stray)
    assert set(reply["reviewed_sha256"]) == set(OWN) | set(stray)


def test_declared_path_with_a_product_delta_is_never_no_delta(repo, monkeypatch):
    def partition_missing_one(repo_root, base, paths, slice_prefix, **kwargs):
        diff_path = kwargs["whole_diff"]({})["diff_path"]
        return {
            "verdict": "PARTITION-MANDATORY",
            "error": None,
            "slices": [{"slice_id": f"{slice_prefix}-1", "paths": OWN[1:], "diff_path": diff_path}],
        }

    monkeypatch.setattr(rp, "partition_slices", partition_missing_one)
    excluded = [e["path"] for e in _prep(repo)["excluded"]]
    assert OWN[0] not in excluded


def _restore_clean(repo: Path, rels) -> None:
    """Rewrite ``rels`` to the committed bytes and stamp the index's recorded mtime, so the worktree
    stat matches the index without a git spawn (``update-index --refresh`` does not resync it on Windows)."""
    from coordinator_core.git import git_index

    entries = git_index.parse_index_identity(repo)
    for rel in rels:
        (repo / rel).write_text("v1\n", encoding="utf-8")
        entry = entries[rel]
        stamp = entry.mtime * 1_000_000_000 + entry.mtime_nsec
        os.utime(repo / rel, ns=(stamp, stamp))


def test_run_with_clean_declared_paths_adopts_no_foreign_dirt(repo):
    _restore_clean(repo, [*OWN, *PEERS[rp.MAX_ADOPTED_UNDECLARED - 6 :]])
    stray = [p for p in PEERS if (repo / p).read_text(encoding="utf-8") != "v1\n"]
    assert 0 < len(stray) <= rp.MAX_ADOPTED_UNDECLARED
    assert rp.undeclared_edits_for_run(repo, RUN, OWN, SESSION) == []
