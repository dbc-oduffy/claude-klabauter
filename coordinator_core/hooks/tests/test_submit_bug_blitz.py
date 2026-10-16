"""`hooks.submit_bug_blitz.evaluate` on a tmp repo through the real `dispatch.emit` queue route, with fake
parse and mint: the rewrite envelope, the run records, the denials, and an exact spawn count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.bug_blitz.contract import FailingTest, ParseResult, TargetSpec, run_tag
from coordinator_core.hooks import submit_bug_blitz as mod

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN_ID = "20261015T120000Z"
_FIXTURE_PROFILE = (
    Path(__file__).resolve().parents[2] / "ops" / "dispatch_emit" / "tests" / "fixtures" / "queue-profiles" / "fixture.yaml"
)
TESTS = (
    FailingTest(file="pkg/a.test.ts", name="a one", package="pkg", message="boom"),
    FailingTest(file="pkg/b.test.ts", name="b one", package="pkg"),
)


def _budget() -> int:
    return load_manifest()["overrides"]["hooks.submit_bug_blitz"]["spawn_count_budget"]["targets_submission"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    root = tmp_path / "repo"
    (root / "state" / "bug-backlog").mkdir(parents=True)
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    _head(root, "work/box/2026-10-15")
    profiles = tmp_path / "queue-profiles"
    profiles.mkdir()
    (profiles / "bug.yaml").write_text(_FIXTURE_PROFILE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(mod, "_profile_dir", lambda: str(profiles))
    return root


def _head(root: Path, branch: str) -> None:
    (root / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")


def _payload(root: Path, args, name: str = "coordinator:bug-blitz") -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": name, "args": args},
        "session_id": "sess-blitz",
        "cwd": str(root),
    }


def _resolve(raw: str, root: Path) -> TargetSpec:
    return TargetSpec(kind="backlog" if raw == "backlog" else "dir", path="" if raw == "backlog" else raw)


def _parse(spec: TargetSpec) -> ParseResult:
    return ParseResult(tests=TESTS, unparsed_count=2, unparsed_sample=("junk",))


def _mint(root: Path, run_id: str, tests: tuple, *, target_ref: str) -> list[str]:
    return [f"TF-{i}" for i, _ in enumerate(sorted({t.file for t in tests}))]


def _call(root: Path, args, **kw) -> dict:
    kw.setdefault("resolve", _resolve)
    kw.setdefault("parse", _parse)
    kw.setdefault("mint", _mint)
    kw.setdefault("run_id", RUN_ID)
    return mod.evaluate(_payload(root, args), **kw)


def _reason(env: dict) -> str:
    return env["hookSpecificOutput"]["permissionDecisionReason"]


def _no_grant_records(root: Path) -> None:
    assert not [p for p in root.rglob("*") if p.is_file() and "tier-u" in p.name.lower()]


def test_targets_submission_emits_the_grind_and_rewrites_the_call(repo, monkeypatch):
    with _count_spawns_attributed(monkeypatch) as spawns:
        env = _call(repo, {"targets": "red-set"})

    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    updated = hso["updatedInput"]
    script = Path(updated["scriptPath"])
    assert script.is_file()
    assert script.parent == repo / "state" / "scratch" / "bug-blitz" / RUN_ID
    assert script.with_name(script.name + ".emitted.json").is_file()
    assert updated["args"]["run_stamp"] == RUN_ID
    assert updated["args"]["script_path"] == str(script)
    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    assert decisions["commit_authority"] == {"basis": "submission"}
    assert decisions["verify"] == "targeted-tests"
    assert decisions["parse"] == {"parsed": 2, "unparsed_count": 2, "unparsed_sample": ["junk"]}
    assert decisions["minted"] == ["state/bug-backlog/TF-0.yaml", "state/bug-backlog/TF-1.yaml"]
    assert len(spawns) == _budget(), [s.argv for s in spawns]
    _no_grant_records(repo)


def test_rewritten_args_carry_a_run_token_that_admits(repo):
    from coordinator_core.ops.dispatch_emit import run_token

    env = _call(repo, {"targets": "red-set"})
    updated = env["hookSpecificOutput"]["updatedInput"]
    token = updated["args"]["run_token"]
    assert run_token.TOKEN_RE.match(token)
    assert run_token.admits(repo, RUN_ID, token, updated["args"]["script_path"])


def test_mint_failure_submits_without_a_token(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    def _boom(*_a, **_k):
        raise OSError("no token store")

    monkeypatch.setattr(run_token, "mint", _boom)
    env = _call(repo, {"targets": "red-set"})
    assert "run_token" not in env["hookSpecificOutput"]["updatedInput"]["args"]


def test_emit_params_carry_run_tag_where_and_sweep_appetite(repo):
    seen: dict = {}

    def emit(params: dict, root: Path) -> dict:
        seen.update(params)
        return {"ok": False, "findings": "stop here"}

    env = _call(repo, {"targets": "red-set", "limit": 5}, emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert seen["profile"] == "bug"
    assert seen["queue"] == ["state/bug-backlog"]
    assert seen["appetite"] == "sweep"
    assert seen["overrides"] == {
        "where": [[["status", "==", "open"], ["tags", "contains", run_tag(RUN_ID)]]],
        "limit": 5,
    }


def test_backlog_target_neither_parses_nor_mints_and_keeps_the_profile_where(repo):
    seen: dict = {}

    def emit(params: dict, root: Path) -> dict:
        seen.update(params)
        return {"ok": False}

    def boom(*a, **k):
        raise AssertionError("backlog target must not parse or mint")

    _call(repo, {"targets": "backlog", "appetite": "hunt"}, emit=emit, parse=boom, mint=boom)
    assert seen["appetite"] == "hunt"
    assert "overrides" not in seen


def test_backlog_dir_target_grinds_existing_rows_without_parsing(repo):
    (repo / "state" / "bug-backlog" / "row-a.md").write_text(
        "---\nid: row-a\nstatus: open\nseverity: P1\npm_ruling: fix it\n---\nbody\n", encoding="utf-8"
    )
    seen: dict = {}

    def emit(params: dict, root: Path) -> dict:
        seen.update(params)
        return {"ok": False}

    def boom(*a, **k):
        raise AssertionError("a backlog dir target must not parse or mint")

    env = _call(repo, {"targets": "state/bug-backlog"}, emit=emit, parse=boom, mint=boom, resolve=mod._default_resolve)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"  # the stub emit refused after resolve
    assert "no failing" not in _reason(env)
    assert seen["queue"] == ["state/bug-backlog"]
    assert "overrides" not in seen


def test_backlog_dir_target_allows_through_the_real_emit(repo):
    (repo / "state" / "bug-backlog" / "row-a.md").write_text(
        "---\nid: row-a\nstatus: open\nseverity: P1\npm_ruling: fix it\n---\nbody\n", encoding="utf-8"
    )
    env = _call(repo, {"targets": "./state/bug-backlog/"}, resolve=mod._default_resolve)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env


def test_decisions_record_the_fire_time_claim_baseline(repo, monkeypatch):
    from coordinator_core.session import scope

    claims = [
        "state/sizings/held.yaml",
        "state/bug-backlog/row-a.md",
        "state/queue-grind/bug/runs/x.json",
        "coordinator_core/other.py",
    ]
    seen_sid: list = []

    def fake_claims(sid, cwd=None):
        seen_sid.append(sid)
        return claims

    monkeypatch.setattr(scope, "own_path_claims_in_process", fake_claims)
    env = _call(repo, {"targets": "red-set"})
    script = Path(env["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    decisions = json.loads((script.parent / mod.DECISIONS_FILE).read_text(encoding="utf-8"))
    assert decisions[mod.BASELINE_DIRTY_KEY] == claims[:3]
    assert seen_sid == ["sess-blitz"]


@pytest.mark.parametrize("branch", ["main", "feature/foo"])
def test_denies_off_a_work_branch(repo, branch):
    _head(repo, branch)
    env = _call(repo, {"targets": "red-set"})
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "work branch" in _reason(env)


def test_denies_a_detached_head(repo):
    (repo / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
    env = _call(repo, {"targets": "red-set"})
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "detached" in _reason(env)


def test_denies_an_unparseable_target_and_mints_nothing(repo):
    def parse(spec):
        raise ValueError("no failing tests parsed from red-set")

    def mint(*a, **k):
        raise AssertionError("must not mint")

    env = _call(repo, {"targets": "red-set"}, parse=parse, mint=mint)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "no failing tests parsed" in _reason(env)


@pytest.mark.parametrize(
    "args",
    [{}, {"targets": ""}, {"targets": "x", "suite": "full"}, {"targets": "x", "limit": 0}, {"targets": "x", "limit": "3"}, 7],
)
def test_denies_a_malformed_args_shape(repo, args):
    env = _call(repo, args)
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Submit shape" in _reason(env)


def test_a_subagent_submission_is_denied(repo):
    payload = _payload(repo, {"targets": "x"})
    payload["agent_id"] = "agent-1"
    assert mod.evaluate(payload)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("name", ["coordinator:engine-ask", "bug-blitz-extra", "other:bug-blitzer"])
def test_other_workflow_names_are_a_no_op(repo, name):
    assert mod.evaluate(_payload(repo, {"targets": "x"}, name=name)) == mod.no_advisory()


@pytest.mark.parametrize("name", ["bug-blitz", "coordinator:bug-blitz"])
def test_stub_name_matches_bare_and_namespaced(repo, name):
    assert mod.is_submission(_payload(repo, {}, name=name))


def test_non_workflow_tool_is_a_no_op(repo):
    payload = _payload(repo, {"targets": "x"})
    payload["tool_name"] = "Bash"
    assert mod.evaluate(payload) == mod.no_advisory()
