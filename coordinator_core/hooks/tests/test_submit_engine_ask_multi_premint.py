"""A multi-ask submission is taken by the submit hook: the fan-out writes each child's sizing and parent record,
each child is emitted inside the hook's budget, and one launcher script is the rewrite that starts them."""

from __future__ import annotations

import json
import re
from pathlib import Path

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.hooks.tests.test_submit_engine_ask_deferred_quoting import _node

repo = real.repo
ASKS = 15
_PARENT = re.compile(r"ask-parent-\d{8}T\d{6}-[0-9a-f]{6}")


def _asks(n: int = ASKS, pad: int = 200) -> list:
    return [
        {"ask": f"job {i}", "label": f"job-{i}", "source": "relayed", "writes": [f"pkg/m{i}.py"], "em_context": "ctx " * pad}
        for i in range(n)
    ]


def _payload(root: Path, asks: list) -> dict:
    return real._payload(root, {"asks": asks})


def _script(env: dict) -> Path:
    return Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])


def _children(text: str) -> list:
    literal = re.search(r"^const CHILDREN = (\[.*\]);$", text, re.MULTILINE)
    assert literal, "the launcher names its children"
    return json.loads(literal.group(1))


def test_fifteen_asks_are_taken_with_one_script_write_and_a_minted_parent_id(repo):
    env = mod.evaluate(_payload(repo, _asks()))
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    parent_id = _PARENT.search(hso["additionalContext"])
    assert parent_id, hso
    parent_id = parent_id.group(0)
    assert (repo / "scratch" / "warp" / parent_id / "parent.json").is_file()
    launcher = _script(env)
    assert launcher.name == f"{parent_id}.launcher.workflow.mjs" and launcher.is_file()
    assert launcher.name in hso["permissionDecisionReason"]
    assert len(list((repo / "state" / "sizings").rglob("*.yaml"))) == ASKS
    assert len(list((repo / "scratch" / "warp").glob("*.launcher.workflow.mjs"))) == 1


def test_the_script_embeds_every_ask_and_names_the_fanout_op_and_the_launcher_body(repo):
    env = mod.evaluate(_payload(repo, _asks()))
    text = _script(env).read_text(encoding="utf-8")
    children = _children(text)
    assert len(children) == ASKS
    assert sorted(c["sizing_rel"] for c in children) == sorted(
        p.relative_to(repo).as_posix() for p in (repo / "state" / "sizings").rglob("*.yaml")
    )
    assert "parallel(" in text  # the engine's launcher body starts the children
    assert not re.findall(r"(?<![A-Za-z0-9_])__[A-Z][A-Z_]*__(?![A-Za-z0-9_])", text)


def test_the_launcher_names_every_child_with_a_deferred_emit_script_and_the_hook_emits_none(repo):
    env = mod.evaluate(_payload(repo, _asks(3, pad=1)))
    text = _script(env).read_text(encoding="utf-8")
    children = _children(text)
    emitted = re.search(r"^const EMITTED = (\[.*\]);$", text, re.MULTILINE)
    assert emitted, "the launcher carries each child's emitted reply"
    replies = json.loads(emitted.group(1))
    assert len(children) == len(replies) == 3
    for child, reply in zip(children, replies):
        assert (repo / child["sizing_rel"]).is_file()
        assert reply.get("deferred"), reply
        assert (repo / reply["path"]).is_file()
        assert not (repo / "scratch" / "warp" / f"{child['run_id']}.workflow.mjs").exists()


def test_an_oversize_submission_params_file_holds_the_payload_and_the_relay_leaves_it(repo):
    asks = _asks(ASKS, pad=2000)
    env = mod.evaluate(_payload(repo, asks))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert len(_children(_script(env).read_text(encoding="utf-8"))) == ASKS
    files = list((repo / "scratch" / "warp").glob("defer-params-*.json"))
    assert len(files) == ASKS
    sent = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    assert len({p["sizing_path"] for p in sent}) == ASKS
    assert all(p["session_id"] == "sess-real-emit" and p["target_root"] == repo.as_posix() for p in sent)
    for deferred in repo.joinpath("scratch", "warp").glob("defer-*.workflow.mjs"):
        text = deferred.read_text(encoding="utf-8")
        (named,) = re.findall(r'^const PARAMS_FILE = "([^"]+)";$', text, re.MULTILINE)
        assert Path(named) in files
        assert "--params-file" in text
    # The relay agent is the consumer: its prompt reads the params file and orders no removal (a relay is denied one).
    one = next(iter(repo.joinpath("scratch", "warp").glob("defer-*.workflow.mjs")))
    got = _node(one, repo, [{"path": "", "run_id": "", "error": "stop here"}], [])
    (prompt,) = [c["agent"] for c in got["calls"] if "agent" in c]
    (read_path,) = re.findall(r'--params-file "([^"]+)"', prompt)
    assert "rm -f" not in prompt and "Do not delete" in prompt and Path(read_path).is_file()


def test_an_injected_fanout_still_runs_inline(repo):
    seen: list = []

    def fan(params, root):
        seen.append(params)
        return {"error": "stub refusal"}

    env = mod.evaluate(_payload(repo, _asks(2, pad=1)), fanout=fan)
    assert len(seen) == 1
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not list((repo / "scratch" / "warp").glob("*.mjs"))


def test_a_launcher_that_cannot_be_written_refuses_and_leaves_no_script(repo):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    env = mod.evaluate(_payload(repo, _asks(2, pad=1)), launch=fail)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and "disk full" in hso["permissionDecisionReason"], env
    assert not list((repo / "scratch" / "warp").glob("*.launcher.workflow.mjs"))
