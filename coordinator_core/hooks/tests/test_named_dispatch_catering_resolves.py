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
import time
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.hooks.cater_subagent_start import (
    OP_NAME,
    SIDECAR_MISS_MARKER,
    SIDECAR_MISS_NOTICE_LEAD,
    SIDECAR_PATH_MARKER_PREFIX,
    _is_named_teammate_agent_id,
    compose_catering,
)
from coordinator_core.subagent_sandbox import engine as engine_mod

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Real captured values (see module docstring) -- NOT hand-paired.
# ---------------------------------------------------------------------------
REAL_RAW_AGENT_ID = "arev-counter-tests-f4498a5559849145"
REAL_SESSION_ID = "0bba1169-01d3-42d4-9bd3-5f27ff453d91"
REAL_TEAMMATE_NAME = "rev-counter-tests"
REAL_CANONICAL_AGENT_ID = "rev-counter-tests@session-0bba1169"
REAL_RESOLVED_TYPE = "coordinator:code-reviewer"

#: A genuine coordinator type, NOT `REAL_RESOLVED_TYPE`, deliberately never
#: added to any policy fixture's `report_sidecar` list below -- the
#: "genuinely off the roster" case the C3 body distinguishes from the
#: key-form bug's "looks off-roster because the lookup missed" case.
OFF_ROSTER_TYPE = "coordinator:git-commit-agent"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
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
    """`compose_catering` always resolves policy via `load_policy(None)`'s
    own cascade -- the env-var rung is this fixture's control point. An
    UNSET policy fails open to an empty policy and every lookup misses
    (C3 body's own warning), which reads exactly like the bug -- always set
    it explicitly rather than relying on whatever ambient default the box
    running this suite happens to carry."""
    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(policy_path))


@pytest.fixture(autouse=True)
def _no_role_append(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate from whatever plugin happens to be installed on the machine
    running this test -- role framing fails open to "" and stays out of the
    way of the sidecar-leg assertions below."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-config"))


def _real_shaped_payload(cwd: str, session_id: str = REAL_SESSION_ID) -> dict:
    """The payload shape SubagentStart actually delivers for a named
    dispatch (module docstring): `agent_id` carries the subagent-side raw
    `a<name>-<16hex>` form, `agent_type` carries the teammate NAME (not a
    policy key -- `cater_subagent_start`'s own module docstring), `session_id`
    is the EM's own session id (the field the fix depends on), `cwd` anchors
    git-root resolution."""
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


# ---------------------------------------------------------------------------
# Precondition step, executed: session_id[:8] really is the `.agents/` short
# form the real ledger writer minted (state/audits/2026-08-25-named-
# dispatch-catering-back-pointer-key-form.md), not merely asserted in prose.
# ---------------------------------------------------------------------------

def test_captured_session_id_short_form_matches_the_real_ledger_key() -> None:
    short = REAL_SESSION_ID[:8]
    assert REAL_CANONICAL_AGENT_ID == f"{REAL_TEAMMATE_NAME}@session-{short}"
    assert short == "0bba1169"


def test_engine_delegation_builds_the_same_canonical_id_from_the_real_payload() -> None:
    """`resolve_subagent_identity`'s own transform, exercised directly
    against the real captured raw agent_id + session_id -- confirms the
    delegation this chunk depends on (C2) actually produces the id the
    real ledger keys by, not a hand-paired stand-in."""
    resolved = engine_mod._canonical_agent_id(REAL_RAW_AGENT_ID, REAL_SESSION_ID)
    assert resolved == REAL_CANONICAL_AGENT_ID


# ---------------------------------------------------------------------------
# AC4 -- a real report_sidecar-eligible type gets a real sidecar_path, given
# the payload shape SubagentStart actually delivers.
# ---------------------------------------------------------------------------

def test_named_report_sidecar_eligible_type_gets_a_real_sidecar_path(git_repo: Path) -> None:
    _write_backpointer(git_repo, REAL_CANONICAL_AGENT_ID, "em-session-real-1", REAL_RESOLVED_TYPE)
    payload = _real_shaped_payload(str(git_repo))

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_PATH_MARKER_PREFIX in result, (
        "a report_sidecar-eligible named dispatch, resolved via the real "
        "captured payload shape, must receive a real sidecar_path"
    )
    assert SIDECAR_MISS_MARKER not in result


