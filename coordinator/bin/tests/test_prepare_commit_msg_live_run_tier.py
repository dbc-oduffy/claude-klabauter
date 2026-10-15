"""Pins the live-run tier's ladder position in coordinator-prepare-commit-msg.py's
mirrored `_resolve_deliverable_id`: after scope-match, before the plan-claim
ambiguity gate and the pickup tiers; omit (callee returns "") falls through.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HOOK_PATH = Path(__file__).resolve().parent.parent / "coordinator-prepare-commit-msg.py"
SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def hook(monkeypatch):
    spec = importlib.util.spec_from_file_location("pcm_live_run_tier", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_list_held_plan_claims", lambda cwd: [])
    monkeypatch.setattr(mod, "_resolve_deliverable_id_from_scope_match", lambda c, p, cl: "")
    monkeypatch.setattr(mod, "_session_holds_multiple_plan_claims", lambda cl: False)
    monkeypatch.setattr(mod, "_session_holds_multiple_held_pickups", lambda g, s: False)
    monkeypatch.setattr(mod, "_resolve_deliverable_id_at", lambda g, s: "dlv-pickup")
    monkeypatch.setattr(mod, "_resolve_content_root", lambda: "")
    monkeypatch.setattr(mod, "_resolve_deliverable_id_from_claimed_plan", lambda: "")
    monkeypatch.setattr(mod, "_resolve_deliverable_id_from_paths", lambda *a, **k: "")
    return mod


def test_live_run_beats_pickup(hook, monkeypatch):
    monkeypatch.setattr(hook, "_resolve_deliverable_id_from_live_run", lambda c, s, p: "dlv-run")
    assert hook._resolve_deliverable_id(".git", SID, ["a.py"]) == "dlv-run"


def test_ambiguous_live_run_omits_and_ladder_continues(hook, monkeypatch):
    monkeypatch.setattr(hook, "_resolve_deliverable_id_from_live_run", lambda c, s, p: "")
    assert hook._resolve_deliverable_id(".git", SID, ["a.py"]) == "dlv-pickup"


def test_scope_match_wins_over_live_run(hook, monkeypatch):
    monkeypatch.setattr(hook, "_resolve_deliverable_id_from_scope_match", lambda c, p, cl: "dlv-scope")
    monkeypatch.setattr(hook, "_resolve_deliverable_id_from_live_run", lambda c, s, p: "dlv-run")
    assert hook._resolve_deliverable_id(".git", SID, ["a.py"]) == "dlv-scope"


def test_tier0_wins_over_live_run(hook, monkeypatch):
    monkeypatch.setattr(hook, "_resolve_deliverable_id_from_paths", lambda *a, **k: "dlv-t0")
    monkeypatch.setattr(hook, "_resolve_deliverable_id_from_live_run", lambda c, s, p: "dlv-run")
    assert hook._resolve_deliverable_id(".git", SID, ["a.md"]) == "dlv-t0"


def test_live_run_precedes_plan_claim_ambiguity_gate(hook, monkeypatch):
    monkeypatch.setattr(hook, "_session_holds_multiple_plan_claims", lambda cl: True)
    monkeypatch.setattr(hook, "_resolve_deliverable_id_from_live_run", lambda c, s, p: "dlv-run")
    assert hook._resolve_deliverable_id(".git", SID, ["a.py"]) == "dlv-run"


def test_import_failure_degrades_to_empty(hook, monkeypatch):
    monkeypatch.setattr(hook, "_ensure_claude_klabauter_on_syspath", lambda: "/x")
    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "coordinator_core.git.commit_trailers":
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert hook._resolve_deliverable_id_from_live_run(".", SID, ["a.py"]) == ""


def test_hook_reaches_live_run_lookup_through_commit_trailers(hook, monkeypatch):
    import coordinator_core.git.commit_trailers as ct

    seen = []
    monkeypatch.setattr(hook, "_ensure_claude_klabauter_on_syspath", lambda: "/x")
    monkeypatch.setattr(
        ct, "resolve_deliverable_id_from_live_run", lambda c, s, p: seen.append((c, s, p)) or "dlv-run"
    )
    assert hook._resolve_deliverable_id_from_live_run("/cwd", SID, ["a.py"]) == "dlv-run"
    assert seen == [("/cwd", SID, ["a.py"])]


def test_no_paths_or_session_skips_import(hook, monkeypatch):
    monkeypatch.setattr(hook, "_ensure_claude_klabauter_on_syspath", lambda: pytest.fail("bootstrap ran"))
    assert hook._resolve_deliverable_id_from_live_run(".", SID, []) == ""
    assert hook._resolve_deliverable_id_from_live_run(".", "", ["a.py"]) == ""
