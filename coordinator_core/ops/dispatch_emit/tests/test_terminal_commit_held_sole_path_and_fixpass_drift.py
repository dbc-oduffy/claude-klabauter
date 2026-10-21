"""dispatch.terminal_commit never stages a path only a held chunk declares, by any admission arm, and
declares a review fix-pass agent's recorded write at its own bytes so it does not read as blob-drift."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit import review_edits  # noqa: I001 -- after terminal_commit: import order breaks a cycle
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.session import core as session_core
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
AGENT = "fixpass-agent-1"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_PLAN = (
    "---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: H1\n  title: held\n  change_kind: code-edit\n  surface: contract/held.json\n"
    "  writes:\n    - contract/held.json\n  disposition: open\n  deferred: false\n```\n"
)


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _session_sink(repo: Path) -> Path:
    return touch_record.sink_path(repo / ".git" / "coordinator-sessions" / SELF)


def _run(repo: Path, request: CommitRequest, **params) -> dict:
    from coordinator_core.ops.dispatch_emit import run_token

    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    token = run_token.mint(repo, "run-held", "run.mjs")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SELF, "run_id": "run-held", "run_token": token, **params},
        repo_root=repo / ".git",
    )


def _committed(repo: Path) -> list:
    out = _git(["show", "--numstat", "--format=", "HEAD"], repo)
    return sorted(ln.split("\t")[-1] for ln in out.splitlines() if ln.strip())


def _held_setup(repo: Path, extra_marker_paths=()) -> CommitRequest:
    (repo / "docs").mkdir()
    (repo / "docs" / "plan.md").write_text(_PLAN, encoding="utf-8", newline="\n")
    (repo / "contract").mkdir()
    (repo / "contract" / "held.json").write_text("{}\n", encoding="utf-8")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    return CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py", *extra_marker_paths)),),
        plan_path="docs/plan.md",
    )


def test_a_held_sole_path_the_session_wrote_is_not_staged(repo):
    request = _held_setup(repo)
    touch_record.append_event(
        _session_sink(repo), session_id=SELF, agent_id=None, verb="T", path="contract/held.json",
        content_hash=_sha("{}\n"), kind="w",
    )
    out = _run(repo, request, held=[{"chunk": "H1", "reason": "halted"}], plan_path="docs/plan.md")
    assert out["committed"] is True, out
    assert "contract/held.json" not in _committed(repo)
    assert "a.py" in _committed(repo)


def test_a_held_sole_path_a_review_agent_wrote_is_not_staged(repo, monkeypatch):
    request = _held_setup(repo)
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {"contract/held.json": AGENT})
    out = _run(repo, request, held=[{"chunk": "H1", "reason": "halted"}], plan_path="docs/plan.md")
    assert out["committed"] is True, out
    assert "contract/held.json" not in _committed(repo)
    assert "a.py" in _committed(repo)


def test_a_held_chunk_with_a_disjoint_landed_chunk_commits_only_the_landed_paths(repo):
    _held_setup(repo)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),
                ChunkCommit(id="C2", title="t2", paths=("b.py",))),
        plan_path="docs/plan.md",
    )
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    out = _run(repo, request, held=[{"chunk": "H1", "reason": "halted"}], plan_path="docs/plan.md")
    assert out["committed"] is True, out
    assert _committed(repo) == ["a.py", "b.py"]


def _agent_write(repo: Path, rel: str, text: str, at: float) -> None:
    hub = repo / ".git" / "coordinator-sessions"
    (hub / SELF).mkdir(parents=True, exist_ok=True)
    (hub / SELF / "dispatched-agents.txt").write_text(f"{AGENT}\thost\tcoordinator:code-reviewer\t1\n", encoding="utf-8")
    agent_dir = hub / ".agents" / AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    touch_record.append_event(
        touch_record.sink_path(agent_dir), session_id=SELF, agent_id=AGENT, verb="T", path=rel,
        content_hash=_sha(text), kind="w", timestamp=at,
    )


def test_review_agent_write_events_name_the_agents_own_bytes(repo):
    now = time.time()
    _agent_write(repo, "src/x.ts", "fixed\n", now)
    events = terminal_commit._review_write_events(repo, {"src/x.ts": AGENT, "gone.ts": "nobody"})
    assert events["src/x.ts"] == (_sha("fixed\n"), pytest.approx(now))
    assert "gone.ts" not in events


def test_a_fixpass_write_is_declared_at_the_agents_hash(repo):
    (repo / "src").mkdir()
    target = repo / "src" / "x.ts"
    target.write_text("fixed\n", encoding="utf-8")
    base = time.time() - 100
    touch_record.append_event(
        _session_sink(repo), session_id=SELF, agent_id=None, verb="T", path="src/x.ts",
        content_hash=_sha("original\n"), kind="w", timestamp=base,
    )
    agent = {"src/x.ts": (_sha("fixed\n"), base + 50)}
    expected = terminal_commit._recorded_fingerprints(
        repo, SELF, ["src/x.ts"], ["src/x.ts"], [], excluded=set(), review_events=agent,
    )
    assert expected == {"src/x.ts": _sha("fixed\n")}


def test_a_foreign_edit_after_the_fixpass_keeps_the_agents_hash_so_it_refuses(repo):
    (repo / "src").mkdir()
    (repo / "src" / "x.ts").write_text("foreign\n", encoding="utf-8")
    base = time.time() - 100
    touch_record.append_event(
        _session_sink(repo), session_id=SELF, agent_id=None, verb="T", path="src/x.ts",
        content_hash=_sha("original\n"), kind="w", timestamp=base,
    )
    agent = {"src/x.ts": (_sha("fixed\n"), base + 50)}
    expected = terminal_commit._recorded_fingerprints(
        repo, SELF, ["src/x.ts"], ["src/x.ts"], [], excluded=set(), review_events=agent,
    )
    assert expected["src/x.ts"] == _sha("fixed\n") != _sha("foreign\n")


def test_a_newer_session_write_outranks_an_older_fixpass_event(repo):
    (repo / "src").mkdir()
    (repo / "src" / "x.ts").write_text("later\n", encoding="utf-8")
    base = time.time() - 100
    touch_record.append_event(
        _session_sink(repo), session_id=SELF, agent_id=None, verb="T", path="src/x.ts",
        content_hash=_sha("later\n"), kind="w", timestamp=base + 80,
    )
    agent = {"src/x.ts": (_sha("fixed\n"), base + 50)}
    expected = terminal_commit._recorded_fingerprints(
        repo, SELF, ["src/x.ts"], ["src/x.ts"], [], excluded=set(), review_events=agent,
    )
    assert expected == {"src/x.ts": _sha("later\n")}


def test_the_fixpass_write_lands_end_to_end_without_blob_drift(repo, monkeypatch):
    (repo / "src").mkdir()
    target = repo / "src" / "x.ts"
    target.write_text("original\n", encoding="utf-8")
    base = time.time() - 100
    touch_record.append_event(
        _session_sink(repo), session_id=SELF, agent_id=None, verb="T", path="src/x.ts",
        content_hash=_sha("original\n"), kind="w", timestamp=base,
    )
    target.write_text("fixed\n", encoding="utf-8")
    os.utime(target, (base + 50, base + 50))
    _agent_write(repo, "src/x.ts", "fixed\n", base + 50)
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {"src/x.ts": AGENT})
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/x.ts",)),))
    out = _run(repo, request)
    assert out["committed"] is True, out
    assert _committed(repo) == ["src/x.ts"]


def test_an_unrecorded_outside_edit_still_refuses_as_blob_drift(repo, monkeypatch):
    (repo / "src").mkdir()
    target = repo / "src" / "x.ts"
    target.write_text("original\n", encoding="utf-8")
    base = time.time() - 100
    touch_record.append_event(
        _session_sink(repo), session_id=SELF, agent_id=None, verb="T", path="src/x.ts",
        content_hash=_sha("original\n"), kind="w", timestamp=base,
    )
    target.write_text("foreign\n", encoding="utf-8")
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {})
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/x.ts",)),))
    out = _run(repo, request)
    assert out["committed"] is False, out
    assert out.get("refused") == "blob-drift", out
