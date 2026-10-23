"""land_run's since-halt gate measures from the chain's first halt: a product commit landed between
two halts counts at the re-fire, and a chain with no product change since its first halt still
refuses, naming that first halt as the base. Built under tmp_path."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_PLAN_FIELD,
    UNCHANGED_SINCE_HALT_REASON,
    halted_record_message,
)

pytestmark = pytest.mark.spawns_process

PLAN = "docs/plans/p.md"
RUN = "run-1"
PRODUCT = "src/p.py"
_MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _halt(root: Path) -> str:
    msg = halted_record_message(RUN, {"halted": "ledger-check", HALT_PLAN_FIELD: PLAN})
    _git(root, "commit", "--allow-empty", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def _repo(root: Path) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "src").mkdir()
    (root / PRODUCT).write_text("v = 0\n", encoding="utf-8")
    _git(root, "add", PRODUCT)
    _git(root, "commit", "-q", "-m", "base")
    return _halt(root)


def _manifest(root: Path) -> Path:
    path = root / "manifest.json"
    path.write_text(json.dumps({"rows": [{"id": "C1", "writes": [PRODUCT]}]}), encoding="utf-8")
    return path


def _check(root: Path) -> tuple[list, dict]:
    detail: dict = {}
    refused = land_run._unchanged_since_halt(
        root, {"run_id": RUN, "plan": PLAN, "blocking": ["R1"]}, _MET, _manifest(root), PLAN, detail,
    )
    return refused, detail


def test_product_commit_between_two_halts_counts_at_the_refire(tmp_path):
    _repo(tmp_path)
    (tmp_path / PRODUCT).write_text("v = 1\n", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "product between fires")
    _halt(tmp_path)

    refused, _detail = _check(tmp_path)

    assert refused == []


def test_no_product_change_since_the_first_halt_still_refuses_naming_the_first_halt(tmp_path):
    first = _repo(tmp_path)
    _halt(tmp_path)

    refused, detail = _check(tmp_path)

    assert refused == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
    assert detail["base"] == first and detail["base_source"] == "resolved"
    assert detail["outcome"] == "unchanged"


def test_resolve_halt_sha_is_gone_from_land_run():
    assert not hasattr(land_run, "_resolve_halt_sha")
