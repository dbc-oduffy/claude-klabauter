
from coordinator_core.backlog_grind_assemble.readers_blitz import (
    _DONE_SUMMARY_CONSTRAINT_TEMPLATE as _BLITZ_DONE_SUMMARY,
    _FOOTPRINT_CONSTRAINT_TEMPLATE as _BLITZ_FOOTPRINT,
)
from coordinator_core.backlog_grind_assemble.readers_mise import (
    _DONE_SUMMARY_CONSTRAINT_TEMPLATE as _MISE_DONE_SUMMARY,
    _FOOTPRINT_CONSTRAINT_TEMPLATE as _MISE_FOOTPRINT,
    _SELF_VERIFY_CONSTRAINT as _MISE_SELF_VERIFY,
)
from coordinator_core.executor_return_contract import (
    FOOTPRINT_CONSTRAINT_TEMPLATE,
    done_summary_constraint,
    self_verify_constraint,
)


def test_footprint_constraint_template_matches_mise_verbatim():
    assert FOOTPRINT_CONSTRAINT_TEMPLATE == _MISE_FOOTPRINT


def test_footprint_constraint_template_matches_blitz_verbatim():
    assert FOOTPRINT_CONSTRAINT_TEMPLATE == _BLITZ_FOOTPRINT


def test_footprint_constraint_template_pinned_bytes():
    assert FOOTPRINT_CONSTRAINT_TEMPLATE == (
        "You MUST NOT create or modify any file outside this footprint: "
        "[list]. If you discover you need to, STOP and report back via the "
        "DONE summary with status BLOCKED. Create and edit each of those files "
        "with Write/Edit, never with a Bash heredoc, sed, tee or redirection: "
        "only the write tools record a session write claim, and a file produced "
        "through Bash reaches the committer as an orphan it must refuse. Bash "
        "stays correct for reading, searching and running tests. If a Bash write "
        "already happened, name those paths in your report."
    )


def test_footprint_constraint_names_the_write_tools_and_forbids_bash_writes():
    text = FOOTPRINT_CONSTRAINT_TEMPLATE
    assert "Write/Edit" in text
    assert "never with a Bash heredoc, sed, tee or redirection" in text
    assert "record a session write claim" in text
    assert "reaches the committer as an orphan it must refuse" in text
    assert "Bash stays correct for reading, searching and running tests" in text


def test_self_verify_constraint_reproduces_mise_hand_dispatch_bytes():
    rendered = self_verify_constraint(commit_authority="the EM")
    assert rendered == _MISE_SELF_VERIFY


def test_self_verify_constraint_step_four_does_not_ban_the_step_two_git_read():
    rendered = self_verify_constraint(commit_authority="the EM")
    assert "you do not invoke git under any circumstance" not in rendered
    assert "git status --porcelain -- <footprint paths>" in rendered
    assert "Only the EM commits, once per wave" in rendered


def test_self_verify_constraint_emitted_path_names_named_authority():
    rendered = self_verify_constraint(
        commit_authority="the `coordinator:git-commit-agent` phase"
    )
    assert "leave it to the EM" not in rendered
    assert "Only the EM commits" not in rendered
    assert "Only the `coordinator:git-commit-agent` phase commits" in rendered
    assert (
        "leave it to the `coordinator:git-commit-agent` phase"
        in rendered
    )


def test_self_verify_constraint_deferred_verification_authority_defaults_to_commit_authority():
    rendered = self_verify_constraint(commit_authority="the EM")
    assert rendered == self_verify_constraint(
        commit_authority="the EM", deferred_verification_authority="the EM"
    )


def test_self_verify_constraint_splits_commit_and_deferred_verification_authority():
    rendered = self_verify_constraint(
        commit_authority="the `coordinator:git-commit-agent` phase",
        deferred_verification_authority=(
            "the `coordinator:git-commit-agent` phase and the terminal "
            "`coordinator:test-runner` phase"
        ),
    )
    assert (
        "Only the `coordinator:git-commit-agent` phase commits, once per wave"
        in rendered
    )
    assert (
        "Only the `coordinator:git-commit-agent` phase and the terminal "
        "`coordinator:test-runner` phase commits"
    ) not in rendered
    assert (
        "leave it to the `coordinator:git-commit-agent` phase and the "
        "terminal `coordinator:test-runner` phase"
        in rendered
    )


def test_done_summary_constraint_reproduces_mise_bytes():
    rendered = done_summary_constraint(
        output_path_template="tasks/mise-done/[item-id].md",
        extra_fields=(
            "changed-path list (from `git status --porcelain -- "
            "<footprint paths> | cut -c4-`)",
            "AC checklist (each criterion checked or note)",
            "footprint-scoped verification commands run + outcomes",
            (
                "any spec-named verification broader than your footprint "
                "that you deferred to the EM"
            ),
            "and any deviations from the spec",
        ),
    )
    assert rendered == _MISE_DONE_SUMMARY


def test_done_summary_constraint_reproduces_blitz_bytes():
    rendered = done_summary_constraint(
        output_path_template=(
            "state/scratch/bug-blitz/[run-id]/[item-id].done.md"
        ),
        extra_fields=(
            "the changed-path list (`files`)",
            "`before`/`after` snippets",
            "the verification result you observed",
            "and any deviations from the recommended fix",
        ),
    )
    assert rendered == _BLITZ_DONE_SUMMARY


def test_done_summary_constraint_no_commit_sha_rule_present():
    rendered = done_summary_constraint(
        output_path_template="some/path.md",
        extra_fields=("a field",),
    )
    assert "Do not include a commit SHA" in rendered
    assert "still uncommitted when you write this summary" in rendered


def test_done_summary_constraint_reply_rule_names_own_path():
    rendered = done_summary_constraint(
        output_path_template="some/path.md",
        extra_fields=("a field",),
    )
    assert "Reply EXACTLY `<STATUS>: some/path.md`" in rendered
    assert "`DONE`, `PARTIAL`, or `BLOCKED`" in rendered
