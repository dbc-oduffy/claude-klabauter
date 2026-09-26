
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.session import identity as session_identity
from coordinator_core.subagent_sandbox import engine
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


CONFINED_TYPE = "coordinator:code-reviewer"
REPORT_SIDECAR_TYPE = "coordinator:executor"

BARE_HEX_AGENT_ID = "abc123def4567890"
NAMED_AGENT_ID = "aReviewBot-0123456789abcdef"
# What NAMED_AGENT_ID resolves to once a session_id of "em-session-1" is in
NAMED_CANONICAL_AGENT_ID = session_identity.build_canonical_agent_id("ReviewBot", "em-session-1"[:8])
#: writers that mint it (track_dispatched_agents._TEAMMATE_AGENT_RE and the two
#: _TEAMMATE_CANONICAL_RE copies), not from observed samples.
CANONICAL_TEAMMATE_AGENT_ID = "c7-agent-probe@session-2c79e462"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
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


def test_resolve_git_root_returns_toplevel(git_repo: Path) -> None:
    assert engine.resolve_git_root(str(git_repo)) == str(git_repo)


def test_resolve_git_root_non_repo_returns_none(tmp_path: Path) -> None:
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    assert engine.resolve_git_root(str(non_repo)) is None


@pytest.fixture(autouse=True)
def _reset_git_root_cache():
    engine.reset_resolve_git_root_cache()
    yield
    engine.reset_resolve_git_root_cache()


def _is_git_rev_parse(args) -> bool:
    cmd = args[0] if args else []
    return list(cmd)[:2] == ["git", "rev-parse"]


@pytest.fixture
def popen_spawn_count(monkeypatch):
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
    first = engine.resolve_git_root(str(git_repo))
    second = engine.resolve_git_root(str(git_repo))
    assert first == str(git_repo)
    assert second == str(git_repo)
    assert len(popen_spawn_count) == 1


def test_resolve_git_root_cwd_none_never_cached(
    git_repo: Path, popen_spawn_count: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(git_repo)
    engine.resolve_git_root(None)
    engine.resolve_git_root(None)
    assert len(popen_spawn_count) == 2


def test_resolve_git_root_failed_resolution_is_not_memoized(
    tmp_path: Path, popen_spawn_count: list
) -> None:
    not_yet_a_repo = tmp_path / "becomes-a-repo"
    not_yet_a_repo.mkdir()

    first = engine.resolve_git_root(str(not_yet_a_repo))
    assert first is None
    assert len(popen_spawn_count) == 1

    subprocess.run(["git", "init", "-q"], cwd=not_yet_a_repo, check=True, **no_console_passthrough_kwargs())
    second = engine.resolve_git_root(str(not_yet_a_repo))
    assert second == str(not_yet_a_repo)
    assert len(popen_spawn_count) == 2


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
    assert engine._canonical_agent_id(NAMED_AGENT_ID, None) == NAMED_AGENT_ID


def test_canonical_agent_id_unrecognized_form_empty() -> None:
    assert engine._canonical_agent_id("not-a-valid-id", None) == ""


def test_canonical_agent_id_em_side_canonical_teammate() -> None:
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
        "c7-agent-probe@session-",
        "@session-2c79e462",
        "c7-agent-probe@session-2C79E462",
        "c7/agent@session-2c79e462",
        "c7-agent-probe@session-2c79e462" + chr(10),
    ],
)
def test_canonical_agent_id_rejects_near_miss_canonical(raw: str) -> None:
    assert engine._canonical_agent_id(raw, "em-session-1") == ""


def test_canonical_agent_id_grammar_matches_the_minters() -> None:
    from coordinator_core.hooks import track_dispatched_agents, track_touched_files
    from coordinator_core.write_guards import _subagent_identity

    assert (
        engine._TEAMMATE_CANONICAL_RE.pattern
        == track_dispatched_agents._TEAMMATE_AGENT_RE.pattern
    )
    assert (
        engine._TEAMMATE_CANONICAL_RE.pattern
        == track_touched_files._TEAMMATE_CANONICAL_RE.pattern
    )
    # a copy growing STRICTER than the built id is caught too, not only a laxer
    built = _subagent_identity._cs_build_canonical_agent_id("c7-agent-probe", "2c79e462")
    assert engine._TEAMMATE_CANONICAL_RE.fullmatch(built)
    assert _subagent_identity._TEAMMATE_CANONICAL_RE.fullmatch(built)
    for raw in ("c7-agent-probe@session-2C79E462", "@session-2c79e462", "c7/agent@session-2c79e462"):
        assert engine._TEAMMATE_CANONICAL_RE.fullmatch(raw) is None
        assert _subagent_identity._TEAMMATE_CANONICAL_RE.fullmatch(raw) is None
        assert track_touched_files._TEAMMATE_CANONICAL_RE.fullmatch(raw) is None


