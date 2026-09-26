"""
coordinator_core.tests.test_ipc_scope_touch_self_report — regression net for the
`_scope_touch_paths` self-report contract (design (b), EM ruling 2026-08-04).

Purpose: pins `coordinator_core.ipc._record_self_reported_touches` /
`_resolve_declared_touch_root_and_path` — the opt-in mechanism a sanctioned-
mutating handler (an engine op reached via a `coordinator/bin/` CLI, never
through the PreToolUse Edit/Write hot path) uses to self-report the file(s) it
actually wrote, so `session.scope.compute_scope` can attribute them to a
session instead of stranding them as orphans (known defect:
state/improvement-queue/2026-08-03-sanctioned-mutating-clis-record-no-sessi-dedd1f017d02.yaml).

Test list (brief-pinned, numbered to match the dispatch brief's own list):
  1. test_declared_path_records_touch_for_exactly_that_path
  2. test_declared_path_lands_in_compute_offer_safe_paths  (PRIMARY — end-to-end
     proof the reported defect is fixed)
  3. test_reserved_key_never_reaches_wire_envelope
  4. test_live_peer_claim_is_not_stolen_by_a_declaration  (security-critical)
  5. test_path_outside_repo_and_absent_from_disk_are_skipped
  6. test_recording_failure_is_fail_open
  7. test_no_resolvable_session_no_claim_op_still_succeeds
  8. test_repeat_identical_declaration_still_resolves_to_one_owned_path
  9. test_handler_declaring_nothing_behaves_exactly_as_before

2026-08-04 REQUIRES_CHANGES rework (staff-eng review
coordinatorstaff-eng-48c065fd.md) — F1/F2/F3/F4 regression coverage:
  10. test_cross_repo_declaration_does_not_steal_target_repos_native_claim
      (F1, security-critical — the reviewer's own reproduced attack, closed)
  11. test_declared_directory_is_rejected_not_recorded  (F2)
  12. test_declaration_list_over_cap_is_truncated_and_logged  (F4)
  13. test_real_queue_append_write_lands_in_compute_offer_safe_paths
      (F3 — drives the REAL queue.append handler end-to-end)
  14. test_real_queue_promote_cross_repo_write_is_skipped_and_surfaced
      (F3 — drives the REAL queue.promote handler; asserts the NEW correct
      cross-repo behaviour: not recorded, surfaced as a skip)

Spec backlink: state/subagent-share/eae4f9b9-decb-4df9-b4de-1427f2dddf68/
               coordinatorexecutor-7e64f8d4.md (original dispatch brief)
Spec backlink: state/subagent-share/eae4f9b9-decb-4df9-b4de-1427f2dddf68/
               coordinatorstaff-eng-48c065fd.md (REQUIRES_CHANGES review, F1-F4)
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
import coordinator_core.ops.queue_append  # noqa: F401 -- registers queue.append
import coordinator_core.ops.queue_promote  # noqa: F401 -- registers queue.promote
from coordinator_core.ipc import dispatch_message, _REGISTRY, _SCOPE_TOUCH_PATHS_KEY
from coordinator_core.session import core, scope, liveness, touch_record
from coordinator_core.ops.session.safe_commit_offer import compute_offer
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run(coro):
    return asyncio.run(coro)


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


def _sdir(repo, sid):
    return Path(repo) / ".git" / "coordinator-sessions" / sid


def _touched_events(repo, sid):
    p = _sdir(repo, sid) / "touch-record.jsonl"
    if not p.is_file():
        return []
    raw = p.read_bytes()
    return [touch_record.decode_line(line) for line in touch_record.iter_complete_lines(raw)]


class _RegistryScope:
    """Install test handlers into _REGISTRY, restore on exit (mirrors
    test_dispatch_message.py's own fixture idiom — see that file's docstring)."""

    def __init__(self, handlers: dict) -> None:
        self._handlers = handlers
        self._saved: dict = {}

    def __enter__(self):
        for name in self._handlers:
            self._saved[name] = _REGISTRY.get(name)
        _REGISTRY.update(self._handlers)
        return self

    def __exit__(self, *_exc):
        for name, old in self._saved.items():
            if old is None:
                _REGISTRY.pop(name, None)
            else:
                _REGISTRY[name] = old


def _dispatch(method, params, origin_worktree=None, id_=1):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}
    if origin_worktree is not None:
        msg["_origin_worktree"] = str(origin_worktree)
    return _run(dispatch_message(msg))


def _handler_declaring(paths):
    def _h(params, ctx=None, repo_root=None):
        return {"ok": True, _SCOPE_TOUCH_PATHS_KEY: list(paths)}

    return _h


def _handler_declaring_nothing(params, ctx=None, repo_root=None):
    return {"ok": True}


def test_declared_path_records_touch_for_exactly_that_path(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    (repo / "written.yaml").write_text("z")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    with _RegistryScope({"test.declare": _handler_declaring([str(repo / "written.yaml")])}):
        d = _dispatch("test.declare", {}, origin_worktree=repo)

    assert "error" not in d
    events = _touched_events(repo, "sid-1")
    assert len(events) == 1
    assert (events[0].verb, events[0].path) == (touch_record.VERB_TOUCH, "written.yaml")


def test_declared_path_lands_in_compute_offer_safe_paths(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    lesson_path = repo / "state" / "lessons" / "2026-08-04-example.yaml"
    lesson_path.parent.mkdir(parents=True)
    lesson_path.write_text("z")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-2")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    with _RegistryScope({"test.declare": _handler_declaring([str(lesson_path)])}):
        d = _dispatch("test.declare", {}, origin_worktree=repo)
    assert "error" not in d

    offer = compute_offer("sid-2", cwd=str(repo))
    assert "state/lessons/2026-08-04-example.yaml" in offer["safe_paths"]
    assert "state/lessons/2026-08-04-example.yaml" not in offer["orphans"]


def test_reserved_key_never_reaches_wire_envelope(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    (repo / "written.yaml").write_text("z")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-3")

    with _RegistryScope({"test.declare": _handler_declaring([str(repo / "written.yaml")])}):
        d = _dispatch("test.declare", {}, origin_worktree=repo)

    assert _SCOPE_TOUCH_PATHS_KEY not in d["result"]
    assert _SCOPE_TOUCH_PATHS_KEY not in d
    assert d["result"] == {"ok": True}


def test_live_peer_claim_is_not_stolen_by_a_declaration(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    shared = repo / "shared.py"
    shared.write_text("z")
    core.init("peer", cwd=str(repo))
    scope.touch("peer", "shared.py", cwd=str(repo))

    monkeypatch.setattr(
        scope.liveness, "live_session_ids", lambda cwd=None: frozenset({"peer", "sid-4"})
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-4")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    with _RegistryScope({"test.declare": _handler_declaring([str(shared)])}):
        d = _dispatch("test.declare", {}, origin_worktree=repo)
    assert "error" not in d

    my_events = _touched_events(repo, "sid-4")
    assert len(my_events) == 1
    assert my_events[0].path == "shared.py"

    offer = compute_offer("sid-4", cwd=str(repo))
    assert "shared.py" not in offer["safe_paths"]
    excluded_paths = {e["path"] for e in offer["excluded"]}
    assert "shared.py" in excluded_paths


def test_path_outside_repo_and_absent_from_disk_are_skipped(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    outside_root = tmp_path.parent / "outside-repo"
    outside_root.mkdir(exist_ok=True)
    outside_file = outside_root / "not-in-repo.txt"
    outside_file.write_text("z")

    absent_file = repo / "never-written.yaml"

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-5")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    with _RegistryScope(
        {"test.declare": _handler_declaring([str(outside_file), str(absent_file)])}
    ):
        d = _dispatch("test.declare", {}, origin_worktree=repo)
    assert "error" not in d
    assert _touched_events(repo, "sid-5") == []


def test_recording_failure_is_fail_open(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    (repo / "written.yaml").write_text("z")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-6")

    def _boom(sid, path, cwd=None):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(scope, "touch", _boom)

    with _RegistryScope({"test.declare": _handler_declaring([str(repo / "written.yaml")])}):
        d = _dispatch("test.declare", {}, origin_worktree=repo)

    assert "error" not in d
    assert d["result"] == {"ok": True}


def test_no_resolvable_session_no_claim_op_still_succeeds(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    (repo / "written.yaml").write_text("z")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    with _RegistryScope({"test.declare": _handler_declaring([str(repo / "written.yaml")])}):
        d = _dispatch("test.declare", {}, origin_worktree=repo)

    assert "error" not in d
    assert d["result"] == {"ok": True}
    assert not (Path(repo) / ".git" / "coordinator-sessions").is_dir() or not any(
        (Path(repo) / ".git" / "coordinator-sessions").glob("*/touch-record.jsonl")
    )


def test_repeat_identical_declaration_still_resolves_to_one_owned_path(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    (repo / "written.yaml").write_text("z")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-8")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    with _RegistryScope({"test.declare": _handler_declaring([str(repo / "written.yaml")])}):
        _dispatch("test.declare", {}, origin_worktree=repo, id_=1)
        _dispatch("test.declare", {}, origin_worktree=repo, id_=2)

    offer = compute_offer("sid-8", cwd=str(repo))
    assert offer["safe_paths"].count("written.yaml") == 1
    assert "written.yaml" not in offer["orphans"]


def test_handler_declaring_nothing_behaves_exactly_as_before(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-9")

    with _RegistryScope({"test.declare": _handler_declaring_nothing}):
        d = _dispatch("test.declare", {}, origin_worktree=repo)

    assert "error" not in d
    assert d["result"] == {"ok": True}
    assert not (
        Path(repo) / ".git" / "coordinator-sessions" / "sid-9" / "touch-record.jsonl"
    ).is_file()


def _live_dirs_liveness(cwd=None):
    """Fake `live_session_ids`: 'live' == a session dir physically exists.

    Faithful enough to reproduce the reported defect: the old code's bug was
    that a foreign declaration MATERIALIZED a real session dir (via
    `scope.touch()`'s lazy `core.init()`) — this fake surfaces exactly that
    side effect as a liveness change, the same signal `compute_scope` Step 3
    and `resolve_session_id`'s tier-4 ambiguity guard both consume.
    """
    root = core.git_root(cwd)
    if not root:
        return frozenset()
    sessions_root = Path(root) / ".git" / "coordinator-sessions"
    if not sessions_root.is_dir():
        return frozenset()
    return frozenset(p.name for p in sessions_root.iterdir() if p.is_dir())


def test_cross_repo_declaration_does_not_steal_target_repos_native_claim(
    tmp_path, monkeypatch, caplog
):
    repo_a_dir = tmp_path / "repo-a"
    repo_b_dir = tmp_path / "repo-b"
    repo_a_dir.mkdir()
    repo_b_dir.mkdir()
    repo_a = _make_repo(repo_a_dir)
    repo_b = _make_repo(repo_b_dir)

    shared = repo_b / "shared.yaml"
    shared.write_text("z")

    core.init("sid-bnative", cwd=str(repo_b))
    scope.touch("sid-bnative", "shared.yaml", cwd=str(repo_b))

    monkeypatch.setattr(liveness, "live_session_ids", _live_dirs_liveness)

    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-bnative")
    assert core.resolve_session_id(cwd=str(repo_b)) == "sid-bnative"

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-caller")
    caplog.set_level(logging.INFO, logger="coordinator_core.ipc")
    with _RegistryScope({"test.declare": _handler_declaring([str(shared)])}):
        d = _dispatch("test.declare", {}, origin_worktree=repo_a)
    assert "error" not in d

    my_events = _touched_events(repo_b, "sid-bnative")
    assert len(my_events) == 1
    assert my_events[0].path == "shared.yaml"

    assert not (repo_b / ".git" / "coordinator-sessions" / "sid-caller").exists()

    offer = compute_offer("sid-bnative", cwd=str(repo_b))
    assert "shared.yaml" in offer["safe_paths"]
    excluded_paths = {e["path"] for e in offer["excluded"]}
    assert "shared.yaml" not in excluded_paths

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-bnative")
    assert core.resolve_session_id(cwd=str(repo_b)) == "sid-bnative"

    assert any(
        "outside the caller's own repo" in rec.message for rec in caplog.records
    )


# 11. F2 — a declared DIRECTORY is rejected, never recorded.


def test_declared_directory_is_rejected_not_recorded(tmp_path, monkeypatch, caplog):
    repo = _make_repo(tmp_path)
    subdir = repo / "state" / "lessons"
    subdir.mkdir(parents=True)
    (subdir / "a.yaml").write_text("a")
    (subdir / "b.yaml").write_text("b")

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-11")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    caplog.set_level(logging.INFO, logger="coordinator_core.ipc")

    with _RegistryScope({"test.declare": _handler_declaring([str(subdir)])}):
        d = _dispatch("test.declare", {}, origin_worktree=repo)
    assert "error" not in d

    assert _touched_events(repo, "sid-11") == []
    offer = compute_offer("sid-11", cwd=str(repo))
    assert "state/lessons" not in offer["safe_paths"]
    assert any(
        "directories are rejected" in rec.message for rec in caplog.records
    )


def test_declaration_list_over_cap_is_truncated_and_logged(tmp_path, monkeypatch, caplog):
    repo = _make_repo(tmp_path)
    n = ipc._MAX_DECLARED_TOUCH_PATHS + 4
    paths = []
    for i in range(n):
        p = repo / f"over-cap-{i}.txt"
        p.write_text(str(i))
        paths.append(str(p))

    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-12")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    caplog.set_level(logging.INFO, logger="coordinator_core.ipc")

    with _RegistryScope({"test.declare": _handler_declaring(paths)}):
        d = _dispatch("test.declare", {}, origin_worktree=repo)
    assert "error" not in d

    events = _touched_events(repo, "sid-12")
    assert len(events) == ipc._MAX_DECLARED_TOUCH_PATHS
    assert any(
        "exceeding the cap" in rec.message for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


def test_real_queue_append_write_lands_in_compute_offer_safe_paths(tmp_path, monkeypatch):
    assert "queue.append" in _REGISTRY, "import guard: queue.append not registered"
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(repo))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-13")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    params = dict(
        schema="debt-backlog",
        title="Wire-path test entry (real queue.append)",
        body="Exercises the real handler, not a synthetic one (F3).",
        source="test/test_ipc_scope_touch_self_report.py",
        risk="Low.",
        proposed_action="None required.",
        status="open",
        created="2026-08-04",
        from_repo="test-repo",
        session_id="",
    )
    d = _dispatch("queue.append", params, origin_worktree=repo)
    assert "error" not in d, d
    assert _SCOPE_TOUCH_PATHS_KEY not in d["result"]
    out_path = d["result"]["out_path"]

    rel = Path(os.path.relpath(out_path, str(repo))).as_posix()
    offer = compute_offer("sid-13", cwd=str(repo))
    assert rel in offer["safe_paths"], (rel, offer)
    assert rel not in offer["orphans"]

    sink = touch_record.sink_path(core.session_dir("sid-13", str(repo)))
    recorded = touch_record.project_live_claims(sink, cwd=str(repo)).claims
    assert recorded[rel].kind == touch_record.KIND_WRITE, recorded.get(rel)


def test_real_queue_promote_cross_repo_write_is_skipped_and_surfaced(
    tmp_path, monkeypatch, caplog
):
    assert "queue.promote" in _REGISTRY, "import guard: queue.promote not registered"
    caller_dir = tmp_path / "caller-repo"
    caller_dir.mkdir()
    repo = _make_repo(caller_dir)
    outbox = tmp_path / "doe-outbox-not-a-git-repo"
    outbox.mkdir()
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-14")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    caplog.set_level(logging.INFO, logger="coordinator_core.ipc")

    params = dict(
        title="Wire-path test lesson (real queue.promote)",
        body="Exercises the real cross-repo handler, not a synthetic one (F3).",
        change_kind="doctrine-edit",
        target_wiki="docs/wiki/wire-path-test.md",
    )
    d = _dispatch("queue.promote", params, origin_worktree=repo)
    assert "error" not in d, d
    assert _SCOPE_TOUCH_PATHS_KEY not in d["result"]
    out_path = Path(d["result"]["out_path"])

    assert out_path.is_file()

    assert _touched_events(repo, "sid-14") == []

    assert any(
        "outside the caller's own repo" in rec.message for rec in caplog.records
    )
