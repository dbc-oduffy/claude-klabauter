"""
coordinator_core.quick_wrap_assemble.tests.test_dispatch_sizing_ship —
`_ship_landed_dispatch_sizings` (§ C1,
docs/plans/2026-09-26-silent-engine-bookkeeping.md).

Exercises the candidate rule (clauses 1-5) and the fail-open/refusal posture
directly against `_ship_landed_dispatch_sizings`, over a real `tmp_path` git
fixture repo — the property under test (the sizing file's on-disk `status`
after the call, and the new commit's Session-Id trailer) is git's own
behaviour, which no fixture stands in for. Mirrors
`coordinator_core/ops/tests/test_sizing_ship.py`'s own fixture shape.

Run (from repo root):
    python3 -m pytest coordinator_core/quick_wrap_assemble/tests/test_dispatch_sizing_ship.py -q
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted
from coordinator_core.quick_wrap_assemble import (
    _SIZING_SHIP_FAILED_JP_ID,
    _ship_landed_dispatch_sizings,
    brief,
)

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

#: `apply_missing_trailers`'s `session_id_override` is only honoured when it is
#: UUID-shaped (`commit_trailers._UUID_RE`) — a non-UUID override silently falls
#: through to real environment/session resolution instead, which would make this
#: fixture's assertions depend on whatever session id this test happens to run
#: under. A UUID keeps the override deterministic.
_SESSION_ID = "abcdefab-cdef-abcd-efab-cdefabcdefab"

_OK_OUTCOME = {"status": "ok", "committed_paths": [], "conflicted_paths": []}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
        timeout=15,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _sizing_body(*, status: str = "routed", plan: str | None = None) -> str:
    lines = [
        "schema: sizing-object",
        "intent: Test intent, verbatim.",
        "estimate:",
        "  tshirt: XS",
        "  provisional: true",
        "route: dispatch",
        f"plan: {plan if plan is not None else 'null'}",
        "detents: []",
        "fork: null",
        "xl_exit: null",
        f"status: {status}",
        "premise:",
        "  provenance: read",
        "  evidence: test fixture, no real premise verified",
    ]
    return "\n".join(lines) + "\n"


def _seed_sizing(
    repo: Path, name: str, *, status: str = "routed", plan: str | None = None, commit: bool = True
) -> Path:
    path = repo / "state" / "sizings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_sizing_body(status=status, plan=plan), encoding="utf-8")
    if commit:
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "seed sizing")
    return path


def _seed_landed_work(repo: Path, rel_path: str = "src/x.py") -> None:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", rel_path)
    _git(repo, "commit", "-m", "land work")


def _status_at_head(repo: Path, rel_path: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{rel_path}"],
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
        timeout=15,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )
    return read_fm_field_unquoted(proc.stdout, "status")


def _head_message(repo: Path) -> str:
    proc = _git(repo, "log", "-1", "--format=%B")
    return proc.stdout


# ---------------------------------------------------------------------------
# ship
# ---------------------------------------------------------------------------


def test_ship_committed_dispatch_sizing_with_landed_work(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "a.yaml", status="routed")
    _seed_landed_work(repo)

    points = _ship_landed_dispatch_sizings(
        repo,
        ["state/sizings/a.yaml", "src/x.py"],
        _OK_OUTCOME,
        sid=_SESSION_ID,
    )

    assert points == []
    assert _status_at_head(repo, "state/sizings/a.yaml") == "shipped"
    assert f"Session-Id: {_SESSION_ID}" in _head_message(repo)


# ---------------------------------------------------------------------------
# skip cases
# ---------------------------------------------------------------------------


def test_skip_plan_fk_present(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "a.yaml", status="routed", plan='"docs/plans/p.md"')
    _seed_landed_work(repo)
    before = (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8")

    points = _ship_landed_dispatch_sizings(
        repo, ["state/sizings/a.yaml", "src/x.py"], _OK_OUTCOME, sid=_SESSION_ID
    )

    assert points == []
    assert (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8") == before


def test_skip_draft_status(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "a.yaml", status="draft")
    _seed_landed_work(repo)
    before = (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8")

    points = _ship_landed_dispatch_sizings(
        repo, ["state/sizings/a.yaml", "src/x.py"], _OK_OUTCOME, sid=_SESSION_ID
    )

    assert points == []
    assert (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8") == before


def test_skip_bookkeeping_only_paths(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "a.yaml", status="routed")
    before = (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8")

    points = _ship_landed_dispatch_sizings(
        repo,
        ["state/sizings/a.yaml", "archive/handoffs/x.md"],
        _OK_OUTCOME,
        sid=_SESSION_ID,
    )

    assert points == []
    assert (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8") == before


def test_skip_planning_artifact_only_paths(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "a.yaml", status="routed")
    before = (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8")

    points = _ship_landed_dispatch_sizings(
        repo,
        ["state/sizings/a.yaml", "docs/plans/p.md"],
        _OK_OUTCOME,
        sid=_SESSION_ID,
    )

    assert points == []
    assert (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8") == before


def test_skip_sizing_not_in_session_path_set(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "a.yaml", status="routed")
    _seed_landed_work(repo)
    before = (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8")

    points = _ship_landed_dispatch_sizings(
        repo, ["src/x.py"], _OK_OUTCOME, sid=_SESSION_ID
    )

    assert points == []
    assert (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8") == before


def test_skip_close_commit_error(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "a.yaml", status="routed")
    _seed_landed_work(repo)
    before = (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8")

    points = _ship_landed_dispatch_sizings(
        repo,
        ["state/sizings/a.yaml", "src/x.py"],
        {"status": "error", "committed_paths": [], "conflicted_paths": []},
        sid=_SESSION_ID,
    )

    assert points == []
    assert (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8") == before


def test_skip_close_commit_conflicted(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "a.yaml", status="routed")
    _seed_landed_work(repo)
    before = (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8")

    points = _ship_landed_dispatch_sizings(
        repo,
        ["state/sizings/a.yaml", "src/x.py"],
        {"status": "ok", "committed_paths": [], "conflicted_paths": ["src/x.py"]},
        sid=_SESSION_ID,
    )

    assert points == []
    assert (repo / "state" / "sizings" / "a.yaml").read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# refuse / fail-open
# ---------------------------------------------------------------------------


def test_refuse_declined_sizing_raises_judgment_point(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "a.yaml", status="declined")
    _seed_landed_work(repo)

    points = _ship_landed_dispatch_sizings(
        repo, ["state/sizings/a.yaml", "src/x.py"], _OK_OUTCOME, sid=_SESSION_ID
    )

    assert len(points) == 1
    assert points[0]["id"] == _SIZING_SHIP_FAILED_JP_ID
    assert "state/sizings/a.yaml" in points[0]["evidence"]


def test_fail_open_on_handler_exception(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "a.yaml", status="routed")
    _seed_landed_work(repo)

    import coordinator_core.quick_wrap_assemble as qwa

    def _boom(params, repo_root=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(qwa, "_sizing_ship_handler", _boom)

    points = qwa._ship_landed_dispatch_sizings(
        repo, ["state/sizings/a.yaml", "src/x.py"], _OK_OUTCOME, sid=_SESSION_ID
    )

    assert len(points) == 1
    assert points[0]["id"] == _SIZING_SHIP_FAILED_JP_ID
    assert "boom" in points[0]["evidence"]


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_idempotent_second_close_adds_no_commit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "a.yaml", status="shipped")
    _seed_landed_work(repo)
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    points = _ship_landed_dispatch_sizings(
        repo, ["state/sizings/a.yaml", "src/x.py"], _OK_OUTCOME, sid=_SESSION_ID
    )

    head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert points == []
    assert head_after == head_before


# ---------------------------------------------------------------------------
# read-only
# ---------------------------------------------------------------------------


def test_brief_bare_never_ships(tmp_path, monkeypatch):
    """`brief()` called bare (`commit=False`, the default) never attempts the
    ship step at all — even when a shippable candidate exists on disk. This
    is a control on the `commit` gate itself, so it monkeypatches
    `_ship_landed_dispatch_sizings` to a sentinel that would fail the test
    loudly if called, rather than re-exercising the full close-gate fact
    plumbing that a real `brief(commit=False)` call still needs (session id,
    every `session_facts` reader) to reach the point where this gate is
    checked.
    """
    import coordinator_core.quick_wrap_assemble as qwa

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("_ship_landed_dispatch_sizings must not run under commit=False")

    monkeypatch.setattr(qwa, "_ship_landed_dispatch_sizings", _must_not_be_called)

    # A degraded-everything session still produces a valid (if degraded) envelope
    # from `brief(commit=False)` without ever reaching a real git worktree or
    # session-fact reader — every fact call below is monkeypatched to a
    # uniformly degraded record, which is enough to reach the gate this test
    # actually checks (the `if commit:` guard) without asserting on envelope
    # shape elsewhere.
    class _Degraded(dict):
        pass

    def _degraded_record(*args, **kwargs):
        return {"degraded": True, "evidence": "test stub", "value": None}

    monkeypatch.setattr(qwa.session_facts, "session_pickup_kind", _degraded_record)
    monkeypatch.setattr(qwa.session_facts, "session_governing_plan", _degraded_record)
    monkeypatch.setattr(qwa.session_facts, "session_diff_brightline", _degraded_record)
    monkeypatch.setattr(qwa.session_facts, "session_terminal_sizings", _degraded_record)
    monkeypatch.setattr(qwa.session_facts, "session_fold_sidecars", _degraded_record)
    monkeypatch.setattr(qwa, "_print_commits_into_baton", lambda *a, **k: None)

    repo = tmp_path / "repo"
    _init_repo(repo)

    envelope = brief(worktree_root=repo, commit=False)

    assert envelope["gates"]["commit_outcome"]["status"] == "skipped"
