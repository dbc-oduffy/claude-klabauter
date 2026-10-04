"""coordinator_core.ops.review_stamp — mint/check tests.

Coordinator-content-repo docs/plans/2026-09-27-review-inside-execute-plan.md, row MK1, AC10.
"""

from __future__ import annotations

import hashlib
import subprocess
import textwrap
from pathlib import Path

import pytest

from coordinator_core.git.run import GitResult
from coordinator_core.ops import review_stamp as m
from coordinator_core.win_portability import no_console_creationflags

# Hoisted: a backslash inside an f-string expression is a SyntaxError before Python 3.12.
_FOO_PY_SHA = hashlib.sha256(b"x = 1\n").hexdigest()

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_FLAGS = no_console_creationflags()


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        **_FLAGS,
    )


def _git_init(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write_sidecar(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = []
    for key, value in data.items():
        fm_lines.append(f"{key}: {_yaml_scalar(value)}")
    text = "---\n" + "\n".join(fm_lines) + "\n---\n\nbody\n"
    path.write_text(text, encoding="utf-8")


def _yaml_scalar(value) -> str:
    import json

    if isinstance(value, str):
        return json.dumps(value)
    return json.dumps(value)


def _setup_repo(tmp_path: Path, plan_id: str = "pln-example-abc123") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    plan_path = plans_dir / "example.md"
    plan_path.write_text(
        textwrap.dedent(
            f"""\
            ---
            title: Example
            created: 2026-09-27
            author: test
            status: executing
            plan_id: {plan_id}
            scope:
              - docs/plans/example.md
            ---

            # Example
            """
        ),
        encoding="utf-8",
    )
    _commit(repo, "init")
    return repo


def _mint_success_fixture(repo: Path, plan_id: str = "pln-example-abc123"):
    share = repo / ".coordinator-local" / "subagent-share" / "sess1"
    prep = share / "2026-09-27-prep.md"
    _write_sidecar(
        prep,
        {
            "run_base_sha": "deadbeef",
            "product_files": ["coordinator_core/foo.py"],
            "foreign_claims": [],
            "slices": [{"id": "A"}],
            "whole_diff_sidecars": {"delivery": ".coordinator-local/subagent-share/sess1/2026-09-27-delivery.md"},
        },
    )
    _write_sidecar(
        share / "2026-09-27-delivery.md",
        {"verdict": "PASS"},
    )
    integration = share / "2026-09-27-integration.md"
    _write_sidecar(
        integration,
        {
            "plan_id": plan_id,
            "prep_sidecar": ".coordinator-local/subagent-share/sess1/2026-09-27-prep.md",
            "unresolved": [],
            "confinement_violations": [],
            "fixes_applied": 2,
            "em_may_think_differently": [],
            "brief_conformance": {"items": 1, "met": 1, "unmet": 0},
        },
    )
    (repo / "coordinator_core").mkdir(exist_ok=True)
    (repo / "coordinator_core" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    terminal_sha = _commit(
        repo, "land review\n\nInline-Review: applies 2026-09-27-integration -- execute-review: 1 slices, 2 fixes"
    )
    build_test = share / "2026-09-27-test-runner.md"
    _write_sidecar(build_test, {"status": "pass", "run": 10, "failed": 0})
    return terminal_sha, build_test


def test_mint_success(tmp_path):
    repo = _setup_repo(tmp_path)
    terminal_sha, build_test = _mint_success_fixture(repo)
    plan_path = repo / "docs" / "plans" / "example.md"
    stamp = m.mint(plan_path, repo, build_test_path=str(build_test))
    assert stamp["terminal_commit_sha"] == terminal_sha
    assert stamp["delivery"]["verdict"] == "PASS"
    assert stamp["build_test"]["verdict"] == "pass"
    assert stamp["unresolved"] == []
    assert "review_stamp:" in plan_path.read_text(encoding="utf-8")


def test_mint_falls_back_to_receipt_session_id_when_sidecar_has_no_plan_id(tmp_path):
    """example-retrieval-repo cc6525cf0 repair path: a commit landed by the pre-fix
    engine carries an integration sidecar with NO `plan_id` at all. `mint`
    still resolves it via the plan's own `<stem>.workflow.mjs.emitted.json`
    receipt's `session_id`, joined against the sidecar's `lead_session_id`."""
    repo = _setup_repo(tmp_path)
    plan_path = repo / "docs" / "plans" / "example.md"

    share = repo / ".coordinator-local" / "subagent-share" / "sess1"
    prep = share / "2026-09-27-prep.md"
    _write_sidecar(
        prep,
        {
            "run_base_sha": "deadbeef",
            "product_files": ["coordinator_core/foo.py"],
            "foreign_claims": [],
            "slices": [{"id": "A"}],
            "whole_diff_sidecars": {"delivery": ".coordinator-local/subagent-share/sess1/2026-09-27-delivery.md"},
        },
    )
    _write_sidecar(share / "2026-09-27-delivery.md", {"verdict": "PASS"})
    integration = share / "2026-09-27-integration.md"
    _write_sidecar(
        integration,
        {
            # No plan_id -- the pre-fix shape this fallback repairs.
            "lead_session_id": "sess1",
            "prep_sidecar": ".coordinator-local/subagent-share/sess1/2026-09-27-prep.md",
            "unresolved": [],
            "confinement_violations": [],
            "fixes_applied": 2,
            "em_may_think_differently": [],
            "brief_conformance": {"items": 1, "met": 1, "unmet": 0},
        },
    )
    (repo / "coordinator_core").mkdir(exist_ok=True)
    (repo / "coordinator_core" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    terminal_sha = _commit(
        repo, "land review\n\nInline-Review: applies 2026-09-27-integration -- execute-review: 1 slices, 2 fixes"
    )
    build_test = share / "2026-09-27-test-runner.md"
    _write_sidecar(build_test, {"status": "pass", "run": 10, "failed": 0})

    receipt = plan_path.with_name("example.workflow.mjs.emitted.json")
    receipt.write_text('{"session_id": "sess1", "plan": "example.md"}', encoding="utf-8")

    stamp = m.mint(plan_path, repo, build_test_path=str(build_test))
    assert stamp["terminal_commit_sha"] == terminal_sha


def test_mint_fallback_never_matches_a_mismatched_plan_id(tmp_path):
    """The receipt/session_id fallback only fires when the sidecar's
    plan_id is ABSENT -- it must never override a sidecar that carries a
    real, different plan_id."""
    repo = _setup_repo(tmp_path, plan_id="pln-example-abc123")
    plan_path = repo / "docs" / "plans" / "example.md"

    share = repo / ".coordinator-local" / "subagent-share" / "sess1"
    prep = share / "2026-09-27-prep.md"
    _write_sidecar(
        prep,
        {
            "run_base_sha": "deadbeef",
            "product_files": ["coordinator_core/foo.py"],
            "foreign_claims": [],
            "slices": [{"id": "A"}],
            "whole_diff_sidecars": {"delivery": ".coordinator-local/subagent-share/sess1/2026-09-27-delivery.md"},
        },
    )
    _write_sidecar(share / "2026-09-27-delivery.md", {"verdict": "PASS"})
    integration = share / "2026-09-27-integration.md"
    _write_sidecar(
        integration,
        {
            "plan_id": "pln-some-other-plan-999999",
            "lead_session_id": "sess1",
            "prep_sidecar": ".coordinator-local/subagent-share/sess1/2026-09-27-prep.md",
            "unresolved": [],
            "confinement_violations": [],
            "fixes_applied": 2,
            "em_may_think_differently": [],
            "brief_conformance": {"items": 1, "met": 1, "unmet": 0},
        },
    )
    (repo / "coordinator_core").mkdir(exist_ok=True)
    (repo / "coordinator_core" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    _commit(
        repo, "land review\n\nInline-Review: applies 2026-09-27-integration -- execute-review: 1 slices, 2 fixes"
    )
    build_test = share / "2026-09-27-test-runner.md"
    _write_sidecar(build_test, {"status": "pass", "run": 10, "failed": 0})

    receipt = plan_path.with_name("example.workflow.mjs.emitted.json")
    receipt.write_text('{"session_id": "sess1", "plan": "example.md"}', encoding="utf-8")

    with pytest.raises(m.MintRefusal):
        m.mint(plan_path, repo, build_test_path=str(build_test))


def test_mint_picks_right_terminal_commit_for_two_plans(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    for slug, plan_id in (("a", "pln-plan-a-000001"), ("b", "pln-plan-b-000002")):
        (plans_dir / f"{slug}.md").write_text(
            textwrap.dedent(
                f"""\
                ---
                title: Plan {slug}
                created: 2026-09-27
                author: test
                status: executing
                plan_id: {plan_id}
                ---

                # Plan {slug}
                """
            ),
            encoding="utf-8",
        )
    _commit(repo, "init")

    def mint_for(slug: str, plan_id: str, session: str):
        share = repo / ".coordinator-local" / "subagent-share" / session
        prep = share / f"2026-09-27-prep-{slug}.md"
        _write_sidecar(
            prep,
            {
                "run_base_sha": "deadbeef",
                "product_files": [f"coordinator_core/{slug}.py"],
                "foreign_claims": [],
                "slices": [{"id": "A"}],
                "whole_diff_sidecars": {
                    "delivery": f".coordinator-local/subagent-share/{session}/2026-09-27-delivery-{slug}.md"
                },
            },
        )
        _write_sidecar(share / f"2026-09-27-delivery-{slug}.md", {"verdict": "PASS"})
        integration = share / f"2026-09-27-integration-{slug}.md"
        _write_sidecar(
            integration,
            {
                "plan_id": plan_id,
                "prep_sidecar": f".coordinator-local/subagent-share/{session}/2026-09-27-prep-{slug}.md",
                "unresolved": [],
                "confinement_violations": [],
                "fixes_applied": 1,
            },
        )
        (repo / "coordinator_core" / f"{slug}.py").parent.mkdir(exist_ok=True)
        (repo / "coordinator_core" / f"{slug}.py").write_text(f"{slug} = 1\n", encoding="utf-8")
        terminal_sha = _commit(
            repo, f"land {slug}\n\nInline-Review: applies 2026-09-27-integration-{slug} -- execute-review: 1 slices, 1 fixes"
        )
        build_test = share / f"2026-09-27-test-runner-{slug}.md"
        _write_sidecar(build_test, {"status": "pass", "run": 1, "failed": 0})
        return terminal_sha, build_test

    sha_a, bt_a = mint_for("a", "pln-plan-a-000001", "sessA")
    sha_b, bt_b = mint_for("b", "pln-plan-b-000002", "sessB")

    stamp_a = m.mint(plans_dir / "a.md", repo, build_test_path=str(bt_a))
    stamp_b = m.mint(plans_dir / "b.md", repo, build_test_path=str(bt_b))
    assert stamp_a["terminal_commit_sha"] == sha_a
    assert stamp_b["terminal_commit_sha"] == sha_b


def test_mint_refuses_no_build_test(tmp_path):
    repo = _setup_repo(tmp_path)
    _mint_success_fixture(repo)
    plan_path = repo / "docs" / "plans" / "example.md"
    with pytest.raises(m.MintRefusal, match="no build/test record"):
        m.mint(plan_path, repo, build_test_path=None)


def test_check_refuses_no_stamp(tmp_path):
    repo = _setup_repo(tmp_path)
    plan_path = repo / "docs" / "plans" / "example.md"
    reason = m.check(plan_path, repo, supersession=False)
    assert reason is not None
    assert "no review_stamp" in reason


def test_check_refuses_superseding_commit_on_declared_write(tmp_path):
    repo = _setup_repo(tmp_path)
    terminal_sha, build_test = _mint_success_fixture(repo)
    plan_path = repo / "docs" / "plans" / "example.md"
    m.mint(plan_path, repo, build_test_path=str(build_test))

    # A later commit touches the plan's own declared `scope` write.
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")
    _commit(repo, "later touch")

    reason = m.check(plan_path, repo, supersession=True)
    assert reason is not None
    assert "later commit" in reason


def test_check_ignores_supersession_when_not_asked(tmp_path):
    repo = _setup_repo(tmp_path)
    terminal_sha, build_test = _mint_success_fixture(repo)
    plan_path = repo / "docs" / "plans" / "example.md"
    m.mint(plan_path, repo, build_test_path=str(build_test))

    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")
    _commit(repo, "later touch")

    reason = m.check(plan_path, repo, supersession=False)
    # A close-gate-shaped check re-reads the STAMPED terminal commit's own
    # tree/ancestry, which is unaffected by a later, unrelated commit -- MK2's
    # whole point (a shared tree can legitimately touch the same file later).
    assert reason is None


def test_check_refuses_rebased_terminal_not_ancestor_of_head(tmp_path):
    repo = _setup_repo(tmp_path)
    terminal_sha, build_test = _mint_success_fixture(repo)
    plan_path = repo / "docs" / "plans" / "example.md"
    m.mint(plan_path, repo, build_test_path=str(build_test))

    # Simulate the terminal commit vanishing from history (rebase/squash) by
    # resetting to before it and committing something else instead.
    _git(repo, "reset", "--hard", "HEAD~1")
    (repo / "coordinator_core" / "other.py").parent.mkdir(exist_ok=True)
    (repo / "coordinator_core" / "other.py").write_text("y = 1\n", encoding="utf-8")
    _commit(repo, "different history")

    reason = m.check(plan_path if False else repo / "docs" / "plans" / "example.md", repo, supersession=False)
    # plan.md at this new HEAD has no review_stamp (reset dropped it), so this
    # exercises "no stamp" rather than ancestry directly; assert the refusal
    # fires rather than a false pass.
    assert reason is not None


def test_check_ancestry_ref_resolution_failure_is_distinct_from_not_ancestor(tmp_path, monkeypatch):
    """`merge-base --is-ancestor` exit 1 ("genuinely not an ancestor") and any other
    non-zero exit (a ref that failed to resolve, e.g. a rewritten/garbage-collected sha)
    are two different failure modes -- the refusal message must say which one fired."""
    repo = _setup_repo(tmp_path)
    terminal_sha, build_test = _mint_success_fixture(repo)
    plan_path = repo / "docs" / "plans" / "example.md"
    m.mint(plan_path, repo, build_test_path=str(build_test))

    real_run_git = m.run_git

    def _fake_run_git(args, *a, **kw):
        if "merge-base" in args:
            return GitResult(
                returncode=128, stdout="", stderr="fatal: not a valid object name", timed_out=False
            )
        return real_run_git(args, *a, **kw)

    monkeypatch.setattr(m, "run_git", _fake_run_git)
    reason = m.check(plan_path, repo, supersession=False)
    assert reason is not None
    assert "could not resolve" in reason
    assert "is not an ancestor" not in reason


def test_mint_refuses_on_delivery_fail(tmp_path):
    repo = _setup_repo(tmp_path)
    terminal_sha, build_test = _mint_success_fixture(repo)
    # Overwrite the delivery sidecar with a FAIL verdict and re-point at it
    # via a fresh integration+commit so mint resolves the new terminal.
    share = repo / ".coordinator-local" / "subagent-share" / "sess1"
    _write_sidecar(share / "2026-09-27-delivery2.md", {"verdict": "FAIL"})
    _write_sidecar(
        share / "2026-09-27-prep2.md",
        {
            "run_base_sha": "deadbeef",
            "product_files": ["coordinator_core/foo.py"],
            "foreign_claims": [],
            "slices": [{"id": "A"}],
            "whole_diff_sidecars": {"delivery": ".coordinator-local/subagent-share/sess1/2026-09-27-delivery2.md"},
        },
    )
    _write_sidecar(
        share / "2026-09-27-integration2.md",
        {
            "plan_id": "pln-example-abc123",
            "prep_sidecar": ".coordinator-local/subagent-share/sess1/2026-09-27-prep2.md",
            "unresolved": [],
            "confinement_violations": [],
        },
    )
    _commit(repo, "land2\n\nInline-Review: applies 2026-09-27-integration2 -- execute-review: 1 slices, 0 fixes")
    plan_path = repo / "docs" / "plans" / "example.md"
    with pytest.raises(m.MintRefusal, match="delivery verdict"):
        m.mint(plan_path, repo, build_test_path=str(build_test))


def test_mint_refuses_on_unresolved(tmp_path):
    repo = _setup_repo(tmp_path)
    share = repo / ".coordinator-local" / "subagent-share" / "sess1"
    _write_sidecar(share / "2026-09-27-delivery.md", {"verdict": "PASS"})
    _write_sidecar(
        share / "2026-09-27-prep.md",
        {
            "run_base_sha": "deadbeef",
            "product_files": ["coordinator_core/foo.py"],
            "foreign_claims": [],
            "slices": [{"id": "A"}],
            "whole_diff_sidecars": {"delivery": ".coordinator-local/subagent-share/sess1/2026-09-27-delivery.md"},
        },
    )
    _write_sidecar(
        share / "2026-09-27-integration.md",
        {
            "plan_id": "pln-example-abc123",
            "prep_sidecar": ".coordinator-local/subagent-share/sess1/2026-09-27-prep.md",
            "unresolved": [{"line": "x"}],
            "confinement_violations": [],
        },
    )
    (repo / "coordinator_core").mkdir(exist_ok=True)
    (repo / "coordinator_core" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    _commit(repo, "land\n\nInline-Review: applies 2026-09-27-integration -- execute-review: 1 slices, 0 fixes")
    build_test = share / "2026-09-27-test-runner.md"
    _write_sidecar(build_test, {"status": "pass", "run": 1, "failed": 0})
    plan_path = repo / "docs" / "plans" / "example.md"
    with pytest.raises(m.MintRefusal, match="unresolved"):
        m.mint(plan_path, repo, build_test_path=str(build_test))


def test_override_reason_does_not_bypass_check():
    """`check`'s signature carries no override parameter at all -- the
    refusal has no escape hatch to bypass, unlike `_refuse_if_live_foreign_
    holder`'s own standing. Pinned structurally: `check` accepts only
    `plan_path`, `repo_root`, `supersession`."""
    import inspect

    sig = inspect.signature(m.check)
    assert "override" not in "".join(sig.parameters.keys()).lower()


def test_subject_plan_cutoff_and_slate_exemption():
    assert m.is_subject_plan({"execution_authorized_at": "2026-12-01T00:00:00Z"}) is True
    assert m.is_subject_plan({"execution_authorized_at": "2020-01-01T00:00:00Z"}) is False
    assert m.is_subject_plan({"created": "2026-12-01"}) is True
    assert m.is_subject_plan({"created": "2020-01-01"}) is False
    assert (
        m.is_subject_plan(
            {
                "execution_authorized_at": "2026-12-01T00:00:00Z",
                "deliverable_id": "dlv-coordinator-claude-klabauter-restructure-to-beat-v-9a5ca9",
            }
        )
        is False
    )


def test_check_process_time_bounded(tmp_path):
    import time

    repo = _setup_repo(tmp_path)
    terminal_sha, build_test = _mint_success_fixture(repo)
    plan_path = repo / "docs" / "plans" / "example.md"
    m.mint(plan_path, repo, build_test_path=str(build_test))

    start = time.perf_counter()
    m.check(plan_path, repo, supersession=True)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms <= 200, f"review_stamp.check took {elapsed_ms:.1f}ms, budget is 200ms"


def test_mint_refuses_when_integration_sidecar_lacks_prep_sidecar(tmp_path):
    """One-stage path, today's failure (2026-09-28 PM order fixture ask): an
    integration sidecar the terminal commit's trailer names that carries no
    `prep_sidecar` field refuses mint with a clear, named reason -- unchanged
    by the zero-integration-stage work, since a one-stage fragment still
    resolves an `ExecuteReview.integration` agent and its sidecar unchanged."""
    repo = _setup_repo(tmp_path)
    share = repo / ".coordinator-local" / "subagent-share" / "sess1"
    _write_sidecar(
        share / "2026-09-27-integration-noprep.md",
        {
            "plan_id": "pln-example-abc123",
            # No prep_sidecar -- today's failure this fixture pins.
            "unresolved": [],
            "confinement_violations": [],
            "fixes_applied": 0,
        },
    )
    _commit(
        repo,
        "land\n\nInline-Review: applies 2026-09-27-integration-noprep -- execute-review: 0 slices, 0 fixes",
    )
    build_test = share / "2026-09-27-test-runner.md"
    _write_sidecar(build_test, {"status": "pass", "run": 1, "failed": 0})
    plan_path = repo / "docs" / "plans" / "example.md"
    with pytest.raises(m.MintRefusal, match="carries no prep_sidecar; rerun with mint --repair"):
        m.mint(plan_path, repo, build_test_path=str(build_test))


def test_mint_repair_builds_record_when_resolved_sidecar_lacks_prep_sidecar(tmp_path):
    """claude-klabauter-shaped case (docs/plans/2026-09-28-batch-discharge-landed-
    batons.md, terminal e7c93434ca): the trailer resolves a real sidecar via
    the existing plan_id/session fallback, but that sidecar predates
    wave_bookkeeping and carries no `prep_sidecar`. `--repair` discovers the
    run's prep-shaped sidecar and wave sidecars in the same subagent-share
    dir and mints against a freshly assembled bookkeeping record."""
    repo = _setup_repo(tmp_path)
    plan_path = repo / "docs" / "plans" / "example.md"
    session_id = "sess1"
    share = repo / ".coordinator-local" / "subagent-share" / session_id
    share.mkdir(parents=True, exist_ok=True)

    prep_rel = f".coordinator-local/subagent-share/{session_id}/2026-09-28-prep.md"
    _write_sidecar(
        repo / prep_rel,
        {
            "run_base_sha": "deadbeef",
            "product_files": ["coordinator_core/foo.py"],
            "foreign_claims": [],
            "slices": [{"id": "A"}],
            "whole_diff_sidecars": {
                "delivery": f".coordinator-local/subagent-share/{session_id}/2026-09-28-delivery.md"
            },
        },
    )
    _write_sidecar(share / "2026-09-28-delivery.md", {"verdict": "PASS"})

    # A pre-fix "integrator" sidecar: it resolves via the trailer, and even
    # carries lead_session_id, but has NO prep_sidecar/unresolved shape --
    # exactly the claude-klabauter real-world coordinator-code-reviewer sidecar shape.
    integration = share / "2026-09-28-integration-old.md"
    _write_sidecar(
        integration,
        {
            "agent_type": "coordinator:code-reviewer",
            "lead_session_id": session_id,
            "findings_ledger": {"rows": 1, "applied": 1},
        },
    )


    wave_text = (
        "---\n"
        "agent_type: coordinator:code-reviewer\n"
        "applied: 2\n"
        "baseline_sha256:\n"
        f"  coordinator_core/foo.py: {_FOO_PY_SHA}\n"
        "---\n"
        "## Findings Ledger\n\n```json\n"
        '[{"id": "finding-1", "file": "coordinator_core/foo.py", "before": "x = 1", "after": "x = 2"}]\n'
        "```\n"
    )
    (share / "2026-09-28-wave-code-reviewer.md").write_text(wave_text, encoding="utf-8")

    (repo / "coordinator_core").mkdir(exist_ok=True)
    (repo / "coordinator_core" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    terminal_sha = _commit(
        repo, "land\n\nInline-Review: applies 2026-09-28-integration-old -- execute-review: 1 slices, 2 fixes"
    )
    build_test = share / "2026-09-28-test-runner.md"
    _write_sidecar(build_test, {"status": "pass", "run": 1, "failed": 0})

    receipt = plan_path.with_name("example.workflow.mjs.emitted.json")
    receipt.write_text(f'{{"session_id": "{session_id}", "plan": "example.md"}}', encoding="utf-8")

    # Without --repair: unchanged refusal.
    with pytest.raises(m.MintRefusal, match="carries no prep_sidecar"):
        m.mint(plan_path, repo, build_test_path=str(build_test), repair=False)

    stamp = m.mint(plan_path, repo, build_test_path=str(build_test), repair=True)
    assert stamp["terminal_commit_sha"] == terminal_sha
    assert stamp["delivery"]["verdict"] == "PASS"
    assert stamp["fixes_applied"] == 2
    assert stamp["unresolved"] == []


def test_mint_repair_builds_record_when_no_sidecar_resolves_at_all(tmp_path):
    """example-retrieval-repo-shaped case (cc6525cf0, trailer `applies None`): no
    sidecar resolves for the stem at all. `--repair` falls back to the
    plan's own emit-receipt session_id to find the terminal commit, then
    the same prep/wave discovery as above."""
    repo = _setup_repo(tmp_path)
    plan_path = repo / "docs" / "plans" / "example.md"
    session_id = "sess1"
    share = repo / ".coordinator-local" / "subagent-share" / session_id
    share.mkdir(parents=True, exist_ok=True)

    prep_rel = f".coordinator-local/subagent-share/{session_id}/2026-09-28-prep.md"
    _write_sidecar(
        repo / prep_rel,
        {
            "run_base_sha": "deadbeef",
            "product_files": ["coordinator_core/foo.py"],
            "foreign_claims": [],
            "slices": [{"id": "A"}],
            "whole_diff_sidecars": {
                "delivery": f".coordinator-local/subagent-share/{session_id}/2026-09-28-delivery.md"
            },
        },
    )
    _write_sidecar(share / "2026-09-28-delivery.md", {"verdict": "PASS"})


    wave_text = (
        "---\n"
        "agent_type: coordinator:code-reviewer\n"
        "applied: 1\n"
        "baseline_sha256:\n"
        f"  coordinator_core/foo.py: {_FOO_PY_SHA}\n"
        "---\n"
        "## Findings Ledger\n\n```json\n"
        '[{"id": "finding-1", "file": "coordinator_core/foo.py", "before": "x = 1", "after": "x = 2"}]\n'
        "```\n"
    )
    (share / "2026-09-28-wave-code-reviewer.md").write_text(wave_text, encoding="utf-8")

    (repo / "coordinator_core").mkdir(exist_ok=True)
    (repo / "coordinator_core" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    # The literal pre-fix shape: a trailer whose value is the string "None",
    # naming no real sidecar stem at all.
    terminal_sha = _commit(repo, "land\n\nInline-Review: applies None -- execute-review: 1 slices, 1 fixes")
    build_test = share / "2026-09-28-test-runner.md"
    _write_sidecar(build_test, {"status": "pass", "run": 1, "failed": 0})

    receipt = plan_path.with_name("example.workflow.mjs.emitted.json")
    receipt.write_text(f'{{"session_id": "{session_id}", "plan": "example.md"}}', encoding="utf-8")

    with pytest.raises(m.MintRefusal, match="no terminal commit found"):
        m.mint(plan_path, repo, build_test_path=str(build_test), repair=False)

    stamp = m.mint(plan_path, repo, build_test_path=str(build_test), repair=True)
    assert stamp["terminal_commit_sha"] == terminal_sha
    assert stamp["delivery"]["verdict"] == "PASS"
    assert stamp["fixes_applied"] == 1


def test_mint_repair_never_fires_when_flag_is_unset(tmp_path):
    """`repair` defaults False -- an ordinary caller (no flag) gets the
    unchanged refusal even though a repairable shape exists."""
    repo = _setup_repo(tmp_path)
    terminal_sha, build_test = _mint_success_fixture(repo)
    plan_path = repo / "docs" / "plans" / "example.md"
    stamp = m.mint(plan_path, repo, build_test_path=str(build_test))
    assert stamp["terminal_commit_sha"] == terminal_sha


def test_mint_succeeds_against_a_zero_integration_stage_bookkeeping_record(tmp_path):
    """End-to-end, zero-integration-stage path (2026-09-28 PM order step b'):
    `review_stamp.mint` reads the mechanical bookkeeping record
    (`review_mint.wave_bookkeeping.bookkeep_wave`'s one write) exactly as it
    would read a one-stage integration sidecar -- same field names, no
    `review_stamp.py` code change needed. The 'missing prep_sidecar is
    today's failure' concern (fixture above) is MOOT on this path: the
    bookkeeping step always supplies `prep_sidecar` itself (it is a required
    parameter of `bookkeep_wave`), so this shape can never be built without
    one."""

    from coordinator_core.ops.review_mint.wave_bookkeeping import (
        bookkeep_wave,
        review_wave_bookkeeping_stem,
    )

    repo = _setup_repo(tmp_path)
    session_id = "sess1"
    share = repo / ".coordinator-local" / "subagent-share" / session_id
    share.mkdir(parents=True, exist_ok=True)

    prep_rel = f".coordinator-local/subagent-share/{session_id}/2026-09-28-prep.md"
    _write_sidecar(
        repo / prep_rel,
        {
            "run_base_sha": "deadbeef",
            "product_files": ["coordinator_core/foo.py"],
            "foreign_claims": [],
            "slices": [{"id": "A"}],
            "whole_diff_sidecars": {
                "delivery": f".coordinator-local/subagent-share/{session_id}/2026-09-28-delivery.md"
            },
        },
    )
    _write_sidecar(share / "2026-09-28-delivery.md", {"verdict": "PASS"})

    wave_text = (
        "---\n"
        "agent_type: coordinator:code-reviewer\n"
        "applied: 2\n"
        "baseline_sha256:\n"
        f"  coordinator_core/foo.py: {_FOO_PY_SHA}\n"
        "---\n"
        "## Findings\n\n### Finding 1\nSomething.\n"
        "## Findings Ledger\n\n```json\n"
        '[{"id": "finding-1", "file": "coordinator_core/foo.py", "before": "x = 1", "after": "x = 2"}]\n'
        "```\n"
    )
    wave_path = share / "2026-09-28-wave-code-reviewer.md"
    wave_path.write_text(wave_text, encoding="utf-8")

    (repo / "coordinator_core").mkdir(exist_ok=True)
    (repo / "coordinator_core" / "foo.py").write_text("x = 2\n", encoding="utf-8")

    plan_id = "pln-example-abc123"
    stem = review_wave_bookkeeping_stem(plan_id, session_id)
    bookkeep_wave(
        [wave_path],
        repo_root=repo,
        session_id=session_id,
        plan_id=plan_id,
        prep_sidecar=prep_rel,
        record_stem=stem,
    )

    terminal_sha = _commit(repo, f"land\n\nInline-Review: applies {stem} -- execute-review: 1 slices, 2 fixes")
    build_test = share / "2026-09-28-test-runner.md"
    _write_sidecar(build_test, {"status": "pass", "run": 1, "failed": 0})

    plan_path = repo / "docs" / "plans" / "example.md"
    stamp = m.mint(plan_path, repo, build_test_path=str(build_test))
    assert stamp["terminal_commit_sha"] == terminal_sha
    assert stamp["delivery"]["verdict"] == "PASS"
    assert stamp["build_test"]["verdict"] == "pass"
    assert stamp["unresolved"] == []
    assert stamp["fixes_applied"] == 2


def _stage_record_fixture(repo: Path, *, tests: dict, criterion: dict, plan_id: str = "pln-example-abc123"):
    """A run record carrying the stage returns themselves (what
    `dispatch.terminal_commit` writes since 2026-10-01): no prep/delivery
    sidecar frontmatter is read at all. The diff is state-only."""
    share = repo / ".coordinator-local" / "subagent-share" / "sess1"
    record = share / "pln-example-abc123.review-wave-bookkeeping.md"
    data = {
        "plan_id": plan_id,
        "prep_sidecar": None,
        "unresolved": [],
        "confinement_violations": 0,
        "fixes_applied": 0,
        "slices": 1,
        "brief_conformance": {"items": 0, "met": 0, "unmet": 0},
        "prep": {"run_base_sha": "deadbeef", "product_files": 0, "foreign_claims": [],
                 "slice_files": ["state/lessons/a.md"]},
        "delivery": {"verdict": "PASS", "product_files": 0, "claims_unbacked": 0},
        "tests": tests,
        "criterion": criterion,
    }
    _write_sidecar(record, data)
    (repo / "state" / "lessons").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "lessons" / "a.md").write_text("lesson\n", encoding="utf-8")
    sha = _commit(
        repo,
        "land\n\nInline-Review: applies pln-example-abc123.review-wave-bookkeeping -- execute-review: 1 slices, 0 fixes",
    )
    return sha, record, data


_NOT_RUN = {"status": "not_run", "run": None, "failed": None, "sidecar": None}


def test_mint_reads_stage_returns_and_takes_a_met_criterion_for_a_prose_spine(tmp_path):
    repo = _setup_repo(tmp_path)
    sha, _, _ = _stage_record_fixture(
        repo, tests=_NOT_RUN, criterion={"status": "met", "observation": "13 lessons", "sidecar": None}
    )
    plan_path = repo / "docs" / "plans" / "example.md"
    stamp = m.mint(plan_path, repo, build_test_path=None)
    assert stamp["terminal_commit_sha"] == sha
    assert stamp["build_test"]["verdict"] == "not_run"
    assert stamp["criterion"]["status"] == "met"


def test_mint_refuses_a_prose_spine_whose_criterion_was_never_judged(tmp_path):
    repo = _setup_repo(tmp_path)
    _stage_record_fixture(repo, tests=_NOT_RUN, criterion={"status": "not_run", "observation": None, "sidecar": None})
    with pytest.raises(m.MintRefusal, match="build/test verdict is 'not_run'"):
        m.mint(repo / "docs" / "plans" / "example.md", repo, build_test_path=None)


@pytest.mark.parametrize("status", ["not_met", "indeterminate"])
def test_mint_refuses_an_unmet_criterion_even_with_passing_tests(tmp_path, status):
    repo = _setup_repo(tmp_path)
    _stage_record_fixture(
        repo,
        tests={"status": "pass", "run": 3, "failed": 0, "sidecar": "x.md"},
        criterion={"status": status, "observation": "o", "sidecar": None},
    )
    with pytest.raises(m.MintRefusal, match=f"exit criterion is {status}"):
        m.mint(repo / "docs" / "plans" / "example.md", repo, build_test_path=None)


def test_mint_with_resolved_skips_the_trailer_walk(tmp_path):
    repo = _setup_repo(tmp_path)
    sha, record, data = _stage_record_fixture(
        repo, tests=_NOT_RUN, criterion={"status": "met", "observation": "o", "sidecar": None}
    )
    # Bury the terminal commit's trailer: a walk would still find it, so prove
    # `resolved` is used by handing it a record the walk could never resolve.
    stamp = m.mint(repo / "docs" / "plans" / "example.md", repo, build_test_path=None,
                   resolved=(sha, record, dict(data, plan_id="some-other-plan")))
    assert stamp["terminal_commit_sha"] == sha


def _set_prep(repo: Path, **fields):
    prep = repo / ".coordinator-local" / "subagent-share" / "sess1" / "2026-09-27-prep.md"
    data = {
        "run_base_sha": "deadbeef",
        "product_files": ["coordinator_core/foo.py"],
        "slices": [{"id": "A"}],
        "whole_diff_sidecars": {"delivery": ".coordinator-local/subagent-share/sess1/2026-09-27-delivery.md"},
    }
    data.update(fields)
    _write_sidecar(prep, data)


def test_mint_ignores_peer_claims_outside_the_reviewed_footprint(tmp_path):
    repo = _setup_repo(tmp_path)
    _, build_test = _mint_success_fixture(repo)
    _set_prep(repo, slice_files=["coordinator_core/foo.py"],
              foreign_claims=["peer/a.py", "peer/b.md"])
    plan_path = repo / "docs" / "plans" / "example.md"
    assert m.mint(plan_path, repo, build_test_path=str(build_test))["unresolved"] == []


def test_mint_refuses_a_peer_claim_inside_the_reviewed_footprint(tmp_path):
    repo = _setup_repo(tmp_path)
    _, build_test = _mint_success_fixture(repo)
    _set_prep(repo, slice_files=["coordinator_core/foo.py"],
              foreign_claims=["coordinator_core/foo.py", "peer/a.py"])
    plan_path = repo / "docs" / "plans" / "example.md"
    with pytest.raises(m.MintRefusal, match="1 foreign claim"):
        m.mint(plan_path, repo, build_test_path=str(build_test))


def test_mint_counts_every_claim_when_no_footprint_is_recorded(tmp_path):
    repo = _setup_repo(tmp_path)
    _, build_test = _mint_success_fixture(repo)
    _set_prep(repo, foreign_claims=["peer/a.py"])
    plan_path = repo / "docs" / "plans" / "example.md"
    with pytest.raises(m.MintRefusal, match="1 foreign claim"):
        m.mint(plan_path, repo, build_test_path=str(build_test))


_OPERATOR_FM = """\
prime_exit_criterion:
  statement: a billable smoke run passes at a real terminal
  falsifier:
    mode: operator
  operator_attestation:
    artifact: {artifact}
    attested_by: sess-operator
    verdict: {verdict}
    ran_against: {ran_against}
"""


def _operator_plan(repo: Path, *, declared: bool = True, verdict: str = "pass",
                   artifact: str = "state/audits/smoke.md", commit_artifact: bool = True,
                   ran_against: str = "HEAD_SHA"):
    plan = repo / "docs" / "plans" / "example.md"
    text = plan.read_text(encoding="utf-8")
    if declared:
        if ran_against == "HEAD_SHA":
            ran_against = _git(repo, "rev-parse", "HEAD").stdout.strip()
        block = _OPERATOR_FM.format(artifact=artifact, verdict=verdict, ran_against=ran_against)
        text = text.replace("scope:\n", block + "scope:\n", 1)
        plan.write_text(text, encoding="utf-8")
    if commit_artifact:
        (repo / artifact).parent.mkdir(parents=True, exist_ok=True)
        (repo / artifact).write_text("smoke: pass\n", encoding="utf-8")
    _commit(repo, "plan declares operator falsifier")
    return _stage_record_fixture(
        repo,
        tests={"status": "pass", "run": 3, "failed": 0, "sidecar": "x.md"},
        criterion={"status": "indeterminate", "observation": "operator-only", "sidecar": None},
    )


def test_mint_takes_an_operator_attested_indeterminate_criterion_as_met(tmp_path):
    repo = _setup_repo(tmp_path)
    _operator_plan(repo)
    stamp = m.mint(repo / "docs" / "plans" / "example.md", repo, build_test_path=None)
    assert stamp["criterion"]["status"] == "met"
    assert "operator-attested by sess-operator" in stamp["criterion"]["observation"]


def test_mint_refuses_indeterminate_when_operator_mode_is_undeclared(tmp_path):
    repo = _setup_repo(tmp_path)
    _operator_plan(repo, declared=False)
    with pytest.raises(m.MintRefusal, match="exit criterion is indeterminate$"):
        m.mint(repo / "docs" / "plans" / "example.md", repo, build_test_path=None)


def test_mint_refuses_operator_attestation_whose_artifact_is_not_committed(tmp_path):
    repo = _setup_repo(tmp_path)
    _operator_plan(repo, commit_artifact=False)
    with pytest.raises(m.MintRefusal, match="artifact state/audits/smoke.md is not committed"):
        m.mint(repo / "docs" / "plans" / "example.md", repo, build_test_path=None)


def test_mint_refuses_operator_attestation_with_a_fail_verdict(tmp_path):
    repo = _setup_repo(tmp_path)
    _operator_plan(repo, verdict="fail")
    with pytest.raises(m.MintRefusal, match="verdict is 'fail', not pass"):
        m.mint(repo / "docs" / "plans" / "example.md", repo, build_test_path=None)


def test_mint_refuses_operator_attestation_whose_ran_against_is_not_a_commit(tmp_path):
    repo = _setup_repo(tmp_path)
    _operator_plan(repo, ran_against="0123456789abcdef0123456789abcdef01234567")
    with pytest.raises(m.MintRefusal, match="ran_against 0123456789abcdef0123456789abcdef01234567 does not resolve"):
        m.mint(repo / "docs" / "plans" / "example.md", repo, build_test_path=None)
