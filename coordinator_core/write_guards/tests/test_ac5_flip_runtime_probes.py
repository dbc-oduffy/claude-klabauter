"""AC5 runtime probes (docs/plans/2026-08-06-apply-guard-class-census.md) --
the acceptance criterion the whole plan hangs on: declared class and actual
RUNTIME behaviour must agree. A probe that calls `module.check(payload)`
directly bypasses the engine's two-phase ordering and cannot detect a
swallowed slot (an incumbent hard-deny/advisory shadowing the flipped
guard's own envelope) -- every probe here goes through the REAL entrypoint,
`coordinator_core.write_guards.engine.evaluate`, exactly as the DoE
PreToolUse dispatcher calls it.

Covers the write-side wave's nine C2-C12 hard-deny -> advisory flips
(DR-277, docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md):
block_cutover_phase_hand_edit, block_dev_repo_sentinel_write,
block_em_hand_edit_pending_review_integration, block_priority_ledger_edit,
check_claude_md_size, guard_memory_store_cap,
nudge_improvement_queue_write, nudge_prose_queue_creation.

Bash-side flips (see `test_ac5_bash_flip_runtime_probes.py` in
`coordinator_core/bash_guards/tests/`) are covered separately, through
`bash_guards.dispatch.evaluate_payload_json`, because they run through a
different real entrypoint on a different two-band ordering.

Each probe:
  1. Reconstructs a payload that DENIED under the guard's PRE-flip
     behaviour (drawn from that guard's own pre-existing test corpus).
  2. Drives it through `engine.evaluate`, not `module.check`.
  3. Confirms the envelope now carries no `permissionDecision` (an
     advisory-shaped envelope, `additionalContext` only) -- the write-side
     advisory contract (`engine.py`'s advisory phase never emits
     `permissionDecision`).
  4. Confirms the envelope's `additionalContext` carries THIS guard's own
     distinguishing text (not a swallowed/incumbent advisory's) -- the
     "is it CONFIRMED it's this guard's envelope, not an incumbent's" half
     of AC5.

Spec backlink: pln-apply-the-guard-class-census-u-4cae4a, AC5.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import engine
from coordinator_core.write_guards import block_cutover_phase_hand_edit
from coordinator_core.write_guards import block_dev_repo_sentinel_write
from coordinator_core.write_guards import block_em_hand_edit_pending_review_integration
from coordinator_core.write_guards import block_priority_ledger_edit
from coordinator_core.write_guards import check_claude_md_size
from coordinator_core.write_guards import guard_memory_store_cap
from coordinator_core.write_guards import nudge_improvement_queue_write
from coordinator_core.write_guards import nudge_prose_queue_creation
from coordinator_core.claude_md_budget import DEV_REPO_SENTINEL, HARD_LIMIT_BYTES


def _assert_advisory_not_deny(out, *, must_contain: str):
    assert out is not None, "flip regression: engine.evaluate returned ALLOW, expected advisory"
    hso = out["hookSpecificOutput"]
    assert "permissionDecision" not in hso, (
        "flip did NOT take effect: engine.evaluate still returned a "
        "permissionDecision (deny-shaped) envelope: %r" % out
    )
    assert "additionalContext" in hso
    assert must_contain in hso["additionalContext"], (
        "advisory envelope came back but without this guard's own "
        "distinguishing text -- possibly an INCUMBENT guard's advisory "
        "swallowed the slot instead: %r" % out
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

    def test_former_deny_now_advises_through_engine(self, tmp_path, monkeypatch):
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
        _assert_advisory_not_deny(out, must_contain="cutover-cli advance")


class TestBlockDevRepoSentinelWrite:
    def test_former_deny_now_advises_through_engine(self):
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/repo/.coordinator-dev-repo"},
        }
        out = engine.evaluate(payload)
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert "permissionDecision" not in hso
        assert hso["additionalContext"]


class TestBlockEmHandEditPendingReviewIntegration:
    _FINDINGS_FRONTMATTER = (
        "---\nstatus: open\nagent_type: coordinator:code-reviewer\n"
        "spawned_at: 2026-07-27T00:00:00Z\nlead_session_id: sess-abc\n"
        "divergence:\n  diverged: false\ncommits: []\ndispatch_feed: null\n---\n\n"
    )
    _TARGET_FILE = "coordinator_core/write_guards/block_priority_ledger_edit.py"
    _TARGET_BASENAME = "block_priority_ledger_edit.py"

    def _findings_body(self) -> str:
        citation = (
            f"- [P2] `{self._TARGET_BASENAME}:42` unused import — disposition: accepted — "
            "rationale: dead import, safe to drop.\n\n"
        )
        return "## Findings\n\n" + citation

    def test_former_deny_now_advises_through_engine(self, tmp_path, monkeypatch):
        monkeypatch.delenv(
            block_em_hand_edit_pending_review_integration._OVERRIDE_ENV_VAR, raising=False
        )
        sidecar_dir = tmp_path / "state" / "subagent-share" / "sess-abc"
        sidecar_dir.mkdir(parents=True)
        (sidecar_dir / "codereview-sliceA.md").write_text(
            self._FINDINGS_FRONTMATTER + self._findings_body(), encoding="utf-8"
        )
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": self._TARGET_FILE,
                "old_string": "x",
                "new_string": "y",
            },
            "cwd": str(tmp_path),
            "session_id": "sess-abc",
        }
        out = engine.evaluate(payload)
        _assert_advisory_not_deny(out, must_contain="review")


class TestBlockPriorityLedgerEdit:
    @pytest.mark.pending_fix(
        reason=(
            "state/bug-backlog/2026-08-06-priority-ledger-advisory-is-swallowed-"
            "by-7d2cb865e06f.yaml -- red on purpose, pinning the live "
            "slot-swallow rather than padding around it."
        )
    )
    def test_former_deny_now_advises_through_engine(self, monkeypatch):
        monkeypatch.delenv(block_priority_ledger_edit._OVERRIDE_ENV_VAR, raising=False)
        # A bare "priority: urgent\n" body -- the real trigger shape, no
        # padding. `validate_frontmatter_schema_advisory` (PRIORITY 100, an
        # EARLIER advisory slot) also matches this path, wins the "first
        # non-None advisory wins" race, and swallows this guard's own
        # envelope entirely -- so a real priority-ledger write gets a schema
        # warning instead of the "hand-editing disk truth" advisory that
        # exists for it. The fix belongs in the swallowing guard (stand down
        # for `state/priority-ledger/*`), whose module is held by another
        # live session; this probe stays un-padded so it goes green the
        # moment that lands.
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/priority-ledger/hnd-abc123.yaml",
                "content": "priority: urgent\n",
            },
        }
        out = engine.evaluate(payload)
        _assert_advisory_not_deny(out, must_contain="priority-set")


class TestCheckClaudeMdSize:
    def test_former_deny_now_advises_through_engine(self, tmp_path):
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
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert "permissionDecision" not in hso
        assert hso["additionalContext"]


class TestGuardMemoryStoreCap:
    def test_former_deny_now_advises_through_engine(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        mem = home / ".claude" / "projects" / "-Some-project" / "memory"
        mem.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv("CLAUDE_HOME", raising=False)
        target = str(mem / "MEMORY.md")
        content = "# Memory Index\n\n" + ("x" * 2100)
        payload = {"tool_name": "Write", "tool_input": {"file_path": target, "content": content}}
        out = engine.evaluate(payload)
        _assert_advisory_not_deny(out, must_contain="cap")


class TestNudgeImprovementQueueWrite:
    def test_former_deny_now_advises_through_engine(self):
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/improvement-queue/new-item.yaml",
                "content": "title: test\ndescription: a thing",
            },
        }
        out = engine.evaluate(payload)
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert "permissionDecision" not in hso
        assert hso["additionalContext"]


class TestNudgeProseQueueCreation:
    def test_former_deny_now_advises_through_engine(self, tmp_path, monkeypatch):
        monkeypatch.delenv(nudge_prose_queue_creation._OVERRIDE_ENV_VAR, raising=False)
        target = tmp_path / "state" / "improvement-queue.md"
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "# queue\n"},
            "cwd": str(tmp_path),
        }
        out = engine.evaluate(payload)
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert "permissionDecision" not in hso
        assert hso["additionalContext"]


# Guard the guards above: none of them should ever be shadowed silently by
# unrelated ambient state leaking priority ordering -- a sanity check that
# every probe above actually reached its OWN advisory phase slot, not some
# earlier-firing guard's.
@pytest.mark.parametrize(
    "module,priority",
    [
        (block_cutover_phase_hand_edit, 112),
        (block_dev_repo_sentinel_write, 122),
        (block_em_hand_edit_pending_review_integration, 115),
        (block_priority_ledger_edit, 114),
        (check_claude_md_size, 106),
        (guard_memory_store_cap, 121),
        (nudge_improvement_queue_write, 120),
        (nudge_prose_queue_creation, 119),
    ],
)
def test_flipped_module_is_registered_advisory_at_expected_slot(module, priority):
    assert module.CLASS == "advisory"
    assert module.PRIORITY == priority
