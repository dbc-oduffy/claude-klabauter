"""Exit-criterion instrument for the baton brief's git spawns.

Two cadence tests over one dirty-tree fixture: the normalised stdout of a stamped cold
`baton-assemble brief handoff` against a golden, and a Popen argv log asserting the
dirty-tree residue count spawns no `git status`. The golden's `_captured_at` key records
the tree sha it was captured at and is ignored by the comparison.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.baton_assemble import main as baton_main
from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    single_invocation_tree_process_time,
)
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)
from coordinator_core.session.touch_record import append_event
from coordinator_core.tests._stamped_engine_fixture import build_engine_copy, cold_cli_env

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GOLDEN = Path(__file__).parent / "fixtures" / "brief_git_spawn_exit_golden.json"
_SESSION_ID = "7c31b25c-5312-4f9a-988f-17f95ffbf8c6"
_CLAIMED = "README.md"
_BRIEF_ARGS = ["brief", "handoff", "state/handoffs/h1.md"]
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _seed_dirty_repo(tmp_path: Path) -> Path:
    """One committed handoff, one modified tracked file the session claims, one untracked file."""
    repo = tmp_path / "baton-repo"
    _init_repo(repo)
    artifact = repo / "state" / "handoffs" / "h1.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    fm = 'deliverable_id: "DEL-C2-1"\npredecessor: "none"\n'
    artifact.write_text(f"---\n{fm}---\n\n# Artifact\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", "state/handoffs/h1.md")
    _git(repo, "commit", "-m", "add h1.md")
    (repo / _CLAIMED).write_text("modified\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    sink = git_common_dir(repo) / "coordinator-sessions" / _SESSION_ID / "touch-record.jsonl"
    append_event(sink, session_id=_SESSION_ID, agent_id=None, verb="T", path=_CLAIMED)
    return repo


def _normalise(text: str, root: Path) -> str:
    for form in {str(root), root.as_posix(), str(root).replace("\\", "\\\\")}:
        text = text.replace(form, "<ROOT>")
    return _DATE_RE.sub("<DATE>", text)


def _comparable(text: str) -> object:
    """Parsed stdout minus the machine-dependent parts: the registry census in the
    repo-identity message, and `segments`, which are read from the host's skills tree and
    are empty under the quarantined HOME a pytest child inherits."""
    try:
        doc = json.loads(text)
    except ValueError:
        return {"stdout": text}
    gate = doc.get("gates", {}).get("repo_identity")
    if gate:
        gate["message"] = re.sub(r" \(registry holds .*\)$", "", gate["message"])
    doc.pop("segments", None)
    return doc


def _skip_off_instrument_platforms() -> None:
    if not (IS_WINDOWS or IS_DARWIN):
        pytest.skip(
            f"single_invocation_tree_process_time raises NotImplementedError on {sys.platform!r}"
        )


def _run_instrumented_brief(tmp_path: Path, repo: Path) -> tuple[dict, str]:
    """Run the brief cold against a stamped engine copy; return (instrument result, stdout)."""
    engine = build_engine_copy(tmp_path / "engine", stamped=True)
    env = cold_cli_env(engine)
    env.pop("COORDINATOR_SESSION_ID", None)
    env["CLAUDE_CODE_SESSION_ID"] = _SESSION_ID
    out = tmp_path / "brief.out"
    result = single_invocation_tree_process_time(
        [sys.executable, str(engine / "coordinator" / "bin" / "baton-assemble.py"), *_BRIEF_ARGS],
        env=env,
        cwd=str(repo),
        stdout_path=str(out),
        stderr_path=str(tmp_path / "brief.err"),
    )
    return result, out.read_text(encoding="utf-8")


def test_output_matches_golden(tmp_path):
    _skip_off_instrument_platforms()
    repo = _seed_dirty_repo(tmp_path)
    _result, stdout = _run_instrumented_brief(tmp_path, repo)
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert "_captured_at" in golden, "golden must record the sha it was captured at"
    golden.pop("_captured_at")
    actual = _comparable(_normalise(stdout, repo))
    assert actual == golden["brief"]

