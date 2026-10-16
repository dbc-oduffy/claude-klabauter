"""End-to-end exit criterion: N repeated --ask with per-question targets, split and combine, through the real op."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_research_probe"
_ARGV = [
    "--research",
    "--ask", "Q one?",
    "--ask", "Q two?", "--target", "repo=docs/notes.md",
    "--ask", "Q three?", "--target", "local=claude-json-keys",
]


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


@pytest.fixture
def repo(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _FIXTURE)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-multi-ask")
    return tmp_path


def _run(capsys, argv):
    code = cli_module.main(argv)
    out = capsys.readouterr()
    reply = json.loads(out.out) if out.out.strip().startswith("{") else None
    return code, reply, out.err


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_split_fans_one_scout_per_question(repo, capsys):
    code, reply, _ = _run(capsys, _ARGV)
    assert code == cli_module.EXIT_OK and reply["ok"]
    assert reply["tier"] == "scouts"
    slugs = _segment_questions(reply)
    assert len(slugs) == 3 and len(set(slugs)) == 3

    script = Path(reply["path"]).read_text(encoding="utf-8")
    for slug in slugs:
        assert slug in script

    ask = (repo / reply["scratch_dir"] / "ask.md").read_text(encoding="utf-8")
    sections = _sections(ask)
    assert set(sections) == set(slugs)
    with_target = {s for s, body in sections.items() if "Target:" in body}
    assert len(with_target) == 2
    bodies = [sections[s] for s in slugs]
    assert "Q one?" in bodies[0] and "Target:" not in bodies[0]
    assert "Target: repo `docs/notes.md`" in bodies[1] and "Q two?" in bodies[1]
    assert "Target: local `claude-json-keys`" in bodies[2] and "Q three?" in bodies[2]

    targets = _scout_targets(reply)
    assert targets, "reply carries no scout_targets"
    assert "docs/notes.md" in json.dumps(targets) and "claude-json-keys" in json.dumps(targets)


def test_combine_is_one_scout_over_every_question(repo, capsys):
    code, reply, _ = _run(capsys, [*_ARGV, "--combine"])
    assert code == cli_module.EXIT_OK and reply["ok"]
    assert _segment_questions(reply) == [rc.COMBINED_SCOUT_ITEM] == ["question"]
    ask = (repo / reply["scratch_dir"] / "ask.md").read_text(encoding="utf-8")
    sections = _sections(ask)
    assert list(sections) == ["question"]
    body = sections["question"]
    for text in ("Q one?", "Q two?", "Q three?", "Target: repo `docs/notes.md`", "Target: local `claude-json-keys`"):
        assert text in body


def test_missing_repo_target_is_a_data_error_naming_question_and_path(repo, capsys):
    argv = ["--research", "--ask", "Q one?", "--ask", "Q two?", "--target", "repo=missing/path"]
    code, _, err = _run(capsys, argv)
    assert code == cli_module.EXIT_DATA_ERROR
    assert "question 2" in err and "missing/path" in err


def test_emit_spawns_no_process_and_is_fast(repo, capsys, monkeypatch):
    spawns: list[str] = []

    def counter(name):
        def count(*args, **kwargs):
            spawns.append(name)
            raise AssertionError(f"{name} called on the research route")
        return count

    monkeypatch.setattr(subprocess, "Popen", counter("subprocess.Popen"))
    for attr in ("posix_spawn", "fork"):
        if hasattr(os, attr):
            monkeypatch.setattr(os, attr, counter(f"os.{attr}"))
    t0 = time.process_time()
    code, reply, _ = _run(capsys, _ARGV)
    elapsed_ms = (time.process_time() - t0) * 1000
    assert code == cli_module.EXIT_OK and reply["ok"]
    assert spawns == []
    assert elapsed_ms < 200


def _segment_questions(reply: dict) -> list[str]:
    found = _find(reply, "questions")
    assert found is not None, f"no segment questions in reply keys {sorted(reply)}"
    return list(found)


def _scout_targets(reply: dict):
    return _find(reply, "scout_targets")


def _find(node, key):
    if isinstance(node, dict):
        if key in node and node[key]:
            return node[key]
        for v in node.values():
            r = _find(v, key)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find(v, key)
            if r:
                return r
    return None