def test_older_predicates_reject_trailing_newline() -> None:
    assert engine._canonical_agent_id(BARE_HEX_AGENT_ID + chr(10), None) == ""
    assert engine._canonical_agent_id(NAMED_AGENT_ID + chr(10), "em-session-1") == ""


def test_read_backpointer_resolves_for_canonical_teammate_id(git_repo: Path) -> None:
    _write_backpointer(git_repo, CANONICAL_TEAMMATE_AGENT_ID, "em-session-1", CONFINED_TYPE)
    _, _, subagent_type = engine.resolve_effective_types(
        {"agent_id": CANONICAL_TEAMMATE_AGENT_ID, "session_id": "em-session-1"},
        str(git_repo),
    )
    assert subagent_type == CONFINED_TYPE


def test_read_backpointer_subagent_type_resolves(git_repo: Path) -> None:
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == CONFINED_TYPE


def test_read_backpointer_subagent_type_missing_chain_empty(git_repo: Path) -> None:
    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == ""


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
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == CONFINED_TYPE


def test_read_backpointer_subagent_type_duplicate_full_rows_returns_empty(
    git_repo: Path,
) -> None:
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", REPORT_SIDECAR_TYPE)
    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == ""


def test_read_backpointer_subagent_type_legacy_short_row_ignored(
    git_repo: Path,
) -> None:
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
    sessions_base = git_repo / ".git" / "coordinator-sessions"
    dest = sessions_base / ".archive" / f"{em_session_id}-{stamp}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    (sessions_base / em_session_id).rename(dest)
    return dest


def test_read_backpointer_subagent_type_resolves_from_archived_session(
    git_repo: Path,
) -> None:
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    _archive_session(git_repo, "em-session-1")

    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == CONFINED_TYPE


def test_read_backpointer_subagent_type_duplicate_across_live_and_archive_ambiguous(
    git_repo: Path,
) -> None:
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", CONFINED_TYPE)
    _archive_session(git_repo, "em-session-1")
    _write_backpointer(git_repo, NAMED_AGENT_ID, "em-session-1", REPORT_SIDECAR_TYPE)

    resolved = engine._read_backpointer_subagent_type(str(git_repo), NAMED_AGENT_ID)
    assert resolved == ""


def test_read_backpointer_subagent_type_archive_of_another_session_not_read(
    git_repo: Path,
) -> None:
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
    path = _policy_with(
        tmp_path, "bad.yaml", f"report_sidecar:\n  - coordinator:executor\n{raw}\n"
    )
    policy = engine.load_policy(str(path))
    assert policy.report_type_map == {}
    assert policy.report_sidecar == {"coordinator:executor"}


def test_load_policy_report_type_map_drops_bad_entries_not_siblings(tmp_path: Path) -> None:
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
    wrong_typed_path = git_repo / "subagent-sandbox-policy.yaml"
    wrong_typed_path.write_text(
        yaml.safe_dump({"report_sidecar": REPORT_SIDECAR_TYPE}), encoding="utf-8"
    )
    policy = engine.load_policy(str(wrong_typed_path))
    assert policy.report_sidecar == set()


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


# _resolve_default_policy_path (CLAUDE_PLUGIN_ROOT best-effort default fallback)

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


_YAML_RATCHET_PROBE = """
import sys
sys.path.insert(0, {repo!r})
import coordinator_core.engine_provenance_counter
print("yaml" in sys.modules)
"""


def _run_yaml_ratchet_probe() -> bool:
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
    assert not _run_yaml_ratchet_probe(), (
        "coordinator_core.engine_provenance_counter's import registered "
        "'yaml' in sys.modules -- yaml must stay a function-local import "
        "inside engine.load_policy, not a module-scope import in engine.py."
    )
