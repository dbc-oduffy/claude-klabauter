
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["coordinator_core"]\n',
        encoding="utf-8",
    )
    (tmp_path / "coordinator_core" / "frontmatter" / "tests").mkdir(parents=True)
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [guard.ConfiguredCmd(
            "fast_test_cmd", "python3 -m pytest coordinator_core/", 0
        )],
    )
    return tmp_path


def test_classify_command_no_match_on_scoped_invocation(repo):
    assert guard.classify_command(
        "pytest coordinator_core/frontmatter/tests/test_x.py", cwd=str(repo)
    ) == []


def test_classify_command_single_match(repo):
    matches = guard.classify_command("pytest", cwd=str(repo))
    assert len(matches) == 1
    m = matches[0]
    assert m.tier == "U"
    assert m.detected == "pytest"
    assert m.matched_text == "pytest"
    assert m.position == "imperative"
    assert m.span == (0, len("pytest"))
    assert "Scope this" in m.remediation


def test_classify_command_multi_segment_all_classified_not_short_circuited(repo):
    cmd = "pytest && npm test"
    matches = guard.classify_command(cmd, cwd=str(repo))
    detected = {m.detected for m in matches}
    assert detected == {"pytest", "npm test"}
    assert len(matches) == 2


def test_classify_command_semicolon_and_pipe_segments(repo):
    cmd = "pytest ; go test ./... | tee /tmp/log"
    matches = guard.classify_command(cmd, cwd=str(repo))
    detected = {m.detected for m in matches}
    assert detected == {"pytest", "go test ./..."}


def test_classify_command_spans_point_at_matched_text(repo):
    cmd = "cd /repo && pytest"
    matches = guard.classify_command(cmd, cwd=str(repo))
    assert len(matches) == 1
    start, end = matches[0].span
    assert cmd[start:end] == "pytest"


def test_classify_command_quoted_semicolon_not_a_segment_boundary(repo):
    cmd = 'git commit -m "note: never run pytest; pytest is important"'
    assert guard.classify_command(cmd, cwd=str(repo)) == []


def test_classify_command_quoted_pipe_and_ampersand_not_segment_boundaries(repo):
    cmd = 'echo "a | pytest & pytest" && echo done'
    matches = guard.classify_command(cmd, cwd=str(repo))
    assert matches == []


def test_classify_command_quoted_semicolon_preserves_real_match_span(repo):
    """A quoted separator earlier in the command must not shift the SPAN
    reported for a genuine match later in the same command -- spans are
    offsets into the ORIGINAL string, and a wrong split would desync them
    from it even where the classification verdict itself stayed correct."""
    cmd = 'echo "a; b" && pytest'
    matches = guard.classify_command(cmd, cwd=str(repo))
    assert len(matches) == 1
    start, end = matches[0].span
    assert cmd[start:end] == "pytest"


def test_classify_command_tier_f_on_configured_fast_cmd(repo, monkeypatch):
    """A configured fast_test_cmd that is genuinely SCOPED (a real
    descendant of testpaths, not the testpaths root itself) classifies
    Tier F. Updated 2026-07-25 (R1 fix, cross-repo/inbox/2026-07-25-doe-
    claude-em-validate-tier-u-shape-ruling.md): the fixture's ORIGINAL
    command here ("python3 -m pytest coordinator_core/") pointed exactly
    at the pinned testpaths root -- an unscoped-runner-invocation SHAPE by
    ``_is_real_scope``'s own contract ("False for a testpaths root
    itself") -- so the old classify-by-key fast leg wrongly laundered it
    to Tier F purely because it matched the configured key. This is
    exactly the bug the ruling closed (R1: tier is a property of shape,
    not the config key it was read from) -- see
    test_classify_command_unscoped_fast_cmd_match_now_tier_u below for the
    regression guard on that exact shape. This test now configures a
    fast_test_cmd that is a real descendant of testpaths so it continues
    to cover the legitimate Tier F route."""
    scoped_cmd = "pytest coordinator_core/frontmatter/tests/test_x.py"
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [guard.ConfiguredCmd("fast_test_cmd", scoped_cmd, 0)],
    )
    matches = guard.classify_command(scoped_cmd, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "F"
    assert "fast_test_cmd" in matches[0].remediation


def test_classify_command_unscoped_fast_cmd_match_now_tier_u(repo, monkeypatch):
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [guard.ConfiguredCmd(
            "fast_test_cmd", "python3 -m pytest coordinator_core/", 0
        )],
    )
    matches = guard.classify_command("python3 -m pytest coordinator_core/", cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"


def test_classify_command_scoped_full_cmd_match_alone_is_tier_f(repo, monkeypatch):
    scoped_cmd = "pytest coordinator_core/frontmatter/tests"
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [guard.ConfiguredCmd("full_test_cmd", scoped_cmd, 0)],
    )
    matches = guard.classify_command(scoped_cmd, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "F"
    assert "full_test_cmd" in matches[0].remediation


def test_classify_command_unscoped_full_cmd_match_is_tier_u(repo, monkeypatch):
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [guard.ConfiguredCmd(
            "full_test_cmd", "python3 -m pytest coordinator_core/", 0
        )],
    )
    matches = guard.classify_command("python3 -m pytest coordinator_core/", cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"


@pytest.mark.parametrize("inert_cmd", ["true", "exit 3", "echo hello"])
def test_classify_command_inert_full_cmd_match_is_tier_f_not_u(
    repo, monkeypatch, inert_cmd
):
    """``_runner_recognized``'s ``False`` covers two structurally different
    commands, and the ``full_test_cmd`` leg's fail-closed Tier-U default was
    applied to both: the opaque wrapper whose breadth is unknowable (kept
    Tier U by the test below), and a command PROVABLY incapable of spawning a
    test run.

    Measured 2026-08-02 before ``_argv_is_inert``: a repo whose fast tier
    resolved to ``exit 3`` -- with no distinct ``full_test_cmd``, so the
    resolver's rc=3 fallback made the fast string the full string -- had that
    command classified "the repo's configured full_test_cmd", Tier U, so
    ``enforce_tier_u_gate`` refused to run a one-token no-op without a Tier-U
    grant. There is no unknown breadth behind ``exit 3``; Tier U was
    protecting against an ambiguity that does not exist for this class.
    """
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [
            guard.ConfiguredCmd("fast_test_cmd", inert_cmd, 0),
            guard.ConfiguredCmd("full_test_cmd", inert_cmd, 3),
        ],
    )
    matches = guard.classify_command(inert_cmd, cwd=str(repo))
    assert [m.tier for m in matches] in ([], ["F"])


