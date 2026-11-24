"""Falsifier: no engine op, hook, guard or banner treats a G-EM stamp or verdict as a ruling or gate.

Refusal cases pin that each G-EM authority surface is gone; control cases pin that the legitimate side
of each decision still works.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.hooks import group_em_autofire as gea
from coordinator_core.ops import _major_finding_answer as mfa
from coordinator_core.ops import plan_signoff as signoff
from coordinator_core.win_portability import no_console_creationflags

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "coordinator_core"

PLAN = """---
title: t
created: 2026-10-09
author: a
status: executing
plan_id: pln-x-111111
grouping_approvals:
  ruled_out:
    status: pending
  do:
    status: pending
---

## Tasks

```yaml plan-tasks
- id: A1
  title: first
  change_kind: code-edit
  surface: a.py
  writes: [a.py]
  disposition: open
- id: A2
  title: second
  change_kind: code-edit
  surface: b.py
  writes: [b.py]
  disposition: open
```
"""
CUT = {"rows": ["A2"], "disposition": "wont_do"}
PLAN_REL = "docs/plans/p.md"


@pytest.fixture
def plan_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The signoff write takes its lock under the git common dir, so the root must be a repo.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True, **no_console_creationflags())
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8")
    monkeypatch.setattr(signoff, "main_worktree_root", lambda r: r)
    return tmp_path


def _signoff(root: Path, source: str, **extra):
    params = {"plan_path": PLAN_REL, "target": {"grouping": "ruled_out"}, "source": source,
              "on": "2026-10-09", "cut_set": CUT}
    if source == "pm":
        params["pm_quote"] = "yes do it"
    elif source == "apm":
        params.update(apm_ruling="close A2", ruling_ref=PLAN_REL)
    else:
        params.update(approver="group-em-44", ruling="close A2", ruling_ref=PLAN_REL)
    params.update(extra)
    return signoff._handler(params, root)


@pytest.mark.spawns_process
def test_signoff_apm_control_still_accepted(plan_root: Path) -> None:
    assert _signoff(plan_root, "apm")["applied"] is True


@pytest.mark.spawns_process
def test_signoff_refuses_source_g_em(plan_root: Path) -> None:
    before = (plan_root / PLAN_REL).read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="source"):
        _signoff(plan_root, "g-em")
    assert (plan_root / PLAN_REL).read_text(encoding="utf-8") == before


def test_clearing_kinds_is_fix_row_only() -> None:
    assert mfa.CLEARING_KINDS == ("fix-row",)


def test_fix_row_control_still_clears() -> None:
    row = {"severity": "major", "scope": "default-path", "answered_by": {"kind": "fix-row", "row": "F1"}}
    spine = [{"id": "F1", "disposition": "open"}]
    assert mfa.unanswered([row], spine, None) == []


def test_gem_scope_ruling_does_not_clear_a_major_finding(tmp_path: Path) -> None:
    (tmp_path / "ruling.md").write_text("ruled", encoding="utf-8")
    row = {"severity": "major", "scope": "default-path",
           "answered_by": {"kind": "gem-scope-ruling", "ref": "ruling.md"}}
    assert mfa.unanswered([row], [], tmp_path) == [row]


def _nomination(**over) -> dict:
    return {"claimed": True, "message": "claimed", **over}


def test_autofire_banner_control_keeps_roster_and_watch_arm() -> None:
    text = gea.render_additional_context({"nomination": _nomination(), "roster": [], "digest": {}})
    assert "Group EM ACTIVE" in text and "Roster:" in text and "ARM NOW" in text


def test_autofire_claimed_banner_has_no_gate_or_decision_line() -> None:
    text = gea.render_additional_context({"nomination": _nomination(), "roster": [], "digest": {}}).lower()
    for word in ("gate1", "gate2", "needs_pm_decision", "ruling", "pm's call"):
        assert word not in text, word


def test_autofire_refused_banner_has_no_decision_line() -> None:
    text = gea.render_additional_context({"nomination": {"claimed": False, "message": "busy"}}).lower()
    assert "busy" in text
    for word in ("gate1", "gate2", "needs_pm_decision", "ruling", "pm's call"):
        assert word not in text, word


@pytest.mark.parametrize("stem", ["approvability-calibration", "plan-reversibility-eligibility"])
def test_approve_for_execution_scripts_are_absent(stem: str) -> None:
    present = [p.name for ext in (".py", ".cmd", ".ps1") if (p := REPO / "coordinator/bin" / f"{stem}{ext}").exists()]
    assert present == []


_KILL = "taskkill /T /F /PID 48920"


_SSH = "ssh box-2 hostname"


def _kill_verdict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session_id: str, command: str = _KILL, **ident):
    import json

    from coordinator_core.bash_guards import dispatch
    from coordinator_core.group_em import nomination

    monkeypatch.setattr(nomination, "identify",
                        lambda root: {"state": "live", "holder": {"session_id": "S-gem"}})
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path),
               "session_id": session_id, **ident}
    result = dispatch.evaluate_payload_json(json.dumps(payload))
    items = result if isinstance(result, list) else ([result] if result else [])
    return any(((i or {}).get("hookSpecificOutput") or i or {}).get("permissionDecision") in ("deny", "ask")
               or (i or {}).get("decision") == "block" for i in items)


def test_gem_break_glass_kill_is_still_never_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Removing rulings leaves the G-EM's resource-saving break-glass (process kills) intact.
    assert _kill_verdict(monkeypatch, tmp_path, "S-gem") is False


def test_gem_break_glass_ssh_is_still_never_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _kill_verdict(monkeypatch, tmp_path, "S-gem", _SSH) is False


def test_break_glass_kill_control_other_session_still_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _kill_verdict(monkeypatch, tmp_path, "S-other") is True


_DECIDER_PATTERNS = (
    re.compile(r"(?i)G-EM decides"),
    re.compile(r"(?i)decided by the G-EM"),
    re.compile(r"group-em-grant .*<ruling>"),
)


def _non_test_sources():
    for path in CORE.rglob("*.py"):
        parts = set(path.relative_to(CORE).parts)
        if "tests" in parts or "__pycache__" in parts or path.name.startswith("test_") or path == Path(__file__).resolve():
            continue
        yield path


def test_no_non_test_string_literal_names_the_gem_as_decider() -> None:
    hits = []
    for path in _non_test_sources():
        raw = path.read_text(encoding="utf-8", errors="replace")
        if "<ruling>" not in raw and "decide" not in raw.lower():
            continue
        try:
            tree = ast.parse(raw)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(p.search(node.value) for p in _DECIDER_PATTERNS):
                    hits.append(f"{path.relative_to(REPO).as_posix()}:{node.lineno}")
    assert hits == []
