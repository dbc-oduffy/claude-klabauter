"""
coordinator_core.hooks.test_subagent_sidecar_fill_check — round-trip tests for the
PostToolUse-Agent sidecar-fill surfacing op (hooks.subagent_sidecar_fill_check).

Covers: a flagged (status: open, unfilled body) sidecar surfaces a post_advisory
naming the runnable check-sidecar-fill invocation; a filled sidecar and a
status-not-open sidecar both surface nothing; missing session_id/repo_root fail
open to no_advisory; a repeat firing with the SAME flagged set in the same
session is suppressed to no_advisory by the dedupe marker, while a DIFFERENT
flagged set fires again; an internal exception in the scan path fails open to
no_advisory rather than raising; registration-quad presence for the op key.

REAL DISPATCH SHAPE (P1 fix, see subagent_sidecar_fill_check module docstring
"FAIL OPEN, UNCONDITIONALLY" and op_scopes.py's "hooks.subagent_sidecar_fill_check"
comment): `hooks.subagent_sidecar_fill_check` is `common_dir`-scoped, and
`ipc.resolve_op_repo_key` hands every `common_dir`-scoped handler
`repo_root=git_common_dir(request_repo)` — i.e. `<worktree>/.git`, NOT the
worktree root. Every fixture below therefore builds a real git worktree at
`tmp_path` (via `_git_init`), files sidecars under the WORKTREE
(`tmp_path/state/subagent-share/<sid>/`, matching where `provision_report`
actually writes them), and calls `_handler` with
`repo_root=str(_gitdir(tmp_path))` — the gitdir, exactly what `ipc.py` supplies
in production. A fixture that instead passed `repo_root=str(tmp_path)` (the
worktree root) would validate a shape `ipc.py` never hands the op and would
stay green even if the handler regressed to joining `state/` directly onto
the gitdir (the exact P1 this suite now guards against — see
`TestGitdirShapeRegression`).

`_handler` is a plain `def` (sync branch, dispatch-timeout enforceable — see
its own docstring), so `_run` below is a passthrough rather than an
asyncio.run() wrapper — kept as a named seam so call sites need no change.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# SPAWN-RATCHET Rule 2/4: _git_init below spawns a real `git init` for every
# test in this file. See coordinator_core/tests/test_no_new_spawning_tests.py.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git_init(worktree_root: Path) -> None:
    subprocess.run(  # popup-safe-env-suppressed
        ["git", "init", "-q", str(worktree_root)],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _gitdir(worktree_root: Path) -> Path:
    """The gitdir `ipc.py` actually hands `common_dir`-scoped handlers —
    `<worktree>/.git` for a plain (non-linked-worktree) repo, matching
    `lifecycle.git_common_dir`'s own contract for the common case."""
    return worktree_root / ".git"


def _run(result):
    return result


_OPEN_UNFILLED = """---
plan: docs/plans/example.md
chunk: C1
agent_type: executor
spawned_at: 2026-08-15T00:00:00Z
dispatched_by: em-session
status: open
divergence: {"diverged": false}
commits: []
sidecar_schema: v1
---

## Run notes

<!-- placeholder -->

## Observations

- [ ] Complete — flip this box when done; the frontmatter `status:` field remains authoritative.
"""

_OPEN_FILLED = _OPEN_UNFILLED.replace(
    "<!-- placeholder -->", "Dispatched executor; scoped diff in coordinator_core/hooks/."
)

_COMPLETE_UNFILLED = _OPEN_UNFILLED.replace("status: open", "status: complete")


