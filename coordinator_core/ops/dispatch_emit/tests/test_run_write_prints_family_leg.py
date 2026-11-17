"""A family-stage write leg's bytes get a run write-print, so the checkpoint commits them.

A family stage (agy, codex, grok) writes through its vendor CLI, run by the runner subagent's
Bash call; the Write/Edit print hook never sees those writes. With no print, `checkpoint_own_blobs`
withheld every path the leg wrote and the row commit reported "already match HEAD"
(run ask-20261114T133823-7a3cdc, refire 3: evidence.md withheld, nothing landed).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_write_prints

pytestmark = pytest.mark.complete

SCRATCH = "scratch/family-stage/ask-1/LR1"


def _brief(root: Path, stage: str, writes: list[str]) -> None:
    """The run manifest declares the row's writes; the runner's own brief is free text it authors."""
    import json

    from coordinator_core.ops.dispatch_emit import ask_contract

    d = root / SCRATCH
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stage}.brief.md").write_text("Declared write paths (free form):\n- ignored.py\n", encoding="utf-8")
    rec = ask_contract.record_dir(root, "ask-1")
    rec.mkdir(parents=True, exist_ok=True)
    rows = [{"id": "LR1", "writes": writes}, {"id": "LR2", "writes": ["other.py"]}]
    (rec / "manifest.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> list:
    got: list = []
    monkeypatch.setattr(
        run_write_prints, "record_write",
        lambda root, sid, aid, path, tool_input=None: got.append((sid, aid, path)) or "ask-1",
    )
    return got


@pytest.mark.parametrize("command", [
    f'agy -p="$(cat {SCRATCH}/write.prompt.md)" --output-format json --dangerously-skip-permissions > {SCRATCH}/write.raw.json',
    f"codex exec --json - < {SCRATCH}/write.prompt.md",
    f"grok --prompt-file {SCRATCH}/write.prompt.md --output-format json --permission-mode acceptEdits",
])
def test_a_write_leg_prints_every_declared_path(tmp_path: Path, calls: list, command: str) -> None:
    _brief(tmp_path, "write", ["state/run-evidence/ask-1/evidence.md", "pkg/mod.py"])
    run_write_prints.record_family_leg(tmp_path, "sid", "agent", command)
    assert calls == [("sid", "agent", "state/run-evidence/ask-1/evidence.md"), ("sid", "agent", "pkg/mod.py")]


def test_a_read_only_stage_prints_nothing(tmp_path: Path, calls: list) -> None:
    _brief(tmp_path, "review", [])
    run_write_prints.record_family_leg(
        tmp_path, "sid", "agent", f'agy -p="$(cat {SCRATCH}/review.prompt.md)" --output-format json'
    )
    assert calls == []


@pytest.mark.parametrize("command", [
    "git status --porcelain",
    f"cat {SCRATCH}/write.prompt.md",
    f"python3 scripts/family_stage_prompt.py --family agy --brief {SCRATCH}/write.brief.md --out {SCRATCH}/write.prompt.md",
])
def test_a_non_vendor_command_prints_nothing(tmp_path: Path, calls: list, command: str) -> None:
    _brief(tmp_path, "write", ["pkg/mod.py"])
    run_write_prints.record_family_leg(tmp_path, "sid", "agent", command)
    assert calls == []


def test_no_agent_or_no_brief_prints_nothing(tmp_path: Path, calls: list) -> None:
    command = f'agy -p="$(cat {SCRATCH}/write.prompt.md)"'
    run_write_prints.record_family_leg(tmp_path, "sid", "agent", command)
    _brief(tmp_path, "write", ["pkg/mod.py"])
    run_write_prints.record_family_leg(tmp_path, "sid", "", command)
    assert calls == []


def test_is_family_leg_gates_only_vendor_calls() -> None:
    assert run_write_prints.is_family_leg(f'agy -p="$(cat {SCRATCH}/write.prompt.md)"')
    assert not run_write_prints.is_family_leg("git commit -m x")
