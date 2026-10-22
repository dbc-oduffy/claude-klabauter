"""A second close that stands down onto another close's completion entry
refuses loudly, the entry is keyed on the closing plan, and every dropped
`handoff_dispositions` entry reports why."""

from __future__ import annotations

import subprocess
from types import ModuleType

import pytest

from coordinator_core.ops import coordinator_complete_entry as cce
from coordinator_core.workstream_complete import apply as ws_apply
from coordinator_core.workstream_complete import directives_commit_tail as tail

_LOE = "loe:\n  agent_dispatches: null\n  opus_dispatches: null\n  em_tokens: null\n  tshirt: null"
_PROSE = "Shipped the thing. It matters because of reasons."


def _run(monkeypatch, tmp_path, entry, decisions):
    mod = ModuleType("fake_entry")
    mod.main = lambda argv: print(str(entry)) or 0
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda name: mod)
    monkeypatch.setattr(ws_apply, "resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(ws_apply, "compute_repo_identity_gate", lambda *a, **k: {"verdict": "OK"})
    monkeypatch.setattr(ws_apply.directives_review, "record_gate_verdict_if_passed", lambda *a, **k: None)
    directive = {
        "id": "d-complete-entry",
        "cli": "coordinator-complete-entry",
        "args": [],
        "depends_on": None,
        "already_satisfied": False,
    }
    return ws_apply._execute_directives([directive], [], decisions)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_second_close_with_different_title_is_a_collision(monkeypatch, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    entry = tmp_path / "entry.md"
    cce._write_entry(str(entry), "sid-123456", "", "chain-x", False, _LOE, "", "2026-10-13", "")
    base = {"prose": _PROSE, "nature": "roadmap"}

    _code, first = _run(monkeypatch, tmp_path, entry, {**base, "title": "Close WS1"})
    assert first["failed"] == []
    _code, same = _run(monkeypatch, tmp_path, entry, {**base, "title": "Close WS1"})
    assert same["failed"] == []

    code, second = _run(monkeypatch, tmp_path, entry, {**base, "title": "Close WS4"})
    assert second["failed"] and "collision" in second["failed"][0]["error"]
    assert "Close WS1" in second["failed"][0]["error"]
    assert code != 0


def test_entry_filename_has_no_doubled_date():
    path = cce.resolve_entry_path("/repo", "sid-abc123", "2026-10-13-run-close-ws1")
    name = path.replace("\\", "/").rsplit("/", 1)[1]
    assert name.count("-run-close-ws1-") == 1
    assert name.split("-run-close-ws1")[0].count("-") == 2  # exactly one yyyy-mm-dd prefix


def test_rekey_follows_closing_plan_path():
    args = ["--sid", "s", "--governing-plan-slug", "2026-10-13-ws1"]
    out = ws_apply._rekey_complete_entry_args(
        args, {"governing_plan_path": "docs/plans/2026-10-13-ws4.md"}
    )
    assert out == ["--sid", "s", "--governing-plan-slug", "2026-10-13-ws4"]
    assert args[3] == "2026-10-13-ws1"
    assert ws_apply._rekey_complete_entry_args(args, {}) == args
    assert ws_apply._rekey_complete_entry_args(["--sid", "s"], {"governing_plan_path": "p/x.md"}) == [
        "--sid", "s", "--governing-plan-slug", "x",
    ]


def test_every_dropped_disposition_names_its_reason(monkeypatch, tmp_path):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    for name in ("ok.md", "nosha.md", "weird.md"):
        (handoffs / name).write_text("---\n---\n", encoding="utf-8")
    monkeypatch.setattr(
        tail, "_held_handoff_basenames", lambda *_a, **_k: ["ok.md", "nosha.md", "weird.md", "gone.md"]
    )
    decisions = {
        "handoff_dispositions": {
            "ok.md": {"disposition": "shipped", "shipped_in": "abc123"},
            "notobj.md": "shipped",
            "unheld.md": {"disposition": "shipped", "shipped_in": "abc"},
            "gone.md": {"disposition": "shipped", "shipped_in": "abc"},
            "nosha.md": {"disposition": "shipped"},
            "weird.md": {"disposition": "frobnicated"},
        }
    }
    diags = tail.dropped_disposition_diagnostics(tmp_path, "sid", decisions)
    joined = "\n".join(diags)
    assert "ok.md" not in joined
    assert "notobj.md] dropped: entry is not an object" in joined
    assert "unheld.md] dropped: no handoff claim held" in joined
    assert "gone.md] dropped: not active under state/handoffs" in joined
    assert "nosha.md] dropped: disposition shipped carries no shipped_in" in joined
    assert "weird.md] dropped: unrecognized disposition" in joined
    assert len(diags) == 5

    skipped = tail.dropped_disposition_diagnostics(
        tmp_path, "sid", decisions, skipped_reason="close commit not attempted (missing decisions.subject)"
    )
    assert len(skipped) == 6 and all("missing decisions.subject" in d for d in skipped)


def _handoff_claimed_by(tmp_path, name, claimed_by):
    d = tmp_path / "state" / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"---\nclaimed_by: {claimed_by}\n---\n", encoding="utf-8")


def test_frontmatter_claimed_by_closing_session_is_held_no_dropped_diagnostic(monkeypatch, tmp_path):
    _handoff_claimed_by(tmp_path, "mine.md", "sid")
    monkeypatch.setattr(tail, "_held_handoff_basenames", lambda *_a, **_k: [])
    decisions = {
        "handoff_dispositions": {"mine.md": {"disposition": "closed", "closed_reason": "displaced"}}
    }
    assert tail.resolve_close_stamp_candidates(tmp_path, "sid", decisions) == [
        ("state/handoffs/mine.md", "closed", "displaced")
    ]
    assert tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions) == []
    assert tail.dropped_disposition_diagnostics(tmp_path, "sid", decisions) == []


def test_frontmatter_claimed_by_other_session_is_not_held(monkeypatch, tmp_path):
    _handoff_claimed_by(tmp_path, "theirs.md", "other-sid")
    monkeypatch.setattr(tail, "_held_handoff_basenames", lambda *_a, **_k: [])
    decisions = {
        "handoff_dispositions": {"theirs.md": {"disposition": "shipped", "shipped_in": "abc"}}
    }
    assert tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions) == []
    assert tail.resolve_close_stamp_candidates(tmp_path, "sid", decisions) == []
    diags = tail.dropped_disposition_diagnostics(tmp_path, "sid", decisions)
    assert len(diags) == 1 and "no handoff claim held" in diags[0]