@pytest.mark.parametrize(
    "wrapper_cmd",
    ["bash scripts/run-tests.sh --tier fast", "bash run-suite.sh", "python dev.py test"],
)
def test_classify_command_opaque_wrapper_full_cmd_match_stays_tier_u(
    repo, monkeypatch, wrapper_cmd
):
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [
            guard.ConfiguredCmd("fast_test_cmd", wrapper_cmd, 0),
            guard.ConfiguredCmd("full_test_cmd", wrapper_cmd, 3),
        ],
    )
    matches = guard.classify_command(wrapper_cmd, cwd=str(repo))
    assert [m.tier for m in matches] == ["U"]


def test_classify_command_tier_u_default_for_unrelated_suite_shape(repo):
    matches = guard.classify_command("cargo test", cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"


def test_classify_command_prefers_tier_u_when_fast_and_full_coincide(repo, monkeypatch):
    """When fast_test_cmd and full_test_cmd resolve to the identical string,
    the whole-suite invocation must classify Tier U, not Tier F.
    First-match-wins on insertion order would force Tier F here and silently
    ungate the exact coverage this leg exists to provide.

    This holds regardless of WHY the two tiers coincide -- the normal
    fallback shape (no distinct full_test_cmd configured, so
    resolve_full_test_cmd falls back to the fast tier's own string, resolver
    rc=3) and an EXPLICIT declaration of the same string under both keys
    (resolver rc=0 for both) both resolve to Tier U deliberately, per DR-088's
    unscoped-runner-invocation disjunct -- see
    test_check_test_suite_invocation.py's
    test_em_tier_u_explicit_tie_denied_no_fast_route_named and
    test_em_tier_u_fallback_tie_denied for the rc-specific coverage."""
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [
            guard.ConfiguredCmd("fast_test_cmd", "python3 -m pytest coordinator_core/", 0),
            guard.ConfiguredCmd("full_test_cmd", "python3 -m pytest coordinator_core/", 0),
        ],
    )
    matches = guard.classify_command("python3 -m pytest coordinator_core/", cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"


def test_classify_command_identical_scoped_string_under_both_keys_now_classifies_tier_f(
    repo, monkeypatch
):
    """RECONCILED 2026-07-30: this test formerly pinned an OBSERVED
    divergence from the ruling memo's own worked example
    (cross-repo/inbox/2026-07-25-doe-claude-em-validate-tier-u-shape-
    ruling.md "The correction" section) -- the memo illustrates a repo
    declaring an identical, genuinely-scoped (real descendant of
    testpaths, NOT the testpaths root) command string under BOTH
    fast_test_cmd and full_test_cmd, and states this "is not Tier U".

    At the time this test was first written, the classifier's
    full_test_cmd leg forced Tier U on ANY match against the repo's
    configured full_test_cmd UNCONDITIONALLY, regardless of the matched
    segment's own scope shape -- diverging from R1 ("tier is a property
    of the invocation's SHAPE, not of the config key it was read from"),
    which the fast_test_cmd leg already honoured but the full_test_cmd
    leg did not. That asymmetry is now closed: both legs route through
    the single shared shape decision (``_tier_for_cfg_match``), so a
    genuinely-scoped subdirectory declared under BOTH keys classifies
    Tier F here, matching the memo's stated expectation for this exact
    shape. See test_classify_command_prefers_tier_u_when_fast_and_full_coincide
    above for the companion case (an UNSCOPED tie, which stays Tier U
    unchanged by this reconciliation -- shape, not the tie itself, is
    what the rule turns on)."""
    scoped_cmd = "pytest coordinator_core/frontmatter/tests"
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [
            guard.ConfiguredCmd("fast_test_cmd", scoped_cmd, 0),
            guard.ConfiguredCmd("full_test_cmd", scoped_cmd, 0),
        ],
    )
    matches = guard.classify_command(scoped_cmd, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "F"


def test_classify_command_empty_and_non_string_input(repo):
    assert guard.classify_command("", cwd=str(repo)) == []
    assert guard.classify_command(None, cwd=str(repo)) == []  # type: ignore[arg-type]


def test_suite_match_as_dict(repo):
    m = guard.classify_command("pytest", cwd=str(repo))[0]
    d = m.as_dict()
    assert d["tier"] == "U"
    assert d["detected"] == "pytest"
    assert d["span"] == [0, len("pytest")]
    assert d["position"] == "imperative"


def test_classify_text_zero_match_prose(repo):
    text = "This is a normal status update with no test commands in it at all."
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_plain_prose_no_markdown_still_works(repo):
    text = "Run pytest to check your work before you report back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].detected == "pytest"
    assert matches[0].position == "imperative"


def test_classify_text_single_match_in_fence(repo):
    text = "Run this:\n\n```\npytest\n```\n"
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "fenced_code"
    start, end = matches[0].span
    assert text[start:end] == "pytest"


def test_classify_text_multi_match(repo):
    text = (
        "Wave 1: run `pytest` on your chunk.\n\n"
        "Wave 2, after that:\n```\nnpm test\n```\n"
    )
    matches = guard.classify_text(text, cwd=str(repo))
    detected = sorted(m.detected for m in matches)
    assert detected == ["npm test", "pytest"]


def test_position_inline_code(repo):
    text = "For reference, our fast tier runs `pytest` under the hood."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "inline_code"


def test_position_negated_inline(repo):
    text = "Do NOT run `pytest -v` from a dispatched chunk."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "negated"


def test_position_negated_inline_preceding_line(repo):
    text = "Do not run this:\n`pytest`"
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "negated"


def test_position_imperative_inline_no_negation_anywhere(repo):
    text = "For reference, our fast tier runs `pytest` under the hood."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "inline_code"


def test_position_negated_beats_fenced(repo):
    text = "Never run this:\n\n```\npytest -v\n```\n"
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "negated"


def test_position_imperative_bare_line(repo):
    text = "Before you report back, run pytest across your changes."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"
    assert matches[0].detected == "pytest"


def test_position_negated_bare_line_multi_line(repo):
    """P1 regression: a negation marker on the PRECEDING line of plain prose
    (no backticks, no fence) must still flip the bare-line match to
    "negated" -- previously the bare-line pass's window was confined to the
    matched line alone, so this exact shape reported "imperative"."""
    text = "Do not run this:\npytest"
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "negated"


def test_position_negated_bare_line_same_line(repo):
    text = "Do not run pytest against the whole suite from a chunk."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "negated"


def test_position_imperative_bare_line_no_negation_anywhere(repo):
    text = "Before you report back, run pytest across your changes."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


def test_position_imperative_bare_line_negation_too_far_above(repo):
    padding = "x" * 400
    text = f"Do not delete the config file.\n{padding}\nRun pytest across your changes."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


def test_position_unknown_on_unterminated_fence(repo):
    text = "Truncated brief, fence never closes:\n\n```\npytest\nnpm test\n"
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, "expected the dangling-fence content to still be scanned"
    assert all(m.position == "unknown" for m in matches)


def test_position_load_bearing_denylist_deletion_brief(repo):
    text = (
        "Delete this deny-list from the agent body -- it is stale copy:\n\n"
        "```\n"
        "pytest -v\n"
        "npm test\n"
        "cargo test\n"
        "go test ./...\n"
        "```\n"
    )
    matches = guard.classify_text(text, cwd=str(repo))
    detected = sorted(m.detected for m in matches)
    assert detected == ["cargo test", "go test ./...", "npm test", "pytest"]
    assert all(m.position != "imperative" for m in matches)


def test_classify_text_no_match_narrative_testpaths_mention(repo):
    text = (
        "`coordinator/bin/tests` is in pytest `testpaths` and\n"
        '`python_files = ["test_*.py"]`, so hyphenated names are never '
        "collected."
    )
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_no_match_narrative_adjective_mention(repo):
    text = "# Task — re-port the loader as a pytest oracle"
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_no_match_narrative_noun_phrase_mention(repo):
    text = (
        "The gate can only accept a confirmation backed by a re-runnable "
        "pytest node id. So we need genuine standing coverage that pins "
        "the property, not a rubber stamp."
    )
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_still_blocks_unscoped_run_instruction(repo):
    text = "Then run python3 -m pytest to check everything still works."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].detected == "pytest"
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_unscoped_verify_instruction(repo):
    text = "Verify with pytest coordinator_core/ before reporting back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].position == "imperative"


