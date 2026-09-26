from __future__ import annotations

import contextlib
import importlib.util
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module():
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
    def __init__(self, would_release, would_reclaim, dispositions=None, would_undetermined=0):
        self.would_release = would_release
        self.would_reclaim = would_reclaim
        self.dispositions = dispositions or []
        self.would_undetermined = would_undetermined


class _FakeMemoDisposition:
    def __init__(self, path, holder, verdict, detail):
        self.path = path
        self.holder = holder
        self.verdict = verdict
        self.detail = detail


class _FakeMemoSurveyResult:
    def __init__(self, would_release, dispositions=None):
        self.would_release = would_release
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
    monkeypatch.setattr(mod, "apply_dispositions", lambda dispositions: ([], [], []))

    rc = mod.main([])
    assert rc == 0
    assert "test stand-in" in capsys.readouterr().err


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


def test_default_applies_dispositions_from_survey(monkeypatch, capsys):
    mod = _load_module()
    _patch_resolver(mod, monkeypatch)

    dispositions = [_FakeDisposition("state/handoffs/a.md", "dead1", "release", "detail")]
    monkeypatch.setattr(mod, "survey", lambda repo_root: _FakeSurveyResult(1, 0, dispositions))

    apply_calls = []
    monkeypatch.setattr(
        mod, "apply_dispositions",
        lambda passed: (apply_calls.append(passed), (["state/handoffs/a.md"], [], []))[1],
    )

    rc = mod.main([])
    assert rc == 0
    assert apply_calls == [dispositions]
    out = capsys.readouterr().out
    assert "[dry-run]" not in out


def test_retained_reclaim_shipped_row_is_not_declared_as_a_write(monkeypatch, capsys):
    mod = _load_module()
    _patch_resolver(mod, monkeypatch)

    dispositions = [
        _FakeDisposition("state/handoffs/a.md", "dead1", "reclaim_shipped", "detail", sha="deadbeef"),
    ]
    monkeypatch.setattr(mod, "survey", lambda repo_root: _FakeSurveyResult(0, 1, dispositions))
    monkeypatch.setattr(
        mod, "apply_dispositions",
        lambda passed: ([], ["state/handoffs/a.md"], []),
    )

    declared = []
    monkeypatch.setattr(mod, "declare_write", lambda path: declared.append(path))
    monkeypatch.setattr(
        mod, "recording_declared_writes",
        lambda cwd=None: contextlib.nullcontext(),
    )

    rc = mod.main([])
    assert rc == 0
    assert declared == []


def test_apply_failure_is_reported_and_exits_one(monkeypatch, capsys):
    mod = _load_module()
    _patch_resolver(mod, monkeypatch)

    dispositions = [_FakeDisposition("state/handoffs/a.md", "dead1", "release", "detail")]
    monkeypatch.setattr(mod, "survey", lambda repo_root: _FakeSurveyResult(1, 0, dispositions))
    monkeypatch.setattr(
        mod, "apply_dispositions",
        lambda passed: ([], [], ["state/handoffs/a.md: unclaim-handoff failed: rc=3"]),
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
        lambda passed: (apply_calls.append(passed), ([], [], []))[1],
    )

    rc = mod.main([])
    assert rc == 0
    assert apply_calls == [[]]
    assert "would_release=0 would_reclaim=0" in capsys.readouterr().out


def test_dry_run_prints_memo_would_release_and_never_applies(monkeypatch, capsys):
    mod = _load_module()
    _patch_resolver(mod, monkeypatch)

    monkeypatch.setattr(mod, "survey", lambda repo_root: _FakeSurveyResult(0, 0))
    monkeypatch.setattr(
        mod, "apply_dispositions",
        lambda dispositions: (_ for _ in ()).throw(
            AssertionError("--dry-run must never call apply_dispositions"))
    )

    memo_dispositions = [
        _FakeMemoDisposition("state/cross-repo/inbox/a.md", "dead1", "release", "detail"),
    ]
    monkeypatch.setattr(
        mod, "memo_survey",
        lambda repo_root: _FakeMemoSurveyResult(1, memo_dispositions),
    )
    monkeypatch.setattr(
        mod, "memo_apply_dispositions",
        lambda dispositions: (_ for _ in ()).throw(
            AssertionError("--dry-run must never call memo_apply_dispositions"))
    )

    rc = mod.main(["--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "memo_would_release=1" in out
    assert "state/cross-repo/inbox/a.md" in out
    assert "[dry-run] no changes made" in out


def test_default_applies_memo_dispositions_from_memo_survey(monkeypatch, capsys):
    mod = _load_module()
    _patch_resolver(mod, monkeypatch)

    monkeypatch.setattr(mod, "survey", lambda repo_root: _FakeSurveyResult(0, 0, []))
    monkeypatch.setattr(mod, "apply_dispositions", lambda passed: ([], [], []))

    memo_dispositions = [
        _FakeMemoDisposition("state/cross-repo/inbox/a.md", "dead1", "release", "detail"),
    ]
    monkeypatch.setattr(
        mod, "memo_survey", lambda repo_root: _FakeMemoSurveyResult(1, memo_dispositions),
    )
    memo_apply_calls = []
    monkeypatch.setattr(
        mod, "memo_apply_dispositions",
        lambda passed: (memo_apply_calls.append(passed),
                         (["state/cross-repo/inbox/a.md"], []))[1],
    )

    rc = mod.main([])
    assert rc == 0
    assert memo_apply_calls == [memo_dispositions]
    assert "[dry-run]" not in capsys.readouterr().out


def test_memo_apply_failure_is_reported_and_exits_one(monkeypatch, capsys):
    mod = _load_module()
    _patch_resolver(mod, monkeypatch)

    monkeypatch.setattr(mod, "survey", lambda repo_root: _FakeSurveyResult(0, 0, []))
    monkeypatch.setattr(mod, "apply_dispositions", lambda passed: ([], [], []))

    memo_dispositions = [
        _FakeMemoDisposition("state/cross-repo/inbox/a.md", "dead1", "release", "detail"),
    ]
    monkeypatch.setattr(
        mod, "memo_survey", lambda repo_root: _FakeMemoSurveyResult(1, memo_dispositions),
    )
    monkeypatch.setattr(
        mod, "memo_apply_dispositions",
        lambda passed: ([], ["state/cross-repo/inbox/a.md: release-memo failed: rc=3"]),
    )

    rc = mod.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "release-memo failed: rc=3" in err


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
