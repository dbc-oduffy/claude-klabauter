"""
coordinator_core.ops.tests.test_cascade_baton_rows — pytest for AC6g's baton-row
depth (docs/plans/2026-08-04-terminal-state-propagation-join-keys.md § C6g).

Evidence join deleted (P153-C22, docs/plans/2026-09-22-spawn-budget-and-
census.md, R1, DR-344 kill bar): `resolve_baton_rows` no longer has any
mechanical evidence source, so a commit-required `open` row is now ALWAYS
reported `unresolved`, never `advanced` -- this suite pins that permanent
outcome and asserts the deleted `git log` evidence leg is gone (no `git`
subprocess call from this module at all, verified by patching
`subprocess.run` to fail loudly if invoked).

Live-substrate note: at authorship time, zero live `state/handoffs/*.md` roadmap-baton
records carry a `## Tasks` fenced spine in their own body (verified by this chunk's own
dispatch). This suite therefore CONSTRUCTS the fixture (a synthetic roadmap-baton handoff
carrying a spine), mirroring `ops/tests/test_cockpit_ground_truth_regression.py`'s own
constructed-not-waited-for posture for a gap with no live instance yet.

Run: python3 -m pytest coordinator_core/ops/tests/test_cascade_baton_rows.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.cascade_baton_rows import resolve_baton_rows

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


def _seed_baton(root: Path, name: str, deliverable_id: str) -> Path:
    path = root / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BATON_WITH_SPINE.format(deliverable_id=deliverable_id), encoding="utf-8")
    return path


class TestEvidenceJoinDeleted:
    """No fixture in this class ever touches a real git repo -- the whole
    point of the deletion is that `resolve_baton_rows` no longer spawns
    `git` at all. `repo_root` is a plain scratch directory, never an
    initialized repo, and a monkeypatched `subprocess.run` fails the test
    loudly if this module ever calls it."""

    @pytest.fixture(autouse=True)
    def _fail_on_subprocess(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError(
                "resolve_baton_rows spawned a subprocess -- the evidence "
                "join was deleted (P153-C22) and must never shell out to "
                "git for row evidence again"
            )

        monkeypatch.setattr(subprocess, "run", _boom)

    def test_commit_required_open_rows_are_always_unresolved(self, tmp_path):
        """Positive pin for the delete arm: every commit-required `open`
        row is reported `unresolved` with a reason naming the deleted
        join -- never `advanced`, regardless of what the tree contains,
        since there is no longer any evidence source to consult."""
        hp = _seed_baton(tmp_path, "baton1.md", "dlv-baton-111111")

        result = resolve_baton_rows(hp, "dlv-baton-111111", "2026-08-04T00:00:00Z", tmp_path)

        assert result["spine_status"] == "located"
        assert result.get("error") is None
        assert result["advanced"] == []
        assert sorted(u["row_id"] for u in result["unresolved"]) == ["C1", "C2"]
        for entry in result["unresolved"]:
            assert "evidence join was deleted" in entry["reason"] or "commit-log join was deleted" in entry["reason"]

        text = hp.read_text(encoding="utf-8")
        assert "disposition: coded" not in text
        assert "advanced_by" not in text
        assert "advanced_at" not in text

    def test_absent_spine_is_an_honest_noop(self, tmp_path):
        path = tmp_path / "state" / "handoffs" / "flat.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_BATON_NO_SPINE, encoding="utf-8")

        result = resolve_baton_rows(path, "dlv-flat-000", "2026-08-04T00:00:00Z", tmp_path)

        assert result == {"spine_status": "absent", "advanced": [], "unresolved": []}

    def test_deferred_row_is_never_touched(self, tmp_path):
        text = _BATON_WITH_SPINE.format(deliverable_id="dlv-baton-333333").replace(
            "- id: C2\n  title: uncommitted row\n  change_kind: code-edit\n  surface: coordinator_core/\n",
            "- id: C2\n  title: deferred row\n  change_kind: code-edit\n  surface: coordinator_core/\n"
            "  deferred: true\n",
        )
        hp = tmp_path / "state" / "handoffs" / "baton3.md"
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(text, encoding="utf-8")

        result = resolve_baton_rows(hp, "dlv-baton-333333", "2026-08-04T00:00:00Z", tmp_path)

        assert [u["row_id"] for u in result["unresolved"]] == ["C1"]
        assert result["advanced"] == []

    def test_no_commit_required_rows_short_circuits_before_any_row_loop(self, tmp_path):
        """No commit-required chunk ids at all -- `resolve_baton_rows`
        returns clean before even reaching the (now evidence-free) row
        loop."""
        text = _BATON_WITH_SPINE.format(deliverable_id="dlv-baton-444444").replace(
            "- id: C1\n  title: committed row\n  change_kind: code-edit\n  surface: coordinator_core/\n"
            "- id: C2\n  title: uncommitted row\n  change_kind: code-edit\n  surface: coordinator_core/\n",
            "- id: C1\n  title: ruled-out row\n  change_kind: code-edit\n  surface: coordinator_core/\n"
            "  disposition: wont_do\n"
            "  disposition_detail: not needed\n",
        )
        hp = tmp_path / "state" / "handoffs" / "baton7.md"
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(text, encoding="utf-8")

        result = resolve_baton_rows(hp, "dlv-baton-444444", "2026-08-04T00:00:00Z", tmp_path)

        assert result == {"spine_status": "located", "advanced": [], "unresolved": []}
