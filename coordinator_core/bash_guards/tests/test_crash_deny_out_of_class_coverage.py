"""Coverage contract for the crash-path out-of-class skip.

When a fail-closed guard crashes, `dispatch._crash_deny` denies every Bash
command on the box unless `_crash_deny_is_out_of_class` proves the command is
outside the crashed guard's class. That proof needs a per-guard trigger in
`_CRASH_TRIGGER_SUBSTRINGS`; a guard with no cheap necessary precondition is
named in `_CRASH_DENY_EXEMPT` instead. This file pins both halves:

  - every fail-closed guard in the roster is in exactly one of the two tables,
    so a new guard cannot ship with an accidental chain-wide deny-on-crash;
  - every mapped guard's trigger matches commands the guard itself denies,
    run through the guard's own check function.

The denial cases are data, not a table of strings nothing denies: each runner
asserts the guard produced a deny before the trigger is consulted, so a case
that rots into an allow fails loudly rather than making the property vacuous.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from coordinator_core.bash_guards import (
    block_approval_sentinel_creation,
    block_disarm_marker_sentinel_creation,
    block_fleet_delegation_creation,
    block_stash_destruction,
    block_topic_branch,
    block_subagent_findings_reject,
    block_subagent_grant_acquisition,
    block_subagent_guard_grant,
    block_subagent_stash_creation,
    block_worktree_creation,
    block_worktree_sentinel_creation,
    dispatch,
    dispatch_checks as dc,
    guard_repo_setup_claude_home_refusal,
    p4_verb_fence,
)
from coordinator_core.bash_guards.roster import guard_roster
from coordinator_core.session import touch_record

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_SUBAGENT = "agent-crash-coverage"


def _fail_closed_ids() -> List[str]:
    return [entry.id for entry in guard_roster() if entry.fail_closed]


def _exempt_names() -> List[str]:
    return [name for name, _reason in dispatch._CRASH_DENY_EXEMPT]


def test_every_fail_closed_guard_is_mapped_or_exempt_never_both_never_neither() -> None:
    mapped = set(dispatch._CRASH_TRIGGER_SUBSTRINGS)
    exempt = set(_exempt_names())
    both = sorted(mapped & exempt)
    neither = sorted(g for g in _fail_closed_ids() if g not in mapped and g not in exempt)
    assert not both, f"in both _CRASH_TRIGGER_SUBSTRINGS and _CRASH_DENY_EXEMPT: {both}"
    assert not neither, (
        "fail-closed guards with no crash-path decision; map each in "
        f"_CRASH_TRIGGER_SUBSTRINGS or name it in _CRASH_DENY_EXEMPT: {neither}"
    )


def test_exempt_tuple_names_only_registered_fail_closed_guards_once_with_a_reason() -> None:
    names = _exempt_names()
    assert len(names) == len(set(names)), "duplicate _CRASH_DENY_EXEMPT entries"
    unknown = sorted(set(names) - set(_fail_closed_ids()))
    assert not unknown, f"_CRASH_DENY_EXEMPT names non-fail-closed or unknown guards: {unknown}"
    for name, reason in dispatch._CRASH_DENY_EXEMPT:
        assert reason.strip() and "\n" not in reason, f"{name}: reason must be one non-empty line"


def test_every_trigger_is_nonempty_and_lowercase() -> None:
    # `_crash_probe_variants` lowercases the text it searches; an uppercase
    # trigger could never match and would silently narrow its guard.
    for name, triggers in dispatch._CRASH_TRIGGER_SUBSTRINGS.items():
        assert triggers, f"{name}: empty trigger tuple skips the guard on every command"
        for token in triggers:
            assert token and token == token.lower(), f"{name}: trigger {token!r} must be lowercase"


class _Ctx:
    def __init__(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch


def _bash(cmd: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    payload.update(extra)
    return payload


def _subagent(cmd: str) -> Dict[str, Any]:
    return _bash(cmd, agent_id=_SUBAGENT)


def _passthrough_guard_level(ctx: _Ctx, *modules: Any) -> None:
    for module in modules:
        if hasattr(module, "apply_guard_level"):
            ctx.monkeypatch.setattr(module, "apply_guard_level", lambda _name, envelope, risk="": envelope)


def _check_module(module: Any, make_payload: Callable[[str], Dict[str, Any]], entry: str = "check"):
    def run(cmd: str, ctx: _Ctx) -> Optional[Dict[str, Any]]:
        _passthrough_guard_level(ctx, module)
        return getattr(module, entry)(make_payload(cmd))

    return run


def _git_revert(cmd: str, ctx: _Ctx) -> Optional[Dict[str, Any]]:
    return dc._check_destructive_git_revert_full(cmd, "sess", hook_payload=_bash(cmd))[0]


def _blanket_add(cmd: str, ctx: _Ctx) -> Optional[Dict[str, Any]]:
    ctx.monkeypatch.setattr(dc, "_is_hazard_repo", lambda _root: True)
    return dc.check_blanket_git_add(cmd, "sess", hook_payload=_bash(cmd))


def _rm(cmd: str, ctx: _Ctx) -> Optional[Dict[str, Any]]:
    ctx.monkeypatch.delenv("COORDINATOR_OVERRIDE_DESTRUCTIVE_RM", raising=False)
    return dc.check_destructive_rm(cmd, "sess", _bash(cmd))


def _stale_write(cmd: str, ctx: _Ctx) -> Optional[Dict[str, Any]]:
    root = str(ctx.tmp_path)
    os.makedirs(os.path.join(root, ".git"), exist_ok=True)
    target = os.path.join(root, "F.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("original\n")
    digest = touch_record.compute_content_hash(target)
    touch_record.append_touch_claims(["F.txt"], "sess-A", root, content_hashes={"F.txt": digest})
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("peer changed this\n")
    return dc.check_stale_write(cmd, "sess-A", root)


def _p4(cmd: str, ctx: _Ctx) -> Optional[Dict[str, Any]]:
    ctx.monkeypatch.setattr(p4_verb_fence, "_is_p4_gated", lambda _cwd: True)
    return p4_verb_fence.check(_bash(cmd, cwd=str(ctx.tmp_path)))


def _repo_setup(cmd: str, ctx: _Ctx) -> Optional[Dict[str, Any]]:
    claude_home = ctx.tmp_path / ".claude"
    claude_home.mkdir(exist_ok=True)
    return guard_repo_setup_claude_home_refusal.check(
        _bash(cmd, cwd=str(claude_home), env={"HOME": str(ctx.tmp_path)})
    )


def _sentinel(module: Any, entry: str = "check") -> Callable[[str, _Ctx], Optional[Dict[str, Any]]]:
    return _check_module(module, _bash, entry)


def _split(basename: str) -> str:
    return f"touch {basename[:9]}''{basename[9:]}"


_APPROVAL = ".coordinator-doctrine-edit-approved"
_WORKTREE_SENTINEL = ".coordinator-override-worktree-guard"
_FLEET = "fleet-delegation.json"
_DISARM = ".coordinator-bash-guards-disarmed"

Runner = Callable[[str, _Ctx], Optional[Dict[str, Any]]]

# (guard id, command, runner). Commands for the seven original guards come from
# test_crash_trigger_is_wider_than_its_guard.py; the rest from each guard's own
# tests where one exists, else the shape its module docstring names.
_DENIED: List[Tuple[str, str, Runner]] = [
    ("no-verify", "git commit --no-verify -m wip", lambda c, x: dc.check_no_verify(c, "sess", hook_payload=_bash(c))),
    ("destructive-git-orphan", "git reset --hard HEAD~3", lambda c, x: dc.check_destructive_git_orphan(c, "sess", payload=_bash(c))),
    ("destructive-git-orphan", 'gi"a b"t reset --hard HEAD~3', lambda c, x: dc.check_destructive_git_orphan(c, "sess", payload=_bash(c))),
    ("destructive-git-clean", "git clean -fdx", lambda c, x: dc.check_destructive_git_clean(c, "sess", payload=_bash(c))),
    ("destructive-git-revert", "git reset --hard HEAD~3", _git_revert),
    ("blanket-git-add", "git add -A", _blanket_add),
    ("destructive-rm", "rm -rf $(cat targets.txt)", _rm),
    ("runaway-find", "find / -name '*.py'", lambda c, x: dc.check_runaway_find(c, "sess", payload=_bash(c))),
    ("stale-write", "echo hi > F.txt", _stale_write),
    ("stale-write", "echo hi | tee F.txt", _stale_write),
    ("block-worktree-creation", "git worktree add ../wt main", _check_module(block_worktree_creation, _bash)),
    ("block-worktree-creation", "sh -c 'git worktree add ../wt main'", _check_module(block_worktree_creation, _bash)),
    ("p4-verb-fence", "p4 submit -d wip", _p4),
    ("p4-verb-fence", "git clean -fdx", _p4),
    ("p4-verb-fence", "chmod +w locked.txt", _p4),
    ("p4-verb-fence", "P4ALIASES=x ls", _p4),
    ("block-stash-destruction", "git stash drop", _check_module(block_stash_destruction, _bash)),
    ("block-stash-destruction", "sh -c 'git stash clear'", _check_module(block_stash_destruction, _bash)),
    ("block-topic-branch", "git checkout -b topic/x", _check_module(block_topic_branch, _bash)),
    ("block-topic-branch", "git push origin topic/x", _check_module(block_topic_branch, _bash)),
    ("block-subagent-stash-creation", "git stash", _check_module(block_subagent_stash_creation, _subagent)),
    ("block-subagent-stash-creation", "git stash push -u", _check_module(block_subagent_stash_creation, _subagent)),
    (
        "block-subagent-grant-acquisition",
        "python -m coordinator_core.session.claude_md_grant grant",
        _check_module(block_subagent_grant_acquisition, _subagent),
    ),
    (
        "block-subagent-grant-acquisition",
        'python3 -c "from coordinator_core.session.claude_md_grant import write_claude_md_write_grant as w; w()"',
        _check_module(block_subagent_grant_acquisition, _subagent),
    ),
    (
        "block-subagent-findings-reject",
        "python -m coordinator_core.ops.review_findings_ledger reject f1",
        _check_module(block_subagent_findings_reject, _subagent),
    ),
    (
        "block-subagent-findings-reject",
        "python3 coordinator/bin/review-findings-ledger.py targets --add x",
        _check_module(block_subagent_findings_reject, _subagent),
    ),
    (
        "block-subagent-guard-grant",
        "python -m coordinator_core.session.em_guard_grant grant",
        _check_module(block_subagent_guard_grant, _subagent),
    ),
    (
        "block-subagent-guard-grant",
        'python3 -c "from coordinator_core.session.em_guard_grant import write_em_guard_grant as w; w()"',
        _check_module(block_subagent_guard_grant, _subagent),
    ),
    ("guard-repo-setup-claude-home-refusal", "python3 -m coordinator_core.install.scaffold_structure", _repo_setup),
    ("guard-repo-setup-claude-home-refusal", "repo-setup-args-and-register --name demo", _repo_setup),
]

for _guard, _target, _module, _entry in (
    ("block-approval-sentinel-creation", _APPROVAL, block_approval_sentinel_creation, "check_ungated"),
    ("block-worktree-sentinel-creation", _WORKTREE_SENTINEL, block_worktree_sentinel_creation, "check"),
    ("block-fleet-delegation-creation", _FLEET, block_fleet_delegation_creation, "check"),
    ("block-disarm-marker-sentinel-creation", _DISARM, block_disarm_marker_sentinel_creation, "check"),
):
    _DENIED.extend(
        [
            (_guard, f"touch {_target}", _sentinel(_module, _entry)),
            (_guard, _split(_target), _sentinel(_module, _entry)),
            (_guard, "cat run.txt | bash", _sentinel(_module, _entry)),
            (_guard, "ls . | xargs touch", _sentinel(_module, _entry)),
        ]
    )


def _decision(result: Optional[Dict[str, Any]]) -> Optional[str]:
    return (result or {}).get("hookSpecificOutput", {}).get("permissionDecision")


@pytest.mark.parametrize(
    "guard,cmd,runner",
    _DENIED,
    ids=[f"{g}::{c[:48]}" for g, c, _ in _DENIED],
)
def test_trigger_matches_a_command_its_guard_denies(
    guard: str, cmd: str, runner: Runner, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    verdict = runner(cmd, _Ctx(tmp_path, monkeypatch))
    assert _decision(verdict) == "deny", f"{guard} no longer denies {cmd!r}; the case is vacuous: {verdict}"

    triggers = dispatch._CRASH_TRIGGER_SUBSTRINGS[guard]
    variants = dispatch._crash_probe_variants(cmd)
    assert any(token in variant for variant in variants for token in triggers), (
        f"{guard}'s triggers {triggers} are NARROWER than the guard: {cmd!r} is denied by "
        "the guard but would be skipped on the crash path"
    )
    assert not dispatch._crash_deny_is_out_of_class(guard, cmd)


def test_every_mapped_guard_has_a_denial_case() -> None:
    covered = {guard for guard, _cmd, _runner in _DENIED}
    missing = sorted(set(dispatch._CRASH_TRIGGER_SUBSTRINGS) - covered)
    assert not missing, f"mapped with no denial case, so its widening is asserted and not proved: {missing}"


def test_every_denial_case_names_a_mapped_guard() -> None:
    stray = sorted({g for g, _c, _r in _DENIED} - set(dispatch._CRASH_TRIGGER_SUBSTRINGS))
    assert not stray, f"denial cases for guards that are not mapped: {stray}"


def test_stash_guard_trigger_survives_a_cr_that_manufactures_the_keyword() -> None:
    assert not dispatch._crash_deny_is_out_of_class("block-stash-destruction", "git sta\rsh drop")


def test_a_command_outside_each_mapped_class_is_skippable() -> None:
    for guard in dispatch._CRASH_TRIGGER_SUBSTRINGS:
        assert dispatch._crash_deny_is_out_of_class(guard, "ls -la"), guard


def test_exempt_guards_never_skip_on_crash() -> None:
    for name in _exempt_names():
        assert not dispatch._crash_deny_is_out_of_class(name, "ls -la"), name


def test_the_reversed_half_of_crash_deny_stays_reversed() -> None:
    # The original decision rejected ANY pre-filter on the crash path; the
    # 2026-07-30 ruling re-admitted only derived, provably-wider ones. A guard
    # with no entry keeps the chain-wide deny.
    assert not dispatch._crash_deny_is_out_of_class("no-such-guard", "ls -la")
