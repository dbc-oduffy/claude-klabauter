"""test_reap_orphaned_in_flight_handoffs.py — CLI-shell tests for the
rebuilt `reap-orphaned-in-flight-handoffs.py` (C3 of
docs/plans/2026-08-26-two-callers-want-two-numbers-not-a-1301-line-cli.md).

Re-pointing note (C3): the fused implementation this file used to test —
`_batch_commit_timestamps`, `_best_shipped_sha`, `_shipped_orphan_candidate`,
`_has_live_children_exit_code(s)`, and every `main()` predicate-level
disposition case (governed-plan pre-check, P1-P4 ship-detection, live-holder/
live-children skip, pay-for-use index construction) — is DELETED from this
CLI and rebuilt, from the requirement, as `coordinator_core.ops
.reap_in_flight_claims`. That module's own test suite
(`coordinator_core/ops/tests/test_reap_in_flight_claims.py`) re-derives every
one of those predicates directly against `survey()`/`apply_dispositions()` —
duplicating them here against a thin CLI shell that no longer contains the
logic would test nothing this file's own imports can see. Every test below
that exercised one of those predicates is DROPPED as duplicated by that
suite; nothing here re-implements or re-tests survey()'s internals.

What remains, and is unique to this file: the CLI shell itself — argument
parsing (--dry-run / --repo-root / --help / unknown flag), the checked
repo-root resolution and its failure mode, the default-applies /
--dry-run-skips-mutation branch, and the exit-code contract when
`apply_dispositions` reports a failure. These predicates never lived in
`coordinator_core.ops.reap_in_flight_claims` and have no oracle there.

Runs bash-free: `python -m pytest coordinator/bin/tests/test_reap_orphaned_in_flight_handoffs.py -q`
"""
from __future__ import annotations

