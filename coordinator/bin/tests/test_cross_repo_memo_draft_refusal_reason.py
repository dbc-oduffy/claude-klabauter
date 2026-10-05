"""A refused `cross-repo-memo draft` names its reason, on both transports.

The op's refusal envelope carries no reason field by contract; the reason rides
a side channel (child stderr cold, the `_stderr` frame field warm). These tests
drive the real CLI against an isolated registry and sender repo under tmp_path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN = Path(__file__).resolve().parents[1]
_ENGINE_ROOT = Path(__file__).resolve().parents[3]


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _draft(tmp_path: Path, warm: str, extra: list[str] | None = None, to: str = "no-such-receiver-em"):
    sender = tmp_path / "sender_repo"
    _init_repo(sender)
    home = tmp_path / "home"
    (home / "machine-local").mkdir(parents=True)
    (home / "machine-local" / "registry.toml").write_text(
        f'"repos.sender_repo" = {str(sender)!r}\n'.replace("'", '"'), encoding="utf-8"
    )
    env = {
        **os.environ,
        "CLAUDE_HOME": str(home),
        "COORDINATOR_SETTINGS_HOME": str(home),
        "COORDINATOR_ENGINE_ROOT": str(_ENGINE_ROOT),
        "COORDINATOR_WARM": warm,
    }
    return subprocess.run(
        [sys.executable, str(_BIN / "cross-repo-memo.py"), "draft", "refusal-reason",
         "--to", to, "--title", "T", "--kind", "ask",
         "--summary", "S", *(extra or [])],
        cwd=sender, env=env, capture_output=True, text=True, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.parametrize("warm", ["0", "1"])
def test_refused_draft_prints_the_engine_reason(tmp_path, warm):
    proc = _draft(tmp_path, warm)
    assert proc.returncode != 0
    assert "fleet op setup error" in proc.stderr, proc.stderr


@pytest.mark.parametrize("warm", ["0", "1"])
def test_refused_partial_scoped_to_prints_its_reason(tmp_path, warm):
    proc = _draft(tmp_path, warm, ["--scoped-to-artifact", "docs/x.md"], to="sender_repo-em")
    assert proc.returncode != 0
    assert "scoped_to.seam is required" in proc.stderr, proc.stderr


@pytest.fixture
def cc_invoke():
    sys.path.insert(0, str(_BIN / "lib"))
    try:
        import cc_invoke as mod
        yield mod
    finally:
        sys.path.remove(str(_BIN / "lib"))


def test_refusal_message_surfaces_rejection_class_when_no_stderr_crossed(cc_invoke):
    result = {"exit_code": 1, "failed": [], "rejection_class": "registry_error"}
    message = cc_invoke.mutation_refusal_message("memo.draft", result)
    assert "rejection_class='registry_error'" in message
    assert "no reason reached the caller" not in message


def test_refusal_message_names_the_missing_reason_and_how_to_read_it(cc_invoke):
    message = cc_invoke.mutation_refusal_message("memo.draft", {"exit_code": 1, "failed": []})
    assert "no reason reached the caller" in message
    assert "COORDINATOR_WARM=0" in message


def test_refusal_message_does_not_claim_a_missing_reason_when_one_is_present(cc_invoke):
    with_stderr = cc_invoke.mutation_refusal_message(
        "memo.draft", {"exit_code": 1}, op_stderr="fleet op setup error: boom"
    )
    assert "boom" in with_stderr and "no reason reached" not in with_stderr
    per_item = cc_invoke.mutation_refusal_message(
        "memo.draft", {"exit_code": 2, "failed": [{"id": "x", "reason": "collision"}]}
    )
    assert "no reason reached" not in per_item
    assert cc_invoke.mutation_refusal_message("memo.draft", {"exit_code": 0, "failed": []}) is None
