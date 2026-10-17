"""cascade_terminal_sync: the terminal-state cascade runs with no event loop.

Pins that a plan flip's cascade leaves asyncio unloaded when no candidate reaches
leg (b), and that the sync core and the awaitable `_handler` agree.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import deliverable_cascade as cascade_mod
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_DID = "dlv-synccore-000000"

_PROBE = r"""
import json, sys
from coordinator_core.ops import plan_status_transition as pst
rc = pst.main(["stamp-implemented", sys.argv[1], "--no-commit", "--ship-sha", "a" * 40])
print(json.dumps({"rc": rc, "asyncio": "asyncio" in sys.modules}))
"""


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, **no_console_creationflags())


def _plan(root: Path) -> Path:
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "state" / "handoffs").mkdir(parents=True)
    plan = root / "docs" / "plans" / "2026-10-17-sync-core.md"
    plan.write_text(
        f"---\ntitle: P\nstatus: executing\ndeliverable_id: {_DID}\n---\nBody\n",
        encoding="utf-8",
    )
    return plan


def test_flip_with_empty_corpus_leaves_asyncio_unloaded(tmp_path):
    _git_init(tmp_path)
    plan = _plan(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(plan)],
        cwd=tmp_path, capture_output=True, text=True, **no_console_creationflags(),
    )
    last = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")][-1]
    probe = json.loads(last)
    assert probe["asyncio"] is False, proc.stderr


def _params(root: Path, plan: Path) -> dict:
    return {
        "deliverable_id": _DID,
        "source_kind": "plan",
        "source_path": str(plan),
        "target_kind": "handoff",
        "at": "2026-10-17T00:00:00Z",
        "ship_sha": "a" * 40,
    }


def test_empty_corpus_sync_equals_handler(tmp_path):
    _git_init(tmp_path)
    plan = _plan(tmp_path)
    common = tmp_path / ".git"
    sync = cascade_mod.cascade_terminal_sync(_params(tmp_path, plan), repo_root=common)
    awaited = asyncio.run(cascade_mod._handler(_params(tmp_path, plan), repo_root=common))
    assert sync == awaited
    assert sync["candidates_matched"] == 0 and sync["exit_code"] == 1


def test_advance_fixture_sync_equals_handler(tmp_path, monkeypatch):
    _git_init(tmp_path)
    plan = _plan(tmp_path)
    common = tmp_path / ".git"
    handoff = tmp_path / "state" / "handoffs" / "h.md"
    handoff.write_text(
        f"---\ntitle: H\nkind: handoff\ndeployment_state: in_flight\n"
        f"deliverable_id: {_DID}\n---\nBody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cascade_mod, "_advance_one", lambda *a, **k: (True, None))
    monkeypatch.setattr(
        cascade_mod, "resolve_baton_rows", lambda *a, **k: {"advanced": [], "unresolved": []}
    )
    monkeypatch.setattr(cascade_mod, "_commit_mutated_paths", lambda *a, **k: (None, None))
    sync = cascade_mod.cascade_terminal_sync(_params(tmp_path, plan), repo_root=common)
    awaited = asyncio.run(cascade_mod._handler(_params(tmp_path, plan), repo_root=common))
    assert sync == awaited
    assert sync["exit_code"] == 0 and len(sync["advanced"]) == 1
