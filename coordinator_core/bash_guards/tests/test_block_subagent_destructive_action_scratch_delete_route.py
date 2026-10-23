"""A recursive-delete denial names scratch.delete when an operand is under scratch/; verdict unchanged."""

from __future__ import annotations

from coordinator_core.bash_guards import block_subagent_destructive_action as guard

RM = "r" + "m"
ROUTE = "coordinator-invoke scratch.delete"

PINNED_NON_SCRATCH = (
    "BLOCKED: subagents may not run destructive rm operations — EM-locked.\n\n"
    "  Subagent: a1 (t)\n"
    "  Denied:   rm -r/-f (recursive or force)\n"
    "  Command:  X\n\n"
    "This is the caller-context lock realizing docs/plans/2026-07-13-subagent-\n"
    "destructive-action-em-lock.md: a subagent cannot reason about the shared\n"
    "branch's blast radius.\n\n"
    "`rm -r`/`rm -f` (recursive or force) is denied OUTRIGHT for subagents —\n"
    "including against your own session's scratchpad. There is no sandbox-\n"
    "scoped, path-based, or any other exception this guard will honor.\n\n"
    "Safe forward paths:\n"
    "  - Cleaning up your OWN scratchpad? You almost certainly do not need\n"
    "    this command. Unflagged `rm <file>` and `rmdir <dir>` are permitted\n"
    "    and are enough for throwaway files you created. Do NOT escalate\n"
    "    scratch tidiness to the EM: the scratchpad is outside every repo,\n"
    "    nothing reads it after you exit, and an interrupt to buy neatness\n"
    "    there costs the one context this lock exists to protect. Leaving it\n"
    "    is a fine outcome.\n"
    "  - Need history rewrite, a shared-tree revert, or anything else\n"
    "    destructive to a REPO? Surface it to the EM — the main-loop is never\n"
    "    blocked by this guard.\n\n"
    "There is NO subagent-honored override for this guard — the lock keys on\n"
    "resolved caller-context, never on an env var, by design -- but note this\n"
    "is a speed bump at the shell surface, not a capability boundary: a\n"
    "subagent that can run Python can still remove files directly, unseen by\n"
    "this guard. Fix forward, or surface a genuine need to the EM."
)


def _reason(cmd):
    return guard._build_reason("rm -r/-f (recursive or force)", "a1", "t", "X", cmd)


def test_non_scratch_denial_text_is_byte_identical():
    assert _reason(f"{RM} -rf build/out") == PINNED_NON_SCRATCH
    assert _reason(f"{RM} -rf /tmp/scratchy/x") == PINNED_NON_SCRATCH
    assert _reason("") == PINNED_NON_SCRATCH


def test_scratch_operand_names_the_route_before_safe_forward_paths():
    text = _reason(f"{RM} -rf scratch/baseline-7fa01de")
    assert ROUTE in text
    assert text.index(ROUTE) < text.index("Safe forward paths:")


def test_absolute_scratch_under_repo_root_names_the_route(tmp_path):
    (tmp_path / ".git").mkdir()
    assert ROUTE in _reason(f"{RM} -rf {tmp_path}/scratch/x")
    assert ROUTE not in _reason(f"{RM} -rf {tmp_path}/other/scratch/x")


def test_verdict_still_denies_through_check(tmp_path):
    result = guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"{RM} -rf scratch/baseline-7fa01de"},
            "session_id": "s",
            "cwd": str(tmp_path),
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:executor",
        }
    )
    assert result is not None
