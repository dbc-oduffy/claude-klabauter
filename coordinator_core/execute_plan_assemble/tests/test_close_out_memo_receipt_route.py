"""close-out-and-stamp and memo-send rows: the remedy names the memo route for an
open memo row, and a row resolved ``delivered_elsewhere`` / ``memo`` / receipt
lets the plan stamp ``implemented``."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.execute_plan_assemble import close_out_and_stamp as coas
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_RECEIPT = ".coordinator-local/memo-outbox/sent/rcpt-topic.md"

_HEADER = """---
title: "Memo route fixture"
created: 2026-01-01
author: test-fixture
status: draft
branch: "work/test/2026-10-16"
plan_id: "pln-memo-route-fixture-000001"
deliverable_id: "dlv-memo-route-fixture-000001"
---

# Memo route fixture

## Tasks

```yaml plan-tasks
"""

_CODE_ROW = """- id: C1
  title: "Ship the widget"
  change_kind: script-edit
  surface: coordinator/bin/widget.py
  body: |
    Ship it.
"""

_MEMO_OPEN = """- id: C2
  title: "Tell the receiver"
  change_kind: memo-send
  kind: memo-send
  surface: coordinator-content-repo/state/cross-repo/inbox/
  receipt: rcpt-topic
  body: |
    Send the memo.
"""

_MEMO_RESOLVED = """- id: C2
  title: "Tell the receiver"
  change_kind: memo-send
  kind: memo-send
  surface: coordinator-content-repo/state/cross-repo/inbox/
  receipt: rcpt-topic
  disposition: delivered_elsewhere
  delivered_via: memo
  disposition_ref: .coordinator-local/memo-outbox/sent/rcpt-topic.md
  body: |
    Send the memo.
"""

_CODED_ROW = """- id: C1
  title: "Ship the widget"
  change_kind: script-edit
  surface: coordinator/bin/widget.py
  disposition: coded
  disposition_ref: '{sha}'
  body: |
    Ship it.
"""


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _init(root: Path) -> str:
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t"], root)
    _git(["config", "user.name", "test"], root)
    (root / "seed.txt").write_text("x", encoding="utf-8")
    _git(["add", "seed.txt"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return _git(["rev-parse", "HEAD"], root)


def _plan(root: Path, *rows: str) -> None:
    (root / "plan.md").write_text(_HEADER + "".join(rows) + "```\n", encoding="utf-8")
    _git(["add", "plan.md"], root)
    _git(["commit", "-q", "-m", "plan"], root)


def _close_out(monkeypatch, root: Path) -> dict:
    monkeypatch.chdir(root)
    _code, result = coas.close_out_and_stamp("plan.md", repo_root=root)
    return result


def test_open_memo_row_remedy_names_memo_route_not_coded(tmp_path, monkeypatch):
    sha = _init(tmp_path)
    _plan(tmp_path, _CODED_ROW.format(sha=sha), _MEMO_OPEN)
    result = _close_out(monkeypatch, tmp_path)
    assert result["missing_chunk_ids"] == ["C2"]
    message = result["message"]
    assert "delivered_elsewhere" in message and "sent/rcpt-topic.md" in message
    assert "not yet sent" in message
    assert "plan-tasks-resolve --coded" not in message


def test_sent_receipt_drops_not_yet_sent_note(tmp_path, monkeypatch):
    sha = _init(tmp_path)
    receipt = tmp_path / _RECEIPT
    receipt.parent.mkdir(parents=True)
    receipt.write_text("sent", encoding="utf-8")
    _plan(tmp_path, _CODED_ROW.format(sha=sha), _MEMO_OPEN)
    message = _close_out(monkeypatch, tmp_path)["message"]
    assert "not yet sent" not in message
    assert "delivered_elsewhere" in message


def test_mixed_open_rows_name_both_remedies(tmp_path, monkeypatch):
    _init(tmp_path)
    _plan(tmp_path, _CODE_ROW, _MEMO_OPEN)
    result = _close_out(monkeypatch, tmp_path)
    assert sorted(result["missing_chunk_ids"]) == ["C1", "C2"]
    message = result["message"]
    assert "plan-tasks-resolve --coded" in message
    assert "delivered_elsewhere" in message


def test_receipt_resolved_memo_row_lets_plan_stamp_implemented(tmp_path, monkeypatch):
    sha = _init(tmp_path)
    _plan(tmp_path, _CODED_ROW.format(sha=sha), _MEMO_RESOLVED)
    result = _close_out(monkeypatch, tmp_path)
    assert result["missing_chunk_ids"] == [], result
    assert result["shipped"] is True, result
    assert result["status_target"] == "implemented", result