def test_bare_line_is_command_shaped_direct_cases():
    assert guard._bare_line_is_command_shaped("Run ") is True
    assert guard._bare_line_is_command_shaped("Before you report back, run ") is True
    assert guard._bare_line_is_command_shaped("then run python3 -m ") is True
    assert guard._bare_line_is_command_shaped("verify with ") is True
    assert guard._bare_line_is_command_shaped("$ ") is True
    assert guard._bare_line_is_command_shaped("") is True
    assert guard._bare_line_is_command_shaped("... is in ") is False
    assert guard._bare_line_is_command_shaped("as a ") is False
    assert guard._bare_line_is_command_shaped(
        "backed by a re-runnable "
    ) is False


def test_bare_line_is_command_shaped_colon_headed_cue(repo):
    assert guard._bare_line_is_command_shaped("Run: ") is True
    assert guard._bare_line_is_command_shaped("Verify: ") is True
    assert guard._bare_line_is_command_shaped("Command: ") is False
    matches = guard.classify_text("Run: pytest before you report back.", cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


def test_bare_line_is_command_shaped_broadened_cue_vocabulary():
    assert guard._bare_line_is_command_shaped("please ") is True
    assert guard._bare_line_is_command_shaped("just do ") is True
    assert guard._bare_line_is_command_shaped("kick off ") is True
    assert guard._bare_line_is_command_shaped("start ") is True


def test_bare_line_is_command_shaped_lettered_list_marker():
    assert guard._bare_line_is_command_shaped("a. ") is True
    assert guard._bare_line_is_command_shaped("b) ") is True
    assert guard._bare_line_is_command_shaped("Reference ") is False


def test_bare_line_is_command_shaped_markdown_markers_not_command():
    assert guard._bare_line_is_command_shaped("# ") is False
    assert guard._bare_line_is_command_shaped("> ") is False


def test_classify_text_no_match_bare_markdown_heading_names_runner(repo):
    text = "# pytest configuration notes"
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_no_match_bare_blockquote_names_runner(repo):
    text = "> pytest already covers this."
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_still_blocks_colon_headed_instruction(repo):
    text = "Verify: pytest coordinator_core/ before reporting back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_non_closed_set_imperative(repo):
    text = "Please pytest the whole tree before you report back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_lettered_list_command(repo):
    text = "a. Scope your tests.\nb. pytest\nc. Report back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


def test_bare_line_is_command_shaped_cue_not_scoped_across_clause():
    """Direct unit pin for defect A's root cause: a ``run`` cue that belongs
    to an EARLIER clause/sentence (itself part of a prohibition) must not
    license a runner mention in a LATER, unrelated clause on the same
    line."""
    prefix = (
        "Neither consumer may run the test tier or block the ceremony. "
        "A start ceremony that invokes "
    )
    assert guard._bare_line_is_command_shaped(prefix) is False


def test_classify_text_no_match_imperative_cue_in_earlier_unrelated_clause(repo):
    text = (
        "Neither consumer may run the test tier or block the ceremony. "
        "A start ceremony that invokes pytest is a several-minute stall on "
        "every session boot."
    )
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_command_make_test_matches(repo):
    matches = guard.classify_command("make test", cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].detected == "make test"
    assert matches[0].tier == "U"


def test_classify_command_make_parallel_flag_test_matches(repo):
    matches = guard.classify_command("make -j4 test", cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].detected == "make test"


def test_classify_command_make_assignment_check_matches(repo):
    matches = guard.classify_command("make CC=gcc check", cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].detected == "make check"


def test_classify_text_still_blocks_make_test_instruction(repo):
    text = "Run make test before reporting back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].detected == "make test"
    assert matches[0].tier == "U"


def test_classify_command_make_ordinary_verb_usage_no_match(repo):
    assert guard.classify_command(
        "make the exemplar useless: something about which branch the test covers.",
        cwd=str(repo),
    ) == []


def test_classify_command_make_change_and_add_a_test_no_match(repo):
    assert guard.classify_command(
        "make the change and add a test.", cwd=str(repo)
    ) == []


def test_classify_make_direct_adjacency_unit_cases():
    assert guard._classify_make(["test"]) == "make test"
    assert guard._classify_make(["-j4", "test"]) == "make test"
    assert guard._classify_make(["CC=gcc", "check"]) == "make check"


# ``_NEGATION_RE``'s ``\bdo not run\b`` marker never fires here because its

def test_bare_line_is_command_shaped_do_not_non_run_verb_no_match():
    prefix = "do not weaken the guard to "
    assert guard._bare_line_is_command_shaped(prefix) is False


def test_classify_text_no_match_do_not_weaken_guard_repro(repo):
    text = "do not weaken the guard to make tests pass"
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_bare_line_is_command_shaped_do_not_run_still_matches():
    prefix = "do not run "
    assert guard._bare_line_is_command_shaped(prefix) is True


def test_classify_text_still_blocks_do_pytest_instruction(repo):
    text = "please do pytest the whole tree"
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].detected == "pytest"
    assert guard._classify_make(["the", "exemplar", "useless", "test"]) is None
    assert guard._classify_make(["the", "change", "and", "add", "a", "test"]) is None


