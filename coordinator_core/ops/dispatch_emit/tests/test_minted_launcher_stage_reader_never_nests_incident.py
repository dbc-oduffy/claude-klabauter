"""Incident pin: a minted run holding its own `workflow()` stage reader is never started under the minted-start
launcher, which would nest that reader a second level; the hook starts it top-level and names the rest."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as hook
from coordinator_core.hooks.tests.test_submit_engine_ask_mint_or_refuse import _mint
from coordinator_core.hooks.tests.test_submit_engine_ask_real_emit import repo  # noqa: F401  (fixture)
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

pytestmark = pytest.mark.complete

_READER = "ask-20261122T080000-aaaaaa"
_FREE = "ask-20261122T080001-bbbbbb"


def _script(root: Path, run_id: str, body: str) -> None:
    (root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs").write_text(body, encoding="utf-8")


def test_a_reader_holding_run_among_several_starts_top_level_not_under_the_launcher(repo):  # noqa: F811
    _mint(repo, True, _READER)
    _mint(repo, True, _FREE)
    _script(repo, _READER, "const r = await workflow({ scriptPath: reader })\n")
    _script(repo, _FREE, "export const meta = {}\n")
    env = hook._minted_start_envelope(repo, [_READER, _FREE], time.monotonic() + 5.0)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    started = Path(hso["updatedInput"]["scriptPath"]).as_posix()
    assert started.endswith(f"{RUN_DIR_ROOT}/{_READER}.workflow.mjs")
    assert not Path(started).name.startswith("defer-")
    assert _FREE in hso["permissionDecisionReason"] and "dispatch.minted_run" in hso["permissionDecisionReason"]


def test_reader_free_runs_still_ride_the_launcher(repo):  # noqa: F811
    _mint(repo, True, _READER)
    _mint(repo, True, _FREE)
    _script(repo, _READER, "export const meta = {}\n")
    _script(repo, _FREE, "export const meta = {}\n")
    env = hook._minted_start_envelope(repo, [_READER, _FREE], time.monotonic() + 5.0)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    assert Path(hso["updatedInput"]["scriptPath"]).name.startswith("defer-")