# ---------------------------------------------------------------------------
# AC5, as executed rather than assumed -- see module docstring's negative
# spec. Reports the actual behaviour rather than papering over it.
# ---------------------------------------------------------------------------

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

    # CORRECT behaviour (AC5 fix): a type that resolved correctly via the
    # back-pointer chain and is genuinely absent from `policy.report_sidecar`
    # stays silent -- `compose_catering` runs for every SubagentStart, so
    # this is the majority of all dispatches, and this matches the
    # Agent-path hook's own `_is_report_sidecar_eligible` gate on the miss
    # notice verbatim. The population that should hear about a lost sidecar
    # is a type that never resolved at all, covered by
    # `test_named_dispatch_unresolved_type_gets_the_miss_marker` below --
    # not this one.
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
    """Table-style pin over `_is_named_teammate_agent_id`'s two arms: the
    permissive canonical-shape match (any non-empty name, any separator,
    `@session-<suffix>`) and the raw subagent-side `a<name>-<16hex>` form.
    A bare-hex unnamed id (no `@`, not `a<name>-<16hex>` shaped) must match
    neither."""
    assert _is_named_teammate_agent_id(agent_id) is expected


def test_unnamed_dispatch_off_roster_type_stays_silent(git_repo: Path) -> None:
    """Negative guard for the named-teammate predicate
    (`_is_named_teammate_agent_id`): an UNNAMED dispatch -- bare-hex
    `agent_id`, `agent_type` a genuine, resolved, off-roster policy key --
    must stay fully silent. Without this guard, the named-form predicate
    could rot into "any dispatch with an unresolved subagent_type" and
    start broadcasting the miss notice to the majority (unnamed,
    off-roster) population this fix is not supposed to touch."""
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
    """AC5's resolver-exception arm: a payload carrying no `agent_type` and
    no resolvable `agent_id` (so `resolve_effective_types` returns
    `agent_type == subagent_type == ""`, the same shape `compose_catering`'s
    own `except` arm produces on a resolver exception) -- distinct from the
    named-teammate-unresolved leg above, which fires even though
    `agent_type` IS populated (the teammate name). Both legs must surface
    the miss marker; this pins the "nothing resolved at all" leg
    specifically."""
    payload = {
        "agent_id": "",
        "agent_type": "",
        "session_id": REAL_SESSION_ID,
        "cwd": str(git_repo),
    }

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_MISS_MARKER in result
    assert SIDECAR_PATH_MARKER_PREFIX not in result


# ---------------------------------------------------------------------------
# Back-pointer-not-yet-written ordering case (C3 body) -- pinned with a test
# exercising the real relay order, not a docstring.
# ---------------------------------------------------------------------------

def _bookkeeping_params(cwd: str) -> dict:
    # `track_dispatched_agents._valid_agent_id` only accepts the bare-hex or
    # already-canonical `<name>@session-<short>` teammate form (its own
    # `_TEAMMATE_AGENT_RE`) -- NOT the raw subagent-side `a<name>-<16hex>`
    # form `cater_subagent_start` receives on `payload["agent_id"]`. The
    # bookkeeping op's caller resolves the canonical id before this leg
    # (DoE's shim), so this fixture mirrors that: the CANONICAL id, not
    # `REAL_RAW_AGENT_ID`.
    return {
        "session_id": REAL_SESSION_ID,
        "dispatched_agent_id": REAL_CANONICAL_AGENT_ID,
        "dispatched_model": "claude-x",
        "subagent_type": REAL_RESOLVED_TYPE,
    }


def test_bookkeeping_first_caters_the_real_named_dispatch(git_repo: Path) -> None:
    """Documented order (bookkeeping op, then cater op, same event): the
    back-pointer it writes is in place by the time the cater op reads it,
    so the real captured-shape payload gets its sidecar offer."""
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


# ---------------------------------------------------------------------------
# AC9 -- this chunk owns it. Process time, never wall clock; spawn count is
# untouched (this measures an in-process regex/delegation path, no
# subprocess anywhere on it).
# ---------------------------------------------------------------------------

