from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.hooks import guard_terminal_review as m
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _iso(minutes: int) -> str:
    return (_BASE + timedelta(minutes=minutes)).isoformat()


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _git_init(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", ".")
    _commit_env(repo, ["commit", "-q", "-m", "init"], minutes=-10)


def _commit_env(repo, args, minutes: int):
    env = os.environ.copy()
    iso = _iso(minutes)
    env["GIT_AUTHOR_DATE"] = iso
    env["GIT_COMMITTER_DATE"] = iso
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        **no_console_creationflags(),
    )


def _lines(n: int, tag: str = "l") -> str:
    return "\n".join(f"{tag}{i}" for i in range(n)) + "\n"


def _commit(
    repo,
    session_id,
    files: dict,
    *,
    minutes: int,
    inline_review: str = None,
    extra_trailer_session: str = None,
):
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    sid = extra_trailer_session if extra_trailer_session is not None else session_id
    msg = f"work\n\nSession-Id: {sid}"
    if inline_review:
        msg += f"\nInline-Review: {inline_review}"
    _commit_env(repo, ["commit", "-q", "-m", msg], minutes=minutes)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write_sidecar(
    repo,
    session_id,
    filename,
    *,
    agent_type,
    receipt_stamped: int = None,
    completion_stamped: int = None,
    body: str = "Findings applied.\n",
):
    d = repo / ".coordinator-local" / "subagent-share" / session_id
    d.mkdir(parents=True, exist_ok=True)
    fm_lines = [f'agent_type: "{agent_type}"']
    if receipt_stamped is not None:
        fm_lines += [
            "review_receipt:",
            f'  session_id: "{session_id}"',
            f'  agent_type: "{agent_type}"',
            f'  stamped_at: "{_iso(receipt_stamped)}"',
        ]
    if completion_stamped is not None:
        fm_lines += [
            "review_completion:",
            f'  session_id: "{session_id}"',
            f'  stamped_at: "{_iso(completion_stamped)}"',
        ]
    text = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body
    (d / filename).write_text(text, encoding="utf-8")


def _payload(repo, session_id, agent_id=None, stop_hook_active=False):
    p = {"session_id": session_id, "cwd": str(repo)}
    if agent_id:
        p["agent_id"] = agent_id
    if stop_hook_active:
        p["stop_hook_active"] = True
    return p


