"""Parity net for C9's ``envelope._resolve_central_state_root`` native port.

Port of: coordinator-state-root.sh (example-doctrine-repo 6fb5fb37, 2026-07-22)'s
``coordinator_state_root --central`` (Rule 4,
the backward-compat default: no ``--subject``/``--artifact``) — resolves to
``$(_csr_claude_klabauter_root)/state``. The bash lib's own docstring documents ``_csr_claude_klabauter_root``
as ITSELF a native bridge onto ``coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root``, so
this port calls that same native resolver in-process instead of spawning
``bash -c "source ... && coordinator_state_root --central"``.

Spec backlink: docs/plans/2026-07-21-claude-klabauter-pure-python-shop-retire-all-bash.md § C9
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.emit.envelope import _resolve_central_state_root


class TestResolveCentralStateRootNative:
    def test_resolves_to_claude_klabauter_root_slash_state(self, tmp_path: Path) -> None:
        """Success path: claude_klabauter_root/state, exactly Rule 4's contract."""
        claude_klabauter_root = tmp_path / "claude-klabauter"
        with patch(
            "coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root",
            return_value=str(claude_klabauter_root),
        ):
            result = _resolve_central_state_root(tmp_path / "coordinator", tmp_path)
        assert result == claude_klabauter_root / "state"

    def test_falls_back_to_claude_home_state_on_runtime_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Unresolvable claude-klabauter root (RuntimeError, the resolver's documented failure mode)
        falls back to CLAUDE_HOME/.claude/state — the same fallback the old bridge's
        except-clause used on a bash/subprocess failure."""
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
        with patch(
            "coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root",
            side_effect=RuntimeError("repos.claude_klabauter not set"),
        ):
            result = _resolve_central_state_root(tmp_path / "coordinator", tmp_path)
        assert result == tmp_path / ".claude" / "state"

    def test_falls_back_to_claude_home_state_on_falsy_return(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Defensive: ``coordinator_claude_klabauter_root()`` returning a falsy value without raising
        (unreachable today per that function's own documented contract — every rung either
        returns a truthy string or raises ``RuntimeError`` — but the ``if claude_klabauter_root:`` guard
        exists in the code specifically to handle it) falls back the same as the exception
        path. Review: code-reviewer (F7) — pins the guard's own behavior for symmetry with
        the exception-path test above, since nothing else in this file exercised it."""
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
        with patch(
            "coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root",
            return_value="",
        ):
            result = _resolve_central_state_root(tmp_path / "coordinator", tmp_path)
        assert result == tmp_path / ".claude" / "state"

    def test_no_bash_subprocess_spawned(self, tmp_path: Path) -> None:
        """Regression guard: this port must never spawn a subprocess at all."""
        claude_klabauter_root = tmp_path / "claude-klabauter"
        with patch(
            "coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root",
            return_value=str(claude_klabauter_root),
        ), patch("subprocess.run") as mock_run:
            _resolve_central_state_root(tmp_path / "coordinator", tmp_path)
        mock_run.assert_not_called()
