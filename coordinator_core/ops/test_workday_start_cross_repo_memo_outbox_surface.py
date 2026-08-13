"""
Tests for coordinator_core.ops.workday_start_cross_repo_memo_outbox_surface.

Port of: workday-start-cross-repo-memo-outbox-surface.sh (coordinator-claude b5a4192c,
2026-07-20). Cases mirror the coordinator-claude bash test suite (test-outbox-stale-nudge.sh,
Coordinator-claude 894d4bc6, 2026-07-22) plus additional negative and edge cases exercised
during the port (see the golden-oracle snapshot captured for this port).
"""

from __future__ import annotations

import io
import os
import time
from contextlib import redirect_stdout

import pytest

from coordinator_core.ops.workday_start_cross_repo_memo_outbox_surface import main


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
