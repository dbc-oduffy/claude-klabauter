"""
coordinator_core.ops.tests.test_cascade_retract — pytest for the
"deliverable.cascade_retract" op (C6d: docs/plans/2026-08-04-terminal-state-propagation-join-keys.md, AC6f).

Fires a real `deliverable.cascade_terminal` advance first (same git-repo harness as
`test_deliverable_cascade.py`), then exercises retraction against the resulting
uncommitted write -- the gate this chunk's dispatch names explicitly: "fire a
cascade, revise, assert every artifact it can safely revert DOES, and every artifact
it cannot IS NAMED."

Run (from repo root): python3 -m pytest coordinator_core/ops/tests/test_cascade_retract.py -q
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.ops.cascade_retract as retract_mod
import coordinator_core.ops.deliverable_cascade as cascade_mod
import coordinator_core.ops.handoff_children  # noqa: F401 — fires @register_op side effect
import coordinator_core.ops.handoff_transition  # noqa: F401 — fires @register_op side effect
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

_advance = cascade_mod._handler
_retract = retract_mod._handler

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_DEFAULT_TEST_SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _git(
    repo: Path, *args: str, session_id: Optional[str] = _DEFAULT_TEST_SESSION_ID
) -> subprocess.CompletedProcess:
    """Mirrors test_deliverable_cascade.py's own `_git` helper — appends a
    `Session-Id:` trailer to every `commit -m` call so `stamp_shipped_in`'s
    ownership guard resolves the commit as the calling session's own."""
    args_list = list(args)
    if (
        len(args_list) >= 3
        and args_list[0] == "commit"
        and args_list[1] == "-m"
        and session_id is not None
        and "Session-Id:" not in args_list[2]
    ):
        args_list[2] = f"{args_list[2]}\n\nSession-Id: {session_id}"
    return subprocess.run(
        ["git", "-C", str(repo), *args_list],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_handoff(
    repo: Path,
    name: str,
    *,
    status: str = "open",
    deployment_state: str = "ready_to_fire",
    deliverable_id: str = "dlv-test-000000",
    predecessor: str = "none",
    extra: str = "",
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
        f'predecessor: "{predecessor}"\n'
        f"deployment_state: {deployment_state}\n"
        f"deliverable_id: {deliverable_id}\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _advance_run(params: dict, repo_root: Path) -> dict:
    return asyncio.run(_advance(params, repo_root=repo_root))


def _retract_run(params: dict, repo_root: Path) -> dict:
    return _retract(params, repo_root=repo_root)


def _fm_field(path: Path, key: str) -> Optional[str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field(split.fm_text, key)


def _seed_with_scope(repo: Path, name: str, deliverable_id: str) -> Path:
    touched = repo / "coordinator" / "bin" / f"{name}.sh"
    touched.parent.mkdir(parents=True, exist_ok=True)
    touched.write_text("#!/bin/sh\n", encoding="utf-8")
    _git(repo, "add", str(touched.relative_to(repo)))
    _git(repo, "commit", "-m", f"touch {name}")
    return _seed_handoff(
        repo,
        f"{name}.md",
        deliverable_id=deliverable_id,
        extra=f"scope:\n  - {touched.relative_to(repo)}\n",
    )


class TestHappyPathRevert:
    def test_reverts_an_advanced_uncommitted_candidate(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_with_scope(repo, "h1", "dlv-ret-111111")
        original_text = hp.read_text(encoding="utf-8")

        advanced = _advance_run(
            {"deliverable_id": "dlv-ret-111111", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )
        assert advanced["exit_code"] == 0
        assert _fm_field(hp, "deployment_state") == "shipped"

        result = _retract_run({"deliverable_id": "dlv-ret-111111"}, repo_root=repo / ".git")

        assert result["exit_code"] == 0
        assert len(result["reverted"]) == 1
        assert result["refused"] == []
        assert hp.read_text(encoding="utf-8") == original_text
        assert _fm_field(hp, "deployment_state") == "ready_to_fire"
        assert _fm_field(hp, "advanced_by") is None
        assert _fm_field(hp, "shipped_in") is None

    def test_second_retract_is_loud_not_silent(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_with_scope(repo, "h1", "dlv-ret-222222")

        _advance_run(
            {"deliverable_id": "dlv-ret-222222", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )
        first = _retract_run({"deliverable_id": "dlv-ret-222222"}, repo_root=repo / ".git")
        assert first["exit_code"] == 0

        # advanced_by is gone after revert -- provenance index has nothing left to
        # find, mirroring AC6i's own idempotency-by-construction shape.
        second = _retract_run({"deliverable_id": "dlv-ret-222222"}, repo_root=repo / ".git")
        assert second["exit_code"] == 1
        assert second["candidates_matched"] == 0
        assert "dlv-ret-222222" in second["error"]


class TestTouchedSinceIsRefusedAndNamed:
    def test_hand_edited_body_since_the_cascade_is_refused(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_with_scope(repo, "h1", "dlv-ret-333333")

        _advance_run(
            {"deliverable_id": "dlv-ret-333333", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )
        assert _fm_field(hp, "deployment_state") == "shipped"

        # A human hand-edits the body after the cascade fired -- not a cascade field.
        text = hp.read_text(encoding="utf-8")
        hp.write_text(text + "\nHand-edited by a human after the cascade.\n", encoding="utf-8")

        result = _retract_run({"deliverable_id": "dlv-ret-333333"}, repo_root=repo / ".git")

        assert result["exit_code"] == 1
        assert result["reverted"] == []
        assert len(result["refused"]) == 1
        assert "touched since" in result["refused"][0]["reason"]
        # Refused, not reverted -- the hand-edit must survive untouched.
        assert "Hand-edited by a human" in hp.read_text(encoding="utf-8")
        assert _fm_field(hp, "deployment_state") == "shipped"

    def test_already_committed_cascade_write_is_refused_and_named(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_with_scope(repo, "h1", "dlv-ret-444444")

        _advance_run(
            {"deliverable_id": "dlv-ret-444444", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )
        # EM commits the cascade write before deciding to retract -- HEAD now IS
        # the post-cascade state; nothing uncommitted left to diff.
        _git(repo, "add", "state/handoffs/h1.md")
        _git(repo, "commit", "-m", "commit the cascade write")

        result = _retract_run({"deliverable_id": "dlv-ret-444444"}, repo_root=repo / ".git")

        assert result["exit_code"] == 1
        assert result["reverted"] == []
        assert len(result["refused"]) == 1
        assert "already committed" in result["refused"][0]["reason"]
        assert _fm_field(hp, "deployment_state") == "shipped"


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
scope:
  - coordinator/bin/widget.sh
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


class TestRowDepthDivergenceIsRowScopedNotFieldNameScoped:
    """Review: coordinator:code-reviewer Finding 2 — a human edit to a
    DIFFERENT, cascade-untouched row using the same field vocabulary
    (`disposition:`) must be refused, never silently reverted along with
    the cascade's own row-depth write, because that would discard the
    human's edit."""

    def test_human_edit_to_cascade_untouched_sibling_row_is_refused(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        touched = repo / "coordinator" / "bin" / "widget.sh"
        touched.parent.mkdir(parents=True)
        touched.write_text("#!/bin/sh\n", encoding="utf-8")
        _git(repo, "add", str(touched.relative_to(repo)))
        _git(repo, "commit", "-m", "add widget")

        deliverable_id = "dlv-ret-rowdiv-666666"
        hp = repo / "state" / "handoffs" / "baton1.md"
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(_BATON_WITH_SPINE.format(deliverable_id=deliverable_id), encoding="utf-8")
        _git(repo, "add", str(hp.relative_to(repo)))
        _git(repo, "commit", "-m", "add baton1")

        # C1 gets commit evidence -- the cascade will advance it (row depth,
        # AC6g). C2 gets NO commit evidence -- left `unresolved`/untouched by
        # this cascade run, same as test_cascade_baton_rows.py's own fixture.
        marker = repo / "marker.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        with marker.open("a", encoding="utf-8") as fh:
            fh.write("\n<!-- C1 landed -->\n")
        _git(repo, "add", "marker.txt")
        _git(
            repo, "commit", "-m", "C1: land chunk", "-m", f"Deliverable-Id: {deliverable_id}"
        )

        advanced = _advance_run(
            {"deliverable_id": deliverable_id, "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )
        assert advanced["exit_code"] == 0
        assert [a["row_id"] for a in advanced["advanced"][0]["baton_rows_advanced"]] == ["C1"]
        assert [u["row_id"] for u in advanced["advanced"][0]["baton_rows_unresolved"]] == ["C2"]

        # A human independently edits C2 -- the cascade-UNTOUCHED row -- adding a
        # `disposition:` line using the exact field vocabulary the cascade's own
        # C1 write uses, between the advance and the retract.
        text = hp.read_text(encoding="utf-8")
        text = text.replace(
            "- id: C2\n  title: uncommitted row\n  change_kind: code-edit\n  surface: coordinator_core/\n",
            "- id: C2\n  title: uncommitted row\n  change_kind: code-edit\n  "
            "surface: coordinator_core/\n  disposition: coded\n",
        )
        assert text != hp.read_text(encoding="utf-8")
        hp.write_text(text, encoding="utf-8")

        result = _retract_run({"deliverable_id": deliverable_id}, repo_root=repo / ".git")

        assert result["exit_code"] == 1
        assert result["reverted"] == []
        assert len(result["refused"]) == 1
        assert "row this cascade run did not itself advance" in result["refused"][0]["reason"]
        # Refused, not reverted -- the human's edit to C2 must survive untouched,
        # and C1's own cascade write must NOT be silently discarded either.
        final_text = hp.read_text(encoding="utf-8")
        assert "disposition: coded" in final_text
        c1_idx = final_text.index("- id: C1")
        c2_idx = final_text.index("- id: C2")
        assert "advanced_by: " + deliverable_id in final_text[c1_idx:c2_idx]


class TestArchivedSinceIsRefusedAndNamed:
    def test_archived_candidate_is_named_not_silently_absent(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_with_scope(repo, "h1", "dlv-ret-555555")

        _advance_run(
            {"deliverable_id": "dlv-ret-555555", "source_kind": "plan", "source_path": "docs/plans/p.md"},
            repo_root=repo / ".git",
        )

        # Simulate a ceremony archiving the (now-terminal) handoff out of the live
        # corpus -- month-nested, per handoff_archive_dest convention.
        archive_dir = repo / "archive" / "handoffs" / "2026-01"
        archive_dir.mkdir(parents=True)
        archived_path = archive_dir / "h1.md"
        archived_path.write_text(hp.read_text(encoding="utf-8"), encoding="utf-8")
        hp.unlink()

        result = _retract_run({"deliverable_id": "dlv-ret-555555"}, repo_root=repo / ".git")

        assert result["exit_code"] == 1
        assert result["reverted"] == []
        assert len(result["refused"]) == 1
        assert "archived since" in result["refused"][0]["reason"]
        assert str(archived_path) == result["refused"][0]["handoff_path"]


class TestZeroCandidatesIsLoud:
    def test_no_matching_advanced_by_is_exit_1(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "state" / "handoffs").mkdir(parents=True)

        result = _retract_run({"deliverable_id": "dlv-nothing-here"}, repo_root=repo / ".git")

        assert result["exit_code"] == 1
        assert result["candidates_matched"] == 0
        assert result["reverted"] == []
        assert "dlv-nothing-here" in result["error"]

    def test_missing_deliverable_id_param_is_exit_1(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = _retract_run({}, repo_root=repo / ".git")

        assert result["exit_code"] == 1
        assert "required" in result["error"]
