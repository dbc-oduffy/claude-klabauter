"""A cross-repo engine-ask's briefs name record-dir files by path that resolves from a foreign cwd.

The process cwd is the submitting repo, not the target repo that holds the record dir; a leg reads
every staged prompt, the ask text and the ledger by the path the brief spells.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

_READ_POINTER = re.compile(r"Read (\S+?\.md) in full and follow it")
_RECORD_PATH = re.compile(r"(?<![\w/.])((?:/|[A-Za-z]:/)?" + re.escape(RECORD_DIR_ROOT) + r"/[\w.-]+/[\w./-]*[\w-])")


# Repo-relative values the engine parses (script marker, op params, result fields), never read by an agent as a path.
_DATA_PREFIXES = ("ask-run-manifest v1 ", "plan_path: ", "ledger: '", "manifest: '")


def _emit(target: Path, out: Path, ask: str) -> tuple[str, Path]:
    assert cli.main(["--ask", ask, "--ask-source", "relayed", "--repo-root", str(target), "--out", str(out)]) == 0
    script = out.read_text(encoding="utf-8")
    (record,) = {Path(m.group(1)).parent.parent for m in _READ_POINTER.finditer(script)}
    return script, record


def test_two_sibling_cross_repo_emit_briefs_resolve_from_a_foreign_cwd(tmp_path, monkeypatch):
    target = tmp_path / "target"
    (target / ".git").mkdir(parents=True)
    foreign = tmp_path / "submitter"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: "export const meta = { phases: [] };\n")

    records = []
    for n in (1, 2):
        script, record = _emit(target, target / "scratch" / f"sibling-{n}.workflow.mjs", f"sibling {n} asks for a thing")
        records.append(record)
        root = target.as_posix()
        assert record.as_posix().startswith(f"{root}/{RECORD_DIR_ROOT}/")
        pointers = [Path(m.group(1)) for m in _READ_POINTER.finditer(script)]
        assert pointers, "the emit staged no prompt"
        for pointer in pointers:
            assert pointer.is_absolute() and pointer.is_file(), pointer
        assert (record / "relayed-ask.md").is_file()
        text = script.replace("\\'", "'")
        for m in _RECORD_PATH.finditer(text):
            if text[max(0, m.start() - 24) : m.start()].endswith(_DATA_PREFIXES):
                continue
            assert m.group(1).startswith(root), f"record-dir path not anchored to the target: {m.group(1)}"
    assert records[0] != records[1]


def test_a_relative_record_dir_pointer_is_what_the_fix_refuses(tmp_path, monkeypatch):
    target = tmp_path / "target"
    (target / ".git").mkdir(parents=True)
    foreign = tmp_path / "submitter"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: "export const meta = { phases: [] };\n")
    script, record = _emit(target, target / "scratch" / "one.workflow.mjs", "one ask")
    relative = f"Read {RECORD_DIR_ROOT}/{record.name}/prompts/"
    assert relative not in script
    assert not (foreign / RECORD_DIR_ROOT / record.name / "prompts" / "scout.md").exists()
    assert (record / "prompts" / "scout.md").is_file()
