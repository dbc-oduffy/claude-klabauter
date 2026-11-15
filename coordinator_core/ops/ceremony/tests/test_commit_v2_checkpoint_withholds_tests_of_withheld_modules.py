"""A wave checkpoint through `ceremony.commit_v2` never lands a test that imports a withheld module
or names a withheld path, whichever row owns the test; a test of landed code still lands."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_hunk_claims, run_write_prints
from coordinator_core.ops.dispatch_emit.run_hunk_claims import HunkSplit
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "cko-xrow"
PLAN = "docs/plans/p.md"
SPINE = """# plan

## Tasks

```yaml plan-tasks
- id: R1
  title: row one
  disposition: open
  writes:
    - pkg/code.py
- id: R2
  title: row two
  disposition: open
  writes:
    - pkg/other.py
    - pkg/tests/test_imports_code.py
    - pkg/tests/test_names_script.py
    - pkg/tests/test_imports_other.py
    - pkg/tests/test_chain.py
```
"""
FILES = {
    "pkg/code.py": "X = 1\n",
    "pkg/other.py": "Y = 1\n",
    "pkg/tests/test_imports_code.py": "from pkg import code\n",
    "pkg/tests/test_names_script.py": "SCRIPT = 'pkg/code.py'\n",
    "pkg/tests/test_imports_other.py": "import pkg.other\n",
    "pkg/tests/test_chain.py": "from pkg.tests import test_imports_code\n",
}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "docs/plans").mkdir(parents=True)
    (root / "pkg/tests").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / PLAN).write_bytes(SPINE.encode())
    for rel in FILES:
        (root / rel).write_bytes(b"# v1\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    for rel, body in FILES.items():
        (root / rel).write_bytes(body.encode())
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "run1")
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    run_write_prints.record(root, "run1", tuple(FILES))
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks",
        lambda r, run, path, hunks, holders: (
            HunkSplit(unattributed=list(hunks)) if path == "pkg/code.py" else HunkSplit(own=list(hunks))
        ),
    )
    return root


def _landed(repo: Path) -> set:
    seed = _git(["rev-list", "--max-parents=0", "HEAD"], repo).strip()
    return set(_git(["diff", "--name-only", f"{seed}..HEAD"], repo).split()) - {PLAN}


def test_cross_row_tests_importing_or_naming_withheld_code_wait_with_it(repo):
    base = _git(["rev-parse", "HEAD"], repo).strip()
    message = f"checkpoint(wave 1): 2 rows — R1, R2\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base}\n"

    result = commit_v2._handler(
        {"paths": list(FILES), "skip_missing": True, "gone_tracked_as_deleted": True, "message": message},
        repo_root=repo / ".git",
    )

    assert result["committed"] is True, result
    assert _landed(repo) == {"pkg/other.py", "pkg/tests/test_imports_other.py"}
    assert set(result["checkpoint_withheld_paths"]) == {
        "pkg/code.py", "pkg/tests/test_imports_code.py", "pkg/tests/test_names_script.py",
        "pkg/tests/test_chain.py",
    }
    assert result["checkpoint_withheld_reasons"]["pkg/tests/test_imports_code.py"].startswith(
        "test exercises withheld code"
    )


def test_fixpoint_unit_chain_and_non_test_untouched(tmp_path):
    for rel, body in FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body, encoding="utf-8")
    (tmp_path / ".git").mkdir()

    got = checkpoint_hunks.tests_exercising_withheld_code(
        tmp_path, list(FILES), ["pkg/code.py"],
    )

    assert set(got) == {
        "pkg/tests/test_imports_code.py", "pkg/tests/test_names_script.py", "pkg/tests/test_chain.py",
    }
    assert checkpoint_hunks.tests_exercising_withheld_code(tmp_path, list(FILES), []) == {}
