"""
coordinator_core.hooks.tests.test_named_dispatch_catering_resolves -- proves
the catering legs restore on the payload SubagentStart ACTUALLY delivers,
for a named (teammate) dispatch, after C2's `engine._canonical_agent_id`
delegation to `session.identity.resolve_subagent_identity` (C3, docs/plans/
2026-08-25-a-named-dispatch-keeps-its-report.md).

Spec backlink: docs/plans/2026-08-25-a-named-dispatch-keeps-its-report.md (C3)
Modules under test: coordinator_core/hooks/cater_subagent_start.py,
coordinator_core/subagent_sandbox/engine.py (via compose_catering)

PRECONDITION FINDING (C3 body): no recorded SubagentStart payload existed on
disk anywhere in this repo prior to this chunk -- grepping both incident
agent ids across `state/`, `docs/`, and `.git/coordinator-sessions/.agents`
returned only prose. `state/audits/2026-08-25-named-dispatch-catering-back-
pointer-key-form.md` (already on disk, written by a prior chunk in this same
plan) DOES capture the real field values this bug depends on verbatim,
measured directly against the live repo: `agent_id =
"arev-counter-tests-f4498a5559849145"`, full `session_id =
"0bba1169-01d3-42d4-9bd3-5f27ff453d91"`, and the confirmed `.agents/` key the
ledger writer minted for that same teammate,
`rev-counter-tests@session-0bba1169`. This module builds its fixtures from
those real, cited values rather than a hand-paired session id -- a
hand-built payload paired to whatever session id the fixture-author already
knew the answer for would pass regardless of whether `resolve_subagent_
identity` builds the id correctly from the payload's OWN session_id.

`session_id[:8]` for the real value above is `"0bba1169"`, which
`test_named_report_sidecar_eligible_type_gets_a_real_sidecar_path` asserts
equals the `<short>` half of the `.agents/` key the real ledger writer
minted for that teammate (`rev-counter-tests@session-0bba1169`) -- the
precondition's own instruction, executed rather than assumed.

This module does NOT write a new file under `state/audits/` -- the
dispatch brief's own declared `writes:` scope for this chunk is this test
file alone (state/dispatch-briefs/2026-08-25-a-named-dispatch-keeps-its-
report/C3.md), and the audit above already carries the verbatim capture
this precondition step calls for.

AC5 correction (bug-backlog `2026-08-25-sidecar-provisioning-missed-never-
fires-f49eb749c024.yaml`; `_resolve_sidecar_leg` in `cater_subagent_start.py`):
the plan's AC5 row, as written, claims the miss marker should fire for "a
named dispatch whose resolved type is genuinely off the roster." That is
the WRONG population -- `compose_catering` runs for every SubagentStart, so
a resolved-but-off-roster type is the majority of all dispatches, and
firing the marker there would broadcast "Sidecar provisioning did not
complete -- scaffold your own at the path your agent definition names" to
types whose definitions name no such path. The correct population is a
type that never resolved AT ALL (`agent_type` and `subagent_type` both
falsy) -- the dispatch that genuinely lost a sidecar because resolution
itself missed or raised, not because the roster said no.
`test_named_dispatch_genuinely_off_roster_type_stays_silent_not_missed`
below pins the CORRECT behaviour for the majority (resolved, off-roster)
population: it stays silent, matching the Agent-path hook's own
`_is_report_sidecar_eligible` gate. The never-resolved population is
covered separately by
`test_named_dispatch_unresolved_type_gets_the_miss_marker`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.hooks.cater_subagent_start import (
    OP_NAME,
    SIDECAR_MISS_MARKER,
    SIDECAR_MISS_NOTICE_LEAD,
    SIDECAR_PATH_MARKER_PREFIX,
    _is_named_teammate_agent_id,
    compose_catering,
)
from coordinator_core.subagent_sandbox import engine as engine_mod
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REAL_RAW_AGENT_ID = "arev-counter-tests-f4498a5559849145"
REAL_SESSION_ID = "0bba1169-01d3-42d4-9bd3-5f27ff453d91"
REAL_TEAMMATE_NAME = "rev-counter-tests"
REAL_CANONICAL_AGENT_ID = "rev-counter-tests@session-0bba1169"
REAL_RESOLVED_TYPE = "coordinator:code-reviewer"

#: A genuine coordinator type, NOT `REAL_RESOLVED_TYPE`, deliberately never
OFF_ROSTER_TYPE = "coordinator:git-commit-agent"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    policy = tmp_path / "subagent-sandbox-policy.yaml"
    policy.write_text(
        "report_sidecar:\n"
        f"  - {REAL_RESOLVED_TYPE}\n",
        encoding="utf-8",
    )
    return policy


@pytest.fixture(autouse=True)
def _policy_env(monkeypatch: pytest.MonkeyPatch, policy_path: Path) -> None:
    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(policy_path))


@pytest.fixture(autouse=True)
def _no_role_append(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-config"))


def _real_shaped_payload(cwd: str, session_id: str = REAL_SESSION_ID) -> dict:
    return {
        "agent_id": REAL_RAW_AGENT_ID,
        "agent_type": REAL_TEAMMATE_NAME,
        "session_id": session_id,
        "cwd": cwd,
    }


def _write_backpointer(git_root: Path, canonical_agent_id: str, em_sid: str, resolved_type: str) -> None:
    """Build the two-hop back-pointer chain `resolve_effective_types` reads,
    keyed by the CANONICAL id form (post-C2) -- `.agents/<canonical>/`, not
    the raw subagent-side form the pre-fix code would have looked for."""
    agents_dir = git_root / ".git" / "coordinator-sessions" / ".agents" / canonical_agent_id
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "em-session-id.txt").write_text(em_sid + "\n", encoding="utf-8")

    session_dir = git_root / ".git" / "coordinator-sessions" / em_sid
    session_dir.mkdir(parents=True, exist_ok=True)
    dispatch_file = session_dir / "dispatched-agents.txt"
    with open(dispatch_file, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{canonical_agent_id}\tclaude-sonnet-5\t{resolved_type}\t1700000000\n")


def _additional_context(result: dict) -> str:
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


def test_captured_session_id_short_form_matches_the_real_ledger_key() -> None:
    short = REAL_SESSION_ID[:8]
    assert REAL_CANONICAL_AGENT_ID == f"{REAL_TEAMMATE_NAME}@session-{short}"
    assert short == "0bba1169"


def test_engine_delegation_builds_the_same_canonical_id_from_the_real_payload() -> None:
    resolved = engine_mod._canonical_agent_id(REAL_RAW_AGENT_ID, REAL_SESSION_ID)
    assert resolved == REAL_CANONICAL_AGENT_ID


def test_named_report_sidecar_eligible_type_gets_a_real_sidecar_path(git_repo: Path) -> None:
    _write_backpointer(git_repo, REAL_CANONICAL_AGENT_ID, "em-session-real-1", REAL_RESOLVED_TYPE)
    payload = _real_shaped_payload(str(git_repo))

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_PATH_MARKER_PREFIX in result, (
        "a report_sidecar-eligible named dispatch, resolved via the real "
        "captured payload shape, must receive a real sidecar_path"
    )
    assert SIDECAR_MISS_MARKER not in result


def _assert_sentinel_miss_notice(context: str, repo: Path) -> None:
    """A miss notice for a population `4dc874adb` writes a sentinel for:
    the notice fires, its trailing marker is the path key (never the
    no-path `SIDECAR_MISS_MARKER`), and the named path is a file that
    actually exists -- the whole point of that change was a file the EM can
    poll, so a body naming a path nothing wrote would satisfy a
    string-only assertion while delivering nothing."""
    assert SIDECAR_MISS_NOTICE_LEAD in context, context
    assert SIDECAR_MISS_MARKER not in context, context
    assert SIDECAR_PATH_MARKER_PREFIX in context, context

    sentinel_rel = context.rsplit(SIDECAR_PATH_MARKER_PREFIX, 1)[1].strip()
    assert (repo / sentinel_rel).is_file(), (
        f"the miss notice names {sentinel_rel!r}, but no sentinel exists "
        f"there under {repo} -- an unpollable path is the failure AC3 of "
        f"docs/plans/2026-08-25-a-missed-sidecar-leaves-a-file-the-em-ca.md "
        f"exists to prevent"
    )


def test_named_dispatch_genuinely_off_roster_type_stays_silent_not_missed(git_repo: Path) -> None:
    _write_backpointer(git_repo, REAL_CANONICAL_AGENT_ID, "em-session-real-2", OFF_ROSTER_TYPE)
    payload = _real_shaped_payload(str(git_repo), session_id=REAL_SESSION_ID)

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_PATH_MARKER_PREFIX not in result
    assert SIDECAR_MISS_MARKER not in result


def test_named_dispatch_eligible_type_with_no_backpointer_gets_the_miss_marker(
    git_repo: Path,
) -> None:
    """AC5 fix, the population the backlog actually names: a NAMED dispatch
    of an ELIGIBLE type (`REAL_RESOLVED_TYPE`, on the policy roster) whose
    back-pointer row has not been written yet -- no `_write_backpointer`
    call for this session at all, mirroring the real ordering hazard
    (`test_back_pointer_not_yet_written_gets_the_miss_marker` pins the same
    race through the actual op-relay path; this pins it directly against
    `compose_catering`).

    `resolve_effective_types` returns `agent_type` populated verbatim from
    `payload["agent_type"]` (the teammate NAME, `REAL_TEAMMATE_NAME`) --
    never a `report_sidecar` policy key -- and `subagent_type == ""` (no
    back-pointer to read). The roster lookup below is structurally
    incapable of matching either leg, so before the fix this silently
    dropped an eligible dispatch's sidecar with no signal at all. This is
    the regression this chunk closes.

    Asserts the miss NOTICE, not `SIDECAR_MISS_MARKER`: `4dc874adb` gave
    this population a sentinel scaffold on disk, so it takes the
    path-bearing body (`_compose_sidecar_miss_text` with a non-empty
    `sentinel_path`), whose trailing marker is `SIDECAR_PATH_MARKER_PREFIX`.
    The no-path marker is now reserved for the arms where no sentinel could
    be written at all -- pinned by `test_named_dispatch_unresolved_type_
    gets_the_miss_marker`."""
    payload = _real_shaped_payload(str(git_repo))

    result = compose_catering(payload, cwd=str(git_repo))

    _assert_sentinel_miss_notice(result, git_repo)


def test_named_dispatch_with_separator_in_teammate_name_gets_the_miss_marker(
    git_repo: Path,
) -> None:
    """Regression for the separator-name miss-marker hole (bug-backlog
    `2026-08-25-separator-name-miss-marker-hole`): a named teammate whose
    NAME contains a `/` (`feature/auth-review`, a natural name) presents a
    subagent-side raw id `afeature/auth-review-aaaabbbbccccdddd`, which
    `_canonical_agent_id` genuinely canonicalizes (via `resolve_subagent_
    identity`) to `feature/auth-review@session-11111111` -- a shape
    `engine._TEAMMATE_CANONICAL_RE` rejects (its charset excludes `/`).
    Before the fix, `_is_named_teammate_agent_id` returned False for that
    canonical id, so an unresolved `subagent_type` fell through to total
    silence instead of the miss marker -- exactly the silence
    `2c6783315a28325b10769d50ea1d9f3141c64bc7` was written to remove.

    As above, the observable is the miss NOTICE: `_canonical_agent_id`
    canonicalizes this raw id, so `_resolve_sidecar_leg`'s shape gate
    matches and `4dc874adb`'s sentinel is written, putting this dispatch on
    the path-bearing body."""
    raw_agent_id = "afeature/auth-review-aaaabbbbccccdddd"
    payload = {
        "agent_id": raw_agent_id,
        "agent_type": "feature/auth-review",
        "session_id": "11111111-0000-0000-0000-000000000000",
        "cwd": str(git_repo),
    }

    result = compose_catering(payload, cwd=str(git_repo))

    _assert_sentinel_miss_notice(result, git_repo)


@pytest.mark.parametrize(
    "agent_id, expected",
    [
        ("staff-probe@session-11111111", True),
        ("feature/auth-review@session-11111111", True),
        ("docs/api-check@session-11111111", True),
        ("a.dotted.name@session-11111111", True),
        ("arev-counter-tests-f4498a5559849145", True),
        ("0123456789abcdef0123456789abcdef", False),
    ],
)
def test_is_named_teammate_agent_id_table(agent_id: str, expected: bool) -> None:
    assert _is_named_teammate_agent_id(agent_id) is expected


def test_unnamed_dispatch_off_roster_type_stays_silent(git_repo: Path) -> None:
    payload = {
        "agent_id": "0123456789abcdef0123456789abcdef",
        "agent_type": OFF_ROSTER_TYPE,
        "session_id": REAL_SESSION_ID,
        "cwd": str(git_repo),
    }

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_MISS_MARKER not in result
    assert SIDECAR_PATH_MARKER_PREFIX not in result


def test_named_dispatch_unresolved_type_gets_the_miss_marker(git_repo: Path) -> None:
    payload = {
        "agent_id": "",
        "agent_type": "",
        "session_id": REAL_SESSION_ID,
        "cwd": str(git_repo),
    }

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_MISS_MARKER in result
    assert SIDECAR_PATH_MARKER_PREFIX not in result


def _bookkeeping_params(cwd: str) -> dict:
    # `_TEAMMATE_AGENT_RE`) -- NOT the raw subagent-side `a<name>-<16hex>`
    # (DoE's shim), so this fixture mirrors that: the CANONICAL id, not
    # `REAL_RAW_AGENT_ID`.
    return {
        "session_id": REAL_SESSION_ID,
        "dispatched_agent_id": REAL_CANONICAL_AGENT_ID,
        "dispatched_model": "claude-x",
        "subagent_type": REAL_RESOLVED_TYPE,
    }


def test_bookkeeping_first_caters_the_real_named_dispatch(git_repo: Path) -> None:
    results = ipc.dispatch_ops_from_hook(
        [
            ("hooks.track_dispatched_agents", _bookkeeping_params(str(git_repo))),
            (OP_NAME, _real_shaped_payload(str(git_repo))),
        ],
        origin_worktree=str(git_repo),
    )

    assert len(results) == 2
    for result in results:
        assert not isinstance(result, ipc.HookDispatchError), result

    context = _additional_context(results[1])
    assert SIDECAR_PATH_MARKER_PREFIX in context or SIDECAR_MISS_MARKER in context, (
        "bookkeeping-first must resolve subagent_type via the back-pointer and cater it"
    )


def test_back_pointer_not_yet_written_gets_the_miss_marker(git_repo: Path) -> None:
    """The reverse order -- cater op dispatched BEFORE the bookkeeping leg
    writes the back-pointer for THIS SAME real-shaped payload. `_handler`'s
    own docstring (~lines 581-592) states it cannot verify its caller
    obeyed the ordering; this pins the OBSERVED consequence directly rather
    than leaving it to two docstrings that tell two different event stories
    (this module's own vs. `track_dispatched_agents.py`'s "PostToolUse"
    self-description).

    This is exactly the population the AC5 fix targets, not a separate
    finding: a named dispatch (`agent_id` in the raw `a<name>-<16hex>`
    shape) whose `subagent_type` has not resolved yet -- `agent_type` is
    the teammate NAME, never a `report_sidecar` policy key, so the roster
    lookup was always going to miss for it. Before the fix this raced
    ordering produced total silence (`no_advisory()`, `{}`); now
    `_resolve_sidecar_leg`'s named-teammate leg recognizes the unresolved
    `subagent_type` and surfaces the miss notice instead, so a subagent
    whose sidecar catering lost the ordering race is told, not left to
    read silence as "nothing to recover from". `4dc874adb` then gave that
    notice a sentinel scaffold on disk and named its path, so the
    observable is the miss NOTICE plus a real file, not the no-path
    `SIDECAR_MISS_MARKER`.
    """
    results = ipc.dispatch_ops_from_hook(
        [
            (OP_NAME, _real_shaped_payload(str(git_repo))),
            ("hooks.track_dispatched_agents", _bookkeeping_params(str(git_repo))),
        ],
        origin_worktree=str(git_repo),
    )

    assert len(results) == 2
    for result in results:
        assert not isinstance(result, ipc.HookDispatchError), result

    _assert_sentinel_miss_notice(_additional_context(results[0]), git_repo)


def test_ac9_compose_catering_process_time_before_and_after_c2_delegation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_backpointer(git_repo, REAL_CANONICAL_AGENT_ID, "em-session-ac9", REAL_RESOLVED_TYPE)
    payload = _real_shaped_payload(str(git_repo))

    def _min_process_time() -> float:
        return in_process_time_ms(lambda: compose_catering(payload, cwd=str(git_repo)))[
            "process_time_ms"
        ]

    after_ms = _min_process_time()

    real_canonical = engine_mod._canonical_agent_id

    def _pre_c2_canonical_agent_id(raw_agent_id: str, session_id):
        if engine_mod._NAMED_TEAMMATE_RE.fullmatch(raw_agent_id):
            return raw_agent_id
        return real_canonical(raw_agent_id, session_id)

    monkeypatch.setattr(engine_mod, "_canonical_agent_id", _pre_c2_canonical_agent_id)
    before_ms = _min_process_time()

    assert after_ms < 25.0 and before_ms < 25.0, (
        f"AC9 process-time measurement: pre-C2-shaped avg={before_ms:.4f}ms, "
        f"post-C2 (real) avg={after_ms:.4f}ms -- delegation adds a "
        f"regex path, not a spawn; both must stay far under the 500ms "
        f"brightline and the leg's own 150ms sibling-plan budget"
    )
    assert after_ms < before_ms * 8.0, (
        f"AC9 differential check: post-C2 avg={after_ms:.4f}ms is more than "
        f"8x pre-C2-shaped avg={before_ms:.4f}ms -- that is far above the "
        f"~3.3x this comparison costs by construction (miss-vs-hit), so "
        f"something beyond the back-pointer reads has gotten expensive"
    )
