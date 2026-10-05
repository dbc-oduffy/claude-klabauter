"""brief() refuses a main/master/detached checkout before building any directive.

d_step3_consolidate refuses those checkouts too, but no committing directive
depends on it, so apply() once committed and pushed to main (example-game-repo,
2026-09-22). apply() recomputes brief() and runs nothing on a refusal.
"""

from __future__ import annotations

import pytest

from coordinator_core.workday_complete import brief as brief_mod


@pytest.mark.parametrize(
    ("head", "shown"),
    [("main", "'main'"), ("master", "'master'"), ("HEAD", "detached HEAD"), (None, "detached HEAD")],
)
def test_protected_checkout_is_refused(monkeypatch, head, shown):
    monkeypatch.setattr(brief_mod, "head_branch", lambda _repo: head)
    rc, envelope = brief_mod.brief()
    assert rc == int(brief_mod.WorkdayExitCode.BUSINESS_FAIL)
    assert shown in envelope["error"]
    assert "directives" not in envelope


def test_apply_runs_nothing_on_a_protected_checkout(monkeypatch):
    from coordinator_core.workday_complete import apply as apply_mod

    monkeypatch.setattr(brief_mod, "head_branch", lambda _repo: "main")
    ran = []
    monkeypatch.setattr(apply_mod, "_execute_directives", lambda *a, **k: ran.append(a) or (0, {}))
    rc, report = apply_mod.apply()
    assert ran == []
    assert rc != 0
    assert "'main'" in report["error"]
