"""block_unreal_engine_resave: no commandlet rewrites the installed engine."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_unreal_engine_resave as guard

_ED = '"C:/Program Files/Epic Games/UE_5.8/Engine/Binaries/Win64/UnrealEditor-Cmd.exe"'  # abs-path-ok: fixture command text
_PROJ = "C:/Game/Game.uproject"


def _bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


@pytest.fixture(autouse=True)
def roots(monkeypatch):
    monkeypatch.setattr(guard, "_engine_roots", lambda: ["C:/Program Files/Epic Games/UE_5.8"])  # abs-path-ok: fixture root


@pytest.mark.parametrize(
    "cmd",
    [
        f"{_ED} {_PROJ} -run=ResavePackages",
        f"UnrealEditor.exe {_PROJ} -RUN=resavepackages -unattended",
        f"{_ED} {_PROJ} -run=ResavePackages -packagefolder=C:/Other/Content",
        f'{_ED} {_PROJ} -run=Compile -out="C:/Program Files/Epic Games/UE_5.8/Engine/Content/x"',  # abs-path-ok: fixture
    ],
)
def test_denied(cmd):
    out = guard.check(_bash(cmd))
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        f"{_ED} {_PROJ} -run=ResavePackages -projectonly",
        f"{_ED} {_PROJ} -run=ResavePackages -packagefolder=C:/Game/Content/Maps",
        f"{_ED} {_PROJ} -run=DerivedDataCache -fill",
        f"{_ED} {_PROJ} -game",
        'echo "UnrealEditor-Cmd -run=ResavePackages"',
    ],
)
def test_allowed(cmd):
    assert guard.check(_bash(cmd)) is None
