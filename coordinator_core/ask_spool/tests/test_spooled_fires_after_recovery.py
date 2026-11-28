"""A spooled-while-down ask fires through the warm server's drain trigger once the engine answers again.

Drives the production path: record.spool_ask (as the hook's _spool_envelope writes it), then
`_DrainTrigger.maybe_drain(Path(repo))` at default arguments (as `record_served_repo` calls it), then
`drain.run` with a stubbed `evaluate`. Time is the entry's age and the trigger's recorded start, never a sleep.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.ask_spool import drain, record
from coordinator_core.warm import server

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SESSION = "sess-submitter"
_HUNG_FOR_S = 24 * 3600.0


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


class _Engine:
    """Stands in for submit_engine_ask.evaluate on a recovered engine: mints a run dir and names it."""

    def __init__(self, root: Path):
        self.root, self.calls = root, 0

    def __call__(self, payload: dict) -> dict:
        self.calls += 1
        run_id = f"ask-20261126T13{self.calls:04d}-{self.calls:06x}"
        run_dir = self.root / "scratch" / "warp" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "submission.json").write_text(
            json.dumps({"run_id": run_id, "session_id": payload["session_id"], "tool_use_id": payload["tool_use_id"]}),
            encoding="utf-8",
        )
        return {
            "hookSpecificOutput": {
                "permissionDecision": "allow",
                "updatedInput": {"scriptPath": "x"},
                "permissionDecisionReason": f"Submitted to the engine as run {run_id}.",
            }
        }


def _spool_while_down(root: Path) -> str:
    payload = json.dumps(
        {"tool_name": "Workflow", "session_id": _SESSION, "tool_use_id": "tu-hung", "tool_input": {"name": "ask"}}
    ).encode()
    spool_id, created = record.spool_ask(
        str(root), payload, session_id=_SESSION, tool_use_id="tu-hung", reason="engine_unreachable"
    )
    assert created
    return spool_id


def _run_dirs(root: Path) -> list:
    warp = root / "scratch" / "warp"
    return sorted(p.name for p in warp.iterdir() if p.is_dir()) if warp.is_dir() else []


def _notices(root: Path) -> list:
    path = root / ".coordinator-local" / "ask-spool" / "notices" / f"{_SESSION}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _wire(monkeypatch, engine: _Engine) -> None:
    monkeypatch.setattr(server, "_run_ask_spool_drain", lambda repo_root: drain.run(str(repo_root), evaluate=engine))
    monkeypatch.setattr(server, "_spawn_background", lambda fn, *a: fn(*a))


def _hang_prior_drain(trigger: "server._DrainTrigger", root: Path) -> None:
    """A drain that began long ago and never returned: the repo key is held, its start is old."""
    key = str(root)
    trigger._running.add(key)
    trigger._last_start[key] = server.time.time() - _HUNG_FOR_S


def _assert_fired_once(root: Path, engine: _Engine, spool_id: str) -> None:
    runs = _run_dirs(root)
    assert len(runs) == 1, f"expected one minted run dir, found {runs}"
    assert engine.calls == 1
    (note,) = _notices(root)
    assert note["status"] == "minted" and note["spool_id"] == spool_id
    assert note["run_id"] == runs[0]
    assert os.path.exists(record.entry_path(str(root), spool_id, record.MINTED_SUFFIX))
    assert not os.path.exists(record.entry_path(str(root), spool_id))


def test_a_spool_written_while_the_engine_was_down_fires_on_the_next_served_request(tmp_path, monkeypatch):
    engine = _Engine(tmp_path)
    _wire(monkeypatch, engine)
    spool_id = _spool_while_down(tmp_path)

    server._DrainTrigger().maybe_drain(tmp_path)

    _assert_fired_once(tmp_path, engine, spool_id)
    assert drain.run(str(tmp_path), evaluate=engine)["minted"] == 0
    assert len(_run_dirs(tmp_path)) == 1 and engine.calls == 1


def test_a_spool_fires_after_a_prior_drain_hung_with_the_engine(tmp_path, monkeypatch):
    engine = _Engine(tmp_path)
    _wire(monkeypatch, engine)
    trigger = server._DrainTrigger()
    _hang_prior_drain(trigger, tmp_path)
    spool_id = _spool_while_down(tmp_path)

    trigger.maybe_drain(tmp_path)

    _assert_fired_once(tmp_path, engine, spool_id)


def test_a_second_drain_after_recovery_mints_no_duplicate(tmp_path, monkeypatch):
    engine = _Engine(tmp_path)
    _wire(monkeypatch, engine)
    trigger = server._DrainTrigger()
    _hang_prior_drain(trigger, tmp_path)
    spool_id = _spool_while_down(tmp_path)

    trigger.maybe_drain(tmp_path)
    again = drain.run(str(tmp_path), evaluate=engine)

    assert again["minted"] == 0 and again["skipped"] == 0 and again["failed"] == 0
    _assert_fired_once(tmp_path, engine, spool_id)


def test_a_drain_still_inside_its_run_does_not_stack_a_second(tmp_path, monkeypatch):
    held: list = []
    monkeypatch.setattr(server, "_run_ask_spool_drain", lambda repo_root: None)
    monkeypatch.setattr(server, "_spawn_background", lambda fn, *a: held.append((fn, a)))
    trigger = server._DrainTrigger()
    trigger.maybe_drain(tmp_path)
    _spool_while_down(tmp_path)

    trigger.maybe_drain(tmp_path)

    assert len(held) == 1