def test_classify_text_pipe_delimited_prose_enum_not_shredded_into_bare_runner(repo):
    text = (
        "The dispatch brief pins a contract enum "
        "`runner: pytest | node-test | bats | unknown` for this wave."
    )
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_tier_f_vs_u(repo, monkeypatch):
    scoped_cmd = "pytest coordinator_core/frontmatter/tests/test_x.py"
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [guard.ConfiguredCmd("fast_test_cmd", scoped_cmd, 0)],
    )
    text = (
        f"Fast tier: `{scoped_cmd}`\n"
        "Unrelated suite run: `cargo test`\n"
    )
    matches = guard.classify_text(text, cwd=str(repo))
    by_span_text = {text[m.span[0]:m.span[1]]: m.tier for m in matches}
    assert by_span_text["cargo test"] == "U"
    assert by_span_text[scoped_cmd] == "F"


def test_classify_text_undeterminable_defaults_to_tier_u(repo, monkeypatch):
    monkeypatch.setattr(guard, "_configured_test_cmds", lambda root: [])
    matches = guard.classify_text("`pytest`", cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"


def test_classify_text_scoped_invocation_no_match(repo):
    text = "Run `pytest coordinator_core/frontmatter/tests/test_x.py` for your chunk."
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_command_scoped_node_id_no_match(repo):
    assert guard.classify_command(
        "pytest coordinator_core/frontmatter/tests/test_x.py::test_case", cwd=str(repo)
    ) == []


def _payload(command, cwd, agent_id=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": str(cwd),
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    return p


def test_check_still_allows_scoped_subagent_command(repo, monkeypatch):
    """Since the Tier-T CONCURRENCY leg (0.5) this is an ALLOW carrying a
    slot-wrapper rewrite rather than a bare None. The property under test --
    a dispatched caller's scoped run is permitted -- is unchanged; only the
    encoding of "permitted" moved. A deny still fails here."""
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    out = guard.check(_payload(
        "pytest coordinator_core/frontmatter/tests/test_x.py", repo, agent_id="a0123456789abcdef"
    ))
    if out is not None:
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert out["hookSpecificOutput"]["updatedInput"]["command"].startswith(
            "with-tier-t-slot -- "
        )


def test_check_still_denies_unscoped_subagent_command(repo, monkeypatch):
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    out = guard.check(_payload("pytest", repo, agent_id="a0123456789abcdef"))
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("Full-suite subagent runs are denied")


# false-bd5afe033da4.yaml. Root cause: ``_IMPERATIVE_CUE_RE``'s bare
# comment) -- NOT via ``_NEGATION_RE`` (that marker requires "do not run",

def test_classify_text_no_match_re_verify_settled_claims_repro(repo):
    text = (
        "Do NOT re-verify claims your prior pass already confirmed clean "
        "(769 lines, grep→0, apply_base's 4 consumers, the pytest "
        "result, the forwarders). Those are settled."
    )
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_bare_line_is_command_shaped_re_verify_prefix_not_command_shaped():
    prefix = (
        "Do NOT re-verify claims your prior pass already confirmed clean "
        "(769 lines, grep→0, apply_base's 4 consumers, the "
    )
    assert guard._bare_line_is_command_shaped(prefix) is False


def test_imperative_cue_re_does_not_match_re_verify():
    assert guard._IMPERATIVE_CUE_RE.search("re-verify") is None
    assert guard._IMPERATIVE_CUE_RE.search("re-verifying") is None


def test_classify_text_no_match_re_verify_short_form(repo):
    text = "Do not re-verify the pytest result, it is already settled."
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_still_blocks_bare_pytest_instruction(repo):
    text = "Once your change lands, run pytest to confirm nothing broke."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].detected == "pytest"
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_python_module_pytest_instruction(repo):
    text = "Please run python -m pytest coordinator_core/ before you report back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_npm_test_instruction(repo):
    text = "Kick off npm test once the build finishes."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].detected == "npm test"
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_fenced_multiline_suite_command(repo):
    text = (
        "Run this before you report back:\n\n"
        "```\n"
        "cd coordinator_core\n"
        "pytest\n"
        "```\n"
    )
    matches = guard.classify_text(text, cwd=str(repo))
    pytest_matches = [m for m in matches if m.detected == "pytest"]
    assert len(pytest_matches) == 1
    assert pytest_matches[0].tier == "U"
    assert pytest_matches[0].position == "fenced_code"


