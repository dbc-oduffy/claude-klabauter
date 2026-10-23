"""terminal_commit's since-halt gate measures from the run's first halt, over a real git repo under tmp_path."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_PLAN_FIELD,
    UNCHANGED_SINCE_HALT_REASON,
    halted_record_message,
)

pytestmark = pytest.mark.spawns_process

PLAN = "docs/plans/p.md"
RUN = "run-1"
MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _halt(root: Path) -> str:
    msg = halted_record_message(RUN, {"halted": "ledger-check", HALT_PLAN_FIELD: PLAN})
    _git(root, "commit", "--allow-empty", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def chain(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "prod.py")
    _git(tmp_path, "commit", "-q", "-m", "base")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, manifest, _halt(tmp_path)


def _resume() -> dict:
    return {"run_id": RUN, "plan": PLAN, "blocking": ["R1"], "unlanded": ["C1"]}


def test_product_committed_between_two_halts_counts_from_the_first_halt(chain):
    root, manifest, h1 = chain
    (root / "prod.py").write_text("x = 2\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "review-finding fix")
    _halt(root)
    detail: dict = {}

    got = tc._unchanged_since_halt_failures(root, _resume(), MET, manifest, PLAN, detail)

    assert got == [], (got, detail)


def test_no_product_change_since_the_first_halt_still_refuses_naming_the_first_halt(chain):
    root, manifest, h1 = chain
    _halt(root)
    detail: dict = {}

    got = tc._unchanged_since_halt_failures(root, _resume(), MET, manifest, PLAN, detail)

    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
    assert detail["base"] == h1 and detail["base_source"] == "resolved"
