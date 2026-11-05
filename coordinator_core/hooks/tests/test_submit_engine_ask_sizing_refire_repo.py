"""A `{sizing}` submission with no `repo` either runs on the root resolved from the sizing or session, or is refused naming the sizing and the root tried."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from coordinator_core.bash_guards import _write_bump_applicability as wba
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker

REL = "state/sizings/2026-11-01-refire.yaml"
SID = "sizing-refire-repo-test"


@pytest.fixture
def repos(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(wba, "resolve_launch_anchor", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_spawn_recompose", lambda *a, **k: True)
    session, other = tmp_path / "session", tmp_path / "other"
    for repo in (session, other):
        (repo / ".git").mkdir(parents=True)
        (repo / "state" / "sizings").mkdir(parents=True)
    return session.resolve(), other.resolve()


def _put(root: Path) -> Path:
    path = root / REL
    path.write_text(yaml.safe_dump({"schema": "sizing-object", "name": "fx", "route": "dispatch"}), encoding="utf-8")
    return path


def _halt(root: Path) -> None:
    run = root / RECORD_DIR_ROOT / "warp-2026-11-01-refire-20261101T000000-abc123"
    write_halt_marker(run, halted="ledger-check", sizing_rel=REL, blocking=("R1",), blocking_items=[])


def _payload(cwd: Path, sizing: str, **extra) -> dict:
    return {
        "tool_name": "Workflow",
        "cwd": str(cwd),
        "session_id": SID,
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": sizing, **extra}},
    }


class _Spy:
    def __init__(self, root: Path):
        self.emits: list[tuple[dict, Path]] = []
        self.launches: list[tuple[Path, list]] = []
        self._root = root

    def emit(self, params: dict, root: Path) -> dict:
        from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing

        load_sizing(Path(root), params["sizing_path"])  # the engine's own refusal when the sizing is not under root
        self.emits.append((params, Path(root)))
        return {"path": str(self._root / "run.mjs"), "run_id": "r1", "fire_args": {}}

    def launch(self, root: Path, parent_id: str, children: list, session_id) -> Path:
        self.launches.append((Path(root), children))
        return self._root / "launcher.mjs"


def _run(payload: dict, spy: _Spy) -> tuple[str, str]:
    hso = mod.evaluate(payload, emit=spy.emit, launch=spy.launch)["hookSpecificOutput"]
    return hso["permissionDecision"], hso["permissionDecisionReason"]


def test_fresh_sizing_under_session_root_emits_with_that_root(repos):
    session, _ = repos
    _put(session)
    spy = _Spy(session)
    decision, why = _run(_payload(session, REL), spy)
    assert decision == "allow", why
    assert len(spy.emits) == 1 and spy.emits[0][1] == session


def test_fresh_sizing_in_no_candidate_root_is_denied_naming_sizing_and_root(repos):
    session, other = repos
    _put(other)
    spy = _Spy(session)
    decision, why = _run(_payload(session, REL), spy)
    assert decision == "deny"
    assert REL in why and str(session) in why
    assert not spy.emits


def test_halted_refire_under_session_root_launches_with_that_root(repos):
    session, _ = repos
    _put(session)
    _halt(session)
    spy = _Spy(session)
    decision, why = _run(_payload(session, REL), spy)
    assert decision == "allow", why
    assert len(spy.launches) == 1 and spy.launches[0][0] == session
    assert not spy.emits


def test_halted_refire_whose_sizing_sits_in_another_root_is_denied_not_silent(repos):
    session, other = repos
    _put(other)
    _halt(other)
    spy = _Spy(session)
    decision, why = _run(_payload(session, REL), spy)
    assert decision == "deny"
    assert REL in why and str(session) in why
    assert not spy.emits and not spy.launches


def test_absolute_sizing_in_another_repo_takes_that_repo_as_the_run_root(repos):
    session, other = repos
    path = _put(other)
    spy = _Spy(session)
    decision, why = _run(_payload(session, path.as_posix(), cross_repo_approved=True), spy)
    assert decision == "allow", why
    params, root = spy.emits[0]
    assert root == other
    assert params["sizing_path"] == REL


def test_absolute_sizing_in_an_unapproved_other_repo_is_refused_with_a_reason(repos):
    session, other = repos
    path = _put(other)
    spy = _Spy(session)
    decision, why = _run(_payload(session, path.as_posix()), spy)
    assert decision == "deny"
    assert str(other) in why
    assert not spy.emits


def test_absolute_sizing_outside_any_repo_is_denied_naming_it(tmp_path, repos):
    loose = tmp_path / "loose-sizing-dir"
    loose.mkdir()
    path = loose / "x.yaml"
    path.write_text("schema: sizing-object\n", encoding="utf-8")
    session, _ = repos
    spy = _Spy(session)
    decision, why = _run(_payload(session, path.as_posix()), spy)
    assert decision == "deny"
    assert path.as_posix() in why
    assert not spy.emits


def test_deferred_sizing_emit_script_carries_the_resolved_root(tmp_path):
    script = mod._write_deferred_script(tmp_path, {"ask": True, "sizing_path": REL, "session_id": ""})
    params_file = Path(json.loads(re.search(r"^const PARAMS_FILE = (.*);$", script.read_text(encoding="utf-8"), re.M).group(1)))
    assert json.loads(params_file.read_text(encoding="utf-8"))["target_root"] == tmp_path.as_posix()
