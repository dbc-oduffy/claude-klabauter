"""Every command-word guard in coordinator_core.bash_guards matches executing command words only.

CLASSIFICATION audits each non-test entry-point module; GUARD_CASES drives one
(heredoc-body, quoted-argument, real-command) triple per command-word guard. Cases
for guards owned by plan rows C1-C3 carry pending_fix + xfail(strict=True) until
they are routed through the shared strip/split helper.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pytest

from coordinator_core.bash_guards import (
    _dc_commit,
    _dc_rewrites,
    bump_foreign_repo_write,
    bump_outside_repo_write,
    block_dev_repo_sentinel_removal,
    block_editor_kill_by_name,
    block_empty_source_push,
    block_perforce_submit,
    block_stash_destruction,
    block_subagent_commit,
    block_subagent_destructive_action,
    block_whole_filesystem_scan,
    block_worktree_creation,
    check_raw_pid_liveness,
    check_test_suite_invocation,
    find_exec_rewrite,
    guard_background_publish,
    guard_head_tail_rewrite,
    guard_headless_claude_plugin_dir,
    guard_move_from_inside_cwd,
    guard_no_optional_locks,
    guard_offer_git_c,
    guard_offer_invoke_params_stdin,
    guard_piped_pytest_exit,
    guard_reap_stale_git_lock,
    guard_trailing_background_ampersand,
    multiprobe_banner_rewrite,
)

pytestmark = pytest.mark.complete

GUARDS_DIR = Path(__file__).resolve().parent.parent

FIXED = "calls _strip_heredoc_bodies / _awk_quote_aware_split before matching"
TOKENIZED = "matches through the quote-aware tokenizer / segment resolver; pinned by its GUARD_CASES row"
NEEDS_HELPER = "matches command words on raw text; owned by plan rows C1-C3"
EXEMPT = {
    "block_approval_sentinel_creation": "matches file-write targets of the sentinel name, not a command word",
    "block_external_workspace_vcs_write": "classifies the cwd workspace, not command words",
    "block_illegal_filename": "matches a written filename token, not a command word",
    "block_reviewer_bash_outside_allowlist": "tokenizes the head against an allowlist; unlisted heads are denied by design",
    "block_subagent_plan_body_bash_write": "matches write targets under plan files, not command words",
    "block_worktree_sentinel_creation": "matches the sentinel path argument, not a command word",
    "guard_disk_fill": "matches a byte-count argument shape, not a command word",
    "guard_em_post_fire_shell_write": "matches write targets after a fire marker, not a command word",
    "guard_heavy_command_admission": "classifies the launch via the heavy-command allowlist, not a bare command word",
    "guard_host_subagent_bash_ban": "gates on the caller identity, not on command text",
    "guard_host_subagent_bash_spawn_shapes": "gates on caller identity plus spawn-shape classifier output",
    "guard_post_executor_verification": "reads the hook payload's tool result, not command words",
    "guard_repo_setup_claude_home_refusal": "matches the setup entry path argument, not a command word",
    "guard_subagent_heavy_ue_launch": "matches UE launch binaries via the heavy-command classifier",
    "guard_xs_lane_shell_write": "matches write targets, not a command word",
    "guard_powershell_via_bash": "matches a shell-interpreter head via the tokenizer; dialect hop is its subject",
    "guard_doctrine_surface_bash_write": "matches write targets under governed surfaces; strips heredocs already",
    "guard_deploy_dirty_tree": "matches the deploy script path through resolve_command_positions",
    "guard_uncapped_pytest_plugin": "matches pytest flags through resolve_command_positions",
    "block_noncanonical_branch_creation": "tokenized via resolve_command_positions; needs live repo state to fire",
    "block_topic_branch": "tokenized via resolve_command_positions; needs live repo state to fire",
    "block_push_pr_non_owned_remote": "tokenized via resolve_command_positions; needs a remote registry to fire",
    "block_home_root_bash_write": "tokenized; fires on the machine home root, so no portable real command",
    "block_drive_root_new_dir_bash_write": "tokenized and already strips heredocs; fires on a machine drive root",
    "guard_grep_via_bash": "tokenized via segments_from_tokens; fires only on Windows hosts",
    "guard_multiprobe_banner": "tokenized via the shape classifier; fires only on Windows hosts",
    "roster": "guard registry; matches nothing",
    "p4_verb_fence":"already strips heredocs; fires only with a p4 fence configured",
    "commit_tripwires": "seven tripwires over staged state; the one command-reading check calls _awk_quote_aware_split",
}

# module name -> classification
CLASSIFICATION: Dict[str, str] = {
    "_dc_blanket_add": FIXED,
    "_dc_git_misc": FIXED,
    "_dc_rm": FIXED,
    "block_subagent_commit": FIXED,
    "block_subagent_destructive_action": FIXED,
    "block_stash_destruction": FIXED,
    "block_worktree_creation": FIXED,
    "check_raw_pid_liveness": FIXED,
    "check_test_suite_invocation": FIXED,
    "_dc_rewrites": FIXED + " (partial; rewrite twins pinned below)",
    "_dc_commit": TOKENIZED,
    "block_dev_repo_sentinel_removal": TOKENIZED,
    "block_editor_kill_by_name": TOKENIZED,
    "block_empty_source_push": TOKENIZED,
    "block_perforce_submit": TOKENIZED,
    "block_whole_filesystem_scan": TOKENIZED,
    "bump_foreign_repo_write": TOKENIZED,
    "bump_outside_repo_write": TOKENIZED,
    "find_exec_rewrite": TOKENIZED,
    "guard_background_publish": TOKENIZED,
    "guard_head_tail_rewrite": TOKENIZED,
    "guard_inprocess_search": TOKENIZED,
    "guard_no_optional_locks": TOKENIZED,
    "guard_offer_git_c": TOKENIZED,
    "guard_plumbing_and_loops": TOKENIZED,
    "guard_reap_stale_git_lock": NEEDS_HELPER,
    "guard_headless_claude_plugin_dir": NEEDS_HELPER,
    "guard_move_from_inside_cwd": NEEDS_HELPER,
    "guard_offer_invoke_params_stdin": NEEDS_HELPER,
    "guard_piped_pytest_exit": NEEDS_HELPER,
    "guard_trailing_background_ampersand": NEEDS_HELPER,
    "multiprobe_banner_rewrite": TOKENIZED,
    "rewrite_repo_root_search": TOKENIZED,
    **{name: f"EXEMPT: {why}" for name, why in EXEMPT.items()},
}

_ENTRY_RE = re.compile(r"^def (check|check_[a-z_0-9]+|guard_[a-z_0-9]+)\(", re.MULTILINE)


def _entry_modules():
    for path in sorted(GUARDS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        if _ENTRY_RE.search(path.read_text(encoding="utf-8")):
            yield path.stem


def test_every_entry_module_is_classified():
    unclassified = [m for m in _entry_modules() if m not in CLASSIFICATION]
    assert not unclassified, f"classify in CLASSIFICATION (or EXEMPT with a reason): {unclassified}"


def test_classification_names_only_existing_modules():
    existing = {p.stem for p in GUARDS_DIR.glob("*.py")}
    assert not [m for m in CLASSIFICATION if m not in existing]


def test_exempt_reasons_are_nonempty():
    assert all(reason.strip() for reason in EXEMPT.values())


@dataclass
class Env:
    root: Path
    mp: pytest.MonkeyPatch

    def payload(self, cmd: str, *, agent: bool = False) -> Dict[str, Any]:
        p: Dict[str, Any] = {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": "sess-c0",
            "cwd": str(self.root),
        }
        if agent:
            p["agent_id"] = "abc123def456"
            p["agent_type"] = "coordinator:executor"
        return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(tmp_path)
    return Env(tmp_path, monkeypatch)


def _fired(result: Any) -> bool:
    return result is not None


def _reap_spy(cmd: str, e: Env) -> bool:
    calls = []
    e.mp.setattr(guard_reap_stale_git_lock, "_reap_candidate_locks", lambda git_dir: calls.append(git_dir))
    guard_reap_stale_git_lock.check_reap_stale_git_lock(cmd, str(e.root), "s")
    return bool(calls)


def _move_cwd(cmd: str, e: Env) -> bool:
    return _fired(
        guard_move_from_inside_cwd.check_move_from_inside_cwd(
            cmd, "s", payload=e.payload(cmd), cwd=e.root.as_posix()
        )
    )


def _armed_perforce(cmd: str, e: Env) -> bool:
    e.mp.setattr(block_perforce_submit, "_armed", lambda: True)
    return _fired(block_perforce_submit.check(e.payload(cmd)))


def _headless(cmd: str, e: Env) -> bool:
    e.mp.setattr(guard_headless_claude_plugin_dir, "_plugin_root", lambda: "/plugin-root")
    e.mp.setattr(guard_headless_claude_plugin_dir, "_is_dev_install", lambda: True)
    return _fired(guard_headless_claude_plugin_dir.check_headless_claude_plugin_dir(cmd, "s"))


def _candidates(fn: Callable[..., Any], cmd: str, e: Env) -> bool:
    return bool(list(fn(cmd, e.root.as_posix())))


@dataclass(frozen=True)
class GuardCase:
    guard_id: str
    call: Callable[[str, Env], bool]
    real_cmd: str
    owner: Optional[str] = None  # plan row (C1-C3) that routes this guard through the helper

    @property
    def trigger_cmd_in_heredoc(self) -> str:
        return f"cat <<'EOF'\n{self.real_cmd}\nEOF"

    @property
    def trigger_cmd_in_quotes(self) -> str:
        return 'echo "' + self.real_cmd.replace('"', "'") + '"'


def _payload_guard(fn, *, agent: bool = False) -> Callable[[str, Env], bool]:
    return lambda cmd, e: _fired(fn(e.payload(cmd, agent=agent)))


_BANNER = 'echo "=== SESSION FACTS ==="; pwd; whoami; git status --short'
_INVOKE = (
    "PYTHONPATH=/r python3 -m coordinator_core.invoke "
    "ceremony.scoped_git_commit '{\"m\": \"it's\"}' --repo /r --bare"
)

GUARD_CASES = [
    GuardCase("no_optional_locks", lambda c, e: _fired(guard_no_optional_locks.check_git_no_optional_locks(c, "s", payload=e.payload(c))), "git status", "C1"),
    GuardCase("reap_stale_git_lock", _reap_spy, "git commit -m x", "C1"),
    GuardCase("offer_git_c", lambda c, e: _fired(guard_offer_git_c.check_offer_git_c(c, "s", str(e.root))), "cd /repo && git log -1", "C1"),
    GuardCase("dev_repo_sentinel_removal", _payload_guard(block_dev_repo_sentinel_removal.check), "rm .coordinator-dev-repo", "C1"),
    GuardCase("safe_commit_advise", lambda c, e: _fired(_dc_commit.check_git_commit_safe_commit_advise(c)), "git add one.py && git commit -m wip", "C1"),
    GuardCase("dc_find_exec_rewrite", lambda c, e: _fired(_dc_rewrites.check_find_exec_rewrite(c, "s")), "find . -name x -exec grep foo {} \\;", "C1"),
    GuardCase("dc_multiprobe_banner", lambda c, e: _fired(_dc_rewrites.check_multiprobe_banner_rewrite(c, "s")), _BANNER, "C1"),
    GuardCase("dc_runaway_find", lambda c, e: _fired(_dc_rewrites.check_runaway_find(c, "s")), "find / -name x", "C1"),
    GuardCase("dc_sed_range_read", lambda c, e: _fired(_dc_rewrites.check_sed_range_read_advise(c, "s")), "sed -n 1,50p f.py", "C1"),
    GuardCase("dc_grep_via_bash", lambda c, e: _fired(_dc_rewrites.check_grep_via_bash_rewrite(c, "s")), "grep -rn foo src", "C1"),
    GuardCase("perforce_submit", _armed_perforce, "p4 submit -d x", "C2"),
    GuardCase("background_publish", _payload_guard(guard_background_publish.check_background_publish), "python percolate-round.py --dest x", "C2"),
    GuardCase("headless_claude_plugin_dir", _headless, "claude -p x", "C2"),
    GuardCase("trailing_background_ampersand", lambda c, e: _fired(guard_trailing_background_ampersand.check_trailing_background_ampersand(c, "s")), "sleep 1 &", "C2"),
    GuardCase("piped_pytest_exit", lambda c, e: _fired(guard_piped_pytest_exit.check_piped_pytest_exit(c, "s")), "pytest tests | tail -5", "C2"),
    GuardCase("offer_invoke_params_stdin", lambda c, e: _fired(guard_offer_invoke_params_stdin.check_offer_invoke_params_stdin(c, "s")), _INVOKE, "C2"),
    GuardCase("find_exec_rewrite", lambda c, e: _fired(find_exec_rewrite.check_find_exec_rewrite(c, "s")), "find . -name x -exec grep foo {} \\;", "C3"),
    GuardCase("head_tail_rewrite", lambda c, e: _fired(guard_head_tail_rewrite.check_head_tail_plumbing_rewrite(c, "s")), "grep -rn foo src | head -5", "C3"),
    GuardCase("multiprobe_banner_rewrite", lambda c, e: _fired(multiprobe_banner_rewrite.check_multiprobe_banner_rewrite(c, "s")), _BANNER, "C3"),
    GuardCase("move_from_inside_cwd", _move_cwd, "mv {ROOT} {ROOT}z", "C3"),
    GuardCase("bump_foreign_repo_write", lambda c, e: _candidates(bump_foreign_repo_write._iter_write_sink_candidates, c, e), "git -C {ROOT}x commit -m x", "C3"),
    GuardCase("bump_outside_repo_write", lambda c, e: _candidates(bump_outside_repo_write._iter_write_sink_candidates, c, e), "echo hi > {ROOT}x/p.txt", "C3"),
    GuardCase("stash_destruction", _payload_guard(block_stash_destruction.check), "git stash drop"),
    GuardCase("worktree_creation", _payload_guard(block_worktree_creation.check), "git worktree add ../x"),
    GuardCase("editor_kill_by_name", _payload_guard(block_editor_kill_by_name.check), "pkill UnrealEditor"),
    GuardCase("empty_source_push", _payload_guard(block_empty_source_push.check), "git push origin :main"),
    GuardCase("whole_filesystem_scan", _payload_guard(block_whole_filesystem_scan.check), "find / -name x"),
    GuardCase("raw_pid_liveness", _payload_guard(check_raw_pid_liveness.check), "kill -0 1234"),
    GuardCase("test_suite_invocation", _payload_guard(check_test_suite_invocation.check), "pytest -n auto tests"),
    GuardCase("subagent_destructive_action", _payload_guard(block_subagent_destructive_action.check, agent=True), "git push --force origin main"),
    GuardCase("subagent_commit", _payload_guard(block_subagent_commit.check, agent=True), "git commit -m x"),
]


def _cmd(case: GuardCase, text: str, e: Env) -> str:
    return text.replace("{ROOT}", e.root.as_posix())


# Guards whose heredoc/quoted case is red at HEAD; the owning plan row routes them through the helper.
PENDING_FIX = frozenset(
    {"headless_claude_plugin_dir", "piped_pytest_exit", "offer_invoke_params_stdin", "move_from_inside_cwd"}
)


def _param(case: GuardCase):
    if case.guard_id not in PENDING_FIX:
        return pytest.param(case, id=case.guard_id)
    return pytest.param(
        case,
        id=case.guard_id,
        marks=[
            pytest.mark.pending_fix,
            pytest.mark.xfail(strict=True, reason=f"{case.owner}: not yet routed through the shared helper"),
        ],
    )


@pytest.mark.spawns_process
@pytest.mark.parametrize("case", [pytest.param(c, id=c.guard_id) for c in GUARD_CASES])
def test_real_command_fires(case: GuardCase, env: Env):
    assert case.call(_cmd(case, case.real_cmd, env), env), f"{case.guard_id}: real command no longer fires"


@pytest.mark.spawns_process
@pytest.mark.parametrize("case", [_param(c) for c in GUARD_CASES])
def test_trigger_words_in_heredoc_and_quotes_do_not_fire(case: GuardCase, env: Env):
    heredoc = _cmd(case, case.trigger_cmd_in_heredoc, env)
    quoted = _cmd(case, case.trigger_cmd_in_quotes, env)
    assert not case.call(quoted, env), f"{case.guard_id}: fired on a quoted literal"
    assert not case.call(heredoc, env), f"{case.guard_id}: fired on a heredoc body"
