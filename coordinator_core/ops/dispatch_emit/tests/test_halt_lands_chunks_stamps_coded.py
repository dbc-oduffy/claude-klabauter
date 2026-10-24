"""A ledger-check halt's Lands-Chunks rows read coded at the halt sha, through both halt writers."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import emit, land_run_halt, sizing_fire, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261024T000000-aaaaaa"
PLAN = "docs/plans/p.md"
PASSING = {"targeted_tests": {"status": "pass", "tests_failed": 0, "baseline": None}}
SPINE = (
    "---\ntitle: p\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n"
    "- id: C1\n  title: code it\n  change_kind: script-edit\n  surface: a.py\n"
    "  writes: [a.py]\n  disposition: open\n  body: |\n    Do it.\n"
    "- id: C2\n  title: code more\n  change_kind: script-edit\n  surface: b.py\n"
    "  writes: [b.py]\n  disposition: open\n  body: |\n    Do it.\n"
    "```\n"
)
HALT = {"halted": "ledger-check", "reason": "row C2 open", "plan": PLAN}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _write(tmp_path, PLAN, SPINE)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    for name in ("ask.md", "run.json"):
        _write(tmp_path, f"{RECORD_DIR_ROOT}/{RUN}/{name}", f"{name}\n")
    return tmp_path


def _chunks(root: Path, chunks: dict) -> None:
    built = []
    for cid, paths in chunks.items():
        _write(root, f"reports/{cid}.md", f"DONE: {cid}\n")
        for p in paths:
            _write(root, p, f"{p} = 1\n")
        built.append(ChunkCommit(id=cid, title=cid, paths=tuple(paths), report=f"reports/{cid}.md"))
    marker = render_marker(CommitRequest(chunks=tuple(built), session_id=None, repo_root=None, plan_path=None))
    _write(root, "run.mjs", f"{marker}\n")


def _params(**extra) -> dict:
    return {"script_path": "run.mjs", "run_id": RUN, HALTED_RECORD_PARAM: HALT, **extra}


def _terminal(root: Path, params: dict) -> dict:
    return terminal_commit._handler(params, repo_root=root / ".git")


def _land_run(root: Path, params: dict) -> dict:
    return land_run_halt.land(root, root, params)


WRITERS = pytest.mark.parametrize("call", [_terminal, _land_run], ids=["halted_record_commit", "land_run_halt"])


def _rows(root: Path) -> dict:
    text = (root / PLAN).read_text(encoding="utf-8")
    block = re.search(r"```yaml plan-tasks\n(.*?)```", text, re.S).group(1)
    return {r["id"]: r for r in yaml.safe_load(block)}


@WRITERS
def test_landed_chunk_reads_coded_at_the_halt_sha_in_a_plan_only_second_commit(repo, call):
    _chunks(repo, {"C1": ["a.py"]})

    out = call(repo, _params(**PASSING))

    assert out["committed"] is True, out
    halt_sha = out["sha"]
    stamp = out["lands_chunks_stamp"]
    assert stamp["rows_coded"] == {PLAN: ["C1"]}, stamp
    assert "Lands-Chunks: C1" in _git(repo, "log", "-1", "--format=%B", halt_sha)
    rows = _rows(repo)
    assert rows["C1"]["disposition"] == "coded" and rows["C1"]["disposition_ref"] == halt_sha
    assert rows["C2"]["disposition"] == "open"
    assert _git(repo, "rev-parse", "HEAD") == stamp["coded_sha"] != halt_sha
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == [PLAN]
    assert halt_sha in _git(repo, "rev-list", "HEAD~1").split()


@WRITERS
def test_a_refire_still_resumes_at_the_ledger_after_the_stamp(repo, call):
    _chunks(repo, {"C1": ["a.py"]})
    out = call(repo, _params(**PASSING))
    assert out["lands_chunks_stamp"]["rows_coded"] == {PLAN: ["C1"]}, out

    state = emit.resume_landed_state(repo, PLAN)
    rows = list(_rows(repo).values())

    assert out["sha"] in state.landing_shas["C1"]
    assert sizing_fire._terminal_coded(rows, state) is False


@WRITERS
def test_a_halt_with_no_landed_chunk_makes_no_second_commit(repo, call):
    out = call(repo, _params())

    assert out["committed"] is True, out
    assert "lands_chunks_stamp" not in out
    assert _git(repo, "rev-parse", "HEAD") == out["sha"]
    assert _rows(repo)["C1"]["disposition"] == "open"


@WRITERS
def test_a_non_ledger_halt_stamps_nothing(repo, call):
    _chunks(repo, {"C1": ["a.py"]})
    params = _params(**PASSING)
    params[HALTED_RECORD_PARAM] = {"halted": "refusal", "reason": "x", "plan": PLAN}

    out = call(repo, params)

    assert out["committed"] is True, out
    assert "lands_chunks_stamp" not in out
    assert _rows(repo)["C1"]["disposition"] == "open"


@WRITERS
def test_a_stamp_failure_is_named_and_the_halt_commit_stands(repo, call, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("stamp exploded")

    monkeypatch.setattr(terminal_commit, "_stamp_coded_commit", boom)
    _chunks(repo, {"C1": ["a.py"]})

    out = call(repo, _params(**PASSING))

    assert out["committed"] is True, out
    assert "stamp exploded" in out["lands_chunks_stamp"]["coded_stamp_error"]
    assert _rows(repo)["C1"]["disposition"] == "open"
    assert out["sha"] in _git(repo, "rev-list", "HEAD").split()
