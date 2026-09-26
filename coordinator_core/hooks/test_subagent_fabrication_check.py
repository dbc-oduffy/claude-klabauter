
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

# real git-command failure. The spawn ratchet's `_BASELINE` is shrink-only
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run(coro):
    return asyncio.run(coro)


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "test")
    target = repo_root / "target.py"
    target.write_text("original\n")
    _git(repo_root, "add", "target.py")
    _git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


def _tool_use_block(name: str) -> dict:
    return {"type": "tool_use", "name": name, "input": {}}


def _assistant_line(tool_names: list[str]) -> str:
    content = [_tool_use_block(n) for n in tool_names]
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}})


def _write_subagent_transcript(parent_transcript: Path, agent_id: str, tool_names: list[str]) -> Path:
    stem = parent_transcript.stem
    subagents_dir = parent_transcript.parent / stem / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    sub = subagents_dir / f"agent-{agent_id}.jsonl"
    sub.write_text(_assistant_line(tool_names) + "\n")
    return sub


def _params(parent_transcript: Path, agent_id: str, target: str) -> dict:
    return {
        "transcript_path": str(parent_transcript),
        "agent_id": agent_id,
        "target_paths": target,
    }


class TestFabricationSuspected:
    def test_zero_mutating_zero_bash_clean_target_fires(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler

        repo_root = _init_repo(tmp_path)
        parent = repo_root / "transcript.jsonl"
        parent.write_text("{}\n")
        agent_id = "deadbeefcafef00d"
        _write_subagent_transcript(parent, agent_id, [])

        result = _run(_handler(
            _params(parent, agent_id, "target.py"),
            repo_root=str(repo_root / ".git"),
        ))
        assert result["verdict"] == "fabrication_suspected"
        assert result["mutating_tool_call_count"] == 0
        assert result["bash_tool_call_count"] == 0
        assert result["target_changed"] is False

    def test_control_fires_when_guard_conjunction_broken(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler

        repo_root = _init_repo(tmp_path)
        parent = repo_root / "transcript.jsonl"
        parent.write_text("{}\n")
        agent_id = "0123456789abcdef"

        _write_subagent_transcript(parent, agent_id, ["Edit"])
        broken = _run(_handler(
            _params(parent, agent_id, "target.py"),
            repo_root=str(repo_root / ".git"),
        ))
        assert broken["verdict"] == "no_signal", (
            "GUARD BROKEN: fired fabrication_suspected despite a real Edit call "
            f"— result was {broken!r}"
        )
        assert broken["mutating_tool_call_count"] == 1

        restored_transcript = parent.parent / parent.stem / "subagents" / f"agent-{agent_id}.jsonl"
        restored_transcript.write_text(_assistant_line([]) + "\n")
        restored = _run(_handler(
            _params(parent, agent_id, "target.py"),
            repo_root=str(repo_root / ".git"),
        ))
        assert restored["verdict"] == "fabrication_suspected"


class TestFalsePositiveGuards:
    def test_bash_only_editing_is_silent(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler

        repo_root = _init_repo(tmp_path)
        parent = repo_root / "transcript.jsonl"
        parent.write_text("{}\n")
        agent_id = "bashonlyagent001"
        _write_subagent_transcript(parent, agent_id, ["Bash"])

        result = _run(_handler(
            _params(parent, agent_id, "target.py"),
            repo_root=str(repo_root / ".git"),
        ))
        assert result["verdict"] == "no_signal"
        assert result["bash_tool_call_count"] == 1
        assert result["mutating_tool_call_count"] == 0

    def test_real_edit_calls_are_silent(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler

        repo_root = _init_repo(tmp_path)
        parent = repo_root / "transcript.jsonl"
        parent.write_text("{}\n")
        agent_id = "realeditagent001"
        _write_subagent_transcript(parent, agent_id, ["Edit", "Write"])

        result = _run(_handler(
            _params(parent, agent_id, "target.py"),
            repo_root=str(repo_root / ".git"),
        ))
        assert result["verdict"] == "no_signal"
        assert result["mutating_tool_call_count"] == 2

    def test_multiple_target_paths_any_dirty_one_is_changed(self, tmp_path: Path) -> None:
        """Slice-s7 review finding: every existing fixture passes a
        single-element `target_paths` list, so the actual point of this
        site's batching -- one `git status` correctly attributing dirty vs
        clean per path across MULTIPLE pathspecs in a single call -- was
        unverified. Two committed files, only the second dirtied; the
        single batched `git status --porcelain -- a.py b.py` call must
        still report the pair as changed (an all-or-nothing regression that
        only checked the FIRST pathspec would report False here)."""
        from coordinator_core.hooks.subagent_fabrication_check import _targets_changed

        repo_root = _init_repo(tmp_path)
        second = repo_root / "second.py"
        second.write_text("original\n")
        _git(repo_root, "add", "second.py")
        _git(repo_root, "commit", "-q", "-m", "add second.py")
        second.write_text("edited\n")

        result = _targets_changed(str(repo_root), ["target.py", "second.py"])
        assert result is True

    def test_multiple_target_paths_all_clean_is_unchanged(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _targets_changed

        repo_root = _init_repo(tmp_path)
        second = repo_root / "second.py"
        second.write_text("original\n")
        _git(repo_root, "add", "second.py")
        _git(repo_root, "commit", "-q", "-m", "add second.py")

        result = _targets_changed(str(repo_root), ["target.py", "second.py"])
        assert result is False

    def test_genuinely_changed_target_is_silent(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler

        repo_root = _init_repo(tmp_path)
        (repo_root / "target.py").write_text("changed on disk\n")
        parent = repo_root / "transcript.jsonl"
        parent.write_text("{}\n")
        agent_id = "dirtytargetagent"
        _write_subagent_transcript(parent, agent_id, [])

        result = _run(_handler(
            _params(parent, agent_id, "target.py"),
            repo_root=str(repo_root / ".git"),
        ))
        assert result["verdict"] == "no_signal"
        assert result["target_changed"] is True


class TestFailOpen:
    def test_absent_transcript_is_no_signal(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler

        repo_root = _init_repo(tmp_path)
        parent = repo_root / "transcript.jsonl"
        parent.write_text("{}\n")
        result = _run(_handler(
            _params(parent, "nevercalledagent", "target.py"),
            repo_root=str(repo_root / ".git"),
        ))
        assert result["verdict"] == "no_signal"

    def test_missing_agent_id_is_no_signal(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler
        result = _run(_handler({"transcript_path": "x", "target_paths": "a.py"}, repo_root="root"))
        assert result["verdict"] == "no_signal"

    def test_missing_transcript_path_is_no_signal(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler
        result = _run(_handler({"agent_id": "abc123", "target_paths": "a.py"}, repo_root="root"))
        assert result["verdict"] == "no_signal"

    def test_missing_target_paths_is_no_signal(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler
        result = _run(_handler({"agent_id": "abc123", "transcript_path": "x"}, repo_root="root"))
        assert result["verdict"] == "no_signal"

    def test_missing_repo_root_is_no_signal(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler
        result = _run(_handler(
            {"agent_id": "abc123", "transcript_path": "x", "target_paths": "a.py"},
            repo_root=None,
        ))
        assert result["verdict"] == "no_signal"

    def test_hostile_agent_id_path_traversal_is_no_signal(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler
        repo_root = _init_repo(tmp_path)
        parent = repo_root / "transcript.jsonl"
        parent.write_text("{}\n")
        result = _run(_handler(
            _params(parent, "../../escaped", "target.py"),
            repo_root=str(repo_root / ".git"),
        ))
        assert result["verdict"] == "no_signal"

    def test_git_status_probe_failure_is_no_signal(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler

        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        parent = not_a_repo / "transcript.jsonl"
        parent.write_text("{}\n")
        agent_id = "nogitrepoagent01"
        _write_subagent_transcript(parent, agent_id, [])

        result = _run(_handler(
            _params(parent, agent_id, "target.py"),
            repo_root=str(not_a_repo / ".git"),
        ))
        assert result["verdict"] == "no_signal"


class TestVerifyTargetClean:
    def test_clean_target_reports_clean(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import verify_target_clean
        repo_root = _init_repo(tmp_path)
        verdict = verify_target_clean(str(repo_root / ".git"), ["target.py"])
        assert verdict.startswith("CLEAN")

    def test_dirty_target_reports_dirty(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import verify_target_clean
        repo_root = _init_repo(tmp_path)
        (repo_root / "target.py").write_text("edited\n")
        verdict = verify_target_clean(str(repo_root / ".git"), ["target.py"])
        assert verdict.startswith("DIRTY")


class TestRegistrationQuad:
    def test_op_present_in_all_four_surfaces(self) -> None:
        from coordinator_core.hooks import subagent_fabrication_check  # noqa: F401 — self-registers
        from coordinator_core.ipc import _REGISTRY
        from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
        from coordinator_core.op_scopes import _OP_KEY_SCOPE
        from coordinator_core.ops._registry_map import OP_MODULE_MAP

        op_key = "hooks.subagent_fabrication_check"
        assert op_key in _REGISTRY
        assert OP_CLASSIFICATION[op_key] == OpClass.COMPUTE_ONLY
        assert _OP_KEY_SCOPE[op_key] == "common_dir"
        assert OP_MODULE_MAP[op_key] == "coordinator_core.hooks"