def test_classify_text_still_blocks_re_run_pytest_instruction(repo):
    text = "Please re-run pytest to confirm the flake is gone."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].detected == "pytest"
    assert matches[0].position == "imperative"


def test_classify_text_negation_on_preceding_line_still_flips_position(repo):
    text = "Do not run this:\npytest -v"
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "negated"


# instruction anywhere in the sentence. ``_NEGATION_RE`` could not fix this:

def test_classify_text_reported_speech_field_report_repro(repo):
    text = "They stated plainly they could not run pytest to confirm."
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, "expected the reported-speech pytest mention to still be a match"
    assert all(m.position == "reported" for m in matches)
    assert all(m.position != "imperative" for m in matches)


@pytest.mark.parametrize("phrase", [
    "They could not run pytest before the deadline.",
    "They couldn't run pytest before the deadline.",
    "They cannot run pytest on this machine.",
    "They can't run pytest on this machine.",
    "She was unable to run pytest against the branch.",
    "She was not able to run pytest against the branch.",
    "They were unable to run pytest against the branch.",
    "The build failed to run pytest during CI.",
    "The agent did not run pytest before reporting.",
    "The agent didn't run pytest before reporting.",
    "The team has not run pytest since the rename.",
    "The team have not run pytest since the rename.",
    "They never ran `pytest` against that branch.",
])
def test_classify_text_reported_modal_capability_negation_variants(repo, phrase):
    matches = guard.classify_text(phrase, cwd=str(repo))
    assert matches, f"expected a pytest match in: {phrase!r}"
    assert all(m.position == "reported" for m in matches), phrase


