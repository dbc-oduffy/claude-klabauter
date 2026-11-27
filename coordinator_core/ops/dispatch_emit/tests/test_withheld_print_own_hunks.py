"""A withheld path a co-tenant run also holds is printed as this run's own-hunk blob, or as a typed
``co_tenant`` entry when the hunks cannot be separated; a path no peer holds keeps the tree fingerprint."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import co_tenant_owner, run_hunk_claims
from coordinator_core.ops.dispatch_emit.halted_record_ledger import _record_withheld_prints
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha, fingerprint
from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE

REL = "state/warp/run-own"
HEAD = b"a\nb\nc\nd\ne\n"
BOTH = b"a\nOWN\nc\nd\nPEER\n"
OWN_ONLY = b"a\nOWN\nc\nd\ne\n"


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "shared.py").write_bytes(HEAD)
    _git(tmp_path, "add", "shared.py")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / REL).mkdir(parents=True)
    (tmp_path / "shared.py").write_bytes(BOTH)
    return tmp_path


def _prints(root):
    return json.loads((root / REL / WITHHELD_PRINTS_FILE).read_text(encoding="utf-8"))


def _owner(monkeypatch, run_id):
    seen: dict = {}

    def owner_of(root, path, *, exclude_run_id, include_waiting=False):
        seen["exclude"] = exclude_run_id
        return {"run_id": run_id, "state": "live" if run_id else None}

    monkeypatch.setattr(co_tenant_owner, "owner_of", owner_of)
    return seen


def _split_own_line_two(monkeypatch):
    def own_hunks(root, run_id, path, hunks, holders, *, head_blob=None):
        mine = [h for h in hunks if h.old_start == 2]
        return run_hunk_claims.HunkSplit(own=mine, unattributed=[h for h in hunks if h not in mine])

    monkeypatch.setattr(run_hunk_claims, "own_hunks", own_hunks)


def test_shared_path_prints_the_own_hunk_blob_not_the_tree(repo, monkeypatch):
    seen = _owner(monkeypatch, "peer-run")
    _split_own_line_two(monkeypatch)

    _record_withheld_prints(repo, REL, {"shared.py": "run-dirt-uncommitted"}, [])

    got = _prints(repo)["shared.py"]
    assert seen["exclude"] == "run-own"
    assert got == blob_sha(OWN_ONLY) and got != fingerprint(repo, "shared.py")


def test_inseparable_hunks_print_a_typed_co_tenant_entry(repo, monkeypatch):
    _owner(monkeypatch, "peer-run")
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda root, run_id, path, hunks, holders, *, head_blob=None: run_hunk_claims.HunkSplit(
            unattributed=list(hunks)
        ),
    )

    _record_withheld_prints(repo, REL, {"shared.py": "run-dirt-uncommitted"}, [])

    assert _prints(repo) == {"shared.py": {"kind": "co_tenant", "owner": "peer-run"}}


def test_path_no_peer_holds_keeps_the_tree_fingerprint(repo, monkeypatch):
    _owner(monkeypatch, None)

    _record_withheld_prints(repo, REL, {"shared.py": "run-dirt-uncommitted"}, [])

    assert _prints(repo) == {"shared.py": blob_sha(BOTH)}


def test_attribution_error_prints_typed_never_the_tree(repo, monkeypatch):
    _owner(monkeypatch, "peer-run")

    def boom(*a, **k):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(run_hunk_claims, "own_hunks", boom)

    _record_withheld_prints(repo, REL, {"shared.py": "run-dirt-uncommitted"}, [])

    assert _prints(repo)["shared.py"] == {"kind": "co_tenant", "owner": "peer-run"}


def test_owner_lookup_failure_falls_back_to_the_tree(repo, monkeypatch):
    def boom(*a, **k):
        raise OSError("index unreadable")

    monkeypatch.setattr(co_tenant_owner, "owner_of", boom)

    _record_withheld_prints(repo, REL, {"shared.py": "run-dirt-uncommitted"}, [])

    assert _prints(repo) == {"shared.py": blob_sha(BOTH)}


def test_typed_entry_is_skipped_by_the_str_readers(repo, monkeypatch):
    """The typed entry never reaches a reader as a blob id: commit-time credit and the superseded
    close take only str values."""
    from coordinator_core.ops.dispatch_emit import halted_record_commit, halted_run_superseded  # noqa: F401

    _owner(monkeypatch, "peer-run")
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda root, run_id, path, hunks, holders, *, head_blob=None: run_hunk_claims.HunkSplit(
            unattributed=list(hunks)
        ),
    )
    _record_withheld_prints(repo, REL, {"shared.py": "run-dirt-uncommitted"}, [])

    matched, mismatched = halted_record_commit.print_verdicts(repo, REL, ["shared.py"])

    assert matched == set() and mismatched == set()
