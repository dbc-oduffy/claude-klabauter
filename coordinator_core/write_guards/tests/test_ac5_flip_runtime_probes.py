"""AC5 runtime probes for the write-side guards DR-277 demoted and the 2026-10-04 amendment
restored: declared class and RUNTIME behaviour must agree, probed through the real entrypoint
`engine.evaluate` (not `module.check`), so an incumbent guard swallowing the slot is caught.

At guard level strict (the author default, pinned suite-wide) each guard denies with its own
text; at warn (the consumer default) the policy point turns the same deny into an advisory.

Spec backlink: pln-apply-the-guard-class-census-u-4cae4a, AC5.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import engine
from coordinator_core.write_guards import block_cutover_phase_hand_edit
from coordinator_core.write_guards import block_dev_repo_sentinel_write
from coordinator_core.write_guards import block_priority_ledger_edit
from coordinator_core.write_guards import check_claude_md_size
from coordinator_core.write_guards import nudge_improvement_queue_write
from coordinator_core.write_guards import nudge_prose_queue_creation
from coordinator_core.claude_md_budget import DEV_REPO_SENTINEL, HARD_LIMIT_BYTES


def _assert_deny(out, *, must_contain: str = ""):
    assert out is not None, "engine.evaluate returned ALLOW, expected deny"
    hso = out["hookSpecificOutput"]
    assert hso.get("permissionDecision") == "deny", out
    assert must_contain in hso["permissionDecisionReason"], (
        "deny came back without this guard's own text -- an incumbent guard "
        "may have swallowed the slot: %r" % out
    )


class TestBlockCutoverPhaseHandEdit:
    def _make_repo(self, tmp_path: Path):
        cutovers_dir = tmp_path / "state" / "roadmap" / "lifecycle-vocab" / "cutovers"
        cutovers_dir.mkdir(parents=True)
        record_path = cutovers_dir / "closed-reason-terminal.md"
        record_path.write_text(
            "---\nsurface: closed_reason\nphase: dual-write\nconfirmed_consumers: []\n"
            "gate_source:\n  kind: value-vocabulary\n---\n\n# closed_reason cutover\n",
            encoding="utf-8",
        )
        return tmp_path, record_path

    def test_denies_through_engine(self, tmp_path, monkeypatch):
        repo_root, record_path = self._make_repo(tmp_path)
        monkeypatch.setattr(
            block_cutover_phase_hand_edit, "_resolve_git_root", lambda cwd: str(repo_root)
        )
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(record_path.relative_to(repo_root)).replace("\\", "/"),
                "old_string": "phase: dual-write",
                "new_string": "phase: retiring",
            },
            "cwd": str(repo_root),
        }
        out = engine.evaluate(payload)
        _assert_deny(out, must_contain="cutover-cli advance")


class TestBlockDevRepoSentinelWrite:
    def test_denies_through_engine(self):
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/repo/.coordinator-dev-repo"},
        }
        out = engine.evaluate(payload)
        _assert_deny(out)


class TestBlockPriorityLedgerEdit:
    def test_denies_through_engine(self, monkeypatch):
        monkeypatch.delenv(block_priority_ledger_edit._OVERRIDE_ENV_VAR, raising=False)
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/priority-ledger/hnd-abc123.yaml",
                "content": "priority: urgent\n",
            },
        }
        out = engine.evaluate(payload)
        _assert_deny(out, must_contain="priority-set")


class TestCheckClaudeMdSize:
    def test_denies_through_engine(self, tmp_path):
        (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
        (tmp_path / DEV_REPO_SENTINEL).write_text("sentinel", encoding="utf-8")
        coord_dir = tmp_path / "coordinator"
        coord_dir.mkdir()
        target = coord_dir / "CLAUDE.md"
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(target),
                "content": "x" * (HARD_LIMIT_BYTES + 1),
            },
        }
        out = engine.evaluate(payload)
        _assert_deny(out)


class TestNudgeImprovementQueueWrite:
    def test_denies_through_engine(self):
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/improvement-queue/new-item.yaml",
                "content": "title: test\ndescription: a thing",
            },
        }
        out = engine.evaluate(payload)
        _assert_deny(out)


class TestNudgeProseQueueCreation:
    def test_denies_through_engine(self, tmp_path, monkeypatch):
        monkeypatch.delenv(nudge_prose_queue_creation._OVERRIDE_ENV_VAR, raising=False)
        target = tmp_path / "state" / "improvement-queue.md"
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "# queue\n"},
            "cwd": str(tmp_path),
        }
        out = engine.evaluate(payload)
        _assert_deny(out)


@pytest.mark.parametrize(
    "module,priority",
    [
        (block_cutover_phase_hand_edit, 112),
        (block_dev_repo_sentinel_write, 122),
        (block_priority_ledger_edit, 114),
        (check_claude_md_size, 106),
        (nudge_improvement_queue_write, 120),
        (nudge_prose_queue_creation, 119),
    ],
)
def test_restored_module_is_registered_hard_deny_at_expected_slot(module, priority):
    assert module.CLASS == "hard-deny"
    assert module.PRIORITY == priority


def test_consumer_warn_level_turns_the_deny_into_an_advisory(monkeypatch):
    from coordinator_core import machine_profile

    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "warn")
    machine_profile.reset_cache()
    out = engine.evaluate(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/improvement-queue/new-item.yaml",
                "content": "title: test\ndescription: a thing",
            },
        }
    )
    hso = out["hookSpecificOutput"]
    assert hso.get("permissionDecision") != "deny"
    assert hso["additionalContext"].startswith(machine_profile.ADVISORY_PREFIX)
