"""A multi-ask submission is taken by the submit hook with one script write and one run-token marker; the
fan-out, each child's emit and the launcher run in the deferred script, never in the hook."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.ops.dispatch_emit import ask_fanout, run_token
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import FANOUT_OP

# Checkpoint 82d8e6c9b3 landed this test ahead of the submit-hook code it pins (run b2b619 still open).
pytestmark = pytest.mark.pending_fix

repo = real.repo
ASKS = 15


def _asks(n: int = ASKS, pad: int = 200) -> list:
    return [
        {"ask": f"job {i}", "label": f"job-{i}", "source": "relayed", "writes": [f"pkg/m{i}.py"], "em_context": "ctx " * pad}
        for i in range(n)
    ]


def _payload(root: Path, asks: list) -> dict:
    return real._payload(root, {"asks": asks})


def _script(env: dict) -> Path:
    return Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])


def _spool(text: str) -> dict:
    inline = re.search(r'^const _FAN_PARAMS = (".*");$', text, re.MULTILINE)
    assert inline, "the script embeds the fan-out params inline"
    return json.loads(json.loads(inline.group(1)))


@pytest.fixture
def forbidden(monkeypatch):
    """The hook may not fan out or emit; the deferred script does."""
    calls: list = []

    def boom(name):
        def fn(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} ran in the hook")

        return fn

    monkeypatch.setattr(ask_fanout, "fanout", boom("fanout"))
    monkeypatch.setattr(mod, "_default_emit", boom("_default_emit"))
    monkeypatch.setattr(mod, "_default_launch", boom("_default_launch"))
    return calls


def test_fifteen_asks_are_taken_with_one_script_write_and_a_minted_parent_id(repo, forbidden):
    started = time.monotonic()
    env = mod.evaluate(_payload(repo, _asks()))
    assert time.monotonic() - started < 2.0
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    parent_id = re.search(r"ask-parent-\d{8}T\d{6}-[0-9a-f]{6}", hso["additionalContext"])
    assert parent_id, hso
    assert run_token._record_path(repo, parent_id.group(0)).is_file()  # the one marker write
    assert parent_id.group(0) in hso["permissionDecisionReason"]
    assert forbidden == []
    written = sorted(p.name for p in (repo / "scratch" / "warp").iterdir())
    assert written == [_script(env).name], written  # the script is the spool: no second file
    assert not list((repo / "state" / "sizings").glob("*.yaml"))


def test_the_script_embeds_every_ask_and_names_the_fanout_op_and_the_launcher_body(repo, forbidden):
    env = mod.evaluate(_payload(repo, _asks()))
    text = _script(env).read_text(encoding="utf-8")
    spooled = _spool(text)
    assert [a["ask"] for a in spooled["asks"]] == [f"job {i}" for i in range(ASKS)]
    assert FANOUT_OP in text
    assert "await _emitRuntime()" in text and "_fan.parent_id" in text and "_FAN_CHILDREN" in text
    assert "parallel(" in text  # the engine's launcher body starts the children
    assert "FANOUT_PARENT_SENTINEL" not in text and "FANOUT_RUN_SENTINEL" not in text and "fanout_emitted_sentinel" not in text
    assert not re.findall(r"(?<![A-Za-z0-9_])__[A-Z][A-Z_]*__(?![A-Za-z0-9_])", text)


def test_the_embedded_spool_fans_out_exactly_as_the_hook_would_have(repo):
    env = mod.evaluate(_payload(repo, _asks(3, pad=1)))
    reply = ask_fanout.fanout(_spool(_script(env).read_text(encoding="utf-8")), repo)
    assert reply["applied"] is True and len(reply["ready"]) == 3, reply


def test_an_oversize_submission_rides_one_params_file_not_one_per_ask(repo, forbidden):
    env = mod.evaluate(_payload(repo, _asks(ASKS, pad=2000)))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    names = sorted(p.suffix for p in (repo / "scratch" / "warp").iterdir())
    assert names == [".json", ".mjs"], names
    text = _script(env).read_text(encoding="utf-8")
    assert '--params-file "' in text or "_FAN_PARAMS_FILE" in text


def test_an_injected_fanout_still_runs_inline(repo):
    seen: list = []

    def fan(params, root):
        seen.append(params)
        return {"error": "stub refusal"}

    env = mod.evaluate(_payload(repo, _asks(2, pad=1)), fanout=fan)
    assert len(seen) == 1
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not list((repo / "scratch" / "warp").glob("*.mjs"))


def test_a_script_that_cannot_be_written_refuses_and_discards_the_marker(repo, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_write_deferred_fanout_script", fail)
    env = mod.evaluate(_payload(repo, _asks(2, pad=1)))
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and "could not be written" in hso["permissionDecisionReason"], env
    assert not list(run_token._record_path(repo, "ask-20000101T000000-000000").parent.glob("ask-parent-*.json"))