@pytest.mark.parametrize("phrase", [
    "They stated that `pytest` could not confirm the fix.",
    "They said `pytest` never ran in CI last night.",
    "They reported that `pytest` failed to run in CI.",
    "The QA lead noted that `pytest` never ran in CI.",
    "Reported that `pytest` never ran during the outage.",
])
def test_classify_text_reported_speech_framing_variants(repo, phrase):
    matches = guard.classify_text(phrase, cwd=str(repo))
    assert matches, f"expected a pytest match in: {phrase!r}"
    assert all(m.position == "reported" for m in matches), phrase


def test_classify_text_reported_speech_precedence_negated_wins(repo):
    """If ``_NEGATION_RE`` ALSO matches (e.g. "never run", already a
    recognized negation marker), existing "negated" behavior wins -- the new
    "reported" value must never override an already-correct classification."""
    text = "Never run pytest against the whole suite from a chunk."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "negated"


def test_classify_text_reported_speech_does_not_suppress_later_imperative(repo):
    """NON-NEGOTIABLE recall guard: a reported-speech cue in an earlier,
    unrelated clause must NOT bleed forward and suppress a genuinely
    imperative command later in the same text. This is the exact shape
    named as a hard recall-regression risk in the fix's own spec: the
    first clause's "could not run" is reported speech, but the SECOND,
    separate `pytest -q` command is a real instruction and must keep its
    ordinary (non-"reported", non-"negated") inline-code position."""
    text = "They could not run pytest. Run `pytest -q` yourself and report the result."
    matches = guard.classify_text(text, cwd=str(repo))
    q_matches = [m for m in matches if m.matched_text.strip() == "pytest -q"]
    assert q_matches, "expected the second, genuinely imperative command to still match"
    assert all(m.position == "inline_code" for m in q_matches)
    # SCOPE NOTE (2026-07-28): this assertion read "no match ANYWHERE in the
    second_command_start = min(m.span[0] for m in q_matches)
    assert all(m.position not in ("reported", "negated")
               for m in matches if m.span[0] >= second_command_start)


def test_classify_text_reported_speech_later_imperative_still_denies(repo):
    """The recall guard above, pinned at the position value the deny gate
    actually reads. Its backticked instruction classifies "inline_code",
    which never satisfies the consumer's ``position == "imperative"`` gate
    on its own -- so on its own it cannot show that a real dispatch-blocking
    invocation SURVIVES an earlier reported-speech clause. This shape can:
    the first line is relabelled "reported" while the second stays
    "imperative", i.e. the guard still denies exactly the command it should.
    """
    text = ("They stated they could not run pytest to confirm.\n"
            "Run pytest -q yourself and report the result.")
    positions = [m.position for m in guard.classify_text(text, cwd=str(repo))]
    assert positions == ["reported", "imperative"]


def test_classify_text_reported_speech_true_positives_still_block(repo):
    cases = [
        "Run pytest across your changes.",
        "Run python -m pytest across your changes.",
        "Run npm test across your changes.",
        "Please re-run pytest to confirm the flake is gone.",
    ]
    for text in cases:
        matches = guard.classify_text(text, cwd=str(repo))
        assert matches, f"expected a match in: {text!r}"
        assert any(m.position == "imperative" for m in matches), text

    fenced = "Run this:\n\n```\npytest\nnpm test\n```\n"
    fenced_matches = guard.classify_text(fenced, cwd=str(repo))
    assert fenced_matches
    assert all(m.position == "fenced_code" for m in fenced_matches)


def test_classify_text_reported_speech_word_boundary_hyphen_compound_no_false_positive(repo):
    """Word-boundary discipline: a new reported-speech cue token embedded in
    an unrelated hyphenated compound word must NOT fire, and must NOT
    suppress a genuinely imperative command later in the same clause. Repo
    has already been bitten by exactly this shape once (``\\bverify\\b``
    firing inside ``re-verify``, fixed in c235a02c) -- this pins the same
    discipline for the new cue set (``never``/``ran``/``run`` etc.).

    "never-say-die" hyphenates ``never`` against ``-say-die``, NOT against
    a running verb -- ``_REPORTED_SPEECH_RE`` requires whitespace (not a
    hyphen) directly between the modal-negation word and the running verb,
    so this must not be mistaken for "never ran"/"never run".

    POLARITY FLIP (2026-07-28): this assertion originally read
    ``position == "imperative"``. At the time it was written, ``"reported"``
    was the only non-``"imperative"`` value that existed, so the assertion
    was really standing in for "did NOT get mislabeled reported-speech" --
    it was never meant to certify that this sentence IS an imperative, and
    it is not one: "an attitude toward running pytest daily" is a noun
    phrase, "running pytest" the gerund object of the preposition "toward",
    with no instruction anywhere in it. Now that ``"descriptive"`` exists as
    a THIRD non-imperative value (structural clause-head predicate,
    ``_cue_is_clause_head``), the stale assertion is itself a false-positive
    class the guard now correctly declines to deny. Flipped to assert
    NOT-``"reported"`` (the property this test actually verifies) and
    ``"descriptive"`` (the now-correct label) -- do NOT restore
    ``"imperative"`` here, that was the bug, not a regression target."""
    text = "It's a never-say-die attitude toward running pytest daily."
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, "expected the pytest mention to still be a match"
    assert all(m.position != "reported" for m in matches)
    assert all(m.position == "descriptive" for m in matches)


