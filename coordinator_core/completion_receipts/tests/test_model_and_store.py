"""Receipt model validation, exclusive-create store, chain heads and the batched introducing-commit read."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from coordinator_core.completion_receipts import store
from coordinator_core.completion_receipts.model import (
    mint_receipt_id,
    receipt_rel_path,
    render,
    validate,
)
from coordinator_core.completion_receipts.store import (
    ReceiptWriteError,
    current_heads,
    introducing_commits,
    read_receipts,
    write_receipt,
)

from coordinator_core.win_portability import no_console_creationflags

NOW = "2026-10-01T12:00:00Z"


def _fm(receipt_id: str = "rcp-demo-a1b2c3", **over) -> dict:
    fm = {
        "schema": "completion-receipt",
        "receipt_id": receipt_id,
        "baton_id": "bat-demo-111111",
        "deliverable_id": "dlv-demo-222222",
        "workstream_id": None,
        "repo": "claude-klabauter",
        "plan_path": "docs/plans/x.md",
        "branch": "main",
        "run": {"kind": "execute-plan", "id": "x.workflow"},
        "verdict": None,
        "judge": None,
        "commit_range": {"base": "abc", "head": None},
        "started_at": None,
        "concluded_at": NOW,
        "loe": {"estimated": "M", "actual": None},
        "prose_ref": receipt_rel_path(receipt_id, NOW),
        "supersedes": None,
        "approved_by": None,
        "provenance": {
            "observed_at": NOW,
            "derivation": "dispatch.terminal_commit",
            "ref": {"branch": "main", "sha": "abc"},
        },
    }
    fm.update(over)
    return fm


_JUDGE = {"identity": "execute-review", "observation_summary": "met", "judged_at": NOW}


def test_ids_and_paths():
    rid = mint_receipt_id("My Plan: Foo/Bar!")
    assert rid.startswith("rcp-my-plan-foo-bar-") and len(rid.rsplit("-", 1)[1]) == 6
    assert receipt_rel_path(rid, NOW) == f"state/completion-receipts/2026-10/{rid}.md"


def test_valid_null_verdict():
    assert validate(_fm()) == []


def test_valid_agent_delivered():
    assert validate(_fm(verdict="agent-delivered", judge=_JUDGE)) == []


def test_valid_human_approved():
    fm = _fm(verdict="human-approved", judge=_JUDGE, supersedes="rcp-demo-ffffff", approved_by="pm")
    assert validate(fm) == []


def test_agent_delivered_without_judge_fails():
    assert validate(_fm(verdict="agent-delivered", judge=None))


def test_human_approved_without_supersedes_fails():
    assert validate(_fm(verdict="human-approved", approved_by="pm", supersedes=None))


def test_baton_and_deliverable_both_null_fails():
    assert validate(_fm(baton_id=None, deliverable_id=None))


def test_unknown_key_fails():
    fm = copy.deepcopy(_fm())
    fm["extra"] = 1
    assert validate(fm)


def test_render_is_lf_only_and_roundtrips(tmp_path: Path):
    rel = write_receipt(tmp_path, _fm(), "## What\r\nbody")
    raw = (tmp_path / rel).read_bytes()
    assert b"\r" not in raw
    [r] = read_receipts(tmp_path)
    assert r["receipt_id"] == "rcp-demo-a1b2c3" and r["_path"] == rel
    assert validate({k: v for k, v in r.items() if k != "_path"}) == []


def test_exclusive_create_refuses_existing(tmp_path: Path):
    write_receipt(tmp_path, _fm(), "p")
    with pytest.raises(ReceiptWriteError):
        write_receipt(tmp_path, _fm(), "p")


def test_write_refuses_invalid_and_leaves_no_file(tmp_path: Path):
    with pytest.raises(ReceiptWriteError):
        write_receipt(tmp_path, _fm(verdict="agent-delivered", judge=None), "p")
    assert read_receipts(tmp_path) == []


def test_write_refuses_mismatched_prose_ref(tmp_path: Path):
    with pytest.raises(ReceiptWriteError):
        write_receipt(tmp_path, _fm(prose_ref="elsewhere.md"), "p")


def test_read_month_filter_and_empty(tmp_path: Path):
    assert read_receipts(tmp_path) == []
    write_receipt(tmp_path, _fm(), "p")
    assert len(read_receipts(tmp_path, month="2026-10")) == 1
    assert read_receipts(tmp_path, month="2026-09") == []


def test_current_heads_two_link_chain():
    a = _fm("rcp-demo-aaaaaa")
    b = _fm("rcp-demo-bbbbbb", verdict="human-approved", judge=_JUDGE, supersedes="rcp-demo-aaaaaa", approved_by="pm")
    c = _fm("rcp-other-cccccc")
    heads = current_heads([a, b, c])
    assert set(heads) == {"rcp-demo-bbbbbb", "rcp-other-cccccc"}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, **no_console_creationflags())


@pytest.mark.spawns_process
def test_introducing_commits_one_spawn_for_three_paths(tmp_path: Path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    shas = {}
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / name).write_text(name, encoding="utf-8")
        _git(tmp_path, "add", name)
        _git(tmp_path, "commit", "-q", "-m", name)
        shas[name] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True,
            **no_console_creationflags(),
        ).stdout.strip()

    calls = []
    real_run = subprocess.run

    def counting_run(*a, **kw):
        calls.append(a[0])
        return real_run(*a, **kw)

    monkeypatch.setattr(store.subprocess, "run", counting_run)
    got = introducing_commits(tmp_path, ["a.md", "b.md", "c.md", "missing.md"])
    assert len(calls) == 1
    assert got == {**shas, "missing.md": None}


def test_introducing_commits_empty_spawns_nothing(tmp_path: Path):
    assert introducing_commits(tmp_path, []) == {}
