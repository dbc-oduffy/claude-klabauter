"""Tests for `coordinator_core.ops.baton_carry_forward`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import baton_carry_forward as CF
from coordinator_core.session_baton import store
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs


def _make_repo(tmp_path):
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@example.com"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path,
                   **no_console_passthrough_kwargs())
    return tmp_path


def _session(repo: Path, sid: str) -> Path:
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def test_note_lands_and_reports_where(tmp_path):
    """The return value must name the baton path: a session very often does
    not know it has one, so an op that lands a note silently has not made the
    affordance discoverable."""
    repo = _make_repo(tmp_path)
    _session(repo, "sid-cf")
    out = CF.append_note("check the retry budget before trusting the timings",
                         session_id="sid-cf", cwd=str(repo))
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["baton_path"] and out["baton_path"].endswith("baton.json")


def test_notes_accumulate_and_identical_notes_collapse(tmp_path):
    repo = _make_repo(tmp_path)
    _session(repo, "sid-acc")
    CF.append_note("first", session_id="sid-acc", cwd=str(repo))
    CF.append_note("second", session_id="sid-acc", cwd=str(repo))
    out = CF.append_note("first", session_id="sid-acc", cwd=str(repo))
    assert out["count"] == 2
    assert store.read_baton("sid-acc", str(repo))["carry_forward"] == ["first", "second"]


def test_an_earlier_note_is_never_displaced(tmp_path):
    """The hedge is worthless if a later note can cost an earlier one."""
    repo = _make_repo(tmp_path)
    _session(repo, "sid-keep")
    for n in ("a", "b", "c"):
        CF.append_note(n, session_id="sid-keep", cwd=str(repo))
    assert store.read_baton("sid-keep", str(repo))["carry_forward"] == ["a", "b", "c"]


def test_read_back_is_the_other_half_of_the_affordance(tmp_path):
    repo = _make_repo(tmp_path)
    _session(repo, "sid-read")
    CF.append_note("what I would resent re-discovering", session_id="sid-read",
                   cwd=str(repo))
    out = CF.read_notes(session_id="sid-read", cwd=str(repo))
    assert out["ok"] is True
    assert out["notes"] == ["what I would resent re-discovering"]
    assert out["baton_path"]


def test_read_back_on_a_session_with_no_notes_is_empty_not_an_error(tmp_path):
    repo = _make_repo(tmp_path)
    _session(repo, "sid-none")
    out = CF.read_notes(session_id="sid-none", cwd=str(repo))
    assert out["ok"] is True and out["notes"] == []


def test_empty_and_whitespace_notes_are_refused(tmp_path):
    repo = _make_repo(tmp_path)
    _session(repo, "sid-empty")
    for bad in ("", "   ", "\n\t"):
        out = CF.append_note(bad, session_id="sid-empty", cwd=str(repo))
        assert out["ok"] is False and "non-empty" in out["reason"]


def test_note_is_capped_and_the_refusal_names_the_alternative(tmp_path):
    """The baton is read on every UserPromptSubmit, so its size is on a hot
    path. The cap's message has to name what to do instead."""
    repo = _make_repo(tmp_path)
    _session(repo, "sid-cap")
    out = CF.append_note("x" * (CF.MAX_NOTE_CHARS + 1), session_id="sid-cap",
                         cwd=str(repo))
    assert out["ok"] is False
    assert "document" in out["reason"]


def test_note_is_stripped_before_storage(tmp_path):
    repo = _make_repo(tmp_path)
    _session(repo, "sid-strip")
    CF.append_note("  padded  ", session_id="sid-strip", cwd=str(repo))
    assert store.read_baton("sid-strip", str(repo))["carry_forward"] == ["padded"]


def test_absent_session_directory_is_fail_open_not_a_raise(tmp_path):
    """This sits on the context-pressure path. A record write that blocks its
    caller is worse than a note that did not land."""
    repo = _make_repo(tmp_path)  # no session dir minted
    out = CF.append_note("note", session_id="sid-missing", cwd=str(repo))
    assert out["ok"] is False
    assert "session directory" in out["reason"]


def test_appending_does_not_advance_the_batons_lifecycle(tmp_path):
    """A note is an addition to a live journal, never a transition of it."""
    repo = _make_repo(tmp_path)
    _session(repo, "sid-life")
    CF.append_note("note", session_id="sid-life", cwd=str(repo))
    rec = store.read_baton("sid-life", str(repo))
    assert rec["closed_at"] is None
    assert rec["closed_into"] is None
    assert rec["promoted_to"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ops_are_reachable_through_dispatch_under_their_documented_names():
    """The advisory names these op strings verbatim, so they must resolve the
    way a real caller resolves them: `ipc._lazy_import_and_lookup`, in an
    interpreter that has NOT imported this module.

    Checking `ipc._REGISTRY` from here proves nothing. This file's own
    `import baton_carry_forward` registers both ops as a side effect, so that
    form stayed green while every live `coordinator-invoke baton.carry_forward`
    returned "Method not found": the module was in neither `_registry_map` nor
    `ops/__init__`'s import list, so no dispatch path ever loaded it.
    """
    repo_root = Path(__file__).resolve().parents[3]
    probe = (
        "import sys\n"
        "from coordinator_core import ipc\n"
        "assert 'coordinator_core.ops.baton_carry_forward' not in sys.modules\n"
        "missing = [n for n in ('baton.carry_forward', 'baton.carry_forward_read')\n"
        "           if ipc._lazy_import_and_lookup(n) is None]\n"
        "print(','.join(missing))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"not reachable through dispatch: {result.stdout.strip()}"
