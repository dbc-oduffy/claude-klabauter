"""
coordinator_core.subagent_sandbox.tests.test_engine -- resolver + policy-load
pytest harness.

Purpose: drives coordinator_core.subagent_sandbox.engine's surviving public
function seam (resolve_git_root / resolve_effective_types / _canonical_agent_id
/ _read_backpointer_subagent_type / Policy / load_policy) -- no live
coordinator install, no subprocess mocking beyond a real `git init`'d
tmp_path repo so resolve_git_root behaves like production.

DR-058 removed the PreToolUse Write/Edit/MultiEdit DENY enforcement this
module used to carry (evaluate / evaluate_payload_json / to_hook_output /
Decision / the confined/exempt/sanctioned_dirs policy fields, and the
__main__.py CLI entrypoint that exercised them); their tests were removed
in lockstep with the gutted engine.py. This file now covers only the
resolver-helper + load_policy surface that survives for
coordinator_core.bash_guards._helpers and
coordinator_core.subagent_sandbox.provision_report to consume.

Spec backlink: pln-claude-klabauter-subagent-sandbox-enforc-62cc03
                (original enforcement engine, now retired)
Removal: DoE DR-058, commit 0998c6a6 (write_guards splice excision)
Engine under test: coordinator_core/subagent_sandbox/engine.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.session import identity as session_identity
from coordinator_core.subagent_sandbox import engine

# Real git repo is load-bearing: resolve_git_root() is asserted against a
# real `git init`'d tree per this file's own module docstring so it behaves
# exactly as it does against a production checkout -- no subprocess mock
# stands in for git's own root-discovery behaviour.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


CONFINED_TYPE = "coordinator:code-reviewer"
REPORT_SIDECAR_TYPE = "coordinator:executor"

BARE_HEX_AGENT_ID = "abc123def4567890"
NAMED_AGENT_ID = "aReviewBot-0123456789abcdef"
# What NAMED_AGENT_ID resolves to once a session_id of "em-session-1" is in
# hand: `<name>@session-<session_id[:8]>`, the key form `.agents/` is named by.
# Derived via the real builder (not a hand-typed literal) so a future grammar
# change desyncs this assertion loudly instead of silently going stale.
NAMED_CANONICAL_AGENT_ID = session_identity.build_canonical_agent_id("ReviewBot", "em-session-1"[:8])
#: EM-side canonical teammate id — the shape a NAMED dispatch actually presents
#: and the shape `.agents/<agent_id>/` is keyed by. Grammar taken from the three
#: writers that mint it (track_dispatched_agents._TEAMMATE_AGENT_RE and the two
#: _TEAMMATE_CANONICAL_RE copies), not from observed samples.
CANONICAL_TEAMMATE_AGENT_ID = "c7-agent-probe@session-2c79e462"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real, empty git repo rooted at tmp_path (so resolve_git_root
    behaves exactly as it does against a production checkout)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    policy = {"report_sidecar": [REPORT_SIDECAR_TYPE]}
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


def _write_backpointer(
    git_root: Path,
    agent_id: str,
    em_session_id: str,
    subagent_type: str,
) -> None:
    """Fake .git/coordinator-sessions/.agents/<agent_id>/em-session-id.txt +
    .git/coordinator-sessions/<em_session_id>/dispatched-agents.txt back-pointer
    chain, mirroring the reference hook's on-disk layout (lines 179-198)."""
    agents_dir = git_root / ".git" / "coordinator-sessions" / ".agents" / agent_id
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "em-session-id.txt").write_text(em_session_id + "\n", encoding="utf-8")

    session_dir = git_root / ".git" / "coordinator-sessions" / em_session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    dispatch_file = session_dir / "dispatched-agents.txt"
    row = f"{agent_id}\t2026-07-12T00:00:00Z\t{subagent_type}\n"
    if dispatch_file.exists():
        with dispatch_file.open("a", encoding="utf-8") as fh:
            fh.write(row)
    else:
        dispatch_file.write_text(row, encoding="utf-8")


# ---------------------------------------------------------------------------
# resolve_git_root
# ---------------------------------------------------------------------------

def test_resolve_git_root_returns_toplevel(git_repo: Path) -> None:
    assert engine.resolve_git_root(str(git_repo)) == str(git_repo)


