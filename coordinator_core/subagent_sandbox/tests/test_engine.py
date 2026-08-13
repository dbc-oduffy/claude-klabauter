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
Removal: coordinator-claude DR-058, commit 0998c6a6 (write_guards splice excision)
Engine under test: coordinator_core/subagent_sandbox/engine.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

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
    assert engine._canonical_agent_id(NAMED_AGENT_ID, "em-session-1") == NAMED_AGENT_ID


def test_canonical_agent_id_named_teammate_session_absent_fallback() -> None:
    """the Staff Engineer F4: session_id-absent named-teammate leg keys on the raw
    agent_id itself, not a session_id-resolved value."""
    assert engine._canonical_agent_id(NAMED_AGENT_ID, None) == NAMED_AGENT_ID


def test_canonical_agent_id_unrecognized_form_empty() -> None:
    assert engine._canonical_agent_id("not-a-valid-id", None) == ""


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
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    payload = {"agent_id": NAMED_AGENT_ID, "session_id": "em-session-1"}
    agent_id, agent_type, subagent_type = engine.resolve_effective_types(
        payload, str(git_repo)
    )
    assert agent_id == NAMED_AGENT_ID
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
    assert agent_id == NAMED_AGENT_ID
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
    sanctioned_dirs keys (pending coordinator-claude's lockstep strip) must not raise or
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
