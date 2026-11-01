"""A ledger item served only by Row-Unlanded chunks reads partial, in the verdict and the note's table."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_ledger as tcl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

_SERVED = {"C1": ("R2",), "C2": ("R3",), "C3": ("R2", "R3")}
_VERDICT = {"items": [
    {"id": "R1", "status": "met"},
    {"id": "R2", "status": "met", "remaining": []},
    {"id": "R3", "status": "met"},
    {"id": "R4", "status": "unmet", "remaining": ["x"]},
]}
_UNLANDED = [("C1", "tests/test_addon_compat.py", "withheld by ask-1")]
_INLINE = {"criterion": {"status": "met"}}


def _status(verdict):
    return {i["id"]: i["status"] for i in verdict["items"]}


def test_item_served_only_by_an_unlanded_chunk_reads_partial_with_the_path():
    got = tcl.downgrade_unlanded_items(_VERDICT, _UNLANDED, {"C1": ("R2",), "C2": ("R3",)})
    assert _status(got) == {"R1": "met", "R2": "partial", "R3": "met", "R4": "unmet"}
    assert got["items"][1]["remaining"] == ["tests/test_addon_compat.py"]
    assert _VERDICT["items"][1]["status"] == "met"


def test_item_with_a_landed_server_stays_met():
    got = tcl.downgrade_unlanded_items(_VERDICT, _UNLANDED, _SERVED)
    assert _status(got)["R2"] == "met"
    assert got is _VERDICT


def test_claimed_by_counts_as_a_server():
    verdict = {"items": [{"id": "R9", "status": "met", "claimed_by": ["C1"]}]}
    got = tcl.downgrade_unlanded_items(verdict, _UNLANDED, {})
    assert got["items"][0]["status"] == "partial"


def test_no_unlanded_row_leaves_the_verdict_untouched():
    assert tcl.downgrade_unlanded_items(_VERDICT, [], _SERVED) is _VERDICT


def test_note_table_shows_partial_and_never_gets_a_second_section(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Delivery\n\nbody\n", encoding="utf-8")
    downgraded = tcl.downgrade_unlanded_items(_VERDICT, _UNLANDED, {"C1": ("R2",)})
    tcl.rewrite_note_verdicts(note, _VERDICT, _INLINE, tmp_path)
    assert "| R2 | met |" in note.read_text(encoding="utf-8")
    tcl.rewrite_note_verdicts(note, downgraded, _INLINE, tmp_path)
    text = note.read_text(encoding="utf-8")
    assert text.count("## Verdicts") == 1
    assert "| R2 | partial |" in text and "| R2 | met |" not in text
    assert "tests/test_addon_compat.py" in text
    assert text.startswith("# Delivery\n\nbody\n")


def test_rewrite_keeps_a_section_that_follows_verdicts(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# D\n\n## Verdicts\n\nold\n\n## Appendix\n\nkeep\n", encoding="utf-8")
    tcl.rewrite_note_verdicts(note, _VERDICT, _INLINE, tmp_path)
    text = note.read_text(encoding="utf-8")
    assert "old" not in text and "## Appendix\n\nkeep" in text and text.count("## Verdicts") == 1


SELF = "5e1f0000-0000-4000-8000-0000000000c3"
RUN_A = "plan-20261021T100000-cccccc"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _claim(root: Path, rel: str) -> None:
    session_scope.touch(SELF, rel, cwd=str(root))
    sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, "README.md", "seed\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _land(repo: Path, *, peer_holds_c2: bool) -> dict:
    _write(repo, "src/a.py", "a = 1\n")
    _write(repo, "src/b.py", "b = 1\n")
    for rel in ("src/a.py", "src/b.py"):
        _claim(repo, rel)
    if peer_holds_c2:
        _write(repo, "state/warp/run-b/manifest.json", json.dumps({"rows": [{"id": "X1", "writes": ["src/b.py"]}]}))
        run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    _write(repo, f"{RECORD_DIR_ROOT}/{RUN_A}/run.json", json.dumps({"run_id": RUN_A}))
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t", paths=("src/a.py",)),
            ChunkCommit(id="C2", title="t", paths=("src/b.py",)),
        ),
        plan_path=None,
    )
    _write(repo, "run-a.mjs", "// emitted\n" + render_marker(request) + "\n")
    verdict = {"items": [
        {"id": "R1", "status": "met", "claimed_by": ["C1"]},
        {"id": "R2", "status": "met", "claimed_by": ["C2"]},
    ]}
    return land_run._handler(
        {"script_path": "run-a.mjs", "incomplete_chunks": [], "ledger_verdict": verdict,
         "inline_review": {**reviewed(), "integration_stem": "s",
                           "criterion": {"status": "met", "observation": "obs", "sidecar": "j.md"}},
         "session_id": SELF, "run_id": RUN_A},
        repo_root=repo / ".git",
    )


@pytest.mark.spawns_process
def test_land_run_reply_names_the_item_an_unlanded_row_leaves_partial(repo):
    out = _land(repo, peer_holds_c2=True)

    assert out["committed"] is True, out
    assert list(out["ledger_partial"]) == ["R2"]


@pytest.mark.spawns_process
def test_land_run_with_every_row_landed_reports_no_partial_item(repo):
    out = _land(repo, peer_holds_c2=False)

    assert out["committed"] is True, out
    assert "ledger_partial" not in out
