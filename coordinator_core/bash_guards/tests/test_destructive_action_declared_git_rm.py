"""Declared-delete admission of `git rm` in block_subagent_destructive_action (fixtures under tmp_path)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.guard_messages.testing import envelope_of

RUN = "warp-2026-10-25-demo-20261025T000000-abcdef"
ROW = "C2"
SESSION = "sess-emitter"
MARK = f"COORDINATOR_DISPATCH_ROW={RUN}/{ROW}"
DECLARED = "pkg/a.py"


def _write_run(root: Path, *, session=SESSION, run=RUN, row=ROW, writes=(DECLARED,)) -> None:
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT

    rec = root / RECORD_DIR_ROOT / run
    rec.mkdir(parents=True)
    manifest = {
        "run_dir": str(rec),
        "rows": [
            {"id": row, "agent_type": "x", "model": "m", "brief_path": "b.md", "writes": list(writes), "wave": 0}
        ],
        "review_declared_paths": [],
        "marker_path": "m",
    }
    (rec / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    run_dir = root / RUN_DIR_ROOT
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{run}.workflow.mjs.emitted.json").write_text(json.dumps({"session_id": session}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _channel_live(monkeypatch):
    monkeypatch.setattr(guard, "_DECLARED_DELETE_CHANNEL_LIVE", True)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("y", encoding="utf-8")
    (tmp_path / "pkg" / "sub").mkdir()
    _write_run(tmp_path)
    return tmp_path


def _check(repo, command, *, session=SESSION, tool="Bash", agent=True):
    payload = {
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": session,
        "cwd": str(repo),
    }
    if agent:
        payload["agent_id"] = "deadbeef0123"
        payload["agent_type"] = "coordinator:executor"
    return envelope_of(guard.check(payload))


def test_declared_path_admitted(repo):
    assert _check(repo, f"{MARK} git rm -q -- {DECLARED}") is None


def test_declared_path_admitted_without_dashdash_and_with_cached(repo):
    assert _check(repo, f"{MARK} git rm --cached {DECLARED}") is None


def test_absolute_declared_path_admitted(repo):
    assert _check(repo, f"{MARK} git rm -q -- {(repo / DECLARED).as_posix()}") is None


@pytest.mark.parametrize(
    "command",
    [
        f"git rm -q -- {DECLARED}",
        f"{MARK} git rm -q -- pkg/b.py",
        f"{MARK} git rm -q -- {DECLARED} pkg/b.py",
        f"{MARK} git rm -r -- {DECLARED}",
        f"{MARK} git rm -f -- {DECLARED}",
        f"{MARK} git rm -rf -- {DECLARED}",
        f"{MARK} git rm --force -- {DECLARED}",
        f"{MARK} git rm -q -- 'pkg/*.py'",
        f"{MARK} git rm -q -- pkg/sub",
        f"{MARK} git rm -q",
        f"{MARK} git rm --ignore-unmatch -- {DECLARED}",
        f"{MARK} git -C pkg rm -q -- a.py",
        f"{MARK} git --git-dir=.git rm -q -- {DECLARED}",
        f"{MARK} git rm -q -- ../outside.py",
        f"echo {MARK}; git rm -q -- {DECLARED}",
        f"{MARK} echo hi; git rm -q -- {DECLARED}",
        f"export {MARK}; git rm -q -- {DECLARED}",
        f"{MARK} git rm -q -- {DECLARED}; {MARK[:-2]}C9 git rm -q -- {DECLARED}",
        f"COORDINATOR_DISPATCH_ROW={RUN}/NOPE git rm -q -- {DECLARED}",
        f"COORDINATOR_DISPATCH_ROW=warp-unknown-run/{ROW} git rm -q -- {DECLARED}",
    ],
)
def test_refused_forms(repo, command):
    result = _check(repo, command)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_session_mismatch_denied(repo):
    assert _check(repo, f"{MARK} git rm -q -- {DECLARED}", session="sess-other") is not None


def test_powershell_dialect_still_denied(repo):
    assert _check(repo, f"git rm -q -- {DECLARED}", tool="PowerShell") is not None
    assert _check(repo, f"$env:COORDINATOR_DISPATCH_ROW='{RUN}/{ROW}'; git rm -q -- {DECLARED}", tool="PowerShell") is not None


def test_em_payload_unaffected(repo):
    assert _check(repo, "git rm -q -- pkg/b.py", agent=False) is None


def test_rm_is_not_in_the_safe_allowlist():
    import re

    src = Path(guard.__file__).read_text(encoding="utf-8")
    block = re.search(r"^_SAFE_GIT_SUBCOMMANDS = frozenset.*?^\)", src, re.S | re.M).group(0)
    assert '"rm"' not in block


def test_no_marker_command_does_not_read_the_license(repo, monkeypatch):
    from coordinator_core.bash_guards import _declared_delete_license as lic

    def boom(*a, **k):
        raise AssertionError("license read without a marker")

    monkeypatch.setattr(lic, "resolve_license", boom)
    assert _check(repo, f"git rm -q -- {DECLARED}") is not None


def test_license_read_once_per_check(repo, monkeypatch):
    from coordinator_core.bash_guards import _declared_delete_license as lic

    calls = []
    real = lic.resolve_license
    monkeypatch.setattr(lic, "resolve_license", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert _check(repo, f"{MARK} git rm -q -- {DECLARED}") is None
    assert len(calls) == 1
