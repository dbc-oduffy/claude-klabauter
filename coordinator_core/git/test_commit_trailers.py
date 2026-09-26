
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.git import commit_trailers
from coordinator_core.git.commit_trailers import compute_missing_trailer_args
from coordinator_core.session.claimed_plan import list_held_plan_claims
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SID = "12121212-1212-4121-8121-121212121212"


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags())


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _write_shape(repo: Path, sid: str, shape: dict) -> None:
    shape_dir = repo / ".git" / "coordinator-sessions" / sid
    shape_dir.mkdir(parents=True, exist_ok=True)
    (shape_dir / "session-shape.json").write_text(json.dumps(shape), encoding="utf-8")


def _write_plan(repo: Path, rel_path: str, fm_extra: str = 'deliverable_id: "dlv-plan-value"\n') -> None:
    plan_path = repo / rel_path
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        f"---\ntitle: example plan\n{fm_extra}---\n\n# Example plan\n",
        encoding="utf-8",
    )


def _write_plan_with_scope(
    repo: Path,
    rel_path: str,
    deliverable_id: str,
    scope_paths: list,
) -> None:
    plan_path = repo / rel_path
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    scope_block = "\n".join(f"  - {p}" for p in scope_paths)
    plan_path.write_text(
        f"---\ntitle: example plan\ndeliverable_id: \"{deliverable_id}\"\n"
        f"scope:\n{scope_block}\n---\n\n# Example plan\n",
        encoding="utf-8",
    )


def _write_plan_claim(repo: Path, sid: str, plan_stem: str, claimed_at: str) -> None:
    claim_dir = repo / ".git" / "coordinator-sessions" / "plan-claims" / plan_stem
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(sid, encoding="utf-8")
    (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")


def _msg_file(repo: Path, text: str = "chore: land trailers\n") -> Path:
    p = repo / "MSG"
    p.write_text(text, encoding="utf-8")
    return p


def test_pickup_tier_wins_claimed_plan_never_consulted(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(
        repo, _SID,
        {
            "pickup": {"deliverable_id": "dlv-from-pickup"},
            "plan": {"path": "docs/plans/should-not-be-read.md"},
        },
    )
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    assert "--trailer" in args
    joined = " ".join(args)
    assert "Deliverable-Id: dlv-from-pickup" in joined


def test_claimed_plan_tier_used_when_pickup_empty(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"plan": {"path": "docs/plans/example.md"}})
    _write_plan(repo, "docs/plans/example.md", 'deliverable_id: "dlv-plan-value"\n')
    _write_plan_claim(repo, _SID, "example", "2026-08-13T10:00:00Z")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-plan-value" in joined


def test_no_pickup_no_claimed_plan_no_trailer(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert "Session-Id:" in joined


@pytest.mark.parametrize(
    "fm_extra",
    [
        "",
        "deliverable_id: null\n",
        "deliverable_id: \n",
    ],
    ids=["absent", "null", "blank"],
)
def test_claimed_plan_missing_deliverable_id_no_crash(tmp_path, monkeypatch, fm_extra):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"plan": {"path": "docs/plans/example.md"}})
    _write_plan(repo, "docs/plans/example.md", fm_extra)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined


