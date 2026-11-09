"""A subagent may `git checkout-index --prefix=<dir>/` into its repo's scratch/; any other checkout-index stays denied."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.bash_guards import block_subagent_destructive_action as guard


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "scratch").mkdir()
    return root


def _run(command, cwd):
    return guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "sess-cio",
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:executor",
            "cwd": str(cwd),
        }
    )


LANDING = (
    "git add -- coordinator_core/backlog_grind_assemble/__init__.py "
    "coordinator_core/test_backlog_grind_assemble.py; "
    "git checkout-index -a --prefix=scratch/land-a9c9f7/idx/; "
    "rm -rf scratch/land-a9c9f7"
)


def test_landing_compound_is_allowed(repo):
    assert _run(LANDING, repo) is None


def test_prefix_as_separate_argument_is_allowed(repo):
    assert _run("git checkout-index -a --prefix scratch/x/", repo) is None


def test_checkout_index_without_prefix_is_denied(repo):
    assert _run("git checkout-index -a", repo) is not None
    assert _run("git checkout-index -a -f", repo) is not None


def test_prefix_outside_scratch_is_denied(repo):
    assert _run("git checkout-index -a --prefix=out/", repo) is not None
    assert _run("git checkout-index -a --prefix=scratch/../src/", repo) is not None
    assert _run("git checkout-index -a --prefix=scratch/", repo) is not None
    assert _run("git checkout-index -a --prefix=", repo) is not None


def test_rm_is_allowed_only_for_the_export_the_same_command_made(repo):
    export = "git checkout-index -a --prefix=scratch/land-1/idx/"
    assert _run(f"{export}; rm -rf scratch/land-1/idx", repo) is None
    assert _run("rm -rf scratch/land-1", repo) is not None
    assert _run(f"{export}; rm -rf scratch/other", repo) is not None
    assert _run(f"{export}; rm -rf scratch", repo) is not None
    assert _run(f"{export}; rm -rf src", repo) is not None
    assert _run(f"{export}; rm -rf scratch/land-1/*", repo) is not None


@pytest.mark.parametrize(
    "command",
    ["git rev-list --count HEAD", "git cat-file -p HEAD", "git ls-tree HEAD"],
)
def test_harmless_git_verb_is_allowed(repo, command):
    assert _run(command, repo) is None


def test_unrecognized_git_verb_is_advisory_not_deny(repo):
    verdict = _run("git frobnicate --x", repo)
    assert verdict is not None
    assert verdict.verdict_kind == "advisory"
    assert "frobnicate" in verdict.summary


def test_unrecognized_verb_advisory_does_not_mask_a_deny(repo):
    verdict = _run("git frobnicate; git rm -f a.txt", repo)
    assert verdict is not None
    assert verdict.verdict_kind != "advisory"


def test_other_irreversible_verbs_stay_denied(repo):
    assert _run("git rm -f a.txt", repo) is not None
    assert _run("git update-index --add a.txt", repo) is not None
