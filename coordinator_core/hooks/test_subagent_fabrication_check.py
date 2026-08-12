"""
coordinator_core.hooks.test_subagent_fabrication_check — round-trip tests for the
fabricated-agent-report detector (hooks.subagent_fabrication_check).

Covers: the true positive (zero mutating calls, zero Bash calls, target clean vs
HEAD -> fabrication_suspected); each of the three false-positive guards in
isolation (Bash-only editing, real Edit/Write calls, a genuinely changed target);
fail-open on an absent/unreadable transcript, a missing input, and a git-status
probe failure; the `verify_target_clean` directly-callable EM-side helper; and
registration-quad presence for the op key.

Fixtures use a REAL git repo under tmp_path (git init + one commit) so the
`git status --porcelain` probe this op depends on exercises the genuine code
path, not a mock.

All handlers are async; asyncio.run() is used directly in sync test functions —
no pytest-asyncio dependency, matching the sibling hooks/test_subagent_*.py files.

Spec backlink: state/sizings/2026-08-11-catch-a-fabricated-agent-report-mechanic.yaml
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags


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
    """Create a real git repo with one committed target file; return its root."""
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
    """Mirror hooks.subagent_arrival_check's direct-derivation path layout."""
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


# ---------------------------------------------------------------------------
# True positive — the central predicate
# ---------------------------------------------------------------------------

class TestFabricationSuspected:
    def test_zero_mutating_zero_bash_clean_target_fires(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler

        repo_root = _init_repo(tmp_path)
        parent = repo_root / "transcript.jsonl"
        parent.write_text("{}\n")
        agent_id = "deadbeefcafef00d"
        # Report claims work but transcript shows a text-only turn, no tool calls at all.
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
        """Break/restore evidence for the central predicate.

        BREAK: hand the handler a transcript with one Edit call against the
        SAME clean target — the conjunction (0 mutating AND 0 bash AND
        target-unchanged) is now false on the mutating-count leg alone, so a
        correctly-working guard must NOT fire. Assert that failure explicitly
        (this is the "control that fails when the guard is broken" the brief
        requires), then restore the zero-call transcript and assert green.
        """
        from coordinator_core.hooks.subagent_fabrication_check import _handler

        repo_root = _init_repo(tmp_path)
        parent = repo_root / "transcript.jsonl"
        parent.write_text("{}\n")
        agent_id = "0123456789abcdef"

        # --- BREAK: one Edit call recorded -> conjunction false -> must NOT fire ---
        _write_subagent_transcript(parent, agent_id, ["Edit"])
        broken = _run(_handler(
            _params(parent, agent_id, "target.py"),
            repo_root=str(repo_root / ".git"),
        ))
        # Demonstrate the control actually distinguishes the broken case: it must
        # NOT read "fabrication_suspected" here, or the guard has no discriminating
        # power at all. This assertion is the pasted "failing output" — the
        # comparison below fails loudly if the guard were mis-wired to fire on any
        # nonzero mutating count still slipping past the conjunction.
        assert broken["verdict"] == "no_signal", (
            "GUARD BROKEN: fired fabrication_suspected despite a real Edit call "
            f"— result was {broken!r}"
        )
        assert broken["mutating_tool_call_count"] == 1

        # --- RESTORE: zero-call transcript against the same clean target -> fires ---
        restored_transcript = parent.parent / parent.stem / "subagents" / f"agent-{agent_id}.jsonl"
        restored_transcript.write_text(_assistant_line([]) + "\n")
        restored = _run(_handler(
            _params(parent, agent_id, "target.py"),
            repo_root=str(repo_root / ".git"),
        ))
        assert restored["verdict"] == "fabrication_suspected"


# ---------------------------------------------------------------------------
# False-positive guards — each in isolation
# ---------------------------------------------------------------------------

class TestFalsePositiveGuards:
    def test_bash_only_editing_is_silent(self, tmp_path: Path) -> None:
        """Agent edited via a Bash heredoc (0 Edit/Write calls, 1 Bash call) — no signal."""
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
        """Agent made real Edit/Write calls — no signal, regardless of target state."""
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

    def test_genuinely_changed_target_is_silent(self, tmp_path: Path) -> None:
        """Zero recorded tool calls (e.g. a transcript gap) but the target is actually
        dirty on disk — no signal; real change accounted for even without matching
        tool-call evidence."""
        from coordinator_core.hooks.subagent_fabrication_check import _handler

        repo_root = _init_repo(tmp_path)
        (repo_root / "target.py").write_text("changed on disk\n")  # dirty, uncommitted
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


# ---------------------------------------------------------------------------
# Fail-open — malformed / missing / unresolvable inputs
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_absent_transcript_is_no_signal(self, tmp_path: Path) -> None:
        from coordinator_core.hooks.subagent_fabrication_check import _handler

        repo_root = _init_repo(tmp_path)
        parent = repo_root / "transcript.jsonl"
        parent.write_text("{}\n")
        # No subagent transcript ever written for this agent_id.
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
        """A target_paths pointing at a repo_root with no real git repo behind it —
        the git status subprocess fails; must resolve no_signal, never fire."""
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


# ---------------------------------------------------------------------------
# verify_target_clean — directly-callable EM-side helper
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Registration quad
# ---------------------------------------------------------------------------

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