def _sidecar_dir(worktree_root: Path, session_id: str) -> Path:
    """Sidecars live under the WORKTREE, matching `provision_report._provision`
    — never under the gitdir."""
    d = worktree_root / "state" / "subagent-share" / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestFlaggedFires:
    def test_open_unfilled_sidecar_surfaces_advisory_naming_the_cli(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_sidecar_fill_check import _handler

        _git_init(tmp_path)
        sid = "sess-fire-01"
        d = _sidecar_dir(tmp_path, sid)
        (d / "executor-0a.md").write_text(_OPEN_UNFILLED, encoding="utf-8")

        result = _run(_handler(
            {"session_id": sid, "hook_event_name": "PostToolUse"},
            repo_root=str(_gitdir(tmp_path)),
        ))

        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "check-sidecar-fill --session %s" % sid in ctx
        assert "1 sidecar" in ctx

    def test_multiple_flagged_pluralizes(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_sidecar_fill_check import _handler

        _git_init(tmp_path)
        sid = "sess-fire-02"
        d = _sidecar_dir(tmp_path, sid)
        (d / "executor-0a.md").write_text(_OPEN_UNFILLED, encoding="utf-8")
        (d / "executor-0b.md").write_text(_OPEN_UNFILLED, encoding="utf-8")

        result = _run(_handler(
            {"session_id": sid, "hook_event_name": "PostToolUse"},
            repo_root=str(_gitdir(tmp_path)),
        ))

        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "2 sidecars" in ctx


class TestGitdirShapeRegression:
    """Directly proves the P1 fix: `repo_root` as the real gitdir must still
    locate sidecars filed under the WORKTREE. Pre-fix, `_flagged_paths` joined
    `state/subagent-share/<sid>` straight onto the raw (gitdir) `repo_root`,
    scanning `<worktree>/.git/state/subagent-share/<sid>/` — which never
    exists — and always returned `[]`, so this test fails against the
    pre-fix handler and passes post-fix."""

    def test_gitdir_repo_root_still_finds_worktree_sidecars(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_sidecar_fill_check import _handler

        _git_init(tmp_path)
        sid = "sess-gitdir-shape-01"
        d = _sidecar_dir(tmp_path, sid)
        (d / "executor-0a.md").write_text(_OPEN_UNFILLED, encoding="utf-8")

        gitdir = _gitdir(tmp_path)
        assert gitdir.is_dir(), "sanity: git init must produce a real .git directory"

        result = _run(_handler(
            {"session_id": sid, "hook_event_name": "PostToolUse"},
            repo_root=str(gitdir),
        ))

        assert result != {}, (
            "handler returned no_advisory() when given the real gitdir-shaped "
            "repo_root ipc.py supplies — the P1 worktree-root regression"
        )
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "check-sidecar-fill --session %s" % sid in ctx


class TestQuietPaths:
    def test_filled_sidecar_no_advisory(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_sidecar_fill_check import _handler

        _git_init(tmp_path)
        sid = "sess-quiet-01"
        d = _sidecar_dir(tmp_path, sid)
        (d / "executor-0a.md").write_text(_OPEN_FILLED, encoding="utf-8")

        result = _run(_handler(
            {"session_id": sid, "hook_event_name": "PostToolUse"},
            repo_root=str(_gitdir(tmp_path)),
        ))
        assert result == {}

    def test_complete_unfilled_sidecar_no_advisory(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_sidecar_fill_check import _handler

        _git_init(tmp_path)
        sid = "sess-quiet-02"
        d = _sidecar_dir(tmp_path, sid)
        (d / "executor-0a.md").write_text(_COMPLETE_UNFILLED, encoding="utf-8")

        result = _run(_handler(
            {"session_id": sid, "hook_event_name": "PostToolUse"},
            repo_root=str(_gitdir(tmp_path)),
        ))
        assert result == {}

    def test_missing_session_id_no_advisory(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_sidecar_fill_check import _handler

        _git_init(tmp_path)
        result = _run(_handler(
            {"hook_event_name": "PostToolUse"},
            repo_root=str(_gitdir(tmp_path)),
        ))
        assert result == {}

    def test_missing_repo_root_no_advisory(self) -> None:
        from coordinator_core.hooks.subagent_sidecar_fill_check import _handler

        result = _run(_handler({"session_id": "sess-no-root", "hook_event_name": "PostToolUse"}, repo_root=None))
        assert result == {}

    def test_no_sidecar_dir_no_advisory(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_sidecar_fill_check import _handler

        _git_init(tmp_path)
        result = _run(_handler(
            {"session_id": "sess-nonexistent", "hook_event_name": "PostToolUse"},
            repo_root=str(_gitdir(tmp_path)),
        ))
        assert result == {}

    def test_non_git_repo_root_still_fails_open(self, tmp_path: Path) -> None:
        """No git repo at all — worktree-root resolution raises RuntimeError,
        caught and logged (not surfaced), falling back to repo_root as-is;
        the scan then legitimately finds nothing and the op still fails
        open rather than raising."""
        from coordinator_core.hooks.subagent_sidecar_fill_check import _handler

        result = _run(_handler(
            {"session_id": "sess-no-git", "hook_event_name": "PostToolUse"},
            repo_root=str(tmp_path / "not-a-repo"),
        ))
        assert result == {}


class TestDedupe:
    def test_repeat_firing_same_flagged_set_is_suppressed(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_sidecar_fill_check import _handler

        _git_init(tmp_path)
        sid = "sess-dedupe-01"
        d = _sidecar_dir(tmp_path, sid)
        (d / "executor-0a.md").write_text(_OPEN_UNFILLED, encoding="utf-8")
        gitdir = str(_gitdir(tmp_path))

        first = _run(_handler({"session_id": sid, "hook_event_name": "PostToolUse"}, repo_root=gitdir))
        assert first != {}

        second = _run(_handler({"session_id": sid, "hook_event_name": "PostToolUse"}, repo_root=gitdir))
        assert second == {}

    def test_different_flagged_set_fires_again(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_sidecar_fill_check import _handler

        _git_init(tmp_path)
        sid = "sess-dedupe-02"
        d = _sidecar_dir(tmp_path, sid)
        (d / "executor-0a.md").write_text(_OPEN_UNFILLED, encoding="utf-8")
        gitdir = str(_gitdir(tmp_path))

        first = _run(_handler({"session_id": sid, "hook_event_name": "PostToolUse"}, repo_root=gitdir))
        assert first != {}

        (d / "executor-0b.md").write_text(_OPEN_UNFILLED, encoding="utf-8")
        second = _run(_handler({"session_id": sid, "hook_event_name": "PostToolUse"}, repo_root=gitdir))
        assert second != {}
        assert "2 sidecars" in second["hookSpecificOutput"]["additionalContext"]


class TestFailOpen:
    def test_scan_exception_fails_open_to_no_advisory(self, tmp_path: Path, monkeypatch) -> None:
        from coordinator_core.hooks import subagent_sidecar_fill_check as mod

        _git_init(tmp_path)

        def _boom(worktree_root, session_id):
            raise RuntimeError("boom")

        monkeypatch.setattr(mod, "_flagged_paths", _boom)

        result = _run(mod._handler(
            {"session_id": "sess-boom", "hook_event_name": "PostToolUse"},
            repo_root=str(_gitdir(tmp_path)),
        ))
        assert result == {}


class TestRegistrationQuad:
    def test_op_registered_and_classified(self) -> None:
        from coordinator_core.ipc import _REGISTRY
        from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
        from coordinator_core.op_scopes import OP_KEY_SCOPE
        from coordinator_core.ops._registry_map import OP_MODULE_MAP

        import coordinator_core.hooks as hooks_pkg
        hooks_pkg._eager_import_all()

        key = "hooks.subagent_sidecar_fill_check"
        assert key in _REGISTRY
        assert OP_CLASSIFICATION[key] == OpClass.MUTATING
        assert OP_KEY_SCOPE[key] == "common_dir"
        assert OP_MODULE_MAP[key] == "coordinator_core.hooks"
