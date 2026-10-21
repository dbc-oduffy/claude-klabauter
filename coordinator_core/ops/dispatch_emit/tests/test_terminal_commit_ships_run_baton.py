"""terminal_commit._pay_inline_legs ships the baton the run's gate recorded, when the criterion is met."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import completion_receipt, run_close, terminal_commit
from coordinator_core.ops.dispatch_emit import run_close_completion
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir, write_run_baton
from coordinator_core.ops.tests.test_handoff_correct_body import (
    _AUTHOR_SESSION,
    _make_git_repo,
    _seed_claimed_handoff,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
RUN = "run-1"
MARKER = f"run {RUN} shipped in {SHA[:8]}"
BODY = (
    "\n## What this covers\n\n"
    "A realistically sized section standing in for the prose ahead of the ledger block, so the "
    "ledger append's disambiguation window stays far under the body-size ratio bound.\n\n"
    "## Session Ledger\n\n"
    "<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary> -->\n"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _AUTHOR_SESSION)
    monkeypatch.setattr(
        run_close_completion,
        "completion_entry_leg",
        lambda *a, **k: (run_close.LegResult("not-applicable", "stub"), []),
    )
    return _make_git_repo(tmp_path)


def _baton(repo, name="own.md"):
    extra = (
        "deployment_state: ready_to_fire\n"
        f"claimed_by: {_AUTHOR_SESSION}\n"
        "created: 2026-07-05\nbranch: main\npredecessor: none\ncategory: infra\n"
        "summary: fixture baton"
    )
    _seed_claimed_handoff(repo, name, body=BODY, extra_fm=extra)
    return f"state/handoffs/{name}"


def _record(repo, rel):
    record_dir(repo, RUN).mkdir(parents=True, exist_ok=True)
    write_run_baton(repo, RUN, rel)


def _pay(repo, monkeypatch, *, claims=(), met=True):
    monkeypatch.setattr(completion_receipt, "_handoff_claims", lambda root, sid: list(claims))
    ctx = {
        "run_id": RUN,
        "legs": {},
        "plan_rel": "docs/plans/p.md",
        "inline_review": {"criterion": {"status": "met" if met else "not_met"}},
    }
    written = terminal_commit._pay_inline_legs(repo, ctx, SHA, _AUTHOR_SESSION, None)
    return ctx, written


def test_met_ships_the_recorded_baton_with_no_session_claim(repo, monkeypatch):
    rel = _baton(repo)
    _record(repo, rel)

    ctx, written = _pay(repo, monkeypatch)

    assert ctx["legs"]["baton-ship"].status == "ran", ctx["legs"]["baton-ship"].detail
    text = (repo / rel).read_text(encoding="utf-8")
    assert "deployment_state: shipped" in text
    assert text.count(MARKER) == 1
    assert rel in written
    assert ctx["batons"] == [rel]


def test_session_claim_of_the_same_baton_ships_it_once(repo, monkeypatch):
    rel = _baton(repo)
    _record(repo, rel)

    ctx, written = _pay(repo, monkeypatch, claims=[rel])

    assert ctx["batons"] == [rel]
    assert written.count(rel) == 1
    assert (repo / rel).read_text(encoding="utf-8").count(MARKER) == 1


def test_not_met_leaves_the_recorded_baton_untouched(repo, monkeypatch):
    rel = _baton(repo)
    _record(repo, rel)
    before = (repo / rel).read_bytes()

    ctx, written = _pay(repo, monkeypatch, met=False)

    assert (repo / rel).read_bytes() == before
    assert ctx["batons"] == []
    assert ctx["legs"]["baton-ship"].status == "not-applicable"
    assert rel not in written


def test_recorded_path_outside_the_handoffs_home_is_ignored(repo, monkeypatch):
    (repo / "elsewhere.md").write_text("---\ntitle: x\n---\n", encoding="utf-8")
    _record(repo, "elsewhere.md")

    ctx, written = _pay(repo, monkeypatch)

    assert ctx["batons"] == []
    assert ctx["legs"]["baton-ship"].status == "not-applicable"
    assert (repo / "elsewhere.md").read_text(encoding="utf-8") == "---\ntitle: x\n---\n"


def test_recorded_baton_missing_on_disk_is_ignored(repo, monkeypatch):
    _record(repo, "state/handoffs/gone.md")

    ctx, _ = _pay(repo, monkeypatch)

    assert ctx["batons"] == []


def test_already_shipped_at_this_sha_is_discharged_with_no_second_row(repo, monkeypatch):
    rel = _baton(repo)
    _record(repo, rel)
    _pay(repo, monkeypatch)
    before = (repo / rel).read_bytes()

    ctx, written = _pay(repo, monkeypatch)

    assert ctx["legs"]["baton-ship"].status == "discharged"
    assert (repo / rel).read_bytes() == before
    assert (repo / rel).read_text(encoding="utf-8").count(MARKER) == 1
    assert rel not in written
