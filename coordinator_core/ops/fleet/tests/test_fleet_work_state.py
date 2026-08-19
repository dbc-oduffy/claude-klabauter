"""
Tests for coordinator_core.ops.fleet.work_state — "fleet.work_state".

Spec backlink: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md,
chunk C5.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import work_state as fws
from coordinator_core.ops.fleet._memo_resolver import RegistryReadError


def _make_registry(tmp_path: Path, monkeypatch, repos: dict) -> None:
    """Minimal machine-local registry fixture — mirrors
    test_capability_index.py's `_make_registry` (same resolver, same
    CLAUDE_HOME-pointing pattern; MACHINE_LOCAL_REGISTRY_DIR is out of scope
    for `_memo_resolver.read_registry_repos()`)."""
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    lines = []
    for key_suffix, repo_path in repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)


def _no_registry(tmp_path: Path, monkeypatch) -> None:
    claude_home = tmp_path / "claude-home-empty"
    claude_home.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)


def _make_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()


def _write_handoff(repo_root: Path, name: str, body: str) -> None:
    subdir = repo_root / "state" / "handoffs"
    subdir.mkdir(parents=True, exist_ok=True)
    (subdir / name).write_text(body, encoding="utf-8")


def _held_handoff(hnd_id: str, claimed_by: str) -> str:
    return f"""---
id: {hnd_id}
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
claimed_by: {claimed_by}
---
Body.
"""


def _unclaimed_handoff(hnd_id: str) -> str:
    return f"""---
