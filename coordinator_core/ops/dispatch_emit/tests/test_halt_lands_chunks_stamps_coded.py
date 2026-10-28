"""A halted record commit, ledger-check included, lands run artifacts only and stamps no chunk coded."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import terminal_commit
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


def _call(root: Path, params: dict) -> dict:
    return terminal_commit._handler(params, repo_root=root / ".git")


def _rows(root: Path) -> dict:
    text = (root / PLAN).read_text(encoding="utf-8")
    block = re.search(r"```yaml plan-tasks\n(.*?)```", text, re.S).group(1)
    return {r["id"]: r for r in yaml.safe_load(block)}


def test_ledger_halt_lands_no_product_and_stamps_no_row(repo):
    _chunks(repo, {"C1": ["a.py"]})

    out = _call(repo, _params(**PASSING))

    assert out["committed"] is True, out
    assert out["lands_chunks_stamp"] is None
    assert "landed_chunks" not in out
    assert out["withheld"]["a.py"] == "halt-record-only"
    assert "Lands-Chunks" not in _git(repo, "log", "-1", "--format=%B", out["sha"])
    assert _git(repo, "rev-parse", "HEAD") == out["sha"]
    landed = _git(repo, "show", "--name-only", "--format=", out["sha"]).split()
    assert "a.py" not in landed and "reports/C1.md" not in landed
    assert all(p.startswith(RECORD_DIR_ROOT + "/") for p in landed), landed
    assert "a.py" in _git(repo, "status", "--porcelain", "-uall")
    assert _rows(repo)["C1"]["disposition"] == "open"


def test_a_halt_with_no_finished_chunk_stamps_nothing(repo):
    out = _call(repo, _params())

    assert out["committed"] is True, out
    assert out["lands_chunks_stamp"] is None
    assert _git(repo, "rev-parse", "HEAD") == out["sha"]
    assert _rows(repo)["C1"]["disposition"] == "open"


def test_a_non_ledger_halt_withholds_product_the_same_way(repo):
    _chunks(repo, {"C1": ["a.py"]})
    params = _params(**PASSING)
    params[HALTED_RECORD_PARAM] = {"halted": "refusal", "reason": "x", "plan": PLAN}

    out = _call(repo, params)

    assert out["committed"] is True, out
    assert out["lands_chunks_stamp"] is None
    assert out["withheld"]["a.py"] == "halt-record-only"
    assert "a.py" not in _git(repo, "show", "--name-only", "--format=", out["sha"]).split()
    assert _rows(repo)["C1"]["disposition"] == "open"
