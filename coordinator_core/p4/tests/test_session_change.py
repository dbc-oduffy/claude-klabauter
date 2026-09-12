"""
test_session_change.py — pytest coverage for coordinator_core.p4.session_change.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § C2, § D3.

Exercises: lazy mint, reuse (zero spawns), an unregistered workspace's refusal,
a classified mint failure, and `remint_session_change`'s unconditional
overwrite — the D3 title's "lazy mint, reuse, re-mint when closed" in full,
minus the act-and-classify decision itself (owned by D4/D5's callers, not
this module).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.p4.session_change as session_change
from coordinator_core.git.run import GitResult
from coordinator_core.p4 import runner, workspace

# NOTE: `coordinator_core.p4.__init__` re-exports `workspace.session_change`
# (the D9 reader function) under the package attribute name `session_change`,
# shadowing this submodule on a plain `from coordinator_core.p4 import
# session_change`. The dotted-module import above binds the submodule
# directly and is unaffected by that re-export.


@pytest.fixture
def sdir(tmp_path):
    d = tmp_path / "session"
    d.mkdir()
    return d


@pytest.fixture
def identity(tmp_path):
    return workspace.P4Identity(
        port="p4.example.com:1666",
        user="bob",
        client="bob-ws",
        client_root=str(tmp_path),
    )


def _patch_repo_key(monkeypatch, repo_root, identity, repo_key="p4-studio/fifa-main"):
    monkeypatch.setattr(
        session_change,
        "merged_flat_registry",
        lambda: {f"p4.{repo_key}.repo_root": repo_root},
    )
    monkeypatch.setattr(
        workspace,
        "registry_get",
        lambda key: {
            f"p4.{repo_key}.port": identity.port,
            f"p4.{repo_key}.user": identity.user,
            f"p4.{repo_key}.client": identity.client,
            # Deliberately the PARENT of repo_root, not repo_root: `.root` is
            # the client's root and an ordinary client maps several projects
            # under one. If these two rows are ever collapsed back into one,
            # `_resolve_repo_key` stops resolving and these tests fail — which
            # is the point of not making them the same string here.
            f"p4.{repo_key}.client_root": str(Path(repo_root).parent),
        }.get(key),
    )


class TestEnsureSessionChange:
    def test_mints_on_first_use(self, monkeypatch, tmp_path, sdir, identity):
        (sdir / "meta.json").write_text("{}", encoding="utf-8")
        repo_root = str(tmp_path)
        _patch_repo_key(monkeypatch, repo_root, identity)
        monkeypatch.setattr(session_change, "session_dir", lambda sid, cwd=None: str(sdir))
        monkeypatch.setattr(
            session_change,
            "run_git",
            lambda args, **kw: GitResult(returncode=0, timed_out=False, stdout="deadbeef\n", stderr=""),
        )

        spawned = {}

        def fake_run(port, user, client, args, *, spec_input=None):
            spawned["args"] = args
            spawned["spec_input"] = spec_input
            return runner.P4Result(ok=True, stdout="Change 101 created.\n")

        monkeypatch.setattr(session_change.runner, "run", fake_run)

        result = session_change.ensure_session_change(repo_root, "sid-1")

        assert result == 101
        assert spawned["args"] == ["change", "-i"]
        assert "sid-1" in spawned["spec_input"]
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["p4_change"] == "101"
        assert meta["p4_base_sha"] == "deadbeef"

    def test_reuses_recorded_cl_with_zero_spawns(self, monkeypatch, tmp_path, sdir, identity):
        (sdir / "meta.json").write_text(
            json.dumps({"p4_change": "55", "p4_base_sha": "cafefeed"}), encoding="utf-8"
        )
        repo_root = str(tmp_path)
        _patch_repo_key(monkeypatch, repo_root, identity)
        monkeypatch.setattr(session_change, "session_dir", lambda sid, cwd=None: str(sdir))

        def fail_run(*a, **kw):
            raise AssertionError("runner.run must not be called on reuse")

        monkeypatch.setattr(session_change.runner, "run", fail_run)

        result = session_change.ensure_session_change(repo_root, "sid-1")

        assert result == 55

    def test_unregistered_workspace_raises(self, monkeypatch, tmp_path, sdir):
        repo_root = str(tmp_path)
        monkeypatch.setattr(session_change, "merged_flat_registry", lambda: {})
        monkeypatch.setattr(session_change, "session_dir", lambda sid, cwd=None: str(sdir))

        with pytest.raises(session_change.P4SessionChangeError):
            session_change.ensure_session_change(repo_root, "sid-1")

    def test_mint_failure_raises(self, monkeypatch, tmp_path, sdir, identity):
        repo_root = str(tmp_path)
        _patch_repo_key(monkeypatch, repo_root, identity)
        monkeypatch.setattr(session_change, "session_dir", lambda sid, cwd=None: str(sdir))
        monkeypatch.setattr(
            session_change.runner,
            "run",
            lambda *a, **kw: runner.P4Result(
                ok=False, stdout="", error=runner.P4Error(kind="refused", raw="no such client")
            ),
        )

        with pytest.raises(session_change.P4SessionChangeError):
            session_change.ensure_session_change(repo_root, "sid-1")


class TestRemintSessionChange:
    def test_overwrites_recorded_cl(self, monkeypatch, tmp_path, sdir, identity):
        (sdir / "meta.json").write_text(
            json.dumps({"p4_change": "55", "p4_base_sha": "cafefeed"}), encoding="utf-8"
        )
        repo_root = str(tmp_path)
        _patch_repo_key(monkeypatch, repo_root, identity)
        monkeypatch.setattr(session_change, "session_dir", lambda sid, cwd=None: str(sdir))
        monkeypatch.setattr(
            session_change,
            "run_git",
            lambda args, **kw: GitResult(returncode=0, timed_out=False, stdout="newsha\n", stderr=""),
        )
        monkeypatch.setattr(
            session_change.runner,
            "run",
            lambda *a, **kw: runner.P4Result(ok=True, stdout="Change 999 created.\n"),
        )

        result = session_change.remint_session_change(repo_root, "sid-1")

        assert result == 999
        meta = json.loads((sdir / "meta.json").read_text())
        assert meta["p4_change"] == "999"
        assert meta["p4_base_sha"] == "newsha"