id: {hnd_id}
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
---
Body.
"""


@pytest.fixture(autouse=True)
def _stub_snapshot(monkeypatch):
    """Every test stubs `harness_registry.snapshot()` to a cheap, deterministic
    empty registry unless it explicitly overrides this — keeps tests off any
    real live-session substrate."""
    from coordinator_core.session import harness_registry

    monkeypatch.setattr(harness_registry, "snapshot", lambda: {})


class TestMultiSiblingAggregation:
    def test_two_active_git_siblings_each_get_an_entry(self, tmp_path, monkeypatch):
        sib_a = tmp_path / "sib-a"
        sib_b = tmp_path / "sib-b"
        _make_git_repo(sib_a)
        _make_git_repo(sib_b)
        _write_handoff(sib_a, "a.md", _unclaimed_handoff("hnd-fleet-a-000001"))
        _write_handoff(sib_b, "b.md", _unclaimed_handoff("hnd-fleet-b-000002"))
        _make_registry(tmp_path, monkeypatch, {"sib_a": sib_a, "sib_b": sib_b})

        result = fws.build_fleet_work_state()

        assert result["errors"] == []
        assert set(result["repos"].keys()) == {str(sib_a.resolve()), str(sib_b.resolve())}
        assert len(result["repos"][str(sib_a.resolve())]["unclaimed"]) == 1
        assert len(result["repos"][str(sib_b.resolve())]["unclaimed"]) == 1


class TestPerRepoDegradePath:
    def test_build_work_state_failure_for_one_repo_lands_in_errors_not_raised(
        self, tmp_path, monkeypatch,
    ) -> None:
        sib_ok = tmp_path / "sib-ok"
        sib_broken = tmp_path / "sib-broken"
        _make_git_repo(sib_ok)
        _make_git_repo(sib_broken)
        _write_handoff(sib_ok, "ok.md", _unclaimed_handoff("hnd-fleet-ok-000003"))
        _make_registry(tmp_path, monkeypatch, {"ok": sib_ok, "broken": sib_broken})

        real_build_work_state = fws.build_work_state

        def _flaky(repo_root, *, messaging_snapshot=None):
            if repo_root == sib_broken.resolve():
                raise RuntimeError("boom")
            return real_build_work_state(repo_root, messaging_snapshot=messaging_snapshot)

        monkeypatch.setattr(fws, "build_work_state", _flaky)

        result = fws.build_fleet_work_state()

        assert str(sib_ok.resolve()) in result["repos"]
        assert str(sib_broken.resolve()) not in result["repos"]
        error_roots = {e["target_root"] for e in result["errors"]}
        assert str(sib_broken.resolve()) in error_roots


class TestAbsentRegistryDegrade:
    def test_no_registry_configured_yields_empty_result(self, tmp_path, monkeypatch) -> None:
        _no_registry(tmp_path, monkeypatch)

        result = fws.build_fleet_work_state()

        assert result == {"repos": {}, "errors": []}


class TestResolvedPathDedup:
    def test_two_registry_keys_one_tree_yields_one_entry(self, tmp_path, monkeypatch) -> None:
        sib = tmp_path / "sib-shared"
        _make_git_repo(sib)
        _write_handoff(sib, "shared.md", _unclaimed_handoff("hnd-fleet-shared-000004"))
        _make_registry(tmp_path, monkeypatch, {"sib": sib, "sib_alias": sib})

        result = fws.build_fleet_work_state()

        assert result["errors"] == []
        assert list(result["repos"].keys()) == [str(sib.resolve())]


class TestHoistedSnapshot:
    def test_snapshot_taken_exactly_once_across_multiple_siblings(
        self, tmp_path, monkeypatch,
    ) -> None:
        sib_a = tmp_path / "sib-a"
        sib_b = tmp_path / "sib-b"
        sib_c = tmp_path / "sib-c"
        for sib in (sib_a, sib_b, sib_c):
            _make_git_repo(sib)
        _write_handoff(
            sib_a, "held.md", _held_handoff("hnd-fleet-held-000005", "sess-fake-holder-0001")
        )
        _write_handoff(
            sib_b, "held.md", _held_handoff("hnd-fleet-held-000006", "sess-fake-holder-0002")
        )
        _make_registry(tmp_path, monkeypatch, {"a": sib_a, "b": sib_b, "c": sib_c})

        from coordinator_core.session import harness_registry

        call_count = {"n": 0}

        def _counting_snapshot():
            call_count["n"] += 1
            return {}

        monkeypatch.setattr(harness_registry, "snapshot", _counting_snapshot)

        result = fws.build_fleet_work_state()

        assert call_count["n"] == 1
        assert result["errors"] == []


class TestZeroSpawnOverNonGitFixture:
    def test_no_git_subprocess_spawned_with_a_non_git_sibling_present(
        self, tmp_path, monkeypatch,
    ) -> None:
        """AC9b: the runtime proof `test_no_unbatched_per_item_git_spawn.py`'s
        static AST gate cannot provide — a sibling set that includes a
        registered-but-not-a-git-repo entry must still resolve the whole loop
        with zero `subprocess.run`/`Popen`/etc calls anywhere in the chain."""
        sib_git = tmp_path / "sib-git"
        sib_non_git = tmp_path / "sib-non-git"
        _make_git_repo(sib_git)
        sib_non_git.mkdir(parents=True)  # exists, is a dir, no .git — a bare alias/volume-root case
        _write_handoff(sib_git, "a.md", _unclaimed_handoff("hnd-fleet-spawn-000007"))
        _make_registry(tmp_path, monkeypatch, {"git": sib_git, "non_git": sib_non_git})

        def _forbidden(*args, **kwargs):
            raise AssertionError(f"unexpected subprocess spawn: args={args!r} kwargs={kwargs!r}")

        monkeypatch.setattr(subprocess, "run", _forbidden)
        monkeypatch.setattr(subprocess, "Popen", _forbidden)
        monkeypatch.setattr(subprocess, "check_output", _forbidden)
        monkeypatch.setattr(subprocess, "check_call", _forbidden)

        result = fws.build_fleet_work_state()

        assert str(sib_git.resolve()) in result["repos"]
        error_roots = {e["target_root"] for e in result["errors"]}
        assert str(sib_non_git.resolve()) in error_roots
        assert any(
            e["target_root"] == str(sib_non_git.resolve()) and "git" in e["reason"].lower()
            for e in result["errors"]
        )


class TestOpHandlerWiring:
    def test_registered_op_returns_build_fleet_work_state_result(
        self, tmp_path, monkeypatch,
    ) -> None:
        _no_registry(tmp_path, monkeypatch)
        result = fws._fleet_work_state({}, repo_root=None)
        assert result == {"repos": {}, "errors": []}


class TestRegistryReadErrorPropagates:
    def test_corrupt_registry_file_raises_not_swallowed(self, tmp_path, monkeypatch) -> None:
        claude_home = tmp_path / "claude-home-corrupt"
        machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True)
        (machine_local / "registry.toml").write_text("not [ valid toml", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

        with pytest.raises(RegistryReadError):
            fws.build_fleet_work_state()
