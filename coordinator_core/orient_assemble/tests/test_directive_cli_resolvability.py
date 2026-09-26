
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from coordinator_core.install.substrate import _derive_agent_helper_target_map
from coordinator_core.orient_assemble import CADENCES, brief
from coordinator_core.orient_assemble import (
    readers_branch_reconcile as rbr,
    readers_clean_ops as rco,
    readers_handoff_triage as rht,
    readers_health_reaper as rhr,
)
from coordinator_core.orient_assemble.readers_clean_ops import ReaderResult

#: each reader module's own _SOURCE_PATH uses, one level deeper here since
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENT_BIN = _REPO_ROOT / "coordinator" / "bin"

_CEREMONY_OR_SKILL_NAMES = frozenset(
    {"workday-start", "workweek-start", "workstream-start", "update-docs"}
)


def _real_cli_names() -> frozenset[str]:
    return frozenset(_derive_agent_helper_target_map(_AGENT_BIN))


def _fully_stubbed_brief(monkeypatch, cadence: str, tmp_path: Path) -> dict:
    monkeypatch.setattr(rco, "_read_em_environment", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_scan_addon_health_run", lambda mode: (["RED: x"], 1))
    monkeypatch.setattr(rco, "_read_memo_surface", lambda mode, *, repo_root=None: ReaderResult())
    monkeypatch.setattr(rco, "check_rag_state", lambda: ("stale", 1))

    fake_worktree_path = str(tmp_path / "fake-worktree")
    fake_repo_root = str(tmp_path / "fake-repo")

    class _FakeWorktree:
        path = fake_worktree_path

    class _FakeClassification:
        state = "empty-clean"
        dirty_count = 0

    monkeypatch.setattr(rco, "_wt_repo_root", lambda *, cwd=None: fake_repo_root)
    monkeypatch.setattr(rco, "_wt_active_branch", lambda root: "main")
    monkeypatch.setattr(rco, "_is_agent_worktree", lambda path: True)
    monkeypatch.setattr(rco, "_list_worktrees", lambda root: [_FakeWorktree()])
    monkeypatch.setattr(rco, "classify_worktree", lambda path, ref: _FakeClassification())

    monkeypatch.setattr(rht, "_cmd_stale_plans", lambda args: (print("stale: p1"), 0)[1])
    monkeypatch.setattr(rht, "_cmd_ready", lambda args: (print("ready: h1"), 0)[1])
    monkeypatch.setattr(rht, "_cmd_awaiting_gate", lambda args: (print("gated: h2"), 0)[1])
    monkeypatch.setattr(
        rht,
        "list_orphaned",
        lambda repo_root, threshold_days: {
            "authorized_orphan": [
                {"path": "docs/plans/x.md", "execution_authorized_by": "alice"}
            ],
            "chain_gap": [],
            "parked_count": 0,
            "legacy_unjoinable_count": 0,
            "unrecognized_status": [],
            "population_count": 1,
            "owned_count": 0,
        },
    )

    monkeypatch.setattr(rbr, "_read_span_assert", lambda repo_root=None: ReaderResult())
    monkeypatch.setattr(rbr, "_read_auto_reconcile", lambda repo_root=None: ReaderResult())

    import io

    def _fake_claude_klabauter_bin_sentinel(argv):
        import sys as _sys

        print("claude-klabauter-bin sentinel missing at X", file=_sys.stderr)
        return 1

    def _fake_ceremony_hook(argv):
        print("post-ceremony hook output")
        return 0

    monkeypatch.setattr(rhr, "_cmd_claude_klabauter_bin_sentinel", _fake_claude_klabauter_bin_sentinel)
    monkeypatch.setattr(rhr, "_cmd_ceremony_hook", _fake_ceremony_hook)

    monkeypatch.setattr(
        rhr, "_reap_survey", lambda _root: SimpleNamespace(would_release=1, would_reclaim=0)
    )

    from coordinator_core.ops import check_weekly_staleness

    monkeypatch.setattr(
        check_weekly_staleness,
        "_resolve_state_root",
        lambda: str(tmp_path / "fake-state-root-does-not-exist"),
    )

    return brief(cadence)


def test_every_directives_cli_is_a_member_of_the_derived_real_cli_set(monkeypatch, tmp_path):
    real_names = _real_cli_names()
    assert real_names, "expected a non-empty derived CLI name set from coordinator/bin/"

    for cadence in CADENCES:
        envelope = _fully_stubbed_brief(monkeypatch, cadence, tmp_path)
        for directive in envelope["directives"]:
            assert directive["cli"] in real_names, (
                f"cadence={cadence!r} directive {directive['id']!r} names "
                f"cli={directive['cli']!r}, which is not in the derived real "
                f"CLI set (exact match required, not substring)"
            )


def test_no_directives_cli_contains_a_space(monkeypatch, tmp_path):
    for cadence in CADENCES:
        envelope = _fully_stubbed_brief(monkeypatch, cadence, tmp_path)
        for directive in envelope["directives"]:
            assert " " not in directive["cli"], (
                f"cadence={cadence!r} directive {directive['id']!r} cli="
                f"{directive['cli']!r} contains a space — the subcommand "
                "belongs in args[], not concatenated into cli"
            )


def test_no_directives_cli_carries_a_dot_py_suffix(monkeypatch, tmp_path):
    for cadence in CADENCES:
        envelope = _fully_stubbed_brief(monkeypatch, cadence, tmp_path)
        for directive in envelope["directives"]:
            assert not directive["cli"].endswith(".py"), (
                f"cadence={cadence!r} directive {directive['id']!r} cli="
                f"{directive['cli']!r} carries a .py suffix — forwarders "
                "are extension-less by construction"
            )


def test_target_root_flag_parsed_and_forwarded_to_brief(monkeypatch, tmp_path):
    import coordinator_core.orient_assemble as orient_assemble

    seen: dict = {}

    def _fake_brief(cadence, *, repo_root=None):
        seen["cadence"] = cadence
        seen["repo_root"] = repo_root
        return {"ok": True}

    monkeypatch.setattr(orient_assemble, "brief", _fake_brief)
    other_repo = tmp_path / "some-other-repo"
    other_repo.mkdir()
    explicit_root = str(other_repo)
    exit_code = orient_assemble.main(
        ["brief", "--cadence", "session", "--target-root", explicit_root]
    )
    assert exit_code == 0
    assert seen["repo_root"] == str(Path(explicit_root).resolve())


def test_target_root_defaults_to_git_toplevel_from_cwd(monkeypatch, tmp_path):
    import coordinator_core.orient_assemble as orient_assemble

    seen: dict = {}

    def _fake_brief(cadence, *, repo_root=None):
        seen["repo_root"] = repo_root
        return {"ok": True}

    monkeypatch.setattr(orient_assemble, "brief", _fake_brief)
    fake_toplevel = tmp_path / "resolved-toplevel"
    monkeypatch.setattr(
        "coordinator_core.lifecycle.find_repo_root", lambda: fake_toplevel
    )
    exit_code = orient_assemble.main(["brief", "--cadence", "session"])
    assert exit_code == 0
    assert seen["repo_root"] == str(fake_toplevel)


def test_unresolvable_target_root_fails_loud_naming_the_flag(monkeypatch, capsys):
    import coordinator_core.orient_assemble as orient_assemble

    def _raise(*, cwd=None):
        raise RuntimeError("git rev-parse --show-toplevel failed (not a git repo?)")

    monkeypatch.setattr(
        "coordinator_core.lifecycle.find_repo_root", lambda: _raise()
    )
    exit_code = orient_assemble.main(["brief", "--cadence", "session"])
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "--target-root" in captured.err


def test_no_directives_cli_is_a_ceremony_or_skill_name(monkeypatch, tmp_path):
    for cadence in CADENCES:
        envelope = _fully_stubbed_brief(monkeypatch, cadence, tmp_path)
        for directive in envelope["directives"]:
            assert directive["cli"] not in _CEREMONY_OR_SKILL_NAMES, (
                f"cadence={cadence!r} directive {directive['id']!r} cli="
                f"{directive['cli']!r} is a ceremony/skill name, not a bin "
                "CLI — this is the self-referential category error, not a "
                "legitimate directive target"
            )


def test_nonexistent_target_root_fails_loud_rather_than_scanning_nothing(tmp_path, capsys):
    import coordinator_core.orient_assemble as orient_assemble

    missing = tmp_path / "not-a-repo"
    exit_code = orient_assemble.main(
        ["brief", "--cadence", "session", "--target-root", str(missing)]
    )

    assert exit_code != 0
    err = capsys.readouterr().err
    assert "is not a directory" in err
    assert str(missing) in err
    assert "--target-root was not provided" not in err