def test_claimed_plan_file_missing_no_crash(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"plan": {"path": "docs/plans/does-not-exist.md"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined


def test_ops_registry_survives_commit_trailers_import():
    script = (
        "import coordinator_core.git.commit_trailers\n"
        "import coordinator_core.ops as ops\n"
        "ops._eager_import_all()\n"
        "from coordinator_core.ipc import get_op_handler\n"
        "names = ['handoff.normalize', 'handoff.author_fork', "
        "'handoff.correct_body', 'handoff.scaffold_from_queue']\n"
        "missing = [n for n in names if get_op_handler(n) is None]\n"
        "assert not missing, missing\n"
        "print('OK')\n"
    )
    cp = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert cp.returncode == 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    assert "OK" in cp.stdout


# COMMITTED ARTIFACT's own deliverable_id, not the session's (last-write-

def _write_handoff(repo: Path, rel_path: str, deliverable_id: str) -> None:
    _write_plan(repo, rel_path, f'deliverable_id: "{deliverable_id}"\n')


def test_artifact_tier_resolves_each_of_three_commits_independently(tmp_path, monkeypatch):
    """The exact reported scenario: one session, three commits, three
    artifacts with three different deliverable_id values -> three
    DIFFERENT correct trailers, regardless of which pickup claim the
    session-shape record resolved last."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-qsent-03"}})

    _write_handoff(repo, "state/handoffs/roadmap-qsent-02.md", "dlv-qsent-02")
    _write_handoff(repo, "state/handoffs/roadmap-qsent-03.md", "dlv-qsent-03")
    _write_handoff(repo, "state/handoffs/qsent-05-rebase.md", "dlv-qsent-05")

    for rel_path, expected in (
        ("state/handoffs/roadmap-qsent-02.md", "dlv-qsent-02"),
        ("state/handoffs/roadmap-qsent-03.md", "dlv-qsent-03"),
        ("state/handoffs/qsent-05-rebase.md", "dlv-qsent-05"),
    ):
        msg = _msg_file(repo, f"archive handoff: {rel_path}\n")
        args = compute_missing_trailer_args(msg, repo, paths=[rel_path])
        joined = " ".join(args)
        assert f"Deliverable-Id: {expected}" in joined, (rel_path, joined)


def test_no_paths_arg_is_byte_identical_to_before_the_fix(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-from-pickup" in joined


def test_artifact_tier_falls_back_to_session_when_paths_carry_no_deliverable_id(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    _write_plan_with_scope(
        repo, "docs/plans/only-claim.md", "dlv-only-claim", ["some/unrelated/path.md"]
    )
    _write_plan_claim(repo, _SID, "only-claim", "2026-08-01T00:00:00Z")
    assert len(list_held_plan_claims(repo)) == 1, "precondition: exactly one plan claim held"

    (repo / "README.md").write_text("no frontmatter here\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["README.md", "does/not/exist.md"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-from-pickup" in joined


def test_two_claims_code_only_commit_omits_deliverable_id_keeps_session_id(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["docs/plans/claim-a-scope.md"]
    )
    _write_plan_with_scope(
        repo, "docs/plans/claim-b.md", "dlv-claim-b", ["docs/plans/claim-b-scope.md"]
    )
    _write_plan_claim(repo, _SID, "claim-a", "2026-08-01T00:00:00Z")
    _write_plan_claim(repo, _SID, "claim-b", "2026-08-02T00:00:00Z")
    assert len(list_held_plan_claims(repo)) == 2, "precondition: two plan claims held"

    (repo / "some_code.py").write_text("x = 1\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["some_code.py"])

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


def test_two_claims_scope_match_covered_by_exactly_one_plan_wins(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["src/only_in_a.py"]
    )
    _write_plan_with_scope(
        repo, "docs/plans/claim-b.md", "dlv-claim-b", ["src/only_in_b.py"]
    )
    _write_plan_claim(repo, _SID, "claim-a", "2026-08-01T00:00:00Z")
    _write_plan_claim(repo, _SID, "claim-b", "2026-08-02T00:00:00Z")

    (repo / "src").mkdir()
    (repo / "src" / "only_in_a.py").write_text("x = 1\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["src/only_in_a.py"])

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-claim-a" in joined


def test_two_claims_scope_match_covered_by_both_plans_omits(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["src/shared.py"]
    )
    _write_plan_with_scope(
        repo, "docs/plans/claim-b.md", "dlv-claim-b", ["src/shared.py"]
    )
    _write_plan_claim(repo, _SID, "claim-a", "2026-08-01T00:00:00Z")
    _write_plan_claim(repo, _SID, "claim-b", "2026-08-02T00:00:00Z")

    (repo / "src").mkdir()
    (repo / "src" / "shared.py").write_text("x = 1\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["src/shared.py"])

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


def test_scope_match_directory_entry_covers_file_beneath_it(tmp_path, monkeypatch):
    """Regression: a `scope:` entry naming a DIRECTORY covers a committed
    file directly beneath it, not just an exact-string match."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["coordinator_core/ops/fleet/tests"]
    )
    _write_plan_claim(repo, _SID, "claim-a", "2026-08-01T00:00:00Z")

    (repo / "coordinator_core" / "ops" / "fleet" / "tests").mkdir(parents=True)
    (repo / "coordinator_core" / "ops" / "fleet" / "tests" / "test_x.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["coordinator_core/ops/fleet/tests/test_x.py"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-claim-a" in joined


def test_scope_match_leading_slash_entry_covers_path_beneath_it(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["/coordinator_core/ops"]
    )
    _write_plan_claim(repo, _SID, "claim-a", "2026-08-01T00:00:00Z")

    (repo / "coordinator_core" / "ops" / "fleet" / "tests").mkdir(parents=True)
    (repo / "coordinator_core" / "ops" / "fleet" / "tests" / "test_x.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["coordinator_core/ops/fleet/tests/test_x.py"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-claim-a" in joined


def test_scope_match_directory_entry_covers_nested_deeper_file(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["coordinator_core/ops"]
    )
    _write_plan_claim(repo, _SID, "claim-a", "2026-08-01T00:00:00Z")

    (repo / "coordinator_core" / "ops" / "fleet" / "tests").mkdir(parents=True)
    (repo / "coordinator_core" / "ops" / "fleet" / "tests" / "test_x.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["coordinator_core/ops/fleet/tests/test_x.py"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-claim-a" in joined


def test_scope_match_sibling_prefix_does_not_match(tmp_path):
    repo = _init_repo(tmp_path)
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["coordinator_core/ops/fleet/tests"]
    )
    claims = [("docs/plans/claim-a.md", "2026-08-01T00:00:00Z")]

    result = commit_trailers.resolve_deliverable_id_from_scope_match(
        repo, ["coordinator_core/ops/fleet/tests_helper.py"], claims
    )

    assert result == ""


def test_scope_match_exact_file_entry_still_works(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["src/only_in_a.py"]
    )
    _write_plan_claim(repo, _SID, "claim-a", "2026-08-01T00:00:00Z")

    (repo / "src").mkdir()
    (repo / "src" / "only_in_a.py").write_text("x = 1\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["src/only_in_a.py"])

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-claim-a" in joined


def test_scope_match_pathspec_straddling_covered_and_uncovered_abstains(tmp_path):
    repo = _init_repo(tmp_path)
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["coordinator_core/ops/fleet/tests"]
    )
    claims = [("docs/plans/claim-a.md", "2026-08-01T00:00:00Z")]

    result = commit_trailers.resolve_deliverable_id_from_scope_match(
        repo,
        ["coordinator_core/ops/fleet/tests/test_x.py", "unrelated.py"],
        claims,
    )

    assert result == ""


def test_scope_match_two_covering_directory_plans_abstain(tmp_path, monkeypatch):
    """Two claimed plans whose DIRECTORY scopes both cover the committed
    pathspec still abstain -- the tier never picks among multiple covering
    plans, directory containment included."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_plan_with_scope(
        repo, "docs/plans/claim-a.md", "dlv-claim-a", ["coordinator_core/ops"]
    )
    _write_plan_with_scope(
        repo, "docs/plans/claim-b.md", "dlv-claim-b", ["coordinator_core"]
    )
    _write_plan_claim(repo, _SID, "claim-a", "2026-08-01T00:00:00Z")
    _write_plan_claim(repo, _SID, "claim-b", "2026-08-02T00:00:00Z")

    (repo / "coordinator_core" / "ops" / "fleet").mkdir(parents=True)
    (repo / "coordinator_core" / "ops" / "fleet" / "test_x.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["coordinator_core/ops/fleet/test_x.py"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined


def test_artifact_tier_omits_on_genuinely_divergent_multi_artifact_commit(
    tmp_path, monkeypatch
):
    """Two paths in the SAME commit naming two DIFFERENT non-empty
    deliverable_id values is not guessed at -- per producer-contract § 3,
    tier 0 OMITS rather than raises: no Deliverable-Id trailer is stamped,
    and the session tiers below run exactly as if tier 0 found nothing."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_handoff(repo, "state/handoffs/a.md", "dlv-a")
    _write_handoff(repo, "state/handoffs/b.md", "dlv-b")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["state/handoffs/a.md", "state/handoffs/b.md"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


_SENTINEL_SID = "99887766-1122-4334-8ee5-aabbccddeeff"


def _write_sentinel(repo: Path, sid: str) -> None:
    sentinel_dir = repo / ".git" / "coordinator-sessions"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    (sentinel_dir / ".current-session-id").write_text(sid, encoding="utf-8")


def _write_session_meta(repo: Path, sid: str, meta: dict) -> None:
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_sentinel_stale_session_ignored_no_trailers(tmp_path, monkeypatch):
    import datetime as _dt

    repo = _init_repo(tmp_path)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    _write_sentinel(repo, _SENTINEL_SID)
    stale_dt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)
    stale_iso = stale_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_session_meta(
        repo, _SENTINEL_SID, {"pid": "999999", "last_activity": stale_iso}
    )
    _write_shape(repo, _SENTINEL_SID, {"pickup": {"deliverable_id": "dlv-stale"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    assert args == []


def test_sentinel_live_session_still_ignored_no_trailers(tmp_path, monkeypatch):
    import datetime as _dt

    repo = _init_repo(tmp_path)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    _write_sentinel(repo, _SENTINEL_SID)
    fresh_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_session_meta(
        repo, _SENTINEL_SID, {"pid": "999999", "last_activity": fresh_iso}
    )
    _write_shape(repo, _SENTINEL_SID, {"pickup": {"deliverable_id": "dlv-live"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    assert args == []


def test_sentinel_no_session_dir_no_trailers(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    _write_sentinel(repo, _SENTINEL_SID)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    assert args == []


def _git_trailer_value(repo: Path, msg_file: Path, key: str) -> str:
    cp = subprocess.run(
        ["git", "interpret-trailers", "--parse", str(msg_file)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    for line in cp.stdout.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return ""


def test_body_mention_does_not_suppress_emission(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-real"}})
    msg = _msg_file(
        repo,
        "chore: land trailers\n\n"
        "Deliverable-Id: dlv-hand-written-in-body\n\n"
        "Co-Authored-By: Someone <someone@example.com>\n",
    )

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-real" in joined

    cp = subprocess.run(
        ["git", "interpret-trailers", *args, str(msg)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    out_msg = tmp_path / "OUT_MSG"
    out_msg.write_text(cp.stdout, encoding="utf-8")
    assert _git_trailer_value(repo, out_msg, "Deliverable-Id") == "dlv-real"


def test_trailer_block_id_still_deduplicates(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-real"}})
    msg = _msg_file(
        repo,
        "chore: land trailers\n\n"
        "Deliverable-Id: dlv-real\n"
        "Co-Authored-By: Someone <someone@example.com>\n",
    )

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined


def test_env_var_tiers_win_regardless_of_sentinel(tmp_path, monkeypatch):
    """$CLAUDE_SESSION_ID / $CLAUDE_CODE_SESSION_ID win outright regardless
    of whether a sentinel file or session directory exists at all."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert f"Session-Id: {_SID}" in joined


# `$CLAUDE_SESSION_ID`/`$CLAUDE_CODE_SESSION_ID` env read -- the two can

_OTHER_LIVE_SID = "e77424be-b452-43bd-a995-e12d60168cb6"


def test_session_id_override_wins_over_ambient_env(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _OTHER_LIVE_SID)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, session_id_override=_SID)

    joined = " ".join(args)
    assert f"Session-Id: {_SID}" in joined
    assert _OTHER_LIVE_SID not in joined


def test_session_id_override_falls_back_to_env_when_not_uuid_shaped(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, session_id_override="scoped-git-commit-not-a-real-session"
    )

    joined = " ".join(args)
    assert f"Session-Id: {_SID}" in joined


def test_session_id_override_none_reproduces_prior_behaviour(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert f"Session-Id: {_SID}" in joined


def test_multi_deliverable_pathspec_resolves_without_deliverable_trailer(
    tmp_path, monkeypatch
):
    """AC1 + AC2 in one assertion pair: two artifacts naming two DIFFERENT
    non-empty deliverable_id values in the same pathspec must RETURN (not
    raise `DivergentDeliverableIdError`), and the returned arg list must
    carry NO `Deliverable-Id:` trailer at all -- an unresolvable multi-
    deliverable commit omits the trailer rather than guessing or refusing."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_handoff(repo, "state/handoffs/a.md", "dlv-a")
    _write_handoff(repo, "state/handoffs/b.md", "dlv-b")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["state/handoffs/a.md", "state/handoffs/b.md"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


def test_single_deliverable_pathspec_unchanged_behaviour(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_handoff(repo, "state/handoffs/only.md", "dlv-only")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["state/handoffs/only.md"])

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-only" in joined


def test_eleven_distinct_deliverables_at_scale_resolves_without_trailer(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)

    rel_paths = []
    for i in range(11):
        rel_path = f"state/handoffs/mise-{i:02d}.md"
        _write_handoff(repo, rel_path, f"dlv-mise-{i:02d}")
        rel_paths.append(rel_path)
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=rel_paths)

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


def test_emergency_pathspec_with_no_deliverable_id_anywhere_commits_untrailered(
    tmp_path, monkeypatch
):
    """AC12, the EMERGENCY case: a pathspec whose files carry no
    deliverable_id at all must commit untrailered and must never be
    refused. This already passes today (verified: resolves to ``""``) --
    pinned as a regression here because C2 edits the very function that
    produces that ``""``, and a careless early return in the divergence fix
    would turn the emergency path into a refusal instead of a silent
    omission. A test that passes on the first run is the point, not a
    defect in the test."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    (repo / "src").mkdir()
    (repo / "src" / "emergency_fix.py").write_text("x = 1\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["src/emergency_fix.py"])

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {_SID}" in joined


def _msg(tmp_path, text):
    f = tmp_path / "COMMIT_EDITMSG"
    f.write_text(text, encoding="utf-8")
    return f


def test_read_trailer_value_returns_the_trailer_block_value(tmp_path):
    f = _msg(tmp_path, "C1: subject\n\nbody\n\nDeliverable-Id: dlv-a-b-123456\n")
    assert commit_trailers.read_trailer_value(f, "Deliverable-Id:") == "dlv-a-b-123456"


def test_read_trailer_value_ignores_a_body_line_outside_the_trailer_block(tmp_path):
    f = _msg(
        tmp_path,
        "C1: subject\n\nDeliverable-Id: dlv-in-the-body-999999\n\n"
        "Session-Id: 11111111-1111-1111-1111-111111111111\n",
    )
    assert commit_trailers.read_trailer_value(f, "Deliverable-Id:") is None


def test_read_trailer_value_treats_a_blank_value_as_absent(tmp_path):
    f = _msg(tmp_path, "C1: subject\n\nbody\n\nDeliverable-Id:   \n")
    assert commit_trailers.read_trailer_value(f, "Deliverable-Id:") is None


def test_read_trailer_value_degrades_to_none_on_an_unreadable_file(tmp_path):
    assert (
        commit_trailers.read_trailer_value(tmp_path / "nope", "Deliverable-Id:") is None
    )


def test_commit_scoped_refuses_a_branch_name_in_the_deliverable_id_trailer(tmp_path):
    from coordinator_core.ops.ceremony import git_native

    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    f = _msg(
        tmp_path,
        "C1: subject\n\nbody\n\nDeliverable-Id: work/machine-a/2026-08-16to18\n",
    )
    result = git_native.commit_scoped(["f.txt"], f, tmp_path)
    assert result.returncode == -1
    assert "work/machine-a/2026-08-16to18" in result.stderr
    assert "dlv-" in result.stderr and "pln-" in result.stderr


def test_commit_scoped_admits_a_pln_prefixed_authored_trailer(tmp_path):
    from coordinator_core.ops.ceremony import git_native

    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    f = _msg(tmp_path, "C1: subject\n\nbody\n\nDeliverable-Id: pln-a-b-123456\n")
    result = git_native.commit_scoped(["f.txt"], f, tmp_path)
    assert "does not match the 'dlv-' or 'pln-' shape convention" not in result.stderr


def test_override_beats_a_pathspec_plan_declaring_a_different_id(tmp_path, monkeypatch):
    """(a) The override wins even when the pathspec includes a
    docs/plans/other.md whose own frontmatter declares a DIFFERENT dlv- id --
    the exact Defect C shape (tier 0 would otherwise win)."""
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_plan(repo, "docs/plans/other.md", 'deliverable_id: "dlv-other-999999"\n')
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg,
        repo,
        paths=["docs/plans/other.md"],
        deliverable_id_override="dlv-exec-111111",
    )

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-exec-111111" in joined
    assert "dlv-other-999999" not in joined


def test_override_applies_with_non_uuid_session(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "not-a-uuid-at-all")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, deliverable_id_override="dlv-exec-222222"
    )

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-exec-222222" in joined
    assert "Session-Id:" not in joined


def test_malformed_override_falls_through_to_the_ladder(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, deliverable_id_override="work/machine-a/branch-name"
    )

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-from-pickup" in joined
    assert "work/machine-a/branch-name" not in joined


def test_pre_existing_block_trailer_yields_no_override_arg(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    msg = _msg_file(
        repo,
        "C1: subject\n\nbody\n\nDeliverable-Id: dlv-already-there\n",
    )

    args = compute_missing_trailer_args(
        msg, repo, deliverable_id_override="dlv-exec-333333"
    )

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined


def test_blank_line_demoted_body_line_plus_override_yields_override(
    tmp_path, monkeypatch
):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    msg = _msg_file(
        repo,
        "C1: subject\n\nDeliverable-Id: dlv-in-the-body\n\nCo-Authored-By: x <x@x>\n",
    )

    args = compute_missing_trailer_args(
        msg, repo, deliverable_id_override="dlv-exec-444444"
    )

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-exec-444444" in joined


def test_override_none_reproduces_prior_behaviour(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    _write_shape(repo, _SID, {"pickup": {"deliverable_id": "dlv-from-pickup"}})
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-from-pickup" in joined


def test_apply_missing_trailers_forwards_deliverable_id_override(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "not-a-uuid-at-all")

    result = commit_trailers.apply_missing_trailers(
        "chore: land trailers\n",
        repo,
        deliverable_id_override="dlv-exec-555555",
    )

    assert "Deliverable-Id: dlv-exec-555555" in result


# ---------------------------------------------------------------------------
# B2 (F2): pickup-tier ambiguity gate -- multiple held handoff pickups omit.
# ---------------------------------------------------------------------------


def _write_handoff_claim(repo: Path, handoff_relpath: str, sid: str) -> None:
    """Write a LIVE handoff-claims claim-dir for `handoff_relpath`, held by
    `sid` -- the shape `claim_state.handoff_claim_dir` resolves and
    `liveness.cs_claim_holder_live` reads. `sid` must be a real, live
    session for `cs_claim_holder_live` to answer True; tests use the
    running pytest process's own session registry entry (see `_SID`
    fixture setup in each test below)."""
    claim_dir = (
        repo
        / ".git"
        / "coordinator-sessions"
        / "handoff-claims"
        / Path(handoff_relpath).name
    )
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(sid, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-09-26T00:00:00Z", encoding="utf-8")


def _write_pickup_history(
    repo: Path, sid: str, entries: list, *, flat_deliverable_id: str = ""
) -> None:
    """Write `session-shape.json` carrying `pickup_history` (each entry a
    `{"handoff": ..., "deliverable_id": ...}` dict, `record_pickup`'s own
    shape) alongside the flat `pickup.deliverable_id` key the pre-B2 tier
    reads -- `record_pickup` sets the flat key to whichever pickup happened
    LAST, so tests pass it explicitly to reproduce that "last pickup wins"
    shape."""
    shape = {"pickup_history": entries}
    if flat_deliverable_id:
        shape["pickup"] = {"deliverable_id": flat_deliverable_id}
    _write_shape(repo, sid, shape)


@pytest.fixture
def _live_session(monkeypatch):
    """A session id `cs_claim_holder_live` will answer True for, faked via
    the same registry-liveness seam other `claim_state`/`liveness` tests in
    this fleet fake through -- `session.liveness.claim_holder_live`
    resolves via the session registry, so a monkeypatch at that seam avoids
    standing up a real registry entry per test."""
    sid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    monkeypatch.setattr(
        "coordinator_core.liveness.cs_claim_holder_live", lambda claim_path: True
    )
    return sid


def test_two_held_pickups_code_only_commit_omits_deliverable_id(
    tmp_path, monkeypatch, _live_session
):
    repo = _init_repo(tmp_path)
    sid = _live_session
    monkeypatch.setenv("CLAUDE_SESSION_ID", sid)
    _write_pickup_history(
        repo,
        sid,
        [
            {"handoff": "state/handoffs/a.md", "deliverable_id": "dlv-a"},
            {"handoff": "state/handoffs/b.md", "deliverable_id": "dlv-b"},
        ],
        flat_deliverable_id="dlv-b",
    )
    _write_handoff_claim(repo, "state/handoffs/a.md", sid)
    _write_handoff_claim(repo, "state/handoffs/b.md", sid)

    (repo / "some_code.py").write_text("x = 1\n", encoding="utf-8")
    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo, paths=["some_code.py"])

    joined = " ".join(args)
    assert "Deliverable-Id:" not in joined
    assert f"Session-Id: {sid}" in joined


def test_two_held_pickups_plus_one_plan_claim_uses_claimed_plan_id(
    tmp_path, monkeypatch, _live_session
):
    repo = _init_repo(tmp_path)
    sid = _live_session
    monkeypatch.setenv("CLAUDE_SESSION_ID", sid)
    _write_pickup_history(
        repo,
        sid,
        [
            {"handoff": "state/handoffs/a.md", "deliverable_id": "dlv-a"},
            {"handoff": "state/handoffs/b.md", "deliverable_id": "dlv-b"},
        ],
        flat_deliverable_id="dlv-b",
    )
    _write_handoff_claim(repo, "state/handoffs/a.md", sid)
    _write_handoff_claim(repo, "state/handoffs/b.md", sid)
    _write_plan(repo, "docs/plans/example.md", 'deliverable_id: "dlv-plan-value"\n')
    _write_plan_claim(repo, sid, "example", "2026-08-13T10:00:00Z")

    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-plan-value" in joined


def test_one_pickup_released_one_held_uses_the_held_ones_id(
    tmp_path, monkeypatch, _live_session
):
    repo = _init_repo(tmp_path)
    sid = _live_session
    monkeypatch.setenv("CLAUDE_SESSION_ID", sid)
    _write_pickup_history(
        repo,
        sid,
        [
            {"handoff": "state/handoffs/a.md", "deliverable_id": "dlv-a"},
            {"handoff": "state/handoffs/b.md", "deliverable_id": "dlv-b"},
        ],
        flat_deliverable_id="dlv-b",
    )
    # Only b's claim dir exists -- a's pickup was released (no claim dir at
    # all is the same "not held" case a dead-holder claim would degrade to).
    _write_handoff_claim(repo, "state/handoffs/b.md", sid)

    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-b" in joined


def test_tier0_artifact_wins_over_multiple_held_pickups(
    tmp_path, monkeypatch, _live_session
):
    repo = _init_repo(tmp_path)
    sid = _live_session
    monkeypatch.setenv("CLAUDE_SESSION_ID", sid)
    _write_pickup_history(
        repo,
        sid,
        [
            {"handoff": "state/handoffs/a.md", "deliverable_id": "dlv-a"},
            {"handoff": "state/handoffs/b.md", "deliverable_id": "dlv-b"},
        ],
        flat_deliverable_id="dlv-b",
    )
    _write_handoff_claim(repo, "state/handoffs/a.md", sid)
    _write_handoff_claim(repo, "state/handoffs/b.md", sid)
    _write_plan(repo, "docs/plans/carries-its-own-id.md", 'deliverable_id: "dlv-artifact"\n')

    msg = _msg_file(repo)

    args = compute_missing_trailer_args(
        msg, repo, paths=["docs/plans/carries-its-own-id.md"]
    )

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-artifact" in joined


def test_single_held_pickup_unchanged(tmp_path, monkeypatch, _live_session):
    repo = _init_repo(tmp_path)
    sid = _live_session
    monkeypatch.setenv("CLAUDE_SESSION_ID", sid)
    _write_pickup_history(
        repo,
        sid,
        [{"handoff": "state/handoffs/a.md", "deliverable_id": "dlv-a"}],
        flat_deliverable_id="dlv-a",
    )
    _write_handoff_claim(repo, "state/handoffs/a.md", sid)

    msg = _msg_file(repo)

    args = compute_missing_trailer_args(msg, repo)

    joined = " ".join(args)
    assert "Deliverable-Id: dlv-a" in joined
