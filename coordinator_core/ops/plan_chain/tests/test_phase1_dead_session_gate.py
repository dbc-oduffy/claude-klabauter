"""phase1_checks refuses an external_gate addressed to a dead session, never on unknown liveness."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.plan_chain import phase1_checks as p1
from coordinator_core.session import liveness

ROOT = Path("/repo")


def _plan(tmp_path, owner):
    path = tmp_path / "plan.md"
    path.write_text(
        "---\ntitle: t\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        f"- id: C1\n  external_gate:\n    - owner_repo: {owner}\n      condition: c\n```\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def roster(monkeypatch):
    def set_roster(live, *, raises=False):
        def ids(cwd=None):
            if raises:
                raise OSError("unreadable")
            return frozenset(live)

        monkeypatch.setattr(liveness, "live_session_ids", ids)
        monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid in live)

    return set_roster


def test_live_session_passes(tmp_path, roster):
    roster({"coordinator-content-repo-55", "other-aa"})
    assert p1._dead_session_gate(_plan(tmp_path, "coordinator-content-repo-55"), ROOT) is None


def test_dead_session_refused_with_readdress(tmp_path, roster):
    roster({"other-aa"})
    msg = p1._dead_session_gate(_plan(tmp_path, "coordinator-content-repo-55"), ROOT)
    assert "dead session" in msg and "re-address" in msg and "repo coordinator-content-repo" in msg


def test_dead_uuid_session_refused(tmp_path, roster):
    roster({"other-aa"})
    msg = p1._dead_session_gate(_plan(tmp_path, "f4611bb3-286a-4602-b1bf-fa8a7650312a"), ROOT)
    assert "dead session" in msg and "the owning repo" in msg


def test_repo_name_untouched(tmp_path, roster):
    roster({"other-aa"})
    assert p1._dead_session_gate(_plan(tmp_path, "project-rag"), ROOT) is None


def test_unreadable_roster_passes(tmp_path, roster):
    roster(set(), raises=True)
    assert p1._dead_session_gate(_plan(tmp_path, "coordinator-content-repo-55"), ROOT) is None


def test_empty_roster_is_unknown(tmp_path, roster):
    roster(set())
    assert p1._dead_session_gate(_plan(tmp_path, "coordinator-content-repo-55"), ROOT) is None


def test_run_halts_on_dead_session(tmp_path, roster, monkeypatch):
    roster({"other-aa"})
    monkeypatch.setattr(p1, "_write_baseline", lambda *a: None)
    monkeypatch.setattr(p1, "_check_plan", lambda *a: {"verdict": "VALID", "rows": []})
    monkeypatch.setattr(p1, "_spine_read", lambda *a: ([], []))
    h = p1.run(
        p1.ChainManifest("s", "b", None, "pm", ".", "t", {}, "src"),
        _plan(tmp_path, "coordinator-content-repo-55"),
        repo_root=tmp_path,
    )
    assert h.halted_at == "execute" and "re-address" in h.reason
