"""Every `git commit` probe must run against the repo the COMMAND targets.

Purpose: `check_git_commit_safe_commit_advise` resolved each probe's working
directory through `_bt_git_dash_c_value`, which answers only "what did this
command's `git -C` say" and returns None for the overwhelming majority of
commands, which carry no `-C` at all. That None went straight to `_run_git`,
where it does not mean "the command's own repo" -- it means the GUARD
PROCESS's directory, i.e. wherever the harness happened to be sitting.

So the guard read the right repository only for the rare `-C` spelling and
the wrong one for the ordinary case, which inverts which commands it can
reason about at all. doe-claude-em pinned it on 2026-09-04: adding a no-op
`git -C .` to an otherwise identical command flipped allow to deny. The
payload cwd was in scope at every one of these sites and unused, while
`_bt_commit_scope_operand_is_sweeping` two hundred lines away was already
resolving against it for exactly this reason.

NEGATIVE SPEC -- what this file must never be relaxed into asserting:
  - It asserts the probe's DESTINATION, never a verdict. A verdict assertion
    would have to stage something on a real tree to be meaningful, and the
    predicates' fail-open posture would let it pass green for the wrong
    reason on a machine where git is slow.
  - It must not start spawning git to get there. These probes sit on the
    PreToolUse(Bash) hot path (DR-344); a spawning test lands on the cadence
    tier and stops guarding the per-commit path, which is the only path that
    matters here. Stubbing `_run_git` is what keeps this on the fast tier.
  - An explicit `-C` still WINS over the payload cwd. The fix is a fallback,
    not a replacement -- a command that names a repo means that repo.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc

PAYLOAD_CWD = "/some-other-repo"


@pytest.fixture
def probe_cwds(monkeypatch) -> List[Optional[str]]:
    seen: List[Optional[str]] = []

    def _fake_run_git(
        args: List[str],
        cwd: Optional[str] = None,
        timeout: float = 2.0,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str]:
        seen.append(cwd)
        return 0, ""

    monkeypatch.setattr(dc, "_run_git", _fake_run_git)
    return seen


def _run(cmd: str, payload: Optional[Dict[str, Any]] = None) -> None:
    dc.check_git_commit_safe_commit_advise(
        cmd, "", {"cwd": PAYLOAD_CWD} if payload is None else payload
    )


def test_the_solo_bare_commit_index_probe_reads_the_payload_repo(probe_cwds):
    _run("git commit -m x")
    assert probe_cwds, "the index probe never ran"
    assert set(probe_cwds) == {PAYLOAD_CWD}


def test_the_sweep_all_probe_reads_the_payload_repo(probe_cwds, monkeypatch):
    monkeypatch.setattr(dc, "_is_hazard_repo", lambda _p: True)
    _run("git commit -am x")
    assert probe_cwds, "the sweep-all probe never ran"
    assert set(probe_cwds) == {PAYLOAD_CWD}


def test_the_amend_provenance_probe_reads_the_payload_repo(probe_cwds):
    _run("git commit --amend -m x")
    assert probe_cwds, "the amend provenance probe never ran"
    assert set(probe_cwds) == {PAYLOAD_CWD}


def test_a_leading_cd_prefix_wins_over_the_payload_cwd_for_amend(probe_cwds):
    # CEPEF-B9 / V1: `check_offer_git_c` is bypassed here entirely (this test
    # calls `check_git_commit_safe_commit_advise` directly, not the folded
    # dispatch chain), so the amend-provenance probe is the one under test.
    # A leading `cd <dir> &&` names a repo just as explicitly as `-C` does.
    named = "/cd-target-repo"
    _run("cd %s && git commit --amend -m x" % named)
    assert probe_cwds, "the amend provenance probe never ran"
    assert set(probe_cwds) == {named}


def test_a_leading_cd_prefix_still_loses_to_an_explicit_dash_c_for_amend(probe_cwds):
    named = "/dash-c-repo"
    _run("cd /cd-target-repo && git -C %s commit --amend -m x" % named)
    assert probe_cwds, "the amend provenance probe never ran"
    assert set(probe_cwds) == {named}


def test_the_sequencer_probe_reads_the_payload_repo(probe_cwds):
    _run("git add a.py && git commit -m x")
    assert probe_cwds, "the sequencer probe never ran"
    assert set(probe_cwds) == {PAYLOAD_CWD}


def test_an_explicit_dash_c_still_wins_over_the_payload_cwd(probe_cwds):
    named = "/named-repo"
    _run("git -C %s commit -m x" % named)
    assert probe_cwds
    assert set(probe_cwds) == {named}


def test_a_relative_dash_c_is_anchored_to_the_payload_cwd(probe_cwds):
    _run("git -C sub commit -m x")
    assert probe_cwds
    assert set(probe_cwds) == {os.path.join(PAYLOAD_CWD, "sub")}


def test_no_payload_cwd_degrades_to_todays_behaviour(probe_cwds):
    _run("git commit -m x", payload={})
    assert probe_cwds
    assert set(probe_cwds) == {None}
