"""A codex row's apply_patch writes (`file_change` events, never a PostToolUse hook) print as the run's own."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.git import checkpoint_hunks as ch
from coordinator_core.git.tests.test_checkpoint_hunks import MSG, _repo
from coordinator_core.ops.dispatch_emit import run_write_prints

RUN = "runC"


def _stream(path: Path, *changed: str, status: str = "completed") -> Path:
    events = [{"type": "item.completed", "item": {"type": "file_change", "status": status,
                                                  "changes": [{"path": p, "kind": "update"} for p in changed]}},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "x"}}, "not json"]
    path.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in events), encoding="utf-8")
    return path


def test_codex_write_without_print_is_withheld_and_with_print_is_own(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    reasons: dict[str, str] = {}
    _, withheld = ch.checkpoint_own_blobs(root, RUN, ["f.txt"], MSG, reasons)
    assert withheld == ["f.txt"] and reasons["f.txt"] == ch.NO_PRINT_REASON

    stream = _stream(tmp_path / "stdout.jsonl", str(root / "f.txt").replace("/", "\\"), str(tmp_path / "outside.txt"))
    assert run_write_prints.record_codex_writes(root, RUN, stream) == ["f.txt"]
    reasons.clear()
    ch.checkpoint_own_blobs(root, RUN, ["f.txt"], MSG, reasons)
    assert reasons.get("f.txt") != ch.NO_PRINT_REASON


def test_only_completed_file_changes_in_the_tree_are_printed(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    stream = _stream(tmp_path / "stdout.jsonl", str(root / "f.txt"), status="in_progress")
    assert run_write_prints.record_codex_writes(root, RUN, stream) == []
    assert run_write_prints.read(root, RUN) is None
    assert run_write_prints.record_codex_writes(root, RUN, tmp_path / "absent.jsonl") == []
