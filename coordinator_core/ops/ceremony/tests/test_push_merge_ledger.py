"""The push ladder's divergence-recovery merge writes a commit-ledger entry."""
from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.ops.ceremony import push


def _ok(stdout=""):
    return SimpleNamespace(ok=True, returncode=0, stdout=stdout, stderr="")


def _wire(monkeypatch):
    gn = push.git_native
    monkeypatch.setattr(push, "head_sha_local", lambda _r: "a" * 40)
    monkeypatch.setattr(gn, "merge_tree_write_tree", lambda *a, **k: _ok("t" * 40 + "\n"))
    monkeypatch.setattr(gn, "commit_tree_merge", lambda *a, **k: _ok("b" * 40 + "\n"))
    monkeypatch.setattr(gn, "read_tree_merge_update", lambda *a, **k: _ok())
    return gn


def test_merge_commit_records_ledger_entry(monkeypatch, tmp_path):
    gn = _wire(monkeypatch)
    monkeypatch.setattr(gn, "update_ref", lambda *a, **k: _ok())
    calls = []
    import coordinator_core.contract.apply_base as ab

    monkeypatch.setattr(ab, "record_ledger_entry", lambda *a, **k: calls.append(a))

    code, _ = push._rebase_onto_fetched_ref(tmp_path, "origin/main", "main")

    assert code == 0
    assert calls == [(tmp_path, [], "b" * 40)]


def test_failed_ref_move_records_no_ledger_entry(monkeypatch, tmp_path):
    gn = _wire(monkeypatch)
    monkeypatch.setattr(
        gn, "update_ref",
        lambda *a, **k: SimpleNamespace(ok=False, returncode=1, stdout="", stderr="x"),
    )
    calls = []
    import coordinator_core.contract.apply_base as ab

    monkeypatch.setattr(ab, "record_ledger_entry", lambda *a, **k: calls.append(a))

    code, _ = push._rebase_onto_fetched_ref(tmp_path, "origin/main", "main")

    assert code == 1
    assert calls == []
