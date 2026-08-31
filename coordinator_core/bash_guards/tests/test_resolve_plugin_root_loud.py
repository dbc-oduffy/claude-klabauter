"""Regression cover for `dispatch.resolve_plugin_root_loud`'s two branches.

WHY THIS FILE EXISTS. C2 chose loud-fail-open over the two alternatives it
rejected by name: a silent allow (unacceptable -- a deny guard that cannot
resolve its manifest would pass writes it exists to refuse, with nothing on
stderr and nothing counted) and a hard fail-closed deny (would brick Bash on
an OSS-mirror install, where no plugin root exists to find). That choice was
verified only by a manual mock exercise at dispatch time, because C2's
`writes:` scope named `dispatch.py` alone and its executor correctly declined
to author out of scope. Nothing on disk pinned it until here.

NEGATIVE SPEC. The load-bearing property is that a miss NEVER DENIES and
NEVER RAISES. A future edit that "hardens" the miss into a deny would look
defensible in review -- it is a confinement guard's manifest, after all --
and would break every OSS-mirror install silently. That is what the
`returns None` and `does not raise` assertions below are for; they are not
restating the type signature.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from coordinator_core.bash_guards import dispatch


def _payload(**extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "session_id": "resolve-plugin-root-loud-test",
    }
    payload.update(extra)
    return payload


@pytest.fixture
def recorded_fires(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[Any, ...]]:
    """Capture `record_advisory_fire` calls without touching the real counter."""
    calls: List[Tuple[Any, ...]] = []

    def _fake(*args: Any, **kwargs: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(dispatch, "_record_advisory_fire", _fake)
    return calls


class TestMissIsLoudButOpen:
    def test_miss_returns_none_rather_than_denying(
        self, monkeypatch: pytest.MonkeyPatch, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        """The whole point of the chunk: unresolvable plugin root allows."""
        monkeypatch.setattr(
            dispatch, "_resolve_caller_context", lambda payload: _ctx(plugin_root=None)
        )
        result = dispatch.resolve_plugin_root_loud(_payload(), "sess", "/nowhere")
        assert result is None

    def test_miss_writes_a_stderr_line(
        self, monkeypatch: pytest.MonkeyPatch, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        monkeypatch.setattr(
            dispatch, "_resolve_caller_context", lambda payload: _ctx(plugin_root=None)
        )
        dispatch.resolve_plugin_root_loud(_payload(), "sess", "/nowhere")
        assert "plugin_root" in capsys.readouterr().err

    def test_miss_records_exactly_one_counted_event(
        self, monkeypatch: pytest.MonkeyPatch, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        monkeypatch.setattr(
            dispatch, "_resolve_caller_context", lambda payload: _ctx(plugin_root=None)
        )
        dispatch.resolve_plugin_root_loud(_payload(), "sess", "/nowhere")
        assert len(recorded_fires) == 1

    def test_a_raising_counter_does_not_escape(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """The counter is best-effort telemetry. If it throws, the Bash call
        must still proceed -- a broken counter cannot become a broken shell."""

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("counter is down")

        monkeypatch.setattr(dispatch, "_record_advisory_fire", _boom)
        monkeypatch.setattr(
            dispatch, "_resolve_caller_context", lambda payload: _ctx(plugin_root=None)
        )
        assert dispatch.resolve_plugin_root_loud(_payload(), "sess", "/nowhere") is None


class TestHitIsSilent:
    def test_hit_passes_the_value_through_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        monkeypatch.setattr(
            dispatch, "_resolve_caller_context", lambda payload: _ctx(plugin_root="/plug/root")
        )
        assert dispatch.resolve_plugin_root_loud(_payload(), "sess", "/cwd") == "/plug/root"

    def test_hit_is_silent_and_counts_nothing(
        self, monkeypatch: pytest.MonkeyPatch, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        """A per-Bash-call hot path: the common case must emit nothing at
        all, or the stderr line stops being a signal."""
        monkeypatch.setattr(
            dispatch, "_resolve_caller_context", lambda payload: _ctx(plugin_root="/plug/root")
        )
        dispatch.resolve_plugin_root_loud(_payload(), "sess", "/cwd")
        assert capsys.readouterr().err == ""
        assert recorded_fires == []


def _ctx(*, plugin_root):
    """Minimal stand-in for `warm.caller_context.CallerContext`."""
    from coordinator_core.warm.caller_context import CallerContext

    return CallerContext(
        plugin_root=plugin_root, cwd=None, session_id=None, agent_id=None, pid=None
    )