def test_resolve_git_root_non_repo_returns_none(tmp_path: Path) -> None:
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    assert engine.resolve_git_root(str(non_repo)) is None


# ---------------------------------------------------------------------------
# resolve_git_root cache (36e9e3d3c) -- Review: code-reviewer F2, P1. The
# commit added `_resolve_git_root_cached`/`reset_resolve_git_root_cache`
# with no test exercising the cache itself; these pin the three properties
# the commit message claims. Spawn count is measured by wrapping
# `subprocess.Popen` ONLY (not `subprocess.run` as well -- patching both
# double-counts, since `subprocess.run` is implemented on top of `Popen`
# and each real `git` invocation would otherwise be tallied twice).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_git_root_cache():
    """Cache is process-global (`functools.lru_cache`) and keyed on `cwd`
    only -- `tmp_path` is unique per test so cross-test collisions are not
    expected, but resetting keeps these cache-focused tests independent of
    ordering/collection changes regardless."""
    engine.reset_resolve_git_root_cache()
    yield
    engine.reset_resolve_git_root_cache()


def _is_git_rev_parse(args) -> bool:
    cmd = args[0] if args else []
    return list(cmd)[:2] == ["git", "rev-parse"]


@pytest.fixture
def popen_spawn_count(monkeypatch):
    """Wrap the real `subprocess.Popen` to count `resolve_git_root`'s own
    `git rev-parse --show-toplevel` spawns while still executing real `git`
    calls -- returns a mutable list whose length is the spawn count so far.
    Filters to that one command shape so a fixture setup step in the same
    test (e.g. `git init` for the failed-then-succeeds case) does not
    inflate the count this fixture exists to pin."""
    calls: list = []
    real_popen = subprocess.Popen

    def _counting_popen(*args, **kwargs):
        if args and _is_git_rev_parse(args):
            calls.append((args, kwargs))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _counting_popen)
    return calls


def test_resolve_git_root_cache_hits_on_repeat_same_cwd(
    git_repo: Path, popen_spawn_count: list
) -> None:
    # Same explicit cwd twice -> the second call is served from cache, so
    # exactly one `git` spawn total (not two).
    first = engine.resolve_git_root(str(git_repo))
    second = engine.resolve_git_root(str(git_repo))
    assert first == str(git_repo)
    assert second == str(git_repo)
    assert len(popen_spawn_count) == 1


