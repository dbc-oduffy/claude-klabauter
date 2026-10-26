"""A review-only terminal commit on a plan stamped at an older commit advances the stamp to the commit it landed, clearing the late-delta refusal."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops import review_stamp
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REL = "docs/plan.md"
_SESSION = "11111111-2222-3333-4444-555555555555"
_PLAN = """---
title: p
status: executing
plan_id: pln-x-123456
scope:
  - a.py
prime_exit_criterion:
  text: a.py exists
---

# Plan

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
```
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _review(base: str, files: int) -> dict:
    return {
        "integration_stem": "pln-x-123456.review-wave-bookkeeping", "slices": 1, "fixes": 0,
        "plan_id": "pln-x-123456", "prep_sidecar": None, "wave_sidecar_paths": [],
        "wave_reviews": reviewed()["wave_reviews"],
        "prep": {"run_base_sha": base, "product_files": files, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "PASS", "product_files": files, "claims_unbacked": 0},
        "tests": {"status": "pass", "run": 2, "failed": 0, "sidecar": "t.md"},
        "criterion": {"status": "met", "observation": "a.py exists at HEAD", "sidecar": "j.md"},
    }


def _land(
    repo: Path, request: CommitRequest, inline_review: dict, name: str, session: str = _SESSION, **extra
) -> dict:
    (repo / name).write_text(f"// emitted\n{render_marker(request)}\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": name, "incomplete_chunks": [], "session_id": session, "inline_review": inline_review, **extra},
        repo_root=repo / ".git",
    )


def _stamp(repo: Path) -> dict:
    split = split_frontmatter((repo / _REL).read_text(encoding="utf-8"))
    return yaml.safe_load(split.fm_text)["review_stamp"]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / _REL).write_text(_PLAN, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "plan")
    return root


def test_a_review_only_close_moves_the_stamp_to_the_reviewed_head(repo):
    chunk = ChunkCommit(id="C3", title="t3", paths=("a.py",))
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    first = _land(repo, CommitRequest(chunks=(chunk,), plan_path=_REL), _review("a" * 40, 1), "run1.mjs")
    assert first["review_stamp"] == "minted", first
    old = _stamp(repo)["terminal_commit_sha"]
    assert review_stamp.check(repo / _REL, repo, supersession=True) is None

    (repo / "a.py").write_text("a\nlate\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "late delta")
    assert "later commit touches a declared write" in review_stamp.check(repo / _REL, repo, supersession=True)

    request = CommitRequest(chunks=(chunk,), plan_path=_REL, review_only=True, review_open_rows=(), review_run_base=old)
    out = _land(repo, request, _review(old, 0), "run2.mjs", session="99999999-2222-3333-4444-555555555555", allow_empty=True)

    stamp = _stamp(repo)
    assert out["committed"] is True, out
    assert stamp["terminal_commit_sha"] == out["sha"] != old, (out, stamp)
    assert stamp["terminal_tree_sha"] == _git(repo, "log", "-1", "--format=%T", out["sha"])
    assert review_stamp.check(repo / _REL, repo, supersession=True) is None

    # The EM's re-mint walks the Inline-Review trailers: the zero-file review-only run must win.
    remint = review_stamp.mint(repo / _REL, repo, build_test_path=None)
    assert remint["terminal_commit_sha"] == out["sha"], remint
