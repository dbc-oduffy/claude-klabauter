"""_stamp_coded_commit re-attempts its commit_v2 call when a peer moved the ref (lost CAS) and
lands without coded_stamp_error; an exhausted loop is reported as 'retry: ref moved'."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git import ref_cas_retry
from coordinator_core.ops.dispatch_emit import terminal_commit_stamp as tcs

LOST = "compare-and-swap failed -- refs/heads/x moved since abc was read"


def _fake_commit(replies):
    calls = []

    def commit_v2(params, repo_root):
        calls.append(params)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    return commit_v2, calls


def _run(tmp_path: Path, commit_v2, monkeypatch):
    (tmp_path / "plan.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(ref_cas_retry, "head_sha", lambda root: "h1")
    monkeypatch.setattr(ref_cas_retry, "paths_unchanged_between", lambda *a: True)
    # No rows to flip: also_commit alone drives the commit.
    return tcs._stamp_coded_commit(
        commit_v2, tmp_path, tmp_path, {}, "deadbeef" * 5, None, also_commit=("plan.md",),
    )


def test_lost_cas_then_success_lands_without_error(tmp_path, monkeypatch):
    commit_v2, calls = _fake_commit(
        [{"committed": False, "error": LOST}, {"committed": True, "sha": "c0ffee"}]
    )
    out = _run(tmp_path, commit_v2, monkeypatch)
    assert out.get("coded_sha") == "c0ffee"
    assert "coded_stamp_error" not in out
    assert len(calls) == 2


def test_persistent_cas_loss_is_bounded_and_reads_retry(tmp_path, monkeypatch):
    commit_v2, calls = _fake_commit([{"committed": False, "error": LOST}])
    out = _run(tmp_path, commit_v2, monkeypatch)
    assert len(calls) == ref_cas_retry.REF_CAS_ATTEMPTS
    assert out["coded_stamp_error"].startswith("retry: ref moved")


def test_other_refusal_is_not_retried(tmp_path, monkeypatch):
    commit_v2, calls = _fake_commit([{"committed": False, "error": "blob drift"}])
    out = _run(tmp_path, commit_v2, monkeypatch)
    assert len(calls) == 1
    assert not out["coded_stamp_error"].startswith("retry: ref moved")
