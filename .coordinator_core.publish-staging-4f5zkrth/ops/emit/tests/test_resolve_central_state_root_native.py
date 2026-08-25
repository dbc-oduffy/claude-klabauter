"""Parity net for C9's ``envelope._resolve_central_state_root`` native port.

Port of: coordinator-state-root.sh (DoE 6fb5fb37, 2026-07-22)'s
``coordinator_state_root --central`` (Rule 4,
the backward-compat default: no ``--subject``/``--artifact``) — resolves to
``$(_csr_makima_root)/state``. The bash lib's own docstring documents ``_csr_makima_root``
as ITSELF a native bridge onto ``coordinator_core.engine_root.coordinator_engine_root``, so
this port calls that same native resolver in-process instead of spawning
``bash -c "source ... && coordinator_state_root --central"``.

Spec backlink: pln-makima-pure-python-shop-retire-0f8aee § C9
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.emit.resolvers import _resolve_central_state_root


class TestResolveCentralStateRootNative:
    def test_resolves_to_makima_root_slash_state(self, tmp_path: Path) -> None:
        """Success path: makima_root/state, exactly Rule 4's contract."""
        makima_root = tmp_path / "project-makima"
        with patch(
            "coordinator_core.engine_root.coordinator_engine_root",
            return_value=str(makima_root),
        ):
            result = _resolve_central_state_root(tmp_path / "coordinator", tmp_path)
        assert result == makima_root / "state"

    def test_falls_back_to_claude_home_state_on_runtime_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Unresolvable makima root (RuntimeError, the resolver's documented failure mode)
        falls back to CLAUDE_HOME/.claude/state — the same fallback the old bridge's
        except-clause used on a bash/subprocess failure."""
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
        with patch(
            "coordinator_core.engine_root.coordinator_engine_root",
            side_effect=RuntimeError("repos.project_makima not set"),
        ):
            result = _resolve_central_state_root(tmp_path / "coordinator", tmp_path)
        assert result == tmp_path / ".claude" / "state"

    def test_falls_back_to_claude_home_state_on_falsy_return(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Defensive: ``coordinator_makima_root()`` returning a falsy value without raising
        (unreachable today per that function's own documented contract — every rung either
        returns a truthy string or raises ``RuntimeError`` — but the ``if makima_root:`` guard
        exists in the code specifically to handle it) falls back the same as the exception
        path. Review: code-reviewer (F7) — pins the guard's own behavior for symmetry with
        the exception-path test above, since nothing else in this file exercised it."""
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
        with patch(
            "coordinator_core.engine_root.coordinator_engine_root",
            return_value="",
        ):
            result = _resolve_central_state_root(tmp_path / "coordinator", tmp_path)
        assert result == tmp_path / ".claude" / "state"

    def test_no_bash_subprocess_spawned(self, tmp_path: Path) -> None:
        """Regression guard: this port must never spawn a subprocess at all."""
        makima_root = tmp_path / "project-makima"
        with patch(
            "coordinator_core.engine_root.coordinator_engine_root",
            return_value=str(makima_root),
        ), patch("subprocess.run") as mock_run:
            _resolve_central_state_root(tmp_path / "coordinator", tmp_path)
        mock_run.assert_not_called()