def test_resolve_git_root_cwd_none_never_cached(
    git_repo: Path, popen_spawn_count: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cwd=None means "resolve against the current process cwd right now" --
    # documented as the never-cached branch, so each call re-spawns.
    monkeypatch.chdir(git_repo)
    engine.resolve_git_root(None)
    engine.resolve_git_root(None)
    assert len(popen_spawn_count) == 2


def test_resolve_git_root_failed_resolution_is_not_memoized(
    tmp_path: Path, popen_spawn_count: list
) -> None:
    # A failed resolution for a given cwd must NOT be memoized -- pins the
    # `_GitRootResolutionFailed` mechanism specifically (a future
    # "simplification" to catch-and-cache `None` would break this while
    # leaving test_resolve_git_root_cache_hits_on_repeat_same_cwd green).
    not_yet_a_repo = tmp_path / "becomes-a-repo"
    not_yet_a_repo.mkdir()

    first = engine.resolve_git_root(str(not_yet_a_repo))
    assert first is None
    assert len(popen_spawn_count) == 1

    subprocess.run(["git", "init", "-q"], cwd=not_yet_a_repo, check=True)
    second = engine.resolve_git_root(str(not_yet_a_repo))
    assert second == str(not_yet_a_repo)
    # The failed call must have re-spawned rather than being served a
    # memoized `None` -- two total spawns for two calls at the same cwd.
    assert len(popen_spawn_count) == 2


# ---------------------------------------------------------------------------
# _canonical_agent_id
# ---------------------------------------------------------------------------

def test_canonical_agent_id_bare_hex() -> None:
    assert engine._canonical_agent_id(BARE_HEX_AGENT_ID, None) == BARE_HEX_AGENT_ID


def test_canonical_agent_id_named_teammate_session_present() -> None:
    """With a session_id present the named-teammate leg TRANSFORMS the
    subagent-side raw form into the EM-side canonical key the `.agents/`
    directories are actually named by, rather than returning it unchanged.
    Returning it unchanged is the defect
    docs/plans/2026-08-25-a-named-dispatch-keeps-its-report.md fixed."""
    assert (
        engine._canonical_agent_id(NAMED_AGENT_ID, "em-session-1")
        == NAMED_CANONICAL_AGENT_ID
    )


def test_canonical_agent_id_named_teammate_session_absent_fallback() -> None:
    """the Staff Engineer F4: session_id-absent named-teammate leg keys on the raw
    agent_id itself, not a session_id-resolved value."""
    assert engine._canonical_agent_id(NAMED_AGENT_ID, None) == NAMED_AGENT_ID


def test_canonical_agent_id_unrecognized_form_empty() -> None:
    assert engine._canonical_agent_id("not-a-valid-id", None) == ""


def test_canonical_agent_id_em_side_canonical_teammate() -> None:
    """A NAMED dispatch presents `<name>@session-<short>`, not the raw
    subagent-side `a<name>-<16hex>`. Accepting only the latter returned "" for
    every named dispatch, so resolve_effective_types skipped the back-pointer
    leg entirely and the teammate's true subagent_type never resolved."""
    assert (
        engine._canonical_agent_id(CANONICAL_TEAMMATE_AGENT_ID, "em-session-1")
        == CANONICAL_TEAMMATE_AGENT_ID
    )
    assert (
        engine._canonical_agent_id(CANONICAL_TEAMMATE_AGENT_ID, None)
        == CANONICAL_TEAMMATE_AGENT_ID
    )


@pytest.mark.parametrize(
    "raw",
    [
        "c7-agent-probe@session-",           # empty short
        "@session-2c79e462",                 # empty name
        "c7-agent-probe@session-2C79E462",   # short is lowercase-only
        "c7/agent@session-2c79e462",         # separator outside the name grammar
        "c7-agent-probe@session-2c79e462" + chr(10),  # trailing newline (fullmatch, not match)
    ],
)
def test_canonical_agent_id_rejects_near_miss_canonical(raw: str) -> None:
    """The widening is exactly the writers' grammar and no wider — this is an
    id-matching predicate on a guard surface, so a near miss must still fail
    closed rather than resolve a back-pointer directory it was not keyed to."""
    assert engine._canonical_agent_id(raw, "em-session-1") == ""


def test_canonical_agent_id_grammar_matches_the_minters() -> None:
    """Pins the reader to its writers: if a writer's grammar moves, this fails
    rather than the named leg silently going dead again."""
    from coordinator_core.hooks import track_dispatched_agents, track_touched_files
    from coordinator_core.write_guards import _subagent_identity

    assert (
        engine._TEAMMATE_CANONICAL_RE.pattern
        == track_dispatched_agents._TEAMMATE_AGENT_RE.pattern
    )
    # Review: coordinator:code-reviewer (2026-08-23, P3) -- track_touched_files
    # is named as one of the three writers by this module's docstring and was
    # the one copy this test never referenced, so a drift in ITS charset alone
    # would have gone uncaught and killed the leg again for exactly the
    # population that writer governs.
    assert (
        engine._TEAMMATE_CANONICAL_RE.pattern
        == track_touched_files._TEAMMATE_CANONICAL_RE.pattern
    )
    # _subagent_identity's copy adds named capture groups, so its .pattern
    # string differs by construction -- compare behaviour, both directions, so
    # a copy growing STRICTER than the built id is caught too, not only a laxer
    # one (the one-directional check this replaces could not see that).
    built = _subagent_identity._cs_build_canonical_agent_id("c7-agent-probe", "2c79e462")
    assert engine._TEAMMATE_CANONICAL_RE.fullmatch(built)
    assert _subagent_identity._TEAMMATE_CANONICAL_RE.fullmatch(built)
    for raw in ("c7-agent-probe@session-2C79E462", "@session-2c79e462", "c7/agent@session-2c79e462"):
        assert engine._TEAMMATE_CANONICAL_RE.fullmatch(raw) is None
        assert _subagent_identity._TEAMMATE_CANONICAL_RE.fullmatch(raw) is None
        assert track_touched_files._TEAMMATE_CANONICAL_RE.fullmatch(raw) is None


def test_older_predicates_reject_trailing_newline() -> None:
    """Review: coordinator:code-reviewer (2026-08-23, P3). `match` with a `$`
    anchor also matches immediately before one trailing newline, so the two
    pre-existing legs accepted `"<id>
"` and keyed a `.agents/<id>
/`
    directory no writer creates. All three legs now use `fullmatch`."""
    assert engine._canonical_agent_id(BARE_HEX_AGENT_ID + chr(10), None) == ""
    assert engine._canonical_agent_id(NAMED_AGENT_ID + chr(10), "em-session-1") == ""


def test_read_backpointer_resolves_for_canonical_teammate_id(git_repo: Path) -> None:
    """End of the chain the regex was cutting: with the canonical id accepted,
    resolve_effective_types reaches the back-pointer and returns the real type."""
    _write_backpointer(git_repo, CANONICAL_TEAMMATE_AGENT_ID, "em-session-1", CONFINED_TYPE)
    _, _, subagent_type = engine.resolve_effective_types(
        {"agent_id": CANONICAL_TEAMMATE_AGENT_ID, "session_id": "em-session-1"},
        str(git_repo),
    )
    assert subagent_type == CONFINED_TYPE


# ---------------------------------------------------------------------------
# _read_backpointer_subagent_type
# ---------------------------------------------------------------------------

def test_read_backpointer_subagent_type_resolves(git_repo: Path) -> None:
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == CONFINED_TYPE


def test_read_backpointer_subagent_type_missing_chain_empty(git_repo: Path) -> None:
    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == ""


# Review: coordinator:code-reviewer (2026-08-14, Divergence 18 deferred
# finding) -- expected_em_session_id cross-check. The back-pointer chain
# never verified the em_session_id it read from em-session-id.txt matched
# the calling payload's own session_id; a stale/cross-session/fabricated
# back-pointer could resolve an unrelated session's dispatch row.


def test_read_backpointer_subagent_type_matching_session_still_resolves(
    git_repo: Path,
) -> None:
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    resolved = engine._read_backpointer_subagent_type(
        str(git_repo), NAMED_AGENT_ID, expected_em_session_id="em-session-1"
    )
    assert resolved == CONFINED_TYPE


def test_read_backpointer_subagent_type_different_session_fails_lookup(
    git_repo: Path,
) -> None:
    """A back-pointer naming a DIFFERENT session must not resolve a type --
    the confinement-bypass oracle this parameter exists to close."""
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    resolved = engine._read_backpointer_subagent_type(
        str(git_repo), NAMED_AGENT_ID, expected_em_session_id="some-other-session"
    )
    assert resolved == ""


def test_read_backpointer_subagent_type_unset_param_byte_identical_to_before(
    git_repo: Path,
) -> None:
    """Default (unset) expected_em_session_id preserves pre-existing
    behaviour -- resolve_effective_types and every other caller that passes
    nothing must see no change."""
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == CONFINED_TYPE


def test_read_backpointer_subagent_type_duplicate_full_rows_returns_empty(
    git_repo: Path,
) -> None:
    """P3, duplicate-row ambiguity: two full (3+-column) rows for the same
    agent_id must resolve to "" (ambiguous, fail-closed) rather than
    whichever appears first in file order."""
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", REPORT_SIDECAR_TYPE)
    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == ""


def test_read_backpointer_subagent_type_legacy_short_row_ignored(
    git_repo: Path,
) -> None:
    """A legacy 2-column row for agent_id must be ignored outright (never
    matched), so a single co-existing full row still resolves cleanly."""
    session_dir = git_repo / ".git" / "coordinator-sessions" / "em-session-1"
    session_dir.mkdir(parents=True, exist_ok=True)
    dispatch_file = session_dir / "dispatched-agents.txt"
    dispatch_file.write_text(
        f"{NAMED_AGENT_ID}\t2026-07-01T00:00:00Z\n"
        f"{NAMED_AGENT_ID}\t2026-07-12T00:00:00Z\t{CONFINED_TYPE}\n",
        encoding="utf-8",
    )
    agents_dir = git_repo / ".git" / "coordinator-sessions" / ".agents" / NAMED_AGENT_ID
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "em-session-id.txt").write_text("em-session-1\n", encoding="utf-8")

    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == CONFINED_TYPE