# lexical-only leg (``_IMPERATIVE_CUE_RE.search(clause)`` with no notion of

_CLAUSE_HEAD_CASES = [
    ("run ", True),
    ("Run ", True),
    ("Please run ", True),
    ("please run ", True),
    ("Then run ", True),
    ("re-run ", True),
    ("- run ", True),
    ("Before you report back, run ", True),
    ("Neither consumer may run ", False),
    ("they could not run ", False),
    ("other sessions are running ", False),
    ("the tests should run ", False),
    ("other sessions were repeatedly running ", False),
    ("CI is currently running ", False),
    ("Peer sessions run ", False),
    ("It's a never-say-die attitude toward running ", False),
]


@pytest.mark.parametrize("clause,expected", _CLAUSE_HEAD_CASES)
def test_cue_is_clause_head_direct_cases(clause, expected):
    assert guard._cue_is_clause_head(clause) is expected, clause


_FALSE_POSITIVE_DENIAL_REPROS = [
    "Neither consumer may run pytest directly.",
    "other sessions are running pytest against this shared worktree, so a "
    "wide run may show flakes.",
    "other sessions were repeatedly running pytest on this branch.",
    "CI is currently running pytest against main.",
    "Peer sessions run pytest on a shared worktree.",
]


@pytest.mark.parametrize("text", _FALSE_POSITIVE_DENIAL_REPROS)
def test_classify_text_subject_modal_or_copula_governed_mention_is_descriptive(
    text, repo,
):
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, f"expected a match to still be returned for: {text!r}"
    assert all(m.position == "descriptive" for m in matches), text
    assert all(m.position != "imperative" for m in matches), text


def test_classify_text_modal_capability_negation_still_reported_not_descriptive(repo):
    """A modal-capability-negation shape ("could not run") is caught by the
    PRE-EXISTING ``_REPORTED_SPEECH_RE`` relabeling before the new
    structural check ever runs -- reported-speech precedence (already pinned
    elsewhere) must not be disturbed by the new predicate stacking underneath
    it."""
    text = "They stated plainly they could not run pytest to confirm."
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches
    assert all(m.position == "reported" for m in matches)


_TRUE_POSITIVE_CLAUSE_HEAD_SHAPES = [
    "run pytest",
    "Run the full pytest suite",
    "- run pytest",
    "Then run pytest",
    "Please run pytest",
    "re-run pytest",
    "do pytest",
]


@pytest.mark.parametrize("text", _TRUE_POSITIVE_CLAUSE_HEAD_SHAPES)
def test_classify_text_clause_head_shapes_still_imperative(text, repo):
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, f"expected a match for: {text!r}"
    assert any(m.position == "imperative" for m in matches), text


_SENTENCE_FINAL_PUNCTUATION_RECALL = [
    "run pytest.",
    "Then run pytest.",
    "run npm test.",
    "run python3 -m pytest.",
    "Please re-run pytest.",
    "run pytest, then report back.",
    "run pytest; it should be green.",
]


@pytest.mark.parametrize("text", _SENTENCE_FINAL_PUNCTUATION_RECALL)
def test_classify_text_still_blocks_across_sentence_final_punctuation(repo, text):
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, f"expected a match for: {text!r}"
    assert any(m.position == "imperative" for m in matches), text


@pytest.mark.parametrize("text", [
    "run pytest tests/test_foo.py.",
    "run pytest tests/test_foo.py::test_bar.",
])
def test_sentence_punctuation_strip_preserves_path_scoping(repo, text):
    matches = guard.classify_text(text, cwd=str(repo))
    assert not matches, f"scoped invocation should not be reported: {text!r}"


def test_sentence_punctuation_strip_preserves_span_offsets(repo):
    """The normalization replaces rather than deletes, because every reported
    span is an offset into the ORIGINAL text -- a length-changing strip would
    slide every subsequent match off its real position."""
    text = "Notes.\nrun pytest.\n"
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches
    start, end = matches[0].span
    assert text[start:end].startswith("pytest"), text[start:end]


# 2026-07-28 -- ``_PROSE_NEGATIVE_RE`` is NOT dead code.

@pytest.mark.parametrize("text", [
    "run counts is in pytest testpaths",
    "we run it as a pytest oracle",
    "you can run that, backed by a re-runnable pytest node id",
])
def test_prose_negative_gate_survives_a_clause_initial_cue(repo, text):
    matches = guard.classify_text(text, cwd=str(repo))
    assert all(m.position != "imperative" for m in matches), (
        f"{text!r} classified imperative -- _PROSE_NEGATIVE_RE regressed"
    )


# 2026-07-28 review (P1) -- ``\n`` briefly added to ``_CLAUSE_BOUNDARY_RE``
# ``_REPORTED_SPEECH_RE.search(clause)``, which is deliberately designed to

def test_classify_text_reported_speech_survives_line_break_before_cue(repo):
    text = (
        "They said they could not\n"
        "run pytest to confirm the regression is fixed."
    )
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, "expected the pytest mention to still be a match"
    assert all(m.position == "reported" for m in matches)
    assert all(m.position != "imperative" for m in matches)


