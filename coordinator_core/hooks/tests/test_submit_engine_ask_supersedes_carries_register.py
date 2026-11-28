"""A fresh `{sizing, supersedes}` submission through `hooks.submit_engine_ask.evaluate` and the real
`dispatch.emit` carries the predecessor's PM register (pm_verbatims, source docs, notes, all_of_pm_ask), read
back from the run's requirements.md view; a mint that still shrinks the register is refused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

OLD = "ask-20261121T212246-46b2c4"
OLD_REL = "state/sizings/2026-11-21-old.yaml"
NEW_REL = "state/sizings/2026-11-21-new.yaml"
TURN21 = "the PM's turn 21 words, in full, never abridged"
SOURCES = ("docs/a.md", "docs/b.md", "docs/c.md")
NOTE = "holo-23: the proof is CPU-only at runtime"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


def _sizing(root: Path, rel: str, verbatims: list[dict]) -> None:
    doc = {
        "schema": "sizing-object", "name": "carry fixture", "intent": "carry the register",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-carry-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-21", "mode": "pm"}},
    }
    if verbatims:
        doc["pm_verbatims"] = verbatims
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return root


def _predecessor(root: Path) -> None:
    """The walled run: all_of_pm_ask, PM turn 21, three source docs and one EM note."""
    _sizing(root, OLD_REL, [{"turn": 21, "text": TURN21}])
    rl.start(root, OLD, all_of_pm_ask=True)
    rl.bind(root, OLD, OLD_REL, ask_text="the original ask text, long enough")
    rl._mutate_register(root, OLD_REL, lambda rows: rows, extra_sources=[{"doc": d, "ref": "fixture"} for d in SOURCES])
    note_dir = root / RECORD_DIR_ROOT / OLD
    with (note_dir / rl.NOTES_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"note": NOTE, "at": "2026-11-21T22:00:00Z"}) + "\n")
    rl._write_view(root, note_dir, OLD)


def _submit(root: Path, args: dict) -> dict:
    return mod.evaluate({
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-supersedes-carry",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    })


def _new_run(root: Path) -> str:
    runs = [p.name for p in (root / RECORD_DIR_ROOT).iterdir() if p.is_dir() and p.name != OLD]
    assert len(runs) == 1, runs
    return runs[0]


def test_a_fresh_sizing_mint_for_a_walled_run_carries_its_register(repo: Path) -> None:
    _predecessor(repo)
    _sizing(repo, NEW_REL, [])

    env = _submit(repo, {"sizing": NEW_REL, "supersedes": OLD})
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env

    new = _new_run(repo)
    view = (repo / RECORD_DIR_ROOT / new / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    flag = next(x for x in view.splitlines() if "(all_of_pm_ask)" in x)
    assert flag.rsplit(": ", 1)[1] == "yes", flag
    assert TURN21 in view
    assert NOTE in view
    register = yaml.safe_load((repo / NEW_REL).read_text(encoding="utf-8"))["requirement_register"]
    assert {s["doc"] for s in register["sources"]} >= set(SOURCES)
    assert any(TURN21 in r[rr.ROW_TEXT] for r in register["rows"])


def test_a_mint_that_still_shrinks_the_register_is_refused(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _predecessor(repo)
    _sizing(repo, NEW_REL, [])
    monkeypatch.setattr(rl, "_carry_register", lambda *a, **k: ([], None))

    env = _submit(repo, {"sizing": NEW_REL, "supersedes": OLD})

    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny", env
    reason = hso["permissionDecisionReason"]
    assert f"would drop {OLD}'s requirement register" in reason
    assert "source docs/a.md" in reason and "source docs/c.md" in reason
    assert "Resubmit with the predecessor's sizing" in reason


def test_a_mint_with_no_predecessor_is_never_refused(repo: Path) -> None:
    _sizing(repo, NEW_REL, [])

    env = _submit(repo, {"sizing": NEW_REL})

    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