def _archive_session(git_repo: Path, em_session_id: str, stamp: str = "2026-08-20") -> Path:
    """Relocate a live session dir to .archive/<em_sid>-<date>/, as the cadence does."""
    sessions_base = git_repo / ".git" / "coordinator-sessions"
    dest = sessions_base / ".archive" / f"{em_session_id}-{stamp}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    (sessions_base / em_session_id).rename(dest)
    return dest


def test_read_backpointer_subagent_type_resolves_from_archived_session(
    git_repo: Path,
) -> None:
    """The archival cadence moves <em_sid>/ to .archive/<em_sid>-<date>/. The
    back-pointer still names <em_sid>, so a lookup that knows only the live path
    goes blind on every archived session -- the shape that left 1000 of 1250
    back-pointers on this box unresolvable."""
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    _archive_session(git_repo, "em-session-1")

    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == CONFINED_TYPE


def test_read_backpointer_subagent_type_duplicate_across_live_and_archive_ambiguous(
    git_repo: Path,
) -> None:
    """Pooling live and archived rows must not smuggle a resolution past the
    single-match rule: two differing full rows for one agent_id stay fail-closed
    however they are split across the two locations."""
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    _archive_session(git_repo, "em-session-1")
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", REPORT_SIDECAR_TYPE)

    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == ""


