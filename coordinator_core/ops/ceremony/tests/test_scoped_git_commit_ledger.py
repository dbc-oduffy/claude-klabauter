"""
coordinator_core.ops.ceremony.tests.test_scoped_git_commit_ledger

Tests for C5 (state/dispatch-briefs/2026-08-19-the-baton-carries-its-commits/
C5.md): the commit-ledger append `scoped_git_commit._handler` performs at
commit time, after the pipeline's own reconcile has resolved a final sha.

Coverage:
  - a landed commit with a real sha writes exactly one ledger entry, shaped
    `{sha, kind, weight_basis, reviewed_by}` (AC1), keyed on the resolved
    owning handoff_id.
  - `_ledger_kind_and_weight` computes `kind`/`weight_basis` from the
    CALLER'S pathspec only (never a diff), and never returns a falsy/zero
    weight for a path matching no configured elevated glob (AC5, mirrored
    through `commit_ledger.classify.weight_for_path`).
  - a ledger I/O failure (the store forced to raise) degrades to a warning;
    the commit response is untouched (AC3).
  - a `sha_unverified` landing is SKIPPED, never recorded under a
    placeholder sha (AC3b), and the append helper is not called for it.
  - RUNTIME negative (per the brief and its C5-addendum): the ledger-write
    call sequence this chunk adds -- `_ledger_kind_and_weight`,
    `resolve_owner_handoff_id`, `store.append_entry` -- spawns zero git
    subprocesses when driven with the SAME `worktree_root`/`cwd` a real
    commit already warmed `session_core.sessions_dir`'s cache for (see
    `session.core.sessions_dir`'s own caching-policy docstring) -- the
    delta this chunk adds to the commit path, not an absolute spawn count
    for the whole op (C5-addendum: an absolute figure would go stale the
    moment `scoped_git_commit`'s own cost profile changes again).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.commit_ledger import store as ledger_store
from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.commit_ledger import resolve_owner as _resolve_owner_mod
from coordinator_core.commit_ledger import store as _store_mod
from coordinator_core.session import core as session_core

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _call(params: dict) -> dict:
    return scoped_git_commit._handler(params, repo_root=None)


def test_ledger_kind_and_weight_from_pathspec_only(tmp_path):
    repo = _init_repo(tmp_path)
    kind, weight = scoped_git_commit._ledger_kind_and_weight(
        str(repo), ["docs/reference/some-doc.md"]
    )
    assert kind == scoped_git_commit._LEDGER_DOCS_KIND
    assert weight > 0.0  # AC5: an unlisted path still carries weight, never 0.

    kind2, weight2 = scoped_git_commit._ledger_kind_and_weight(
        str(repo), ["coordinator_core/some_module.py"]
    )
    assert kind2 == scoped_git_commit._LEDGER_CODE_KIND
    assert weight2 > 0.0

    # Mixed pathspec (a code path present) is never classified "doctrine".
    kind3, _weight3 = scoped_git_commit._ledger_kind_and_weight(
        str(repo), ["docs/reference/some-doc.md", "coordinator_core/some_module.py"]
    )
    assert kind3 == scoped_git_commit._LEDGER_CODE_KIND


def test_landed_commit_writes_one_ledger_entry(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    fake_handoff_id = "hnd-c5-ledger-test"
    monkeypatch.setattr(
        _resolve_owner_mod,
        "resolve_owner_handoff_id",
        lambda committer_id, root, sessions_dir=None: (fake_handoff_id, False),
    )

    result = _call(
        {
            "worktree_root": str(repo),
            "paths": ["tasks/feature/todo.md"],
            "message": "add feature todo",
        }
    )
    assert result["committed"] is True
    landed_sha = result["sha"]
    assert landed_sha

    entries = ledger_store.read_entries(fake_handoff_id, cwd=str(repo))
    assert len(entries) == 1
    entry = entries[0]
    assert entry["sha"] == landed_sha
    assert entry["kind"] == scoped_git_commit._LEDGER_DOCS_KIND
    assert entry["weight_basis"] > 0.0
    assert entry["reviewed_by"] == []


def test_ledger_io_failure_degrades_to_warning_commit_unaffected(tmp_path, monkeypatch, caplog):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    def _raise(*a, **k):
        raise RuntimeError("forced ledger store failure")

    monkeypatch.setattr(_store_mod, "append_entry", _raise)
    monkeypatch.setattr(
        _resolve_owner_mod,
        "resolve_owner_handoff_id",
        lambda committer_id, root, sessions_dir=None: ("hnd-forced-failure", False),
    )

    with caplog.at_level("WARNING", logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["tasks/feature/todo.md"],
                "message": "add feature todo",
            }
        )

    assert result["committed"] is True
    assert result["sha"]
    assert "error" not in result
    assert any("commit ledger write failed" in rec.message for rec in caplog.records)


def test_sha_unverified_landing_is_skipped_never_placeholder(tmp_path, monkeypatch, caplog):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/feature/todo.md", "content")

    append_calls = []
    monkeypatch.setattr(
        _store_mod,
        "append_entry",
        lambda *a, **k: append_calls.append((a, k)),
    )

    # Fake a landed-but-sha_unverified pipeline result: `committed` becomes
    # True via `sha_unverified`, `sha` stays None -- the exact shape this
    # chunk must skip rather than placeholder-key.
    from types import SimpleNamespace

    from coordinator_core.ops.ceremony import commit_pipeline as _cp

    fake_result = SimpleNamespace(
        committed_sha=None,
        sha_unverified=True,
        pushed=None,
        push_status=_cp.PUSH_STATUS_NO_REMOTE,
        integrity_breach=False,
        diagnostics=["forced sha_unverified for test"],
        reason="",
        commit=None,
        stage=None,
        unprovenanced_paths=[],
    )
    monkeypatch.setattr(
        scoped_git_commit, "run_commit_pipeline", lambda *a, **k: fake_result
    )

    with caplog.at_level("WARNING", logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        result = _call(
            {
                "worktree_root": str(repo),
                "paths": ["tasks/feature/todo.md"],
                "message": "add feature todo",
            }
        )

    assert result["committed"] is True
    assert result["sha"] is None
    assert append_calls == []
    assert any("skipped from the commit ledger" in rec.message for rec in caplog.records)


def test_ledger_write_call_sequence_spawns_no_git_subprocess(tmp_path):
    """RUNTIME negative (C5-addendum): once a real commit has already
    warmed `session_core.sessions_dir`'s process-local cache for this exact
    `worktree_root` string (the same cwd this chunk's write block reuses,
    see `resolve_owner_handoff_id`/`store.append_entry`'s own `cwd=` calls),
    the ledger-write call sequence this chunk adds spawns no ADDITIONAL git
    subprocess and starts no nested interpreter.

    Deliberately isolates the write block's own three calls
    (`_ledger_kind_and_weight`, `resolve_owner_handoff_id`,
    `_ledger_append_entry`) rather than asserting an absolute spawn count
    for the whole op -- see this module's own docstring and the brief's
    C5-addendum for why an absolute figure is the wrong assertion here.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    worktree_root = str(repo)

    # Warm the cache exactly as a real commit already does earlier in
    # `_handler` (`_resolve_committing_session_id` -> ... -> `release_
    # committed_claims`/`release_phantom_claims`, both keyed on the same
    # `cwd=worktree_root` string) -- this is what a real invocation
    # guarantees by the time this chunk's own block runs.
    session_core.sessions_dir(worktree_root)

    with mock.patch("subprocess.run", side_effect=AssertionError(
        "ledger write call sequence must not spawn git"
    )), mock.patch("subprocess.Popen", side_effect=AssertionError(
        "ledger write call sequence must not spawn git"
    )), mock.patch("asyncio.create_subprocess_exec", side_effect=AssertionError(
        "ledger write call sequence must not spawn git"
    )):
        kind, weight_basis = scoped_git_commit._ledger_kind_and_weight(
            worktree_root, ["tasks/feature/todo.md"]
        )
        handoff_id, _degraded = _resolve_owner_mod.resolve_owner_handoff_id(
            "no-such-session-id", Path(worktree_root)
        )
        # Standalone (no held claims) is the expected outcome for a made-up
        # session id -- confirms the call completed without ever reaching
        # for a subprocess, not that it resolved a real baton.
        assert handoff_id is None

        ok = _store_mod.append_entry(
            "hnd-spawn-negative-test",
            "deadbeefcafefeed",
            kind,
            weight_basis=weight_basis,
            cwd=worktree_root,
        )
        assert ok is True
