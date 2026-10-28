"""A withheld path a revision voided is not owed by dispatch.record_halted_run_landed."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as hrl
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVISION = (
    "Rows C3 and C4 are void: Remove from this run's diff every hunk that registers it: "
    "guard_message_corpus.py rows and guard-routes docs."
)
_PLAN = """---
register_claims:
  rows: ["R1"]
---
# P

## Tasks

```yaml plan-tasks
- id: C1
  disposition: voided
  disposition_detail: gone
  writes: [pkg/dead.py, pkg/shared.py]
- id: C2
  disposition: open
  writes: [pkg/shared.py]
```
"""

WITHHELD = [
    "coordinator_core/bash_guards/tests/guard_message_corpus.py",
    "coordinator_core/bash_guards/tests/fixtures/guard_render_baseline.json",
    "docs/reference/guard-routes/README.md",
]


def _sizing(root: Path, anchor: str, status: str = "open", text: str = _REVISION) -> str:
    rel = "state/sizings/s.yaml"
    (root / "state/sizings").mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(
        "requirement_register:\n  rows:\n  - id: R2\n"
        f"    source_text: {text!r}\n    anchor: {anchor!r}\n    status: {status}\n",
        encoding="utf-8",
    )
    return rel


def test_revision_voiding_names_file_and_hyphenated_dir(tmp_path):
    marker = {"sizing_rel": _sizing(tmp_path, "revision@2026-10-28T03:36:54Z")}
    got = hrl._voided_paths(tmp_path, marker, WITHHELD)
    assert got == {WITHHELD[0], WITHHELD[2]}


def test_revision_without_void_wording_voids_nothing(tmp_path):
    marker = {"sizing_rel": _sizing(tmp_path, "revision@2026-10-28T03:36:54Z", text="touch guard_message_corpus.py")}
    assert hrl._voided_paths(tmp_path, marker, WITHHELD) == set()


def test_non_revision_row_or_withheld_revision_voids_nothing(tmp_path):
    assert hrl._voided_paths(tmp_path, {"sizing_rel": _sizing(tmp_path, "relayed")}, WITHHELD) == set()
    deferred = {"sizing_rel": _sizing(tmp_path, "revision@2026-10-28T03:36:54Z", status="deferred")}
    assert hrl._voided_paths(tmp_path, deferred, WITHHELD) == set()


def test_plan_path_voided_only_when_every_declaring_row_is_closed(tmp_path):
    (tmp_path / "plan.md").write_text(_PLAN, encoding="utf-8")
    got = hrl._voided_paths(tmp_path, {"plan": "plan.md"}, ["pkg/dead.py", "pkg/shared.py", "pkg/unknown.py"])
    assert got == {"pkg/dead.py"}


def _head_tree(tmp_path, files: dict):
    """A repo whose HEAD tree holds *files*; returns ``(common_dir, load_dir)``."""
    import subprocess

    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.git_state import _parse_tree_entries

    def git(*a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=str(tmp_path),
                              check=True, capture_output=True, text=True,
                              **no_console_creationflags()).stdout.strip()

    git("init", "-q", "-b", "main")
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(text.encode())
    git("add", "-A")
    git("commit", "-q", "-m", "x")
    common = resolve_git_common_dir(tmp_path)

    def load_dir(sha):
        got = read_object(common, sha)
        return _parse_tree_entries(got[1]) if got and got[0] == "tree" else None

    return common, load_dir, git("rev-parse", "HEAD")


def test_withheld_path_with_no_delta_is_not_owed(tmp_path):
    common, load_dir, base = _head_tree(tmp_path, {"wiki/clean.md": "a\n", "wiki/dirty.md": "b\n"})
    (tmp_path / "wiki/dirty.md").write_bytes(b"changed\n")
    got = hrl._clean_at_head(tmp_path, common, load_dir, ["wiki/clean.md", "wiki/dirty.md", "wiki/never.md"], base)
    assert got == {"wiki/clean.md", "wiki/never.md"}


def test_deleted_tracked_path_is_still_owed(tmp_path):
    common, load_dir, base = _head_tree(tmp_path, {"wiki/gone.md": "a\n"})
    (tmp_path / "wiki/gone.md").unlink()
    assert hrl._clean_at_head(tmp_path, common, load_dir, ["wiki/gone.md"], base) == set()


def test_no_recorded_base_owes_every_path(tmp_path):
    common, load_dir, _ = _head_tree(tmp_path, {"wiki/clean.md": "a\n"})
    assert hrl._clean_at_head(tmp_path, common, load_dir, ["wiki/clean.md"], None) == set()


def test_unreadable_sources_void_nothing(tmp_path):
    assert hrl._voided_paths(tmp_path, {"plan": "gone.md", "sizing_rel": "gone.yaml"}, WITHHELD) == set()
    assert hrl._voided_paths(tmp_path, None, WITHHELD) == set()