def test_read_backpointer_subagent_type_archive_of_another_session_not_read(
    git_repo: Path,
) -> None:
    """The archive fallback follows ONE session's relocation. An archived dir
    belonging to a different em_sid must not answer for this agent_id."""
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    _archive_session(git_repo, "em-session-1")
    sessions_base = git_repo / ".git" / "coordinator-sessions"
    other = sessions_base / ".archive" / "em-session-2-2026-08-20"
    other.mkdir(parents=True, exist_ok=True)
    (other / "dispatched-agents.txt").write_text(
        f"{NAMED_AGENT_ID}\t2026-07-12T00:00:00Z\t{REPORT_SIDECAR_TYPE}\n",
        encoding="utf-8",
    )

    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == CONFINED_TYPE


def test_read_backpointer_subagent_type_prefix_collision_archive_not_read(
    git_repo: Path,
) -> None:
    """`.archive/<em_sid>-*` must not match a LONGER session id that merely
    starts with this one -- em-session-1 vs em-session-1a."""
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    _archive_session(git_repo, "em-session-1")
    sibling = git_repo / ".git" / "coordinator-sessions" / ".archive" / "em-session-1a-2026-08-20"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "dispatched-agents.txt").write_text(
        f"{NAMED_AGENT_ID}\t2026-07-12T00:00:00Z\t{REPORT_SIDECAR_TYPE}\n",
        encoding="utf-8",
    )

    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == CONFINED_TYPE


# ---------------------------------------------------------------------------
# resolve_effective_types
# ---------------------------------------------------------------------------

def test_resolve_effective_types_bare_hex_agent_type_leg(git_repo: Path) -> None:
    payload = {"agent_id": BARE_HEX_AGENT_ID, "agent_type": CONFINED_TYPE}
    agent_id, agent_type, subagent_type = engine.resolve_effective_types(
        payload, str(git_repo)
    )
    assert agent_id == BARE_HEX_AGENT_ID
    assert agent_type == CONFINED_TYPE
    assert subagent_type == ""


def test_resolve_effective_types_named_teammate_backpointer_leg(git_repo: Path) -> None:
    """The back-pointer is keyed by the EM-side CANONICAL id, which is why a
    payload carrying the subagent-side raw form has to be transformed before
    the lookup rather than statted as-is."""
    _write_backpointer(
        git_repo, NAMED_CANONICAL_AGENT_ID, "em-session-1", CONFINED_TYPE
    )
    payload = {"agent_id": NAMED_AGENT_ID, "session_id": "em-session-1"}
    agent_id, agent_type, subagent_type = engine.resolve_effective_types(
        payload, str(git_repo)
    )
    assert agent_id == NAMED_CANONICAL_AGENT_ID
    assert agent_type == ""
    assert subagent_type == CONFINED_TYPE


