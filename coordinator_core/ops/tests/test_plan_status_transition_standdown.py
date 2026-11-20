"""Characterization tests for the stand-down verbs (stamp-landed,
stamp-deferred) of coordinator_core.ops.plan_status_transition: the honest
exits from `executing` for a session that ends with work still open.

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
# executing -> landed / deferred.
# ---------------------------------------------------------------------------

def _fm(p: Path):
    split = split_frontmatter(p.read_text(encoding="utf-8"))
    assert split is not None
    return split.fm_text


def test_stamp_landed_from_executing(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "---\ntitle: T\nstatus: executing\nowner: x\n---\n\nBody.\n")
    t0 = time.process_time()
    rc = main(["stamp-landed", "--plan", str(p), "--reason", "C6 undisposed"])
    elapsed_ms = (time.process_time() - t0) * 1000
    assert rc == 0
    assert elapsed_ms < 200, f"stamp-landed process_time {elapsed_ms:.1f}ms over the 200ms bar"
    assert 'status "executing" → landed' in capsys.readouterr().out
    assert p.read_text(encoding="utf-8") == (
        "---\ntitle: T\nstatus: landed\nstatus_reason: C6 undisposed\nowner: x\n---\n\nBody.\n"
    )


@pytest.mark.parametrize("status", ["executing", "landed"])
def test_stamp_deferred_from_legal_sources(tmp_path, status):
    p = _write(tmp_path, "p.md", f"---\ntitle: T\nstatus: {status}\n---\n\nBody.\n")
    rc = main(["stamp-deferred", "--plan", str(p), "--reason", "pending other workstream"])
    assert rc == 0
    fm = _fm(p)
    assert read_fm_field_unquoted(fm, "status") == "deferred"
    assert read_fm_field_unquoted(fm, "status_reason") == "pending other workstream"


def test_stamp_landed_replaces_existing_status_reason(tmp_path):
    p = _write(
        tmp_path, "p.md",
        "---\ntitle: T\nstatus: executing\nstatus_reason: old\n---\n\nBody.\n",
    )
    assert main(["stamp-landed", "--plan", str(p), "--reason", "new"]) == 0
    assert read_fm_field_unquoted(_fm(p), "status_reason") == "new"
    assert p.read_text(encoding="utf-8").count("status_reason") == 1


@pytest.mark.parametrize(
    "verb,status",
    [
        ("stamp-landed", s)
        for s in ["draft", "reviewed", "approved", "blocked", "landed", "implemented",
                  "superseded", "abandoned", "deferred"]
    ]
    + [
        ("stamp-deferred", s)
        for s in ["approved", "blocked", "implemented",
                  "superseded", "abandoned", "deferred"]
    ]
    + [
        ("stamp-unstaled", s)
        for s in ["draft", "reviewed", "approved", "executing", "landed", "implemented",
                  "abandoned"]
    ],
)
def test_standdown_refuses_other_sources_untouched(tmp_path, capsys, verb, status):
    original = f"---\ntitle: T\nstatus: {status}\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    rc = main([verb, "--plan", str(p), "--reason", "r"])
    assert rc == 1
    assert f'status "{status}"' in capsys.readouterr().err
    assert p.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("verb", ["stamp-landed", "stamp-deferred"])
@pytest.mark.parametrize("reason_args", [[], ["--reason", ""], ["--reason", "   "]])
def test_standdown_requires_nonblank_reason(tmp_path, capsys, verb, reason_args):
    original = "---\ntitle: T\nstatus: executing\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    assert main([verb, "--plan", str(p), *reason_args]) == 1
    assert "requires --reason" in capsys.readouterr().err
    assert p.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("verb", ["stamp-landed", "stamp-deferred"])
@pytest.mark.parametrize(
    "flag", ["--by", "--override-reason", "--findings"]
)
def test_standdown_rejects_foreign_flags(tmp_path, capsys, verb, flag):
    original = "---\ntitle: T\nstatus: executing\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    assert main([verb, "--plan", str(p), "--reason", "r", flag, "x"]) == 1
    assert f"does not accept {flag}" in capsys.readouterr().err
    assert p.read_text(encoding="utf-8") == original


_STALE = "stale: no progress 30+ days, unverified; re-opens on pickup"


@pytest.mark.parametrize("status", ["draft", "reviewed"])
def test_stale_deferral_roundtrips_to_prior_status(tmp_path, status):
    p = _write(tmp_path, "p.md", f"---\ntitle: T\nstatus: {status}\nowner: x\n---\n\nBody.\n")
    assert main(["stamp-deferred", "--plan", str(p), "--reason", _STALE]) == 0
    fm = _fm(p)
    assert read_fm_field_unquoted(fm, "status") == "deferred"
    assert read_fm_field_unquoted(fm, "status_reason") == f"{_STALE} [from: {status}]"
    assert main(["stamp-unstaled", "--plan", str(p)]) == 0
    assert p.read_text(encoding="utf-8") == (
        f"---\ntitle: T\nstatus: {status}\nowner: x\n---\n\nBody.\n"
    )


def test_deferral_from_executing_records_no_marker_and_is_not_reopenable(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "---\ntitle: T\nstatus: executing\n---\n\nBody.\n")
    assert main(["stamp-deferred", "--plan", str(p), "--reason", _STALE]) == 0
    assert read_fm_field_unquoted(_fm(p), "status_reason") == _STALE
    before = p.read_text(encoding="utf-8")
    assert main(["stamp-unstaled", "--plan", str(p)]) == 1
    assert "not as a stale deferral" in capsys.readouterr().err
    assert p.read_text(encoding="utf-8") == before


def test_non_stale_draft_deferral_is_not_reopenable(tmp_path, capsys):
    p = _write(tmp_path, "p.md", "---\ntitle: T\nstatus: draft\n---\n\nBody.\n")
    assert main(["stamp-deferred", "--plan", str(p), "--reason", "paused for X"]) == 0
    before = p.read_text(encoding="utf-8")
    assert main(["stamp-unstaled", "--plan", str(p)]) == 1
    assert "not as a stale deferral" in capsys.readouterr().err
    assert p.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("status", ["draft", "reviewed"])
def test_stale_deferral_accepts_no_commit(tmp_path, status):
    p = _write(tmp_path, "p.md", f"---\ntitle: T\nstatus: {status}\n---\n\nBody.\n")
    assert main(["stamp-deferred", "--plan", str(p), "--reason", _STALE, "--no-commit"]) == 0
    assert read_fm_field_unquoted(_fm(p), "status") == "deferred"
    assert main(["stamp-unstaled", "--plan", str(p), "--no-commit"]) == 0
    assert read_fm_field_unquoted(_fm(p), "status") == status


def test_stamp_deferred_from_draft_still_requires_reason(tmp_path, capsys):
    original = "---\ntitle: T\nstatus: draft\n---\n\nBody.\n"
    p = _write(tmp_path, "p.md", original)
    assert main(["stamp-deferred", "--plan", str(p), "--reason", " "]) == 1
    assert "requires --reason" in capsys.readouterr().err
    assert p.read_text(encoding="utf-8") == original


def test_standdown_commits_the_flip(tmp_path):
    p = _write(tmp_path, "p.md", "---\ntitle: T\nstatus: executing\n---\n\nBody.\n")
    assert main(["stamp-landed", "--plan", str(p), "--reason", "r"]) == 0
    env = {**os.environ, **_GIT_ENV_KEYS}
    log = subprocess.run(
        ["git", "log", "-2", "--format=%B"], cwd=str(tmp_path), capture_output=True,
        text=True, env=env, timeout=15, **no_console_creationflags(),
    ).stdout
    assert '"executing" -> landed' in log
