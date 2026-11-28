"""`evaluate_payload_json(verdict_only=True)` (or `COORDINATOR_GUARD_VERDICT_ONLY=1`) returns the
envelope a recording run returns and leaves every claim, chain-arrival, fire-counter and
advisory-dedupe file untouched; the recording run is the control that proves those files exist."""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from coordinator_core.bash_guards.dispatch import evaluate_payload_json

_ADVISORY_CMD = "cat > a.txt <<'EOF'\nhi\nEOF"
_DENY_CMD = "git reset --hard"
_SESSION = "s-verdict-only"
_ENV = "COORDINATOR_GUARD_VERDICT_ONLY"

# Per-call telemetry is the one row a verdict-only run may still write.
_TELEMETRY_PARTS = ("agent-process-ledger", "destructive-guard-fail-open.log")


def _payload(repo: Path, cmd: str) -> str:
    return json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": _SESSION,
            "cwd": str(repo),
        }
    )


def _snapshot(root: Path) -> Dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not any(part in str(p) for part in _TELEMETRY_PARTS)
    }


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(_ENV, raising=False)
    return tmp_path


def _run(world: Path, cmd: str, **kwargs: Any) -> Any:
    return evaluate_payload_json(_payload(world / "repo", cmd), **kwargs)


def test_signature_spells_out_verdict_only_for_feature_detection() -> None:
    param = inspect.signature(evaluate_payload_json).parameters["verdict_only"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


@pytest.mark.parametrize("cmd", [_ADVISORY_CMD, _DENY_CMD], ids=["advisory", "deny"])
@pytest.mark.parametrize("via_env", [False, True], ids=["flag", "env"])
def test_verdict_only_writes_nothing_and_returns_the_recording_envelope(
    world: Path, monkeypatch: pytest.MonkeyPatch, cmd: str, via_env: bool
) -> None:
    before = _snapshot(world)
    if via_env:
        monkeypatch.setenv(_ENV, "1")
        quiet = _run(world, cmd)
    else:
        quiet = _run(world, cmd, verdict_only=True)
    assert quiet is not None
    assert _snapshot(world) == before

    monkeypatch.delenv(_ENV, raising=False)
    recorded = _run(world, cmd, verdict_only=False)
    assert recorded == quiet
    written = set(_snapshot(world)) - set(before)
    assert any("chain-arrival-ledger" in name for name in written), written
    if cmd == _DENY_CMD:
        assert any(name.endswith("deny-fire-counts.jsonl") for name in written), written
    else:
        assert any(name.endswith("advisory-fire-counts.jsonl") for name in written), written
        assert any(name.endswith("touch-record.jsonl") for name in written), written


def test_flag_does_not_leak_into_the_next_call(world: Path) -> None:
    _run(world, _DENY_CMD, verdict_only=True)
    before = _snapshot(world)
    _run(world, _DENY_CMD)
    assert _snapshot(world) != before


def test_explicit_false_overrides_the_env_var(world: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    _run(world, _DENY_CMD, verdict_only=False)
    assert any(name.endswith("deny-fire-counts.jsonl") for name in _snapshot(world))