def test_resolve_effective_types_no_agent_id_all_empty(git_repo: Path) -> None:
    agent_id, agent_type, subagent_type = engine.resolve_effective_types(
        {}, str(git_repo)
    )
    assert agent_id == ""
    assert agent_type == ""
    assert subagent_type == ""


def test_resolve_effective_types_no_git_root_no_backpointer_lookup() -> None:
    payload = {"agent_id": NAMED_AGENT_ID, "session_id": "em-session-1"}
    agent_id, agent_type, subagent_type = engine.resolve_effective_types(payload, None)
    assert agent_id == NAMED_CANONICAL_AGENT_ID
    assert subagent_type == ""


# ---------------------------------------------------------------------------
# Policy / load_policy
# ---------------------------------------------------------------------------

def test_policy_empty_by_default() -> None:
    policy = engine.Policy()
    assert policy.is_empty is True
    assert policy.report_sidecar == set()


def test_policy_not_empty_with_report_sidecar() -> None:
    policy = engine.Policy(report_sidecar=[REPORT_SIDECAR_TYPE])
    assert policy.is_empty is False
    assert policy.report_sidecar == {REPORT_SIDECAR_TYPE}


def test_load_policy_reads_report_sidecar(policy_path: Path) -> None:
    policy = engine.load_policy(str(policy_path))
    assert policy.report_sidecar == {REPORT_SIDECAR_TYPE}


