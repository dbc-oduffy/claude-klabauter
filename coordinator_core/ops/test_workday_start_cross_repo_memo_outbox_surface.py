"""
Tests for coordinator_core.ops.workday_start_cross_repo_memo_outbox_surface.

Port of: workday-start-cross-repo-memo-outbox-surface.sh (DoE b5a4192c,
2026-07-20). Cases mirror the DoE bash test suite (test-outbox-stale-nudge.sh,
DoE 894d4bc6, 2026-07-22) plus additional negative and edge cases exercised
during the port (see the golden-oracle snapshot captured for this port).
"""

from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.fleet import memo_send as memo_send_module
from coordinator_core.ops.fleet._memo_anchor import ANCHOR_REF_PREFIX, write_anchor
from coordinator_core.ops.workday_start_cross_repo_memo_outbox_surface import main
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# Gone-delivery fixtures — mirrors coordinator_core/ops/fleet/tests/
# test_memo_send.py's own git-repo/registry factories (same pattern, kept
# local rather than imported to avoid a cross-test-file coupling for what is
# a handful of lines).
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args), cwd=str(repo), capture_output=True, check=check,
        **no_console_creationflags(),
    )


def _make_sender_git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "sender-repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "init")
    return root


def _make_receiver_git_repo(tmp_path: Path, name: str = "receiver-repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@claude-klabauter.test")
    _git(root, "config", "user.name", "ClaudeKlabauterTest")
    _git(root, "config", "commit.gpgsign", "false")
    inbox = root / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init receiver")
    return root


def _make_claude_home(tmp_path: Path, receiver_repos: dict) -> Path:
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    lines = []
    for key_suffix, repo_path in receiver_repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return claude_home


def _write_ledger_rows(sender_repo: Path, rows: list) -> Path:
    ledger_path = memo_send_module._sent_ledger_path(sender_repo)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return ledger_path


def _receiver_head_sha(receiver_repo: Path) -> str:
    return _git(receiver_repo, "rev-parse", "HEAD").stdout.decode().strip()


def _write_draft(path, to=None, title=None, body="body", extra_frontmatter=""):
    lines = ["---"]
    if to is not None:
        lines.append(f'to: "{to}"')
    if title is not None:
        lines.append(f'title: "{title}"')
    if extra_frontmatter:
        lines.append(extra_frontmatter)
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines))


def _backdate(path, age_seconds):
    target = time.time() - age_seconds
    os.utime(path, (target, target))


def _run(argv, env_overrides=None, monkeypatch=None):
    if monkeypatch is not None:
        for k, v in (env_overrides or {}).items():
            monkeypatch.setenv(k, v)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, buf.getvalue()