def test_ac9_compose_catering_process_time_before_and_after_c2_delegation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measures `compose_catering`'s process time for the real named-
    dispatch payload under the CURRENT (post-C2) delegation, and under a
    monkeypatched stand-in for the PRE-C2 behaviour (`_canonical_agent_id`'s
    named-teammate leg returning the raw id unchanged, its literal
    contract before C2) -- both in the SAME process, no subprocess on
    either arm, so nothing here is wall-clock/peer-load noise.

    **The two arms do NOT differ only by a regex, and the ratio below must
    not be read as if they did.** The pre-C2 arm returns the raw id, so its
    back-pointer lookup MISSES (this fixture keys `.agents/` by the
    canonical form, which is the whole defect) and it never performs the
    two file reads the post-C2 arm does. So the measured spread is
    dominated by miss-vs-hit -- by the fix doing the work it exists to do
    -- not by delegation cost. Measured directly by giving the pre-C2 arm
    a back-pointer of its OWN so both arms hit: 0.9896ms vs 1.0938ms per
    call, i.e. the delegation itself costs ~10%, which IS the "a regex
    path, not a spawn" claim. Against the fixture as written the spread is
    ~3.3x, and every bit of that beyond ~10% is file reads, not compute.

    N=200, amortized per-call over the batch (NOT min-of-N -- see
    `_min_process_time`'s own docstring for why min-of-N can't read
    anything on this clock). Reports both numbers via the assertion
    message; a generous 25ms ceiling guards against a gross regression
    without being sensitive to a busy box (CLAUDE.md § Load norm -- this
    is process time of a single in-process call, not wall clock), and a
    ratio assertion below pins the actual claim that the only measured
    delta is the added regex/delegation path.

    Measured (this box, N=200): pre-C2-shaped avg~0.31ms, post-C2 (real)
    avg~1.02ms per call -- both numbers vary with machine load, so the
    assertion message reports the live figures on every run rather than
    pinning them as constants.
    """
    _write_backpointer(git_repo, REAL_CANONICAL_AGENT_ID, "em-session-ac9", REAL_RESOLVED_TYPE)
    payload = _real_shaped_payload(str(git_repo))

    def _min_process_time(n: int = 200) -> float:
        """Per-call process time, AMORTIZED over the batch rather than
        min-of-N per call.

        `time.process_time()` advertises a 100ns resolution, but on Windows
        it is backed by GetProcessTimes, whose real granularity is the
        ~15.6ms scheduler tick. A single `compose_catering` call is orders
        of magnitude below that, so min-of-N per call can only ever read
        0.0 -- which measures the clock, not the code, and reports a number
        that would look identical if the call had never run at all.
        Timing the whole batch and dividing puts the per-call figure back
        above the granularity floor.
        """
        start = time.process_time()
        for _ in range(n):
            compose_catering(payload, cwd=str(git_repo))
        return (time.process_time() - start) / n

    after_ms = _min_process_time() * 1000.0

    real_canonical = engine_mod._canonical_agent_id

    def _pre_c2_canonical_agent_id(raw_agent_id: str, session_id):
        # Pre-C2 contract, verbatim: the named-teammate leg was a format
        # predicate, not a transform -- returned the raw id unchanged for
        # BOTH the named-teammate and bare-hex forms alike.
        if engine_mod._NAMED_TEAMMATE_RE.fullmatch(raw_agent_id):
            return raw_agent_id
        return real_canonical(raw_agent_id, session_id)

    monkeypatch.setattr(engine_mod, "_canonical_agent_id", _pre_c2_canonical_agent_id)
    before_ms = _min_process_time() * 1000.0

    assert after_ms < 25.0 and before_ms < 25.0, (
        f"AC9 process-time measurement: pre-C2-shaped avg={before_ms:.4f}ms, "
        f"post-C2 (real) avg={after_ms:.4f}ms over N=200 -- delegation adds a "
        f"regex path, not a spawn; both must stay far under the 500ms "
        f"brightline and the leg's own 150ms sibling-plan budget"
    )
    # Review: coordinator:code-reviewer -- the ceiling above only bounds
    # each arm in isolation and would pass silently even if the added
    # regex/delegation path made post-C2 5x-10x more expensive than
    # pre-C2, as long as it stayed under 25ms. Pin the differential
    # claim itself with a ratio check -- but see the function docstring
    # for what this ratio actually measures. EM correction: the arms
    # differ by miss-vs-hit (the pre-C2 arm's lookup misses and skips two
    # file reads), NOT by a regex, so the baseline spread is ~3.3x by
    # construction and NOT a regression. 8x is chosen to sit above that
    # known-and-explained 3.3x with headroom for scheduler noise at these
    # sub-millisecond magnitudes on a box routinely running 50+ agents.
    # It therefore catches a gross regression (a reintroduced spawn blows
    # the absolute ceiling anyway) while deliberately NOT pretending to
    # resolve the ~10% delegation cost, which is below this instrument's
    # noise floor and was measured separately -- see the docstring.
    assert after_ms < before_ms * 8.0, (
        f"AC9 differential check: post-C2 avg={after_ms:.4f}ms is more than "
        f"8x pre-C2-shaped avg={before_ms:.4f}ms -- that is far above the "
        f"~3.3x this comparison costs by construction (miss-vs-hit), so "
        f"something beyond the back-pointer reads has gotten expensive"
    )