def _policy_with(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_load_policy_reads_report_type_map(tmp_path: Path) -> None:
    path = _policy_with(
        tmp_path,
        "typed.yaml",
        "report_sidecar:\n"
        "  - coordinator:code-reviewer\n"
        "report_type_map:\n"
        "  coordinator:code-reviewer: review-findings\n"
        "  coordinator:executor: run-report\n",
    )
    policy = engine.load_policy(str(path))
    assert policy.report_type_map == {
        "coordinator:code-reviewer": "review-findings",
        "coordinator:executor": "run-report",
    }


def test_load_policy_report_type_map_absent_is_empty_mapping(policy_path: Path) -> None:
    """Absence is the pre-existing shape of every policy — it must never raise
    and must never look like a partial map."""
    assert engine.load_policy(str(policy_path)).report_type_map == {}


@pytest.mark.parametrize(
    "raw",
    [
        "report_type_map: [not, a, dict]",
        "report_type_map: a-bare-string",
        "report_type_map: 17",
        "report_type_map:",
    ],
)
def test_load_policy_report_type_map_non_dict_fails_open(tmp_path: Path, raw: str) -> None:
    """Same fail-open discipline report_sidecar documents: a wrong-typed value
    voids the map rather than raising or blocking a spawn."""
    path = _policy_with(
        tmp_path, "bad.yaml", f"report_sidecar:\n  - coordinator:executor\n{raw}\n"
    )
    policy = engine.load_policy(str(path))
    assert policy.report_type_map == {}
    assert policy.report_sidecar == {"coordinator:executor"}


def test_load_policy_report_type_map_drops_bad_entries_not_siblings(tmp_path: Path) -> None:
    """Per-entry, not all-or-nothing: one malformed row must not take the
    well-formed rows down with it."""
    path = _policy_with(
        tmp_path,
        "mixed.yaml",
        "report_sidecar:\n"
        "  - coordinator:code-reviewer\n"
        "report_type_map:\n"
        "  coordinator:code-reviewer: review-findings\n"
        "  coordinator:broken:\n"
        "    nested: value\n"
        "  17: run-report\n",
    )
    policy = engine.load_policy(str(path))
    assert policy.report_type_map == {"coordinator:code-reviewer": "review-findings"}


def test_load_policy_ignores_dr058_removed_keys(tmp_path: Path) -> None:
    """A YAML that still carries the DR-058-removed confined/exempt/
    sanctioned_dirs keys (pending DoE's lockstep strip) must not raise or
    otherwise fail to load report_sidecar -- unknown keys are silently
    ignored."""
    stale_path = tmp_path / "subagent-sandbox-policy.yaml"
    stale_path.write_text(
        yaml.safe_dump(
            {
                "confined": [CONFINED_TYPE],
                "exempt": ["coordinator:executor"],
                "sanctioned_dirs": ["state/review-trail/findings/"],
                "report_sidecar": [REPORT_SIDECAR_TYPE],
            }
        ),
        encoding="utf-8",
    )
    policy = engine.load_policy(str(stale_path))
    assert policy.report_sidecar == {REPORT_SIDECAR_TYPE}
    assert not hasattr(policy, "confined")
    assert not hasattr(policy, "exempt")
    assert not hasattr(policy, "sanctioned_dirs")


def test_load_policy_absent_file_empty(git_repo: Path) -> None:
    missing_path = str(git_repo / "does-not-exist-policy.yaml")
    policy = engine.load_policy(missing_path)
    assert policy.is_empty is True


def test_load_policy_malformed_yaml_empty(git_repo: Path) -> None:
    bad_policy_path = git_repo / "subagent-sandbox-policy.yaml"
    bad_policy_path.write_text("report_sidecar: [unterminated\n  - foo\nbar: {", encoding="utf-8")
    policy = engine.load_policy(str(bad_policy_path))
    assert policy.is_empty is True


def test_load_policy_not_a_dict_empty(git_repo: Path) -> None:
    list_policy_path = git_repo / "subagent-sandbox-policy.yaml"
    list_policy_path.write_text(
        yaml.safe_dump(["report_sidecar"]), encoding="utf-8"
    )
    policy = engine.load_policy(str(list_policy_path))
    assert policy.is_empty is True


def test_load_policy_wrong_typed_report_sidecar_empty(git_repo: Path) -> None:
    """`report_sidecar` as a bare string (not a list) coerces to empty."""
    wrong_typed_path = git_repo / "subagent-sandbox-policy.yaml"
    wrong_typed_path.write_text(
        yaml.safe_dump({"report_sidecar": REPORT_SIDECAR_TYPE}), encoding="utf-8"
    )
    policy = engine.load_policy(str(wrong_typed_path))
    assert policy.report_sidecar == set()


# ---------------------------------------------------------------------------
# Policy.bash_policy / load_policy bash_policy pickup
# ---------------------------------------------------------------------------

def test_policy_bash_policy_empty_by_default() -> None:
    policy = engine.Policy()
    assert policy.bash_policy == {}


def test_load_policy_reads_two_distinct_bash_policy_rows_no_cross_leak(
    git_repo: Path,
) -> None:
    """AC10: two DISTINCT subagent_type rows resolve independently -- proves
    the grammar's generality (a second row is a plain additional key, no
    code change) without converting a real second agent."""
    first_rule = {"allow": ["git status"]}
    second_rule = {"allow": ["ls"]}
    policy_path = git_repo / "subagent-sandbox-policy.yaml"
    policy_path.write_text(
        yaml.safe_dump(
            {
                "report_sidecar": [REPORT_SIDECAR_TYPE],
                "bash_policy": {
                    "coordinator:code-reviewer": first_rule,
                    "coordinator:executor": second_rule,
                },
            }
        ),
        encoding="utf-8",
    )
    policy = engine.load_policy(str(policy_path))
    assert policy.bash_policy["coordinator:code-reviewer"] == first_rule
    assert policy.bash_policy["coordinator:executor"] == second_rule
    assert policy.bash_policy["coordinator:code-reviewer"] != policy.bash_policy["coordinator:executor"]


def test_load_policy_bash_policy_absent_key_empty(policy_path: Path) -> None:
    """`policy_path` fixture carries no bash_policy key at all -- lookup-miss
    fails open to an empty mapping, never a parse failure."""
    policy = engine.load_policy(str(policy_path))
    assert policy.bash_policy == {}


def test_load_policy_bash_policy_not_a_dict_empty(git_repo: Path) -> None:
    wrong_typed_path = git_repo / "subagent-sandbox-policy.yaml"
    wrong_typed_path.write_text(
        yaml.safe_dump({"bash_policy": ["coordinator:code-reviewer"]}),
        encoding="utf-8",
    )
    policy = engine.load_policy(str(wrong_typed_path))
    assert policy.bash_policy == {}


def test_load_policy_bash_policy_malformed_row_dropped(git_repo: Path) -> None:
    """A non-dict per-key value is dropped rather than raising or blocking
    the spawn; a sibling well-formed row still resolves."""
    mixed_path = git_repo / "subagent-sandbox-policy.yaml"
    mixed_path.write_text(
        yaml.safe_dump(
            {
                "bash_policy": {
                    "coordinator:code-reviewer": "not-a-mapping",
                    "coordinator:executor": {"allow": ["ls"]},
                }
            }
        ),
        encoding="utf-8",
    )
    policy = engine.load_policy(str(mixed_path))
    assert "coordinator:code-reviewer" not in policy.bash_policy
    assert policy.bash_policy["coordinator:executor"] == {"allow": ["ls"]}


def test_load_policy_absent_file_bash_policy_empty(git_repo: Path) -> None:
    missing_path = str(git_repo / "does-not-exist-policy.yaml")
    policy = engine.load_policy(missing_path)
    assert policy.bash_policy == {}


# ---------------------------------------------------------------------------
# _resolve_default_policy_path (CLAUDE_PLUGIN_ROOT best-effort default fallback)
# ---------------------------------------------------------------------------

def test_resolve_default_policy_path_env_absent_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    assert engine._resolve_default_policy_path() is None


def test_resolve_default_policy_path_env_set_file_absent_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    assert engine._resolve_default_policy_path() is None


def test_resolve_default_policy_path_env_set_file_present_returns_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    policy_file = tmp_path / engine._DEFAULT_POLICY_RELATIVE
    policy_file.write_text(
        yaml.safe_dump({"report_sidecar": []}), encoding="utf-8"
    )
    resolved = engine._resolve_default_policy_path()
    assert resolved == policy_file


def test_load_policy_falls_through_arg_env_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No explicit policy_path arg, no SUBAGENT_SANDBOX_POLICY env var -- proves
    the 3-tier resolution order (arg > env > default) falls through to the
    CLAUDE_PLUGIN_ROOT-based default and loads it successfully."""
    monkeypatch.delenv(engine.POLICY_ENV_VAR, raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    policy_file = tmp_path / engine._DEFAULT_POLICY_RELATIVE
    policy_file.write_text(
        yaml.safe_dump({"report_sidecar": [REPORT_SIDECAR_TYPE]}),
        encoding="utf-8",
    )
    loaded = engine.load_policy()
    assert loaded.report_sidecar == {REPORT_SIDECAR_TYPE}


# ---------------------------------------------------------------------------
# yaml-import ratchet -- state/sizings/2026-08-26-the-provenance-seam-stops-
# dragging-yaml.yaml. `yaml` is used ONLY inside `load_policy`; a
# fresh-interpreter import of `coordinator_core.engine_provenance_counter`
# (which imports `resolve_git_root_cheap` from this package, never
# `load_policy`) must not register it. Same idiom as
# `coordinator/bin/tests/test_cc_invoke_warm_in_process.py :: _run_ac7_probe`
# -- a fresh subprocess interpreter, asserting on `sys.modules` membership,
# never on timing.
# ---------------------------------------------------------------------------

_YAML_RATCHET_PROBE = """
import sys
sys.path.insert(0, {repo!r})
import coordinator_core.engine_provenance_counter
print("yaml" in sys.modules)
"""


def _run_yaml_ratchet_probe() -> bool:
    """Import `engine_provenance_counter` in a fresh interpreter; return
    whether `yaml` landed in `sys.modules` as a result."""
    repo_root = Path(engine.__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", _YAML_RATCHET_PROBE.format(repo=str(repo_root))],
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        f"probe failed (rc={proc.returncode}): {proc.stderr[-800:]}"
    )
    return proc.stdout.strip() == "True"


def test_engine_provenance_counter_import_does_not_register_yaml() -> None:
    """A fresh-interpreter import of `engine_provenance_counter` -- which pulls
    `resolve_git_root_cheap` through this package's `__init__.py` -- must not
    pay `import yaml`'s module-registration cost. `yaml` is used exclusively
    inside `load_policy`, which this import path never calls."""
    assert not _run_yaml_ratchet_probe(), (
        "coordinator_core.engine_provenance_counter's import registered "
        "'yaml' in sys.modules -- yaml must stay a function-local import "
        "inside engine.load_policy, not a module-scope import in engine.py."
    )
