"""dispatch.ship_hand_landed_batons ships a hand-landed run's claimed batons, driven through dispatch_message."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.ops.dispatch_emit import ask_contract, run_close, run_close_hand_landed as hl
from coordinator_core.ops.tests.test_handoff_correct_body import (
    _AUTHOR_SESSION,
    _make_git_repo,
    _seed_claimed_handoff,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.complete

RUN = "warp-hand-landed-run-20261127T000000-aaaaaa"
BODY = (
    "\n## What this covers\n\n"
    "A realistically sized section standing in for the prose ahead of the ledger block, so the "
    "ledger append's disambiguation window stays far under the body-size ratio bound.\n\n"
    "## Session Ledger\n\n"
    "<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary> -->\n"
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _AUTHOR_SESSION)
    root = _make_git_repo(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: root / ".git")
    monkeypatch.setattr(hl, "main_worktree_root", lambda p: root)
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)
    return root


def _head(repo: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags()
    )
    return out.stdout.strip()


def _baton(repo: Path, name: str) -> str:
    extra = (
        "deployment_state: in_flight\n"
        f"claimed_by: {_AUTHOR_SESSION}\n"
        "created: 2026-07-05\n"
        "branch: main\n"
        "predecessor: none\n"
        "category: infra\n"
        "summary: fixture baton"
    )
    _seed_claimed_handoff(repo, name, body=BODY, extra_fm=extra)
    return f"state/handoffs/{name}"


def _close_record(repo: Path, batons: list) -> Path:
    rel = run_close.write_close_job(
        repo, RUN, ["baton-ship"], datetime.now().astimezone() + timedelta(minutes=10), claimed_batons=batons
    )
    return repo / rel


def _call(repo: Path, params: dict) -> dict:
    msg = {
        "jsonrpc": "2.0", "id": 1, "method": hl.OP_NAME, "params": params, "_origin_worktree": str(repo),
    }
    response = asyncio.run(ipc.dispatch_message(msg))
    assert "result" in response, response
    return response["result"]


def test_ships_each_claimed_baton_at_the_sha_with_one_row_and_settles_the_close_record(repo: Path) -> None:
    batons = [_baton(repo, "a.md"), _baton(repo, "b.md")]
    record = _close_record(repo, batons)
    sha = _head(repo)

    reply = _call(repo, {"run_id": RUN, "sha": sha})

    assert reply["ok"] is True and reply["status"] == "ran", reply
    assert reply["batons"] == batons and reply["recorded"] is True
    marker = f"run {RUN} shipped in {sha[:8]}"
    for rel in batons:
        text = (repo / rel).read_text(encoding="utf-8")
        # trap: the stamp quotes shipped_in only when the sha8 would not read back as a string (e.g. all digits).
        assert "deployment_state: shipped" in text
        assert f"shipped_in: '{sha[:8]}'" in text or f"shipped_in: {sha[:8]}\n" in text
        assert text.count(marker) == 1
    assert run_close._load(repo, RUN)["settled"]["baton-ship"]["status"] == "ran"
    assert record.is_file()


def test_rerun_is_discharged_and_writes_nothing(repo: Path) -> None:
    batons = [_baton(repo, "a.md")]
    record = _close_record(repo, batons)
    sha = _head(repo)
    assert _call(repo, {"run_id": RUN, "sha": sha})["status"] == "ran"
    baton_before = (repo / batons[0]).read_bytes()
    record_before = record.read_bytes()

    again = _call(repo, {"run_id": RUN, "sha": sha})

    assert again["ok"] is True and again["status"] == "discharged", again
    assert again["recorded"] is False
    assert (repo / batons[0]).read_bytes() == baton_before
    assert record.read_bytes() == record_before


def test_run_with_no_close_record_ships_its_own_baton(repo: Path) -> None:
    rel = _baton(repo, "own.md")
    ask_contract.write_run_baton(repo, RUN, rel)

    reply = _call(repo, {"run_id": RUN, "sha": _head(repo)})

    assert reply["ok"] is True and reply["status"] == "ran" and reply["batons"] == [rel], reply
    assert reply["recorded"] is False
    assert "deployment_state: shipped" in (repo / rel).read_text(encoding="utf-8")


def test_run_claiming_no_baton_is_not_applicable(repo: Path) -> None:
    ask_contract.record_dir(repo, RUN).mkdir(parents=True)

    reply = _call(repo, {"run_id": RUN, "sha": _head(repo)})

    assert reply["ok"] is True and reply["status"] == "not-applicable" and reply["batons"] == [], reply


def test_unknown_run_is_a_typed_refusal(repo: Path) -> None:
    reply = _call(repo, {"run_id": "warp-no-such-run-20261127T000000-bbbbbb", "sha": _head(repo)})

    assert reply["ok"] is False and "unknown run" in reply["error"]


@pytest.mark.parametrize("sha", ["abc1234", "", "z" * 40])
def test_a_malformed_sha_is_refused_and_the_baton_is_untouched(repo: Path, sha: str) -> None:
    rel = _baton(repo, "a.md")
    _close_record(repo, [rel])
    before = (repo / rel).read_bytes()

    reply = _call(repo, {"run_id": RUN, "sha": sha})

    assert reply["ok"] is False and "40-hex" in reply["error"]
    assert (repo / rel).read_bytes() == before


def test_a_sha_that_is_not_an_ancestor_of_head_is_refused_and_the_baton_is_untouched(repo: Path) -> None:
    rel = _baton(repo, "a.md")
    _close_record(repo, [rel])
    before = (repo / rel).read_bytes()

    reply = _call(repo, {"run_id": RUN, "sha": "0123456789abcdef0123456789abcdef01234567"})

    assert reply["ok"] is False and "commit" in reply["error"]
    assert (repo / rel).read_bytes() == before
    assert "settled" not in run_close._load(repo, RUN) or "baton-ship" not in run_close._load(repo, RUN)["settled"]