import importlib.util
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module():
    """Import reap-orphaned-in-flight-handoffs.py as a fresh module object each call."""
    path = os.path.join(SCRIPT_DIR, "reap-orphaned-in-flight-handoffs.py")
    spec = importlib.util.spec_from_file_location(
        "reap_orphaned_in_flight_handoffs_under_test", path
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _FakeDisposition:
    def __init__(self, path, holder, verdict, detail, sha=None):
        self.path = path
        self.holder = holder
        self.verdict = verdict
        self.detail = detail
        self.sha = sha


class _FakeSurveyResult:
    def __init__(self, would_release, would_reclaim, dispositions=None):
        self.would_release = would_release
        self.would_reclaim = would_reclaim
        self.dispositions = dispositions or []


def _patch_resolver(mod, monkeypatch, *, root="/fake/repo", verdict="EXPLICIT"):
    monkeypatch.setattr(
        mod,
        "resolve_checked_repo_root",
        lambda explicit_root=None: (
            root,
            {"verdict": verdict, "message": "test stand-in"},
        ),
    )


# ===========================================================================
# Argument parsing
# ===========================================================================
def test_help_flag_prints_help_and_exits_zero(capsys):
    mod = _load_module()
    rc = mod.main(["--help"])
    assert rc == 0
    assert "Usage" in capsys.readouterr().out


def test_unknown_flag_exits_two(capsys):
    mod = _load_module()
    rc = mod.main(["--bogus"])
    assert rc == 2
    assert "unknown flag" in capsys.readouterr().err


def test_repo_root_requires_a_value(capsys):
    mod = _load_module()
    rc = mod.main(["--repo-root"])
    assert rc == 2
    assert "--repo-root requires a value" in capsys.readouterr().err


def test_repo_root_flag_bypasses_the_resolver(monkeypatch):
    mod = _load_module()
    seen = {}

    def fake_resolve(explicit_root=None):
        seen["explicit_root"] = explicit_root
        return explicit_root, {"verdict": "EXPLICIT", "message": ""}

    monkeypatch.setattr(mod, "resolve_checked_repo_root", fake_resolve)
    monkeypatch.setattr(mod, "survey", lambda repo_root: _FakeSurveyResult(0, 0))
    monkeypatch.setattr(
        mod, "apply_dispositions", lambda dispositions: (_ for _ in ()).throw(
            AssertionError("must not apply on --dry-run"))
    )

    rc = mod.main(["--dry-run", "--repo-root", "/explicit/root"])
    assert rc == 0
    assert seen["explicit_root"] == "/explicit/root"


# ===========================================================================
# Repo-root resolution failure
# ===========================================================================
def test_unresolvable_repo_root_exits_one(monkeypatch, capsys):
    mod = _load_module()
    monkeypatch.setattr(
        mod, "resolve_checked_repo_root",
        lambda explicit_root=None: (None, {"verdict": "UNRESOLVED", "message": ""}),
    )
    rc = mod.main([])
    assert rc == 1
    assert "cannot resolve git repo root" in capsys.readouterr().err


def test_mismatch_verdict_warns_but_proceeds(monkeypatch, capsys):
    mod = _load_module()
    _patch_resolver(mod, monkeypatch, root="/fake/repo", verdict="MISMATCH")
    monkeypatch.setattr(mod, "survey", lambda repo_root: _FakeSurveyResult(0, 0))
    monkeypatch.setattr(mod, "apply_dispositions", lambda dispositions: ([], []))

    rc = mod.main([])
    assert rc == 0
    assert "test stand-in" in capsys.readouterr().err


# ===========================================================================
# --dry-run: survey only, never mutates
# ===========================================================================
def test_dry_run_calls_survey_and_never_apply_dispositions(monkeypatch, capsys):
    mod = _load_module()
    _patch_resolver(mod, monkeypatch)

    survey_calls = []
    monkeypatch.setattr(
        mod, "survey",
        lambda repo_root: (survey_calls.append(repo_root) or _FakeSurveyResult(
            2, 1, [_FakeDisposition("state/handoffs/a.md", "dead1", "release", "detail")]
        )),
    )
    monkeypatch.setattr(
        mod, "apply_dispositions",
        lambda dispositions: (_ for _ in ()).throw(
            AssertionError("--dry-run must never call apply_dispositions"))
    )

    rc = mod.main(["--dry-run"])
    assert rc == 0
    assert len(survey_calls) == 1
    assert str(survey_calls[0]).replace("\\", "/") == "/fake/repo"

    out = capsys.readouterr().out
    assert "would_release=2 would_reclaim=1" in out
    assert "[dry-run] no changes made" in out
    assert "state/handoffs/a.md" in out


# ===========================================================================
# Default (no args): applies every disposition survey() returned
# ===========================================================================
def test_default_applies_dispositions_from_survey(monkeypatch, capsys):
    mod = _load_module()
    _patch_resolver(mod, monkeypatch)

    dispositions = [_FakeDisposition("state/handoffs/a.md", "dead1", "release", "detail")]
    monkeypatch.setattr(mod, "survey", lambda repo_root: _FakeSurveyResult(1, 0, dispositions))

    apply_calls = []
    monkeypatch.setattr(
        mod, "apply_dispositions",
        lambda passed: (apply_calls.append(passed), (["state/handoffs/a.md"], []))[1],
    )

    rc = mod.main([])
    assert rc == 0
    assert apply_calls == [dispositions]
    out = capsys.readouterr().out
    assert "[dry-run]" not in out


def test_apply_failure_is_reported_and_exits_one(monkeypatch, capsys):
    mod = _load_module()
    _patch_resolver(mod, monkeypatch)

    dispositions = [_FakeDisposition("state/handoffs/a.md", "dead1", "release", "detail")]
    monkeypatch.setattr(mod, "survey", lambda repo_root: _FakeSurveyResult(1, 0, dispositions))
    monkeypatch.setattr(
        mod, "apply_dispositions",
        lambda passed: ([], ["state/handoffs/a.md: unclaim-handoff failed: rc=3"]),
    )

    rc = mod.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unclaim-handoff failed: rc=3" in err


def test_no_candidates_applies_empty_list_and_exits_zero(monkeypatch, capsys):
    mod = _load_module()
    _patch_resolver(mod, monkeypatch)

    monkeypatch.setattr(mod, "survey", lambda repo_root: _FakeSurveyResult(0, 0, []))
    apply_calls = []
    monkeypatch.setattr(
        mod, "apply_dispositions",
        lambda passed: (apply_calls.append(passed), ([], []))[1],
    )

    rc = mod.main([])
    assert rc == 0
    assert apply_calls == [[]]
    assert "would_release=0 would_reclaim=0" in capsys.readouterr().out


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
