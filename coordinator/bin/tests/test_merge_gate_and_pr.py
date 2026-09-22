"""test_merge_gate_and_pr — pytest tests for merge-gate-and-pr.py.

Spec backlink: docs/plans/2026-07-21-doe-skill-bash-to-claude-klabauter-python-port.md [DEAD-CITATION: plan file never committed to this repo]
  (M3 chunk MTM-2). Source: DoE-claude
  coordinator/skills/merging-to-main/SKILL.md §§ Step 1.5, Step 1.65, Step 4.

K-001 (state/kill-ledger.md): the `coverage-gate` subcommand this file used
to test was removed — its sole job was relaying `review-coverage-gate.py`'s
VERDICT line, and the verdict computation it wrapped is dead (WARN on 40 of
40 closes, zero effect). See merge-gate-and-pr.py's own docstring.

  pr-body:
    - renders the fleet PR template's sections in order, commit log after.
    - an absent optional section renders the template's guidance comment;
      an absent demo path drops the section.
    - the composer's heading/guidance constants match the template file.
  active-branch-guard:
    - --force always exits 0 without calling gh.
    - commit younger than 5 minutes halts (exit 1).
    - commit older than 5 minutes passes (exit 0).
    - gh failure (non-zero / empty output) halts (exit 1).
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "merge_gate_and_pr",
        _BIN_DIR / "merge-gate-and-pr.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# pr-body
# ---------------------------------------------------------------------------

_TEMPLATE_HEADINGS = [
    "Summary", "Release notes", "Verification", "Risk and rollback", "Demo path", "Links",
]


def _h2_headings(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def test_pr_body_all_sections_in_template_order(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_commit_log", lambda commit_range: "abc123 first commit")
    rc = _mod.main([
        "pr-body",
        "--ship-verdict", "**Ship verdict:** ship — all green",
        "--summary", "- changed a thing",
        "--release-notes", "### Fixed\n- thing",
        "--verification", "pytest: 12 passed",
        "--risk", "Low — docs only",
        "--demo-path", "run the CLI",
        "--links", "Closes #7",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("**Ship verdict:** ship — all green\n")
    assert _h2_headings(out) == _TEMPLATE_HEADINGS
    for text in ("- changed a thing", "### Fixed", "pytest: 12 passed", "Low — docs only",
                 "run the CLI", "Closes #7"):
        assert text in out
    assert "<!--" not in out
    assert out.index("## Links") < out.index("<summary>Commit log</summary>")
    assert "abc123 first commit" in out


def test_pr_body_absent_sections_render_guidance_and_drop_demo_path(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_commit_log", lambda commit_range: "abc123 first commit")
    rc = _mod.main([
        "pr-body",
        "--ship-verdict", "Ship — every hard gate reported.",
        "--release-notes", "Release v1.0.0.",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("**Ship verdict:** Ship — every hard gate reported.\n")
    assert _h2_headings(out) == [h for h in _TEMPLATE_HEADINGS if h != "Demo path"]
    assert "Release v1.0.0." in out
    assert "<!-- 1–3 bullets: what changed, and why it was needed. -->" in out
    assert "Closes #N" in out


def test_pr_body_sections_match_fleet_template():
    """The composer's headings and guidance comments are constants; this pins
    them to the fleet template file they transcribe. Skips only where no
    DoE-claude checkout resolves (the template's source repo)."""
    import pytest

    from coordinator_core.testing.doe_root import doe_root_and_present

    root, present = doe_root_and_present()
    template = Path(root) / "coordinator" / "templates" / "github-pull-request-template.md"
    if not present or not template.is_file():
        pytest.skip("fleet PR template not resolvable (no DoE-claude checkout)")
    text = template.read_text(encoding="utf-8")
    assert _h2_headings(text) == [h for h, _, _ in _mod._PR_BODY_SECTIONS]
    assert _mod._SHIP_VERDICT_PREFIX in text
    for heading, _, guidance in _mod._PR_BODY_SECTIONS:
        if guidance is not None:
            assert f"## {heading}\n\n{guidance}\n" in text, heading


# ---------------------------------------------------------------------------
# active-branch-guard
# ---------------------------------------------------------------------------

def test_active_branch_guard_force_skips_gh(monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("gh should not be called when --force is set")

    monkeypatch.setattr(_mod, "_gh_pr_view_json", _fail)
    rc = _mod.main(["active-branch-guard", "--pr", "123", "--force"])
    assert rc == 0


def test_active_branch_guard_recent_commit_halts(monkeypatch, capsys):
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    recent_iso = (now - datetime.timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _fake_gh(pr, jq_field):
        if jq_field == "commits[-1].committedDate":
            return 0, recent_iso
        return 0, "work/machine/2026-07-23"

    monkeypatch.setattr(_mod, "_gh_pr_view_json", _fake_gh)
    rc = _mod.main(["active-branch-guard", "--pr", "123"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "younger than 5 minutes" in err
    assert "work/machine/2026-07-23" in err


def test_active_branch_guard_settled_commit_passes(monkeypatch):
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    old_iso = (now - datetime.timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _fake_gh(pr, jq_field):
        if jq_field == "commits[-1].committedDate":
            return 0, old_iso
        return 0, "work/machine/2026-07-23"

    monkeypatch.setattr(_mod, "_gh_pr_view_json", _fake_gh)
    rc = _mod.main(["active-branch-guard", "--pr", "123"])
    assert rc == 0


def test_active_branch_guard_gh_failure_halts(monkeypatch, capsys):
    def _fake_gh(pr, jq_field):
        return 1, ""

    monkeypatch.setattr(_mod, "_gh_pr_view_json", _fake_gh)
    rc = _mod.main(["active-branch-guard", "--pr", "123"])
    assert rc == 1
    assert "could not read commit timestamps" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# coverage-gate
# ---------------------------------------------------------------------------
#
# The relay this subcommand restores per docs/plans/2026-08-27-the-merge-gate-
# is-pointed-back-at-the-coverage-engine.md § C1: it calls
# gate.validate_invocable's "review" dimension (gate_dimension_review.py) and
# refuses on FAIL. `_run_gate_validate_invocable` is the isolation seam so
# these tests never touch the real engine, git, or the review-trail corpus.


def test_coverage_gate_no_changed_files_refuses(monkeypatch, capsys):
    """C4/AC1: an empty changed-file set is indistinguishable from a `git
    diff` failure (`_changed_files` folds the rc into empty stdout), so it
    now exits 1 rather than the old fail-open 0."""
    monkeypatch.setattr(_mod, "_changed_files", lambda commit_range: [])

    def _fail(*args, **kwargs):
        raise AssertionError("gate.validate_invocable should not be called for an empty diff")

    monkeypatch.setattr(_mod, "_run_gate_validate_invocable", _fail)
    rc = _mod.main(["coverage-gate"])
    assert rc == 1
    assert "no changed files" in capsys.readouterr().err


def test_coverage_gate_covered_passes(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_changed_files", lambda commit_range: ["a.py"])
    monkeypatch.setattr(
        _mod,
        "_run_gate_validate_invocable",
        lambda changed_files, diff_base, repo_root: {
            "dimensions": [
                {
                    "dimension": "review",
                    "verdict": "PASS",
                    "detail": "covered: all 1 commit(s) touching changed_files are review-trail stamped",
                }
            ]
        },
    )
    rc = _mod.main(["coverage-gate"])
    assert rc == 0
    assert "covered" in capsys.readouterr().out


def test_coverage_gate_uncovered_refuses(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_changed_files", lambda commit_range: ["a.py"])
    monkeypatch.setattr(
        _mod,
        "_run_gate_validate_invocable",
        lambda changed_files, diff_base, repo_root: {
            "dimensions": [
                {
                    "dimension": "review",
                    "verdict": "FAIL",
                    "detail": "uncovered: 1/2 commit(s) touching changed_files have no review-trail stamp (e.g. deadbeef1234)",
                }
            ]
        },
    )
    rc = _mod.main(["coverage-gate"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "deadbeef1234" in err
    # C4 flipped this note when the ruleset went up and flipped it BACK when the
    # ruleset was withdrawn the same day. The note tracks what is actually
    # enforced on the remote, so it is only true while a ruleset exists -- do
    # not re-word it to the server-side claim without checking
    # `GET /repos/dbc-example-operator/claude-klabauter/rulesets` is non-empty first.
    assert "not enforced at the git-push layer" in err
    assert "review coverage is checked at merge, not at session close" in err


def test_coverage_gate_dimension_unavailable_refuses(monkeypatch, capsys):
    """C4/AC1: UNAVAILABLE is not the review-dimension whitelist's PASS, so
    it now exits 1 rather than the old fail-open 0."""
    monkeypatch.setattr(_mod, "_changed_files", lambda commit_range: ["a.py"])
    monkeypatch.setattr(
        _mod,
        "_run_gate_validate_invocable",
        lambda changed_files, diff_base, repo_root: {
            "dimensions": [
                {
                    "dimension": "review",
                    "verdict": "UNAVAILABLE",
                    "detail": "review-trail corpus unavailable: boom",
                }
            ]
        },
    )
    rc = _mod.main(["coverage-gate"])
    assert rc == 1
    assert "unavailable" in capsys.readouterr().err


def test_coverage_gate_dimension_absent_refuses(monkeypatch, capsys):
    """C4/AC1: an absent review dimension now exits 1, not the old 0."""
    monkeypatch.setattr(_mod, "_changed_files", lambda commit_range: ["a.py"])
    monkeypatch.setattr(
        _mod,
        "_run_gate_validate_invocable",
        lambda changed_files, diff_base, repo_root: {"dimensions": []},
    )
    rc = _mod.main(["coverage-gate"])
    assert rc == 1
    assert "review dimension absent" in capsys.readouterr().err


def test_coverage_gate_names_live_and_ended_sessions(monkeypatch, capsys):
    """C4/AC7: a session named in the FAIL detail's by-session grouping is
    marked live (naming the reviewer dispatch as the alternative) or ended
    (stating plainly that no remediation route exists yet), per
    `session_live`."""
    live_sid = "11112222-3333-4444-5555-666677778888"
    ended_sid = "99998888-7777-6666-5555-444433332222"
    detail = (
        "uncovered: 2/2 commit(s) touching changed_files carry neither a "
        "review-trail stamp nor a reviewer sidecar receipt (e.g. "
        "aaaaaaaaaaaa) -- 2 code commit(s), 0 bookkeeping-only commit(s) not "
        "in population\n"
        "uncovered by session:\n"
        f"  {live_sid}: aaaaaaaaaaaa\n"
        f"  {ended_sid}: bbbbbbbbbbbb\n"
    )
    monkeypatch.setattr(_mod, "_changed_files", lambda commit_range: ["a.py"])
    monkeypatch.setattr(
        _mod,
        "_run_gate_validate_invocable",
        lambda changed_files, diff_base, repo_root: {
            "dimensions": [{"dimension": "review", "verdict": "FAIL", "detail": detail}]
        },
    )

    def _fake_session_live(sid, cwd=None):
        return sid == live_sid

    import coordinator_core.session.liveness as liveness

    monkeypatch.setattr(liveness, "session_live", _fake_session_live)

    rc = _mod.main(["coverage-gate"])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"session {live_sid} is live" in err
    assert "dispatch a reviewer from it" in err
    assert f"session {ended_sid} has ended" in err
    assert "no remediation route exists yet" in err


# ---------------------------------------------------------------------------
# coverage-gate --post-status (C5)
# ---------------------------------------------------------------------------


class _FakePostResult:
    def __init__(self, posted, state, reason):
        self.posted = posted
        self.state = state
        self.reason = reason

    def to_json(self):
        return {"posted": self.posted, "state": self.state, "reason": self.reason}


def test_coverage_gate_post_status_success_exits_0(monkeypatch, capsys):
    from coordinator_core.ops import post_coverage_status

    def _fake_post(owner, repo, sha, commit_range, repo_root=None):
        assert (owner, repo, sha) == ("acme", "widgets", "deadbeef")
        return _FakePostResult(True, "success", "posted")

    monkeypatch.setattr(post_coverage_status, "post_coverage_status", _fake_post)
    rc = _mod.main([
        "coverage-gate", "--post-status", "--sha", "deadbeef",
        "--owner", "acme", "--repo", "widgets",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"posted": true' in out
    assert '"state": "success"' in out


def test_coverage_gate_post_status_posted_failure_exits_1(monkeypatch, capsys):
    from coordinator_core.ops import post_coverage_status

    def _fake_post(owner, repo, sha, commit_range, repo_root=None):
        return _FakePostResult(True, "failure", "coverage-gate: verdict 'FAIL'")

    monkeypatch.setattr(post_coverage_status, "post_coverage_status", _fake_post)
    rc = _mod.main([
        "coverage-gate", "--post-status", "--sha", "deadbeef",
        "--owner", "acme", "--repo", "widgets",
    ])
    assert rc == 1
    assert '"state": "failure"' in capsys.readouterr().out


def test_coverage_gate_post_status_unpostable_exits_1(monkeypatch, capsys):
    from coordinator_core.ops import post_coverage_status

    def _fake_post(owner, repo, sha, commit_range, repo_root=None):
        return _FakePostResult(
            False, None,
            "unpostable: no GitHub token resolved (GITHUB_TOKEN/GH_TOKEN env, gh hosts.yml)",
        )

    monkeypatch.setattr(post_coverage_status, "post_coverage_status", _fake_post)
    rc = _mod.main([
        "coverage-gate", "--post-status", "--sha", "deadbeef",
        "--owner", "acme", "--repo", "widgets",
    ])
    assert rc == 1
    out = capsys.readouterr().out
    assert "GITHUB_TOKEN" in out
    assert "GH_TOKEN" in out


def test_coverage_gate_post_status_without_sha_is_argparse_error(capsys):
    import pytest

    with pytest.raises(SystemExit) as exc_info:
        _mod.main(["coverage-gate", "--post-status"])
    assert exc_info.value.code == 2
    assert "--sha" in capsys.readouterr().err


def test_coverage_gate_post_status_owner_repo_resolved_from_remote(monkeypatch, capsys):
    from coordinator_core.ops import post_coverage_status
    from coordinator_core.ops.ceremony import push

    monkeypatch.setattr(push, "_resolve_github_owner_repo", lambda root: ("acme", "widgets"))

    seen = {}

    def _fake_post(owner, repo, sha, commit_range, repo_root=None):
        seen["owner_repo"] = (owner, repo)
        return _FakePostResult(True, "success", "posted")

    monkeypatch.setattr(post_coverage_status, "post_coverage_status", _fake_post)
    rc = _mod.main(["coverage-gate", "--post-status", "--sha", "deadbeef"])
    assert rc == 0
    assert seen["owner_repo"] == ("acme", "widgets")


def test_coverage_gate_post_status_no_resolvable_remote_exits_1_posts_nothing(monkeypatch, capsys):
    from coordinator_core.ops import post_coverage_status
    from coordinator_core.ops.ceremony import push

    monkeypatch.setattr(push, "_resolve_github_owner_repo", lambda root: None)

    def _fail(*args, **kwargs):
        raise AssertionError("post_coverage_status must not be called with no resolvable owner/repo")

    monkeypatch.setattr(post_coverage_status, "post_coverage_status", _fail)
    rc = _mod.main(["coverage-gate", "--post-status", "--sha", "deadbeef"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--owner" in err and "--repo" in err


def test_sessions_named_in_detail_skips_no_session_id_label(monkeypatch) -> None:
    from coordinator_core.ops import gate_dimension_review

    detail = (
        "uncovered by session:\n"
        "  11112222-3333-4444-5555-666677778888: aaaaaaaaaaaa\n"
        f"  {gate_dimension_review._NO_SESSION_ID_LABEL}: bbbbbbbbbbbb\n"
        "  (+3 more session(s))\n"
    )
    assert _mod._sessions_named_in_detail(detail) == [
        "11112222-3333-4444-5555-666677778888"
    ]