def test_empty_outbox_silent(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    rc, out = _run([], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch)
    assert rc == 0
    assert out == ""


def test_absent_outbox_dir_silent(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    rc, out = _run([], {"COORDINATOR_OUTBOX_DIR": str(missing)}, monkeypatch)
    assert rc == 0
    assert out == ""


def test_stale_draft_emits_nudge(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    f = outbox / "test-topic.md"
    _write_draft(f, to="example-retrieval-repo-em", title="Shape memo for example-retrieval-repo")
    _backdate(f, 30 * 3600)

    rc, out = _run([], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch)
    assert rc == 0
    assert "Outbox draft test-topic staged 30h ago → example-retrieval-repo-em  :: Shape memo for example-retrieval-repo" in out
    assert "  → send | compose | discard" in out


def test_fresh_draft_silent(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    f = outbox / "fresh-topic.md"
    _write_draft(f, to="example-game-repo-em", title="Fresh memo")
    _backdate(f, 3600)

    rc, out = _run([], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch)
    assert rc == 0
    assert out == ""


def test_missing_frontmatter_falls_back_to_unknown_untitled(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    f = outbox / "topic-f.md"
    f.write_text("no frontmatter here\n")
    _backdate(f, 30 * 3600)

    rc, out = _run([], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch)
    assert rc == 0
    assert "Outbox draft topic-f staged 30h ago → unknown  :: untitled" in out


def test_custom_stale_hours_env(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    f = outbox / "topic-g.md"
    _write_draft(f, to="x", title="y")
    _backdate(f, 2 * 3600)

    rc, out = _run(
        [],
        {"COORDINATOR_OUTBOX_DIR": str(outbox), "COORDINATOR_OUTBOX_STALE_HOURS": "1"},
        monkeypatch,
    )
    assert rc == 0
    assert "Outbox draft topic-g staged 2h ago → x  :: y" in out


def test_non_md_file_ignored(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "notme.txt").write_text("x\n")

    rc, out = _run([], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch)
    assert rc == 0
    assert out == ""


def test_repo_root_arg_bypasses_state_root_seam(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OUTBOX_DIR", raising=False)
    repo_root = tmp_path / "repo"
    outbox = repo_root / "state" / "memo-outbox"
    outbox.mkdir(parents=True)
    f = outbox / "topic-i.md"
    _write_draft(f, to="z", title="w")
    _backdate(f, 30 * 3600)

    rc, out = _run([str(repo_root)], {}, monkeypatch)
    assert rc == 0
    assert "Outbox draft topic-i staged 30h ago → z  :: w" in out


def test_single_and_double_quotes_stripped(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    f = outbox / "stale1.md"
    f.write_text("---\nto: 'A'\ntitle: 'Title One'\n---\n")
    _backdate(f, 48 * 3600)

    rc, out = _run([], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch)
    assert rc == 0
    assert "Outbox draft stale1 staged 48h ago → A  :: Title One" in out


def test_mixed_stale_and_fresh_only_stale_emitted(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    stale = outbox / "stale1.md"
    stale.write_text("---\nto: 'A'\ntitle: 'Title One'\n---\n")
    _backdate(stale, 48 * 3600)

    fresh = outbox / "fresh1.md"
    fresh.write_text("---\nto: 'B'\ntitle: 'Title Two'\n---\n")

    rc, out = _run([], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch)
    assert rc == 0
    assert "stale1" in out
    assert "fresh1" not in out


def test_nongit_no_override_no_arg_silent(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OUTBOX_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    rc, out = _run([], {}, monkeypatch)
    assert rc == 0
    assert out == ""


# ---------------------------------------------------------------------------
# C5 sibling: gone-delivery sweep (2026-09-11 fix 1 — the sweep gets a
# trigger). Same `main()`/`_run` harness; `COORDINATOR_OUTBOX_DIR` points at
# an empty dir throughout so only the gone-delivery leg is under test.
# ---------------------------------------------------------------------------

def test_no_deliveries_at_all_silent(tmp_path, monkeypatch):
    sender_repo = _make_sender_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    outbox = tmp_path / "empty-outbox"
    outbox.mkdir()

    rc, out = _run(
        [str(sender_repo)], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch,
    )
    assert rc == 0
    assert out == ""


def test_verified_delivery_silent_nothing_gone(tmp_path, monkeypatch):
    """A ledger row whose commit is still a real object in the receiver must
    produce no line — the surfacer is silent when nothing is gone, never
    '0 gone deliveries'."""
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    outbox = tmp_path / "empty-outbox"
    outbox.mkdir()

    real_sha = _receiver_head_sha(receiver_repo)
    _write_ledger_rows(sender_repo, [{
        "sent_at": "2026-09-09T13:00:00Z",
        "to": "example-retrieval-repo-em",
        "topic": "landed-one",
        "delivery_commit_sha": real_sha,
        "delivery_branch": "refs/heads/main",
    }])

    rc, out = _run(
        [str(sender_repo)], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch,
    )
    assert rc == 0
    assert out == ""


def test_gone_delivery_emits_one_nudge_naming_topic_receiver_and_resend(
    tmp_path, monkeypatch
):
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    outbox = tmp_path / "empty-outbox"
    outbox.mkdir()

    gone_sha = "a" * 40
    _write_ledger_rows(sender_repo, [{
        "sent_at": "2026-09-09T13:00:00Z",
        "to": "example-retrieval-repo-em",
        "topic": "lost-one",
        "delivery_commit_sha": gone_sha,
        "delivery_branch": "refs/heads/main",
    }])

    rc, out = _run(
        [str(sender_repo)], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch,
    )
    assert rc == 0
    assert "lost-one" in out
    assert "example-retrieval-repo-em" in out
    assert gone_sha in out
    assert "refs/heads/main" in out  # names the ref it landed on
    assert "memo.send" in out  # the way through: re-run memo.send


def test_gone_delivery_with_delivery_branch_names_the_ref(tmp_path, monkeypatch):
    """delivery_branch is the reader this field was added for: the nudge
    names WHERE the delivery landed, distinguishing 'the branch we landed on
    was rewritten' from 'the object vanished outright'."""
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    outbox = tmp_path / "empty-outbox"
    outbox.mkdir()

    gone_sha = "c" * 40
    _write_ledger_rows(sender_repo, [{
        "sent_at": "2026-09-09T13:00:00Z",
        "to": "example-retrieval-repo-em",
        "topic": "lost-with-branch",
        "delivery_commit_sha": gone_sha,
        "delivery_branch": "refs/heads/feature/rewritten",
    }])

    rc, out = _run(
        [str(sender_repo)], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch,
    )
    assert rc == 0
    assert "refs/heads/feature/rewritten" in out


def test_gone_delivery_without_delivery_branch_names_no_ref_and_no_none(
    tmp_path, monkeypatch
):
    """A ledger row written before delivery_branch existed must read as
    UNKNOWN, never imply a mismatch or print the literal 'None'."""
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    outbox = tmp_path / "empty-outbox"
    outbox.mkdir()

    gone_sha = "d" * 40
    _write_ledger_rows(sender_repo, [{
        "sent_at": "2026-09-09T13:00:00Z",
        "to": "example-retrieval-repo-em",
        "topic": "lost-no-branch",
        "delivery_commit_sha": gone_sha,
        # no delivery_branch key — pre-field ledger row
    }])

    rc, out = _run(
        [str(sender_repo)], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch,
    )
    assert rc == 0
    assert "lost-no-branch" in out
    assert "None" not in out
    assert "landed on" not in out  # no ref clause at all when unrecorded


def test_not_checkable_delivery_emits_no_line(tmp_path, monkeypatch):
    """An unregistered receiver is UNCHECKABLE, never news at workday
    start — a peer repo not on this machine says nothing about delivery, and
    a nudge that cries wolf on that verdict recreates the defect this
    closes."""
    sender_repo = _make_sender_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {})  # no receivers registered
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    outbox = tmp_path / "empty-outbox"
    outbox.mkdir()

    _write_ledger_rows(sender_repo, [{
        "sent_at": "2026-09-09T13:00:00Z",
        "to": "some-unregistered-em",
        "topic": "unreachable-peer",
        "delivery_commit_sha": "b" * 40,
        "delivery_branch": "refs/heads/main",
    }])

    rc, out = _run(
        [str(sender_repo)], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch,
    )
    assert rc == 0
    assert out == ""


# ---------------------------------------------------------------------------
# C7 sibling: the restorable-delivery nudge, alongside gone. A fresh
# restorable row is silent (the anchor's ordinary window); one still
# restorable a day later is news.
# ---------------------------------------------------------------------------

def test_fresh_restorable_delivery_is_silent(tmp_path, monkeypatch):
    """A restorable row sent moments ago is the ordinary in-flight state
    between 'anchor written' and 'receiver's next workday-start restores
    it' — surfacing it immediately would nudge on every send."""
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    outbox = tmp_path / "empty-outbox"
    outbox.mkdir()

    filename = "2026-09-11-someone-fresh-restorable.md"
    sha = "5" * 40
    common_dir = resolve_git_common_dir(receiver_repo)
    write_anchor(common_dir, filename, sha, b"memo bytes")

    now = datetime.datetime.now(datetime.timezone.utc)
    _write_ledger_rows(sender_repo, [{
        "sent_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": "example-retrieval-repo-em",
        "topic": "fresh-restorable",
        "delivery_commit_sha": sha,
        "delivered_to": f"cross-repo/inbox/{filename}",
        "anchor_ref": ANCHOR_REF_PREFIX + filename + "/" + sha,
    }])

    rc, out = _run(
        [str(sender_repo)], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch,
    )
    assert rc == 0
    assert out == ""


def test_stale_restorable_delivery_emits_nudge(tmp_path, monkeypatch):
    """A restorable row still outstanding after 1 day is news at workday
    start, alongside gone — and its nudge names an action the SENDER can
    take (ping the receiver to run a workday-start), never a re-send
    suggestion: the memo is anchored, so re-sending fixes nothing."""
    sender_repo = _make_sender_git_repo(tmp_path)
    receiver_repo = _make_receiver_git_repo(tmp_path)
    claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": receiver_repo})
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    outbox = tmp_path / "empty-outbox"
    outbox.mkdir()

    filename = "2026-09-11-someone-stale-restorable.md"
    sha = "6" * 40
    common_dir = resolve_git_common_dir(receiver_repo)
    write_anchor(common_dir, filename, sha, b"memo bytes")

    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
    _write_ledger_rows(sender_repo, [{
        "sent_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": "example-retrieval-repo-em",
        "topic": "stale-restorable",
        "delivery_commit_sha": sha,
        "delivered_to": f"cross-repo/inbox/{filename}",
        "anchor_ref": ANCHOR_REF_PREFIX + filename + "/" + sha,
    }])

    rc, out = _run(
        [str(sender_repo)], {"COORDINATOR_OUTBOX_DIR": str(outbox)}, monkeypatch,
    )
    assert rc == 0
    assert "stale-restorable" in out
    assert "restorable" in out
    assert "workday-start" in out
    assert "ping example-retrieval-repo-em" in out
    assert "re-run memo.send" not in out