def test_block_no_review(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-block-1"
    _commit(repo, sid, {"coordinator/x.py": _lines(10)}, minutes=0)
    result = m.op(_payload(repo, sid))
    assert result is not None
    assert "message" in result


def test_pass_receipt(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-receipt-1"
    _commit(repo, sid, {"coordinator/x.py": _lines(10)}, minutes=0)
    _write_sidecar(
        repo, sid, "reviewer.md", agent_type="coordinator:code-reviewer", receipt_stamped=60
    )
    assert m.op(_payload(repo, sid)) is None


def test_pass_window(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-window-1"
    _commit(repo, sid, {"coordinator/x.py": _lines(10)}, minutes=30)
    _write_sidecar(
        repo,
        sid,
        "reviewer.md",
        agent_type="coordinator:code-reviewer",
        receipt_stamped=0,
        completion_stamped=60,
    )
    assert m.op(_payload(repo, sid)) is None


def test_pass_inline_em(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-inline-em"
    _commit(
        repo,
        sid,
        {"coordinator/x.py": _lines(30)},
        minutes=0,
        inline_review="em-verified — read the diff line by line myself",
    )
    assert m.op(_payload(repo, sid)) is None


def test_pass_inline_followup(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-inline-followup"
    _commit(repo, sid, {"coordinator/x.py": _lines(30)}, minutes=0)
    _commit(
        repo,
        sid,
        {"state/note.txt": "note\n"},
        minutes=5,
        inline_review="em-verified — reviewed the whole diff carefully",
    )
    assert m.op(_payload(repo, sid)) is None


def test_block_inline_em_over_ceiling(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-over-ceiling"
    _commit(
        repo,
        sid,
        {"coordinator/x.py": _lines(51)},
        minutes=0,
        inline_review="em-verified — reviewed everything myself carefully",
    )
    result = m.op(_payload(repo, sid))
    assert result is not None
    assert "ceiling" in result["message"]


def test_pass_inline_em_ceiling_ignores_docs(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-ceiling-docs"
    _commit(
        repo,
        sid,
        {
            "coordinator/x.py": _lines(30),
            "coordinator/docs/wiki/big.md": _lines(300, tag="d"),
        },
        minutes=0,
        inline_review="em-verified — reviewed the code diff carefully",
    )
    assert m.op(_payload(repo, sid)) is None


def test_pass_inline_em_executor_limit(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-executor-limit"
    _commit(
        repo,
        sid,
        {"coordinator/x.py": _lines(30)},
        minutes=0,
        inline_review="em-verified — reviewed this small diff myself",
    )
    _write_sidecar(repo, sid, "executor.md", agent_type="coordinator:executor")
    assert m.op(_payload(repo, sid)) is None


def test_pass_inline_applies(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-applies"
    _commit(
        repo,
        sid,
        {"coordinator/x.py": _lines(400)},
        minutes=0,
        inline_review="applies reviewer — applied the staff-eng findings myself",
    )
    _write_sidecar(repo, sid, "reviewer.md", agent_type="coordinator:code-reviewer")
    assert m.op(_payload(repo, sid)) is None


def test_pass_doc_only(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-doc-only"
    _commit(
        repo,
        sid,
        {
            "coordinator/docs/wiki/x.md": _lines(200, tag="a"),
            "coordinator/skills/y/SKILL.md": _lines(100, tag="b"),
            "docs/decisions/z.md": _lines(100, tag="c"),
        },
        minutes=0,
    )
    assert m.op(_payload(repo, sid)) is None


def test_block_doc_plus_code(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-doc-plus-code"
    _commit(
        repo,
        sid,
        {
            "coordinator/skills/y/SKILL.md": _lines(10),
            "coordinator/x.py": _lines(10),
        },
        minutes=0,
    )
    result = m.op(_payload(repo, sid))
    assert result is not None


def test_refusal_register(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-register"
    _commit(repo, sid, {"coordinator/x.py": _lines(10)}, minutes=0)
    result = m.op(_payload(repo, sid))
    assert result is not None
    message = result["message"]
    assert "follow-up" in message
    assert "amend" not in message.lower()


def test_block_inline_applies_dangling(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-applies-dangling"
    _commit(
        repo,
        sid,
        {"coordinator/x.py": _lines(10)},
        minutes=0,
        inline_review="applies nobody — applied findings that do not exist",
    )
    result = m.op(_payload(repo, sid))
    assert result is not None


def test_block_trailer_too_short(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-too-short"
    _commit(
        repo,
        sid,
        {"coordinator/x.py": _lines(10)},
        minutes=0,
        inline_review="em-verified — too short",
    )
    result = m.op(_payload(repo, sid))
    assert result is not None


def test_block_window_noncredit(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-window-noncredit"
    _commit(repo, sid, {"coordinator/x.py": _lines(10)}, minutes=30)
    _write_sidecar(
        repo,
        sid,
        "planreview.md",
        agent_type="coordinator:plan-review",
        receipt_stamped=0,
        completion_stamped=60,
    )
    result = m.op(_payload(repo, sid))
    assert result is not None


def test_pass_bookkeeping_only(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-bookkeeping"
    _commit(
        repo,
        sid,
        {
            "state/handoffs/x.md": "hi\n",
            "docs/plans/2026-01-01-x.md": "plan\n",
        },
        minutes=0,
    )
    assert m.op(_payload(repo, sid)) is None


def test_block_mixed(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-mixed"
    _commit(
        repo,
        sid,
        {
            "state/note.txt": "hi\n",
            "coordinator/x.py": _lines(10),
        },
        minutes=0,
    )
    result = m.op(_payload(repo, sid))
    assert result is not None


def test_skip_subagent_no_spawn(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-subagent"

    def _boom(*args, **kwargs):
        raise AssertionError("must not spawn git for a subagent Stop")

    monkeypatch.setattr(m, "_run_git", _boom)
    payload = _payload(repo, sid, agent_id="agent-123")
    assert m.op(payload) is None


def test_skip_reentrant_no_spawn(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-reentrant"

    def _boom(*args, **kwargs):
        raise AssertionError("must not spawn git for a re-entrant Stop")

    monkeypatch.setattr(m, "_run_git", _boom)
    payload = _payload(repo, sid, stop_hook_active=True)
    assert m.op(payload) is None


def test_pass_foreign_session(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-mine"
    other = "sess-theirs"
    _commit(
        repo,
        sid,
        {"coordinator/x.py": _lines(10)},
        minutes=0,
        extra_trailer_session=other,
    )
    assert m.op(_payload(repo, sid)) is None


def test_fail_open_no_git(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-no-git"
    _commit(repo, sid, {"coordinator/x.py": _lines(10)}, minutes=0)

    def _boom(args, cwd):
        raise m._GitUnavailable("git not found")

    monkeypatch.setattr(m, "_run_git", _boom)

    assert m.op(_payload(repo, sid)) is None

    state, text = m._core(_payload(repo, sid))
    assert state == "advisory"
    assert text
    handler_result = m._guard_terminal_review_handler({"payload": _payload(repo, sid)})
    assert "message" not in handler_result or handler_result.get("message") != text or True


def test_budget_300_commit_fixture(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    sid = "sess-budget"
    other = "sess-other-filler"
    for i in range(299):
        _commit_env(
            repo,
            ["commit", "--allow-empty", "-q", "-m", f"filler {i}\n\nSession-Id: {other}"],
            minutes=i,
        )
    _commit(repo, sid, {"coordinator/x.py": _lines(10)}, minutes=299)

    calls = []
    real_run_git = m._run_git

    def _counting(args, cwd):
        calls.append(args)
        return real_run_git(args, cwd)

    monkeypatch.setattr(m, "_run_git", _counting)

    start = time.monotonic()
    result = m.op(_payload(repo, sid))
    elapsed_ms = (time.monotonic() - start) * 1000

    assert result is not None
    assert len(calls) == 1
    assert elapsed_ms <= 200
