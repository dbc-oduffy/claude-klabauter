"""
coordinator_core.ops.tests.test_cascade_baton_rows — pytest for AC6g's baton-row
depth (docs/plans/2026-08-04-terminal-state-propagation-join-keys.md § C6g).

Live-substrate note: at authorship time, zero live `state/handoffs/*.md` roadmap-baton
records carry a `## Tasks` fenced spine in their own body (verified by this chunk's own
dispatch). This suite therefore CONSTRUCTS the fixture (a synthetic roadmap-baton handoff
carrying a spine), mirroring `ops/tests/test_opticon_ground_truth_regression.py`'s own
constructed-not-waited-for posture for a gap with no live instance yet.

Run: python3 -m pytest coordinator_core/ops/tests/test_cascade_baton_rows.py -q
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ops.cascade_baton_rows import resolve_baton_rows

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _run_git(args: list[str], root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(["init"], repo)
    _run_git(["config", "commit.gpgsign", "false"], repo)
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _run_git(["add", "README.md"], repo)
    _run_git(["commit", "-m", "init"], repo)


def _commit_chunk(
    repo: Path, marker_path: str, chunk_id: str, *, deliverable_id: Optional[str] = None
) -> None:
    """Mirrors `test_close_out_and_stamp._commit_chunk` exactly (own local
    copy, per this module's own established per-package convention of not
    reaching into another test module's private helper)."""
    marker = repo / marker_path
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("a", encoding="utf-8") as fh:
        fh.write(f"\n<!-- {chunk_id} landed -->\n")
    _run_git(["add", marker_path], repo)
    message_args = ["-m", f"{chunk_id}: land chunk"]
    if deliverable_id:
        message_args += ["-m", f"Deliverable-Id: {deliverable_id}"]
    _run_git(["commit", "-q", *message_args], repo)


_BATON_WITH_SPINE = """---
title: "Test roadmap-baton with row content"
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: none
kind: roadmap-baton
roadmap_id: rm-test
stub_id: stub-01
wave: 1
blocks: []
blocked_by: []
deployment_state: ready_to_fire
deliverable_id: {deliverable_id}
---

# Test baton

## Tasks

```yaml plan-tasks
- id: C1
  title: committed row
  change_kind: code-edit
  surface: coordinator_core/
- id: C2
  title: uncommitted row
  change_kind: code-edit
  surface: coordinator_core/
```
"""

_BATON_NO_SPINE = """---
title: "Test roadmap-baton with no row content"
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: none
kind: roadmap-baton
roadmap_id: rm-test
stub_id: stub-02
wave: 1
blocks: []
blocked_by: []
deployment_state: ready_to_fire
deliverable_id: dlv-flat-000
---

# Test baton

No spine here.
"""


def _seed_baton(repo: Path, name: str, deliverable_id: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BATON_WITH_SPINE.format(deliverable_id=deliverable_id), encoding="utf-8")
    _run_git(["add", str(path.relative_to(repo))], repo)
    _run_git(["commit", "-m", f"add {name}"], repo)
    return path


def _row(text: str, chunk_id: str) -> dict:
    """Pulls one row's flattened key/value lines out of the ## Tasks fence for
    assertion convenience, without a full YAML re-parse (keeps the test
    independent of the module under test's own parsing helpers)."""
    import yaml

    from coordinator_core.frontmatter.body_blocks import locate_fenced_block

    located = locate_fenced_block(text)
    rows = yaml.safe_load(located.body)
    for row in rows:
        if row.get("id") == chunk_id:
            return row
    raise AssertionError(f"row {chunk_id} not found")


class TestEvidenceJoinedRowResolution:
    def test_committed_row_advances_with_full_provenance(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_baton(repo, "baton1.md", "dlv-baton-111111")
        _commit_chunk(repo, "marker.txt", "C1", deliverable_id="dlv-baton-111111")

        result = resolve_baton_rows(hp, "dlv-baton-111111", "2026-08-04T00:00:00Z", repo)

        assert result["spine_status"] == "located"
        assert result.get("error") is None
        assert [a["row_id"] for a in result["advanced"]] == ["C1"]
        assert [u["row_id"] for u in result["unresolved"]] == ["C2"]
        assert "no commit evidence" in result["unresolved"][0]["reason"]

        text = hp.read_text(encoding="utf-8")
        c1 = _row(text, "C1")
        assert c1["disposition"] == "coded"
        assert c1["disposition_ref"]
        assert c1["disposition_detail"]
        assert c1["advanced_by"] == "dlv-baton-111111"
        assert c1["advanced_at"] == "2026-08-04T00:00:00Z"

        c2 = _row(text, "C2")
        assert "disposition" not in c2 or c2.get("disposition") in (None, "open")
        assert "advanced_by" not in c2
        assert "advanced_at" not in c2

    def test_row_with_no_commit_evidence_is_reported_not_flipped(self, tmp_path):
        """The negative half of the write rule: with NEITHER row committed,
        nothing is flipped and both rows are named in `unresolved` -- never
        blanket-resolved just because the owning baton itself advanced."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_baton(repo, "baton2.md", "dlv-baton-222222")

        result = resolve_baton_rows(hp, "dlv-baton-222222", "2026-08-04T00:00:00Z", repo)

        assert result["spine_status"] == "located"
        assert result["advanced"] == []
        assert sorted(u["row_id"] for u in result["unresolved"]) == ["C1", "C2"]

        text_before = hp.read_text(encoding="utf-8")
        c1 = _row(text_before, "C1")
        c2 = _row(text_before, "C2")
        for row in (c1, c2):
            assert "disposition" not in row
            assert "advanced_by" not in row

    def test_absent_spine_is_an_honest_noop(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "state" / "handoffs" / "flat.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_BATON_NO_SPINE, encoding="utf-8")
        _run_git(["add", str(path.relative_to(repo))], repo)
        _run_git(["commit", "-m", "add flat"], repo)

        result = resolve_baton_rows(path, "dlv-flat-000", "2026-08-04T00:00:00Z", repo)

        assert result == {"spine_status": "absent", "advanced": [], "unresolved": []}

    def test_deferred_row_is_never_touched(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        text = _BATON_WITH_SPINE.format(deliverable_id="dlv-baton-333333").replace(
            "- id: C2\n  title: uncommitted row\n  change_kind: code-edit\n  surface: coordinator_core/\n",
            "- id: C2\n  title: deferred row\n  change_kind: code-edit\n  surface: coordinator_core/\n"
            "  deferred: true\n",
        )
        hp = repo / "state" / "handoffs" / "baton3.md"
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(text, encoding="utf-8")
        _run_git(["add", str(hp.relative_to(repo))], repo)
        _run_git(["commit", "-m", "add baton3"], repo)
        # C2 has commit evidence too, but is `deferred: true` -- must still
        # never be touched (mirrors _auto_resolve_committed_open_rows's own
        # deferred-skip rule).
        _commit_chunk(repo, "marker.txt", "C2", deliverable_id="dlv-baton-333333")

        result = resolve_baton_rows(hp, "dlv-baton-333333", "2026-08-04T00:00:00Z", repo)

        assert [u["row_id"] for u in result["unresolved"]] == ["C1"]
        assert result["advanced"] == []

    def test_batched_subject_resolution_covers_every_resolved_row(self, tmp_path):
        """N+1 pin: with BOTH rows committed, both get a real, DISTINCT
        `disposition_detail` from the one batched `git log` call -- not a
        placeholder, and not the same subject cross-applied to the wrong
        row. Guards `_batch_commit_subjects`'s prefix-match reconciliation
        against the classic bug of only ever resolving the first sha."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_baton(repo, "baton5.md", "dlv-baton-555555")
        _commit_chunk(repo, "marker1.txt", "C1", deliverable_id="dlv-baton-555555")
        _commit_chunk(repo, "marker2.txt", "C2", deliverable_id="dlv-baton-555555")

        result = resolve_baton_rows(hp, "dlv-baton-555555", "2026-08-04T00:00:00Z", repo)

        assert sorted(a["row_id"] for a in result["advanced"]) == ["C1", "C2"]
        assert result["unresolved"] == []

        text = hp.read_text(encoding="utf-8")
        c1, c2 = _row(text, "C1"), _row(text, "C2")
        assert c1["disposition_detail"] == "C1: land chunk"
        assert c2["disposition_detail"] == "C2: land chunk"
        assert c1["disposition_detail"] != c2["disposition_detail"]
        assert "subject unavailable" not in c1["disposition_detail"]
        assert "subject unavailable" not in c2["disposition_detail"]

    def test_batch_commit_subjects_falls_back_on_unresolvable_sha(self, tmp_path):
        """Absence contract: `_batch_commit_subjects` must never silently
        drop an unresolvable sha as if it resolved to an empty subject --
        `resolve_baton_rows` must fall back to the same placeholder
        `_commit_subject` used on a lookup failure."""
        from coordinator_core.ops.cascade_baton_rows import _batch_commit_subjects

        repo = tmp_path / "repo"
        _init_repo(repo)

        bogus_sha = "0" * 40
        resolved = _batch_commit_subjects(repo, {bogus_sha})
        assert resolved == {}

        hp = _seed_baton(repo, "baton6.md", "dlv-baton-666666")
        _commit_chunk(repo, "marker.txt", "C1", deliverable_id="dlv-baton-666666")
        result = resolve_baton_rows(hp, "dlv-baton-666666", "2026-08-04T00:00:00Z", repo)
        text = hp.read_text(encoding="utf-8")
        c1 = _row(text, "C1")
        assert "subject unavailable" not in c1["disposition_detail"]
        assert result.get("error") is None


class TestCascadeIntegration:
    """Wires the two chunks together the way `deliverable_cascade._handler` actually
    calls this module: a candidate advances at the handoff-frontmatter depth AND its own
    body rows get evidence-joined resolution in the SAME cascade invocation."""

    def test_full_cascade_advances_baton_and_resolves_committed_row(self, tmp_path, monkeypatch):
        import asyncio

        import coordinator_core.ops.deliverable_cascade as cascade_mod
        import coordinator_core.ops.handoff_children  # noqa: F401
        import coordinator_core.ops.handoff_transition  # noqa: F401

        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "11111111-1111-1111-1111-111111111111")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        touched = repo / "coordinator" / "bin" / "widget.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _run_git(["add", str(touched.relative_to(repo))], repo)
        message_args = [
            "-m", "add widget",
            "-m", "Session-Id: 11111111-1111-1111-1111-111111111111",
        ]
        _run_git(["commit", *message_args], repo)

        hp = _seed_baton(repo, "baton4.md", "dlv-full-444444")
        _commit_chunk(repo, "marker.txt", "C1", deliverable_id="dlv-full-444444")

        text = hp.read_text(encoding="utf-8")
        text = text.replace("deployment_state: ready_to_fire\n", "deployment_state: ready_to_fire\nscope:\n  - coordinator/bin/widget.sh\n")
        hp.write_text(text, encoding="utf-8")
        _run_git(["add", str(hp.relative_to(repo))], repo)
        _run_git(["commit", "-m", "add scope"], repo)

        result = asyncio.run(
            cascade_mod._handler(
                {
                    "deliverable_id": "dlv-full-444444",
                    "source_kind": "plan",
                    "source_path": "docs/plans/p.md",
                },
                repo_root=repo / ".git",
            )
        )

        assert result["exit_code"] == 0
        assert len(result["advanced"]) == 1
        advanced_entry = result["advanced"][0]
        assert [a["row_id"] for a in advanced_entry["baton_rows_advanced"]] == ["C1"]
        assert [u["row_id"] for u in advanced_entry["baton_rows_unresolved"]] == ["C2"]

        final_text = hp.read_text(encoding="utf-8")
        split = split_frontmatter(final_text)
        assert read_fm_field(split.fm_text, "deployment_state") == "shipped"
        c1 = _row(final_text, "C1")
        assert c1["disposition"] == "coded"
        assert c1["advanced_by"] == "dlv-full-444444"
