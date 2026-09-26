"""Full cross-product attack corpus: SHAPES x CONFINEMENT_GUARDS, driven
through the real dispatcher (``dispatch.evaluate_payload_json``).

Genesis: coordinator:staff-eng (the Staff Engineer) review, DoE-claude
``state/subagent-share/468b3c12-ad00-4773-98e5-901ed8e085e6/
coordinatorstaff-eng-e44a8c47.md``, Findings 0-2 and 6. ``test_cd_prefix_
bypass.py`` is 80% of the right artifact (real dispatcher, attack-not-read)
but is defeated by its own per-guard ``extra_shapes`` opt-in: a shape can be
proven-safe for one guard and simply never tried against the other fifteen.
This file is the amendment -- ONE module-level ``SHAPES`` list, ONE
module-level ``CONFINEMENT_GUARDS`` list, and a single flat parametrization
across their full cross-product. Adding a shape here immediately tests it
against every confinement guard; there is no opt-in and no per-guard
``extra_shapes`` escape hatch by construction.

A red cell is the correct artifact for a live, un-fixed bypass -- not a
reason to narrow the matrix. Where the bar cannot be carried green (this is
a shared branch with concurrent sessions; see ``test_cd_prefix_bypass.py``'s
own corrected docstring on ``_new_attack_shapes``, folded away below), the
exact failing (guard, shape) cell is marked ``pytest.mark.xfail(strict=True)``
with the live bypass named in the reason. ``strict=True`` is load-bearing:
the moment a migration (the Staff Engineer's proposed ``_command_position.py`` layer)
closes a cell, that cell XPASSes, which is a FAILURE under strict xfail --
the corpus is the thing that notices the fix landed and forces the cell to
be flipped to a real assertion. It measures the migration; it does not
merely accompany it.

Advisory/rewrite guards in general (``offer-git-c``, the BX-16 five,
``grep-via-bash-guard``/``multiprobe-banner``/``plumbing-and-loops``) are
still OUT of scope, per Finding 5's confinement-vs-advisory split. The two
exceptions are ``block-subagent-plan-body-bash-write`` and
``check-raw-pid-liveness``, which live in this file's own ``ADVISORY_
GUARDS`` bank (below ``CONFINEMENT_GUARDS``) -- they were CONFINEMENT_
GUARDS members here until C13/C14 (2026-08-06 guard-class census) flipped
both CONFINEMENT_DENY -> ADVISORY_REWRITE; moved rather than deleted so the
SHAPES cross-product coverage they already had survives the flip. Review:
coordinator:code-reviewer sidecar coordinatorcode-reviewer-caf5fbe1.md, P1
finding.

Spec backlink: coordinator_core/bash_guards/dispatch.py (``guard_chain``
registration -- the confinement-guard set below was read directly off that
literal, not copied from the reviewer's enumeration, per the reviewer's own
instruction to verify against source).
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.bash_guards import block_subagent_commit as commit_guard
from coordinator_core.bash_guards import (
    block_subagent_destructive_action as destructive_guard,
)
from coordinator_core.bash_guards import (
    block_subagent_plan_body_bash_write as planbody_guard,
)
from coordinator_core.bash_guards import (
    block_reviewer_bash_outside_allowlist as reviewer_guard,
)
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.bash_guards.tests.test_cd_prefix_bypass import (
    _decision,
    _SUBAGENT_IDENTITY,
    _wire_subagent_identity,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

#: `check-raw-pid-liveness` could be flipped CONFINEMENT_DENY ->
#: ADVISORY_REWRITE (C13/C14) without this corpus noticing -- see those two
#: guards' own `ADVISORY_GUARDS` bank below. Review: coordinator:


SHAPES: List[Tuple[str, Callable[[str], str]]] = [
    ("plain", lambda cmd: cmd),
    ("cd_and", lambda cmd: "cd /tmp && %s" % cmd),
    ("cd_semicolon", lambda cmd: "cd /tmp; %s" % cmd),
    ("cd_dashdash_quoted", lambda cmd: 'cd -- "/tmp" && %s' % cmd),
    ("pushd", lambda cmd: "pushd /tmp && %s" % cmd),
    ("chained_cd", lambda cmd: "cd /tmp && cd / && %s" % cmd),
    ("leading_env_assignment", lambda cmd: "FOO=1 %s" % cmd),
    ("env_wrapper", lambda cmd: "env %s" % cmd),
    ("nice_wrapper", lambda cmd: "nice %s" % cmd),
    ("nice_bare_numeric_wrapper", lambda cmd: "nice -19 %s" % cmd),
    ("time_wrapper", lambda cmd: "time %s" % cmd),
    ("sh_dash_c_wrapper", lambda cmd: "sh -c %s" % shlex.quote(cmd)),
    ("bash_dash_c_wrapper", lambda cmd: "bash -c %s" % shlex.quote(cmd)),
    ("env_sh_dash_c_wrapper", lambda cmd: "env sh -c %s" % shlex.quote(cmd)),
    ("brace_grouping", lambda cmd: "{ %s; }" % cmd),
    ("paren_grouping", lambda cmd: "( %s )" % cmd),
    ("setsid_wrapper", lambda cmd: "setsid %s" % cmd),
    ("busybox_wrapper", lambda cmd: "busybox %s" % cmd),
    ("sh_ic_bundled_wrapper", lambda cmd: "sh -ic %s" % shlex.quote(cmd)),
]
SHAPE_NAMES: List[str] = [name for name, _ in SHAPES]
_SHAPE_FN: Dict[str, Callable[[str], str]] = dict(SHAPES)


def _build_load_bearing_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "shared-tree"
    (repo / "state").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True, capture_output=True,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(repo), check=True, capture_output=True,
        **no_console_creationflags(),
    )

    tracked = repo / "state" / "tracked.md"
    tracked.write_text("committed baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "commit", "-qm", "baseline"], cwd=str(repo), check=True, capture_output=True,
        **no_console_creationflags(),
    )

    tracked.write_text("committed baseline\nuncommitted edit\n", encoding="utf-8")
    (repo / "state" / "untracked-loadbearing.md").write_text("scratch\n", encoding="utf-8")
    return repo


def _setup_no_verify(tmp_path, monkeypatch):
    return _decision, 'git commit --no-verify -m "msg"'


def _setup_destructive_git_orphan(tmp_path, monkeypatch):
    return _decision, "git reset --hard $(echo HEAD~3)"


def _setup_destructive_rm(tmp_path, monkeypatch):
    return _decision, "rm -rf $(echo /tmp/some-target)"


def _setup_destructive_git_clean(tmp_path, monkeypatch):
    repo = _build_load_bearing_repo(tmp_path)
    return _decision, "git -C %s clean -fdx" % repo


def _setup_destructive_git_revert(tmp_path, monkeypatch):
    repo = _build_load_bearing_repo(tmp_path)
    return _decision, "git -C %s stash" % repo


def _setup_blanket_git_add(tmp_path, monkeypatch):
    fake_meta_root = tmp_path / "home" / ".claude"
    fake_meta_root.mkdir(parents=True)

    def fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return 0, str(fake_meta_root) + "\n"
        return 1, ""

    monkeypatch.setattr(dc, "_run_git", fake_run_git)
    monkeypatch.setattr(
        dc.os.path, "expanduser", lambda p: str(tmp_path / "home") if p == "~" else p
    )
    monkeypatch.delenv("COORDINATOR_OVERRIDE_BLANKET_ADD", raising=False)
    monkeypatch.delenv("_COORDINATOR_SAFE_COMMIT_INTERNAL_BLANKET", raising=False)
    return _decision, "git add -A"


def _setup_runaway_find(tmp_path, monkeypatch):
    return _decision, "find / -name '*.pyc'"


def _setup_block_worktree_creation(tmp_path, monkeypatch):
    return _decision, "git worktree add ../wt-1 feature-branch"


def _setup_block_approval_sentinel_creation(tmp_path, monkeypatch):
    return _decision, "touch .coordinator-doctrine-edit-approved"


def _setup_block_worktree_sentinel_creation(tmp_path, monkeypatch):
    return _decision, "touch .coordinator-override-worktree-guard"


def _setup_block_subagent_plan_body_bash_write(tmp_path, monkeypatch):
    _wire_subagent_identity(monkeypatch, planbody_guard, "coordinator:executor")

    def decide(cmd):
        return _decision(cmd, **_SUBAGENT_IDENTITY)

    return decide, "echo x >> docs/plans/foo.md"


def _setup_block_reviewer_bash_outside_allowlist(tmp_path, monkeypatch):
    _wire_subagent_identity(monkeypatch, reviewer_guard, "coordinator:code-reviewer")

    def decide(cmd):
        return _decision(cmd, agent_id="deadbeef0123", agent_type="coordinator:code-reviewer")

    return decide, "curl https://example.com"


def _setup_block_subagent_destructive_action(tmp_path, monkeypatch):
    _wire_subagent_identity(monkeypatch, destructive_guard, "coordinator:executor")

    def decide(cmd):
        return _decision(cmd, **_SUBAGENT_IDENTITY)

    return decide, "git rebase -i HEAD~3"


def _setup_block_subagent_commit(tmp_path, monkeypatch):
    _wire_subagent_identity(monkeypatch, commit_guard, "coordinator:executor")

    def decide(cmd):
        return _decision(cmd, **_SUBAGENT_IDENTITY)

    return decide, 'git commit -m "msg"'


def _setup_check_test_suite_invocation(tmp_path, monkeypatch):
    from coordinator_core.bash_guards import check_test_suite_invocation as tsi_guard

    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["coordinator_core", "coordinator/tests"]\n',
        encoding="utf-8",
    )
    (tmp_path / "coordinator_core" / "frontmatter" / "tests").mkdir(parents=True)
    monkeypatch.setattr(tsi_guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.setattr(tsi_guard, "_tier_u_grant", lambda cwd: (True, None))
    monkeypatch.delenv(tsi_guard._OVERRIDE_ENV_VAR, raising=False)

    def decide(cmd):
        return _decision(
            cmd, cwd=str(tmp_path), agent_id="deadbeef0123", agent_type="coordinator:executor"
        )

    return decide, "pytest"


def _setup_check_raw_pid_liveness(tmp_path, monkeypatch):
    return _decision, "kill -0 1234"


# CONFINEMENT_GUARDS -- every hard-deny (`fail_closed=True`) entry in
# `dispatch.py`'s `guard_chain` EXCLUDING the three machine-load guards
# `test_hard_denies_precede_rewrites.py`'s own `CONFINEMENT_HARD_DENIES` set
# guard-class census) have since flipped both CONFINEMENT_DENY ->
# ADVISORY_REWRITE -- they now live in `ADVISORY_GUARDS` below, not here.
# corpus exists to catch, and `ADVISORY_GUARDS`'s own cross-product proves

CONFINEMENT_GUARDS: List[Tuple[str, Callable]] = [
    ("no-verify", _setup_no_verify),
    ("destructive-git-orphan", _setup_destructive_git_orphan),
    ("destructive-rm", _setup_destructive_rm),
    ("destructive-git-clean", _setup_destructive_git_clean),
    ("destructive-git-revert", _setup_destructive_git_revert),
    ("blanket-git-add", _setup_blanket_git_add),
    ("runaway-find", _setup_runaway_find),
    ("block-worktree-creation", _setup_block_worktree_creation),
    ("block-approval-sentinel-creation", _setup_block_approval_sentinel_creation),
    ("block-worktree-sentinel-creation", _setup_block_worktree_sentinel_creation),
    ("block-reviewer-bash-outside-allowlist", _setup_block_reviewer_bash_outside_allowlist),
    ("block-subagent-destructive-action", _setup_block_subagent_destructive_action),
    ("block-subagent-commit", _setup_block_subagent_commit),
    ("check-test-suite-invocation", _setup_check_test_suite_invocation),
]
GUARD_NAMES: List[str] = [name for name, _ in CONFINEMENT_GUARDS]


# ADVISORY_GUARDS -- guards C13/C14 flipped CONFINEMENT_DENY ->
# ADVISORY_REWRITE. Same SHAPES cross-product, but the expected outcome is

ADVISORY_GUARDS: List[Tuple[str, Callable]] = [
    ("block-subagent-plan-body-bash-write", _setup_block_subagent_plan_body_bash_write),
    ("check-raw-pid-liveness", _setup_check_raw_pid_liveness),
]
ADVISORY_GUARD_NAMES: List[str] = [name for name, _ in ADVISORY_GUARDS]
_ADVISORY_GUARD_SETUP: Dict[str, Callable] = dict(ADVISORY_GUARDS)

#: resolvable here even though they no longer appear in `GUARD_NAMES`
#: (`CONFINEMENT_GUARDS`-only). `GUARD_NAMES`/`ADVISORY_GUARD_NAMES` gate
#: which parametrized test each guard runs under; `_GUARD_SETUP` itself
_GUARD_SETUP: Dict[str, Callable] = dict(CONFINEMENT_GUARDS + ADVISORY_GUARDS)


XFAIL_BYPASSES: Dict[Tuple[str, str], str] = {
    ("no-verify", "setsid_wrapper"): (
        "LIVE BYPASS: check_no_verify does not recognize `setsid` as a "
        "passthrough wrapper -- the Staff Engineer staff-eng review 2026-07-29 Finding 0"
    ),
    ("no-verify", "busybox_wrapper"): (
        "LIVE BYPASS: check_no_verify does not recognize `busybox` as a "
        "passthrough wrapper -- the Staff Engineer staff-eng review 2026-07-29 Finding 0"
    ),
    # docstring ("setsid is a SEPARATE, still-open gap for these six").
    ("destructive-rm", "setsid_wrapper"): (
        "LIVE BYPASS: check_destructive_rm does not recognize `setsid` as a "
        "passthrough wrapper -- found empirically by this corpus 2026-07-29, "
        "same class as the Staff Engineer staff-eng review Finding 0/2"
    ),
    ("destructive-rm", "busybox_wrapper"): (
        "LIVE BYPASS: check_destructive_rm does not recognize `busybox` as a "
        "passthrough wrapper -- found empirically by this corpus 2026-07-29, "
        "same class as the Staff Engineer staff-eng review Finding 0/2"
    ),
    ("destructive-git-clean", "setsid_wrapper"): (
        "LIVE BYPASS: check_destructive_git_clean does not recognize `setsid` "
        "as a passthrough wrapper -- found empirically by this corpus "
        "2026-07-29, same class as the Staff Engineer staff-eng review Finding 0/2"
    ),
    ("destructive-git-clean", "busybox_wrapper"): (
        "LIVE BYPASS: check_destructive_git_clean does not recognize "
        "`busybox` as a passthrough wrapper -- found empirically by this "
        "corpus 2026-07-29, same class as the Staff Engineer staff-eng review Finding 0/2"
    ),
    ("destructive-git-revert", "setsid_wrapper"): (
        "LIVE BYPASS: check_destructive_git_revert does not recognize "
        "`setsid` as a passthrough wrapper -- found empirically by this "
        "corpus 2026-07-29, same class as the Staff Engineer staff-eng review Finding 0/2"
    ),
    ("destructive-git-revert", "busybox_wrapper"): (
        "LIVE BYPASS: check_destructive_git_revert does not recognize "
        "`busybox` as a passthrough wrapper -- found empirically by this "
        "corpus 2026-07-29, same class as the Staff Engineer staff-eng review Finding 0/2"
    ),
    # were fixed -- paren via the shared `_BYPASS_PREFIX` this guard's own
    ("blanket-git-add", "setsid_wrapper"): (
        "LIVE BYPASS: check_blanket_git_add does not recognize `setsid` as a "
        "passthrough wrapper -- found empirically by this corpus 2026-07-29, "
        "same class as the Staff Engineer staff-eng review Finding 0/2"
    ),
    ("blanket-git-add", "busybox_wrapper"): (
        "LIVE BYPASS: check_blanket_git_add does not recognize `busybox` as "
        "a passthrough wrapper -- found empirically by this corpus "
        "2026-07-29, same class as the Staff Engineer staff-eng review Finding 0/2"
    ),
    # 879-process incident and did not). `_FIND_WRAPPER_WORDS` (9 words:
    ("runaway-find", "setsid_wrapper"): (
        "LIVE BYPASS: check_runaway_find's `_FIND_WRAPPER_WORDS` does not "
        "include `setsid` -- the Staff Engineer staff-eng review 2026-07-29 Finding 3, "
        "verified live (`setsid find` allows)"
    ),
    ("runaway-find", "busybox_wrapper"): (
        "LIVE BYPASS: check_runaway_find's `_FIND_WRAPPER_WORDS` does not "
        "include `busybox` -- the Staff Engineer staff-eng review 2026-07-29 Finding 3, "
        "verified live (`busybox find` allows)"
    ),
}


#: Same discipline as `XFAIL_BYPASSES` above, scoped to `ADVISORY_GUARDS`.
#: known-bypass entry in `XFAIL_BYPASSES` while still CONFINEMENT_DENY, so
#: there is no known gap to carry forward into ADVISORY_REWRITE.
XFAIL_ADVISORY_BYPASSES: Dict[Tuple[str, str], str] = {}


def _cells() -> List[object]:
    cells = []
    for guard_name in GUARD_NAMES:
        for shape_name in SHAPE_NAMES:
            reason = XFAIL_BYPASSES.get((guard_name, shape_name))
            marks = [pytest.mark.xfail(reason=reason, strict=True)] if reason else []
            cells.append(
                pytest.param(guard_name, shape_name, marks=marks, id="%s/%s" % (guard_name, shape_name))
            )
    return cells


def _advisory_cells() -> List[object]:
    cells = []
    for guard_name in ADVISORY_GUARD_NAMES:
        for shape_name in SHAPE_NAMES:
            reason = XFAIL_ADVISORY_BYPASSES.get((guard_name, shape_name))
            marks = [pytest.mark.xfail(reason=reason, strict=True)] if reason else []
            cells.append(
                pytest.param(guard_name, shape_name, marks=marks, id="%s/%s" % (guard_name, shape_name))
            )
    return cells


@pytest.mark.parametrize("guard_name,shape_name", _cells())
def test_confinement_attack_corpus(guard_name, shape_name, tmp_path, monkeypatch):
    """One cell of the SHAPES x CONFINEMENT_GUARDS cross-product, driven
    through `dispatch.evaluate_payload_json`. A plain `assert == "deny"` --
    the xfail(strict=True) marks above are the only sanctioned exemption,
    and they name their bypass at the parametrize call site, not here.
    """
    decide, base_cmd = _GUARD_SETUP[guard_name](tmp_path, monkeypatch)
    variant = _SHAPE_FN[shape_name](base_cmd)
    got = decide(variant)
    assert got == "deny", "%s / %s: %r -> %s (expected deny)" % (
        guard_name,
        shape_name,
        variant,
        got,
    )


@pytest.mark.parametrize("guard_name,shape_name", _advisory_cells())
def test_advisory_rewrite_attack_corpus(guard_name, shape_name, tmp_path, monkeypatch):
    """`ADVISORY_GUARDS`'s own SHAPES cross-product -- `block-subagent-plan-
    body-bash-write` and `check-raw-pid-liveness`, moved here from
    `CONFINEMENT_GUARDS` when C13/C14 flipped both CONFINEMENT_DENY ->
    ADVISORY_REWRITE. Asserts `"advisory"`, not `"deny"` -- these guards no
    longer hard-deny anything by design; a plain silent `"allow"` (no
    envelope) on any evasion shape is the regression this proves against.
    coordinatorcode-reviewer-caf5fbe1.md, P1 finding.
    """
    decide, base_cmd = _ADVISORY_GUARD_SETUP[guard_name](tmp_path, monkeypatch)
    variant = _SHAPE_FN[shape_name](base_cmd)
    got = decide(variant)
    assert got == "advisory", "%s / %s: %r -> %s (expected advisory)" % (
        guard_name,
        shape_name,
        variant,
        got,
    )