def test_position_imperative_bare_line_negation_too_far_above_still_holds(repo):
    """Re-pin the fixture the ``\\n`` boundary was originally added to fix,
    now that the boundary lives in ``_cue_is_clause_head`` instead of the
    shared ``_CLAUSE_BOUNDARY_RE``: 400 characters of unrelated padding on a
    prior line must not read as a subject in front of a genuinely
    clause-initial "Run pytest" on the line after it."""
    padding = "x" * 400
    text = f"Do not delete the config file.\n{padding}\nRun pytest across your changes."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


def test_strip_sentence_punctuation_preserves_quoted_internal_punctuation(repo):
    text = "run pytest -m 'not slow: fast'."
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, "expected a match"
    assert matches[0].matched_text == "pytest -m 'not slow: fast'"
    assert matches[0].position == "imperative"


# comma is NOT measured off by ``_FRONTED_ADVERBIAL_BOUNDARY_RE`` (comma-only
# DECISION, not an oversight: widening the predicate to strip a leading

def test_position_fronted_adverbial_without_comma_stays_descriptive_deliberate_gap(repo):
    text = "After merging your change run pytest to confirm."
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, "expected the pytest mention to still be a match"
    assert all(m.position == "descriptive" for m in matches)
    assert all(m.position != "imperative" for m in matches)


@pytest.fixture
def precision_repo(repo):
    (repo / "coordinator_core" / "frontmatter" / "tests" / "sub").mkdir(parents=True, exist_ok=True)
    return repo


def test_classify_command_precision_directory_scoped_pytest_one_match(precision_repo):
    matches = guard.classify_command_precision(
        "pytest coordinator_core/frontmatter/tests/sub", cwd=str(precision_repo)
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.detected == "pytest"
    assert m.directory_args == ["coordinator_core/frontmatter/tests/sub"]
    assert m.position == "imperative"


def test_classify_command_precision_node_id_scoped_no_match(precision_repo):
    assert guard.classify_command_precision(
        "pytest coordinator_core/frontmatter/tests/sub/test_x.py::test_case",
        cwd=str(precision_repo),
    ) == []


def test_classify_command_precision_file_scoped_no_match(precision_repo):
    assert guard.classify_command_precision(
        "pytest coordinator_core/frontmatter/tests/sub/test_x.py",
        cwd=str(precision_repo),
    ) == []


def test_classify_command_precision_suite_shaped_is_not_this_apis_business(precision_repo):
    assert guard.classify_command(
        "pytest coordinator_core/", cwd=str(precision_repo)
    ) != []
    assert guard.classify_command_precision(
        "pytest coordinator_core/", cwd=str(precision_repo)
    ) == []


def test_classify_command_precision_no_cwd_fails_open(precision_repo):
    assert guard.classify_command_precision(
        "pytest coordinator_core/frontmatter/tests/sub"
    ) == []


def test_classify_text_precision_no_cwd_fails_open():
    assert guard.classify_text_precision(
        "Run pytest coordinator_core/frontmatter/tests/sub over your changes."
    ) == []


def test_classify_text_precision_fenced_directory_scoped_pytest(precision_repo):
    text = (
        "Run this:\n"
        "```\n"
        "pytest coordinator_core/frontmatter/tests/sub\n"
        "```\n"
    )
    matches = guard.classify_text_precision(text, cwd=str(precision_repo))
    assert len(matches) == 1
    assert matches[0].position == "fenced_code"
    assert matches[0].directory_args == ["coordinator_core/frontmatter/tests/sub"]
    start, end = matches[0].span
    assert text[start:end] == "pytest coordinator_core/frontmatter/tests/sub"


def test_classify_text_precision_negated_mention_still_classified(precision_repo):
    text = "Do not run `pytest coordinator_core/frontmatter/tests/sub` yourself."
    matches = guard.classify_text_precision(text, cwd=str(precision_repo))
    assert len(matches) == 1
    assert matches[0].position == "negated"


def test_classify_text_precision_descriptive_mention_still_classified(precision_repo):
    text = "Peer sessions run pytest coordinator_core/frontmatter/tests/sub on a shared worktree."
    matches = guard.classify_text_precision(text, cwd=str(precision_repo))
    assert len(matches) == 1
    assert matches[0].position == "descriptive"


def test_classify_text_precision_suite_shaped_no_match(precision_repo):
    text = "Run pytest across the whole tree to confirm nothing regressed."
    assert guard.classify_text(text, cwd=str(precision_repo)) != []
    assert guard.classify_text_precision(text, cwd=str(precision_repo)) == []


def test_classify_text_precision_node_id_no_match(precision_repo):
    text = "Run `pytest coordinator_core/frontmatter/tests/sub/test_x.py::test_case` to confirm."
    assert guard.classify_text_precision(text, cwd=str(precision_repo)) == []


def test_precision_match_as_dict_shape(precision_repo):
    matches = guard.classify_command_precision(
        "pytest coordinator_core/frontmatter/tests/sub", cwd=str(precision_repo)
    )
    d = matches[0].as_dict()
    assert d["detected"] == "pytest"
    assert d["directory_args"] == ["coordinator_core/frontmatter/tests/sub"]
    assert d["position"] == "imperative"
    assert d["span"] == list(matches[0].span)
