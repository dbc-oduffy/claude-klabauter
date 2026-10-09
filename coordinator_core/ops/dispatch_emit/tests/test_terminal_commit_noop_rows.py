"""A done row with no hunk in the run's commit closes coded (no-change detail), and a refused plan flip names its reason at the reply's top level."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from coordinator_core.execute_plan_assemble.row_spans import _row_disposition
from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops.dispatch_emit import terminal_commit
import pytest

# The _git helper drives a real git repo; needs a real process.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PLAN = """---
title: p
---

# Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: sweep
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
- id: C2
  title: conditional no-op
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
```
"""

_SHA = "a" * 40


def _rows(text: str) -> dict:
    return {r["id"]: r for r in yaml.safe_load(locate_fenced_block(text).body)}


def test_flip_stamps_noop_rows_with_the_no_change_detail():
    text, flipped = terminal_commit._flip_rows_coded(
        _PLAN, {"C1", "C2"}, _SHA, {"C2": terminal_commit._NOOP_DETAIL}
    )
    rows = _rows(text)
    assert flipped == ["C1", "C2"]
    assert rows["C2"]["disposition"] == "coded"
    assert rows["C2"]["disposition_ref"] == _SHA
    assert rows["C2"]["disposition_detail"] == terminal_commit._NOOP_DETAIL
    assert "disposition_detail" not in rows["C1"]


def test_coded_stamp_closes_noop_rows_beside_hunk_rows(tmp_path, monkeypatch):
    rel = "docs/plan.md"
    (tmp_path / rel).parent.mkdir()
    (tmp_path / rel).write_text(_PLAN, encoding="utf-8", newline="\n")
    monkeypatch.setattr(terminal_commit, "_changed_paths", lambda _root, paths: set(paths))
    seen: list = []

    def commit_v2(params: dict, repo_root: Path) -> dict:
        seen.append(params)
        return {"committed": True, "sha": "f" * 40}

    out = terminal_commit._stamp_coded_commit(
        commit_v2, tmp_path, tmp_path, {rel: {"C1"}}, _SHA, None, noop_rows={rel: {"C2"}}
    )
    assert out["rows_coded"] == {rel: ["C1", "C2"]}
    rows = _rows((tmp_path / rel).read_text(encoding="utf-8"))
    assert all(_row_disposition(r) == "coded" for r in rows.values())
    assert rows["C2"]["disposition_detail"] == terminal_commit._NOOP_DETAIL
    assert seen[0]["message"] == f"mark 2 rows coded ({_SHA[:7]})"


def test_a_row_already_resolved_is_left_alone(tmp_path, monkeypatch):
    rel = "docs/plan.md"
    (tmp_path / rel).parent.mkdir()
    text = _PLAN.replace("surface: a.py\n  disposition: open", "surface: a.py\n  disposition: wont_do\n  disposition_detail: declined", 1)
    (tmp_path / rel).write_text(text, encoding="utf-8", newline="\n")
    monkeypatch.setattr(terminal_commit, "_changed_paths", lambda _root, paths: set(paths))
    out = terminal_commit._stamp_coded_commit(
        lambda p, r: {"committed": True, "sha": "f" * 40}, tmp_path, tmp_path, {}, _SHA, None,
        noop_rows={rel: {"C1", "C2"}},
    )
    rows = _rows((tmp_path / rel).read_text(encoding="utf-8"))
    assert out["rows_coded"] == {rel: ["C2"]}
    assert rows["C1"]["disposition"] == "wont_do"


_MET_PLAN = """---
title: p
review_stamp:
  criterion:
    status: met
    observation: it holds
---

body
"""


def test_refused_plan_flip_carries_its_reason_at_top_level(tmp_path, monkeypatch):
    import coordinator_core.archive_stamp as archive_stamp

    (tmp_path / "plan.md").write_text(_MET_PLAN, encoding="utf-8", newline="\n")

    def refuse(*_a, **_k) -> int:
        print("open spine rows: C2", file=sys.stderr)
        return 1

    monkeypatch.setattr(archive_stamp, "cs_stamp_plan_implemented", refuse)
    out = terminal_commit._stamp_plan_implemented(tmp_path, "plan.md", _SHA)
    assert out["plan_status"] == "refused"
    assert "open spine rows: C2" in out["plan_status_reason"]


def test_driver_prompt_relays_the_verdict_line():
    from coordinator_core.ops.workflow_fire.fire import _PROMPT_TEMPLATE

    prompt = _PROMPT_TEMPLATE.format(script_path="s.mjs")
    for key in ("criterion_status", "plan_status", "plan_status_reason"):
        assert key in prompt


def _git(root: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def test_rows_landed_by_checkpoint_commits_close_at_the_checkpoint_sha(tmp_path, monkeypatch):
    """fifa's shape: C0/C1 land in a checkpoint commit, C2 in the terminal commit."""
    from types import SimpleNamespace

    rel = "docs/plan.md"
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs").mkdir()
    rows = "".join(
        f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {i.lower()}.py\n  disposition: open\n  deferred: false\n"
        for i in ("C0", "C1", "C2", "C3")
    )
    (tmp_path / rel).write_text(f"---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    for f in ("c0.py", "c1.py"):
        (tmp_path / f).write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(
        tmp_path, "commit", "-qm",
        f"checkpoint(wave 1): 2 rows — C0, C1\n\nCheckpoint-Plan: {rel}\nCheckpoint-Base: {base}",
    )
    checkpoint = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "c2.py").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "terminal")
    terminal = _git(tmp_path, "rev-parse", "HEAD")

    chunks = [SimpleNamespace(id=i, paths=[f"{i.lower()}.py"]) for i in ("C0", "C1", "C3")]
    attributed = terminal_commit._checkpoint_attribution(tmp_path, terminal, rel, chunks)
    assert attributed == {"C0": checkpoint, "C1": checkpoint}

    monkeypatch.setattr(terminal_commit, "_changed_paths", lambda _root, paths: set(paths))
    out = terminal_commit._stamp_coded_commit(
        lambda p, r: {"committed": True, "sha": "f" * 40}, tmp_path, tmp_path,
        {rel: {"C2"}}, terminal, None,
        noop_rows={rel: {"C3"}}, checkpoint_rows={checkpoint: {rel: {"C0", "C1"}}},
    )
    assert out["rows_coded"] == {rel: ["C0", "C1", "C2", "C3"]}
    by_id = _rows((tmp_path / rel).read_text(encoding="utf-8"))
    assert by_id["C0"]["disposition_ref"] == by_id["C1"]["disposition_ref"] == checkpoint
    assert "disposition_detail" not in by_id["C0"]
    assert by_id["C2"]["disposition_ref"] == terminal
    assert by_id["C3"]["disposition_ref"] == terminal
    assert by_id["C3"]["disposition_detail"] == terminal_commit._NOOP_DETAIL
    assert all(_row_disposition(r) == "coded" for r in by_id.values())
