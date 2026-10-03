"""A gated em-performed row is withheld, carried into the run result, and reported incomplete."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER
from coordinator_core.ops.dispatch_emit.ask_stage import _handler as stage
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN_REL = "docs/plans/2026-10-04-gated-em-row.md"
PLAN = """---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: N1
  title: normal
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: V1-gate
  title: gated verification
  change_kind: verification
  surface: other repo
  performer: em
  writes: []
  external_gate:
    - owner_repo: other-repo
      requires: landed-work
      blocks: execution
      cleared: false
      closure_key:
        kind: memo-thread
        id: some-thread
```
"""


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                   **no_console_creationflags())


def test_gated_em_row_is_withheld_and_listed_incomplete(tmp_path):
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)

    manifest = stage({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=root)
    assert "error" not in manifest
    assert [r["id"] for r in manifest["rows"]] == ["N1"]
    assert [(g["id"], g["reason"], g["owner_repo"], g["closure_key"]) for g in manifest["gated"]] == [
        ("V1-gate", "external_gate", "other-repo", {"kind": "memo-thread", "id": "some-thread"})
    ]

    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text("a\n", encoding="utf-8")
    (root / "run.mjs").write_text(
        f"// emitted\n{ASK_MANIFEST_MARKER}{manifest['run_dir']}/manifest.json\n", encoding="utf-8"
    )
    out = terminal_commit._handler(
        {
            "script_path": "run.mjs",
            "incomplete_chunks": ["V1-gate"],
            "inline_review": {"integration_stem": "s", "slices": 1, "fixes": 0},
        },
        repo_root=root / ".git",
    )
    assert out["committed"] is True
    assert out["chunks_committed"] == ["N1"]
    assert out["unmarked_incomplete"] == ["V1-gate"]
    assert out["incomplete_reasons"] == {"V1-gate": "external_gate"}
    assert out["stranded"] == {}
    json.dumps(out)
