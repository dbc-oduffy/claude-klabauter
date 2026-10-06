"""A memo-shaped row resolves through memo_wire at emit time, and its rendered
fence admits the files `memo.send` writes without any receiver path entering
`writes` or the terminal-commit pathspec (AC9-AC11)."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
    CrossRepoWriteError,
)
from coordinator_core.ops.dispatch_emit.emit import _row_return_contract, emit_script
from .conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.memo_row import (
    MemoRowReceiverError,
    check_memo_rows,
    memo_fence_clause,
)
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

TOPIC = "topic"
SURFACE = f".coordinator-local/memo-outbox/{TOPIC}.md"


def _register(tmp_path: Path, monkeypatch, repos: dict) -> None:
    machine_local = tmp_path / "claude-home" / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    lines = [f'"repos.{key}" = "{path.as_posix()}"' for key, path in repos.items()]
    (machine_local / "registry.local.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    sender = tmp_path / "sender-repo"
    sender.mkdir()
    receiver = tmp_path / "receiver-repo"
    (receiver / ".git").mkdir(parents=True)
    _register(tmp_path, monkeypatch, {"receiver_repo": receiver})
    return sender, receiver


def _stage(sender: Path, *, to: str, from_id: str = "sender-em") -> None:
    outbox = sender / ".coordinator-local" / "memo-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"{TOPIC}.md").write_text(
        f'---\ntitle: "A memo"\nfrom: "{from_id}"\nto: "{to}"\nstatus: draft\n---\n\nBody.\n',
        encoding="utf-8",
    )


def _row(row_id="C1", surface=SURFACE, writes=None) -> WaveRow:
    return WaveRow(
        id=row_id,
        title=row_id,
        surface=surface,
        writes=[surface] if writes is None else writes,
        reads=[],
        depends_on=[],
    )


def _plan(sender: Path, *, writes: list) -> Path:
    listed = "".join(f"    - {w}\n" for w in writes)
    plan = sender / "plan.md"
    plan.write_text(
        "---\n---\n\n# Plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: Send the memo\n  change_kind: script-edit\n"
        f"  surface: {SURFACE}\n  writes:\n{listed}```\n",
        encoding="utf-8",
    )
    return plan


def test_staged_draft_renders_inbox_pattern_and_sender_paths(world):
    sender, receiver = world
    _stage(sender, to="receiver-repo-em")
    row = _row()
    deliveries = check_memo_rows([row], sender)
    assert set(deliveries) == {"C1"}
    text = _row_return_contract(row, "plan.md", memo_deliveries=deliveries)
    inbox = (receiver / "cross-repo" / "inbox").as_posix()
    assert f"{inbox}/<YYYY-MM-DD>-sender-em-{TOPIC}.md" in text
    assert "\\" not in deliveries["C1"].inbox_dir
    for sender_path in deliveries["C1"].sender_paths:
        assert sender_path in text
    assert "only `memo.send`" in text


def test_unstaged_draft_renders_the_unresolved_form(world):
    sender, _ = world
    row = _row()
    deliveries = check_memo_rows([row], sender)
    delivery = deliveries["C1"]
    assert delivery.inbox_dir is None and delivery.filename_pattern is None
    clause = memo_fence_clause(delivery)
    assert "the receiver inbox the draft's `to:` names" in clause
    assert "only `memo.send`" in clause


def test_non_memo_row_is_absent_and_renders_unchanged(world):
    sender, _ = world
    row = _row(surface="pkg/other.py", writes=["pkg/other.py"])
    deliveries = check_memo_rows([row], sender)
    assert deliveries == {}
    assert _row_return_contract(row, "plan.md", memo_deliveries=deliveries) == (
        _row_return_contract(row, "plan.md")
    )


def test_no_repo_root_is_noop():
    assert check_memo_rows([_row()], None) == {}


def test_each_distinct_to_resolves_once(world, monkeypatch):
    sender, _ = world
    _stage(sender, to="receiver-repo-em")
    from coordinator_core.ops.dispatch_emit import memo_row

    calls = []
    real = memo_row.resolve_receiver
    monkeypatch.setattr(
        memo_row, "resolve_receiver", lambda name: (calls.append(name), real(name))[1]
    )
    check_memo_rows([_row("C1"), _row("C2")], sender)
    assert calls == ["receiver-repo-em"]


def test_unregistered_receiver_raises_naming_the_row(world):
    sender, _ = world
    _stage(sender, to="no-such-receiver-em")
    with pytest.raises(MemoRowReceiverError) as excinfo:
        check_memo_rows([_row("C7")], sender)
    message = str(excinfo.value)
    assert "C7" in message and TOPIC in message and "UNKNOWN RECEIVER" in message


@pytest.mark.pending_fix(
    reason="9be8c5c0cd routes memo-outbox rows to an EM step, so the emit-time fence is unreachable; see bug-backlog row on the emit-time memo fence"
)
def test_emit_script_raises_for_an_unregistered_receiver(world):
    sender, _ = world
    _stage(sender, to="no-such-receiver-em")
    plan = _plan(sender, writes=[SURFACE])
    with pytest.raises(MemoRowReceiverError) as excinfo:
        emit_script(str(plan), repo_root=sender)
    assert "C1" in str(excinfo.value)


def test_publish_mirror_receiver_raises(tmp_path, monkeypatch):
    sender = tmp_path / "sender-repo"
    sender.mkdir()
    mirror = tmp_path / "mirror-repo"
    (mirror / ".git").mkdir(parents=True)
    _register(tmp_path, monkeypatch, {"mirror_repo": mirror})
    local = tmp_path / "claude-home" / ".coordinator-claude-settings" / "machine-local"
    with (local / "registry.local.toml").open("a", encoding="utf-8") as fh:
        fh.write(f'"publish.mirrors.mirror_repo.path" = "{mirror.as_posix()}"\n')
    _stage(sender, to="mirror-repo-em")
    with pytest.raises(MemoRowReceiverError):
        check_memo_rows([_row()], sender)


@pytest.mark.pending_fix(
    reason="9be8c5c0cd routes memo-outbox rows to an EM step, so the emit-time fence is unreachable; see bug-backlog row on the emit-time memo fence"
)
def test_terminal_commit_pathspec_and_writes_carry_no_receiver_path(world):
    sender, receiver = world
    _stage(sender, to="receiver-repo-em")
    plan = _plan(sender, writes=[SURFACE])
    script = emit_script(str(plan), repo_root=sender, **REVIEW_KW)

    request = parse_marker(script)
    assert request is not None
    pathspec = [p for chunk in request.chunks for p in (*chunk.paths, *chunk.prefixes)]
    assert not any(receiver.as_posix() in p or "cross-repo/inbox" in p for p in pathspec)

    row = _row()
    assert not any("cross-repo/inbox" in str(w) for w in row.writes)
    assert "<YYYY-MM-DD>-sender-em-topic.md" in script


@pytest.mark.pending_fix(
    reason="9be8c5c0cd routes memo-outbox rows to an EM step, so the emit-time fence is unreachable; see bug-backlog row on the emit-time memo fence"
)
def test_cross_repo_write_still_refuses_for_a_memo_shaped_row(world):
    sender, receiver = world
    _stage(sender, to="receiver-repo-em")
    plan = _plan(sender, writes=[SURFACE, "receiver-repo/cross-repo/inbox/x.md"])
    with pytest.raises(CrossRepoWriteError):
        emit_script(str(plan), repo_root=sender)


def test_undeclared_writes_row_still_gets_the_clause(world):
    sender, _ = world
    _stage(sender, to="receiver-repo-em")
    row = _row(writes=UNDECLARED)
    deliveries = check_memo_rows([row], sender)
    assert "only `memo.send`" in _row_return_contract(
        row, "plan.md", memo_deliveries=deliveries
    )
