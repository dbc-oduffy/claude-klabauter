"""Characterization tests for the stamp-abandoned verb of
coordinator_core.ops.plan_status_transition (IBPBE-B04c, docs/plans/2026-
09-26-inbox-blitz-part-b-engine-defects.md): plan-status-transition
stamp-abandoned for a plan cancelled before execution.

Real git, like test_plan_status_transition_blocked.py — the writer-side
commit ownership and locked_rmw machinery this exercises reads actual git
state, so a mocked git would validate the fixture harness, not the verb's
behaviour.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.frontmatter import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops.plan_status_transition import main
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GIT_ENV_KEYS = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _ensure_git_repo(tmp_path: Path) -> None:
    if (tmp_path / ".git").exists():
        return
    env = {**os.environ, **_GIT_ENV_KEYS}
    subprocess.run(
        ["git", "init"], cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", ".gitkeep"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )


def _track(tmp_path: Path, p: Path) -> None:
    env = {**os.environ, **_GIT_ENV_KEYS}
    rel = p.relative_to(tmp_path)
    subprocess.run(
        ["git", "add", "--", str(rel)],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "commit", "-m", f"seed {rel}"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )


def _write(tmp_path: Path, name: str, body: str) -> Path:
    _ensure_git_repo(tmp_path)
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    _track(tmp_path, p)
    return p


# ---------------------------------------------------------------------------
# A draft abandons.
# ---------------------------------------------------------------------------

def test_stamp_abandoned_from_draft(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "---\ntitle: T\nstatus: draft\n---\n\nBody.\n")
    t0 = time.process_time()
    rc = main(["stamp-abandoned", "--plan", str(p), "--reason", "no longer needed"])
    elapsed_ms = (time.process_time() - t0) * 1000
    assert rc == 0
    assert elapsed_ms < 200, f"stamp-abandoned process_time {elapsed_ms:.1f}ms over the 200ms bar"
    out = capsys.readouterr().out
    assert 'status "draft" → abandoned' in out
    text = p.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    assert split is not None
    assert read_fm_field_unquoted(split.fm_text, "status") == "abandoned"
    assert read_fm_field_unquoted(split.fm_text, "status_reason") == "no longer needed"


@pytest.mark.parametrize("status", ["reviewed", "approved", "blocked"])
def test_stamp_abandoned_from_other_legal_sources(tmp_path, status):
    p = _write(tmp_path, "p.md", f"---\ntitle: T\nstatus: {status}\n---\n\nBody.\n")
    rc = main(["stamp-abandoned", "--plan", str(p), "--reason", "cancelled"])
    assert rc == 0
    text = p.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    assert split is not None
    assert read_fm_field_unquoted(split.fm_text, "status") == "abandoned"
    assert read_fm_field_unquoted(split.fm_text, "status_reason") == "cancelled"


def test_stamp_abandoned_inserts_status_reason_after_status_key(tmp_path):
    p = _write(tmp_path, "p.md", "---\ntitle: T\nstatus: draft\nowner: x\n---\n\nBody.\n")
    rc = main(["stamp-abandoned", "--plan", str(p), "--reason", "r"])
    assert rc == 0
    text = p.read_text(encoding="utf-8")
    assert text == (
        "---\ntitle: T\nstatus: abandoned\nstatus_reason: r\nowner: x\n---\n\nBody.\n"
    )


# ---------------------------------------------------------------------------
# An executing plan is refused.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status", ["executing", "landed", "implemented", "superseded", "abandoned", "deferred"]
)
def test_stamp_abandoned_refuses_executing_landed_implemented_and_frozen(tmp_path, capsys, status):
    original = f"---\ntitle: T\nstatus: {status}\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-abandoned", "--plan", str(p), "--reason", "r"])
    assert rc == 1
    err = capsys.readouterr().err
    assert f'status "{status}"' in err
    assert "closed_partial" in err
    assert "stamp-superseded" in err
    assert p.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# A missing status_reason is refused.
# ---------------------------------------------------------------------------

def test_stamp_abandoned_requires_reason(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: draft\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-abandoned", "--plan", str(p)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "requires --reason" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_abandoned_rejects_blank_reason(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: draft\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-abandoned", "--plan", str(p), "--reason", "   "])
    assert rc == 1
    err = capsys.readouterr().err
    assert "requires --reason" in err
    assert p.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Out-of-verb flag rejections, mirroring stamp-blocked's own idiom.
# ---------------------------------------------------------------------------

def test_stamp_abandoned_rejects_by_flag(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: draft\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-abandoned", "--plan", str(p), "--reason", "r", "--by", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not accept --by" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_abandoned_rejects_override_reason_flag(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: draft\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-abandoned", "--plan", str(p), "--reason", "r", "--override-reason", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not accept --override-reason" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_abandoned_rejects_findings_flag(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: draft\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main(["stamp-abandoned", "--plan", str(p), "--reason", "r", "--findings", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not accept --findings" in err
    assert p.read_text(encoding="utf-8") == original


def test_stamp_abandoned_missing_plan_flag(capsys):
    rc = main(["stamp-abandoned", "--reason", "r"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "requires --plan" in err


def test_unknown_verb_message_lists_stamp_abandoned(capsys):
    rc = main(["not-a-real-verb"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "stamp-abandoned" in err
