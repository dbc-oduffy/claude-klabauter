"""Tests for the public classification API (``classify_command`` /
``classify_text`` / ``SuiteMatch``) exposed by
coordinator_core.bash_guards.check_test_suite_invocation for DR-088 layer 2.

These are additive to test_check_test_suite_invocation.py, which covers
``check()``'s hard-deny behavior -- that file's tests re-verify ``check()``
is untouched by this refactor; this file covers only the new payload-shape-
free surface.

Pure Python -- no shell spawns, no writes outside ``tmp_path``.

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-dr088-grant-spec-and-layer2-seam.md § Ask 1
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake repo root whose pytest config pins a real testpaths shape and
    a configured fast_test_cmd, so both the generic classifier and the
    Tier F/U discrimination leg are exercised."""
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


# ---------------------------------------------------------------------------
# classify_command
# ---------------------------------------------------------------------------

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
    """R1 regression guard: a configured fast_test_cmd whose own shape is
    an unscoped runner invocation (pointed exactly at the pinned testpaths
    root, per ``_is_real_scope``) must classify Tier U, never Tier F --
    the fast-key match must not launder an unscoped shape down to F. This
    reproduces makima's OWN real fast_test_cmd shape verbatim (verified
    live at dispatch time: "pytest coordinator_core/" with testpaths
    pinned to ["coordinator_core"])."""
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
    """R1 symmetry guard: a configured full_test_cmd (no fast_test_cmd tie)
    whose own shape is scoped (a real descendant of testpaths, not the
    testpaths root) must classify Tier F -- the full-key match is now
    subject to the same shape test as the fast-key match, via
    ``_tier_for_cfg_match``. Companion to
    test_classify_command_unscoped_full_cmd_match_is_tier_u below."""
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
    """R1 symmetry guard: a configured full_test_cmd whose own shape is an
    unscoped runner invocation (pointed exactly at the pinned testpaths
    root) must classify Tier U -- the full-key match must not launder an
    unscoped shape down to F, mirroring
    test_classify_command_unscoped_fast_cmd_match_now_tier_u above but for
    the full_test_cmd leg, which previously forced Tier U unconditionally
    regardless of shape and therefore never actually exercised this
    discrimination."""
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
    """The other half of ``_runner_recognized``'s ``False``: an opaque
    wrapper declared as the repo's full tier stays Tier U. This is the
    fail-closed default ``_argv_is_inert`` must NOT have widened -- a
    wrapper's breadth cannot be read off its shape, and ``run-suite.sh``
    names neither a known runner nor the substring ``test``."""
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


# ---------------------------------------------------------------------------
# classify_text -- zero/single/multi match prose
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# position discrimination
# ---------------------------------------------------------------------------

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
    """Same defect class as the bare-line P1, with backticks: a negation
    marker on the line BEFORE an inline-code span must still flip it to
    "negated" -- previously the inline-code window was confined to the
    span's own line."""
    text = "Do not run this:\n`pytest`"
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "negated"


def test_position_imperative_inline_no_negation_anywhere(repo):
    """Anti-over-correction: a genuinely imperative inline-code command with
    no negation marker anywhere nearby must still report "inline_code"."""
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
    """Guard against over-correction: a genuinely imperative bare-line
    command with no negation marker anywhere nearby must still report
    "imperative", not accidentally flip to "negated"."""
    text = "Before you report back, run pytest across your changes."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


def test_position_imperative_bare_line_negation_too_far_above(repo):
    """A negation marker outside the look-back window must NOT bleed onto a
    later, unrelated imperative command -- pins the chosen window so it
    doesn't grow unboundedly and poison a genuinely imperative command
    several paragraphs after an earlier, unrelated negated mention."""
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
    """The false-positive class DoE flagged: an executor brief quoting the
    ENTIRE deny-list verbatim, inside a fence, under a delete instruction --
    every match must report a non-imperative position so the caller can
    choose not to deny the very dispatch that fixes the problem."""
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


# ---------------------------------------------------------------------------
# 2026-07-25 P1 regression -- bare-line narrative mentions of a runner name
# must NOT classify as a command. Repro:
# state/subagent-share/2d4d6703-83aa-44c5-83f9-169d0367193d/... (dispatch-
# guard false-positive corpus); root cause is documented on
# ``_bare_line_is_command_shaped``.
# ---------------------------------------------------------------------------

def test_classify_text_no_match_narrative_testpaths_mention(repo):
    """Case 1 of the repro: a runner name appearing as the object of an
    ordinary preposition ("... is in pytest ... and"), not as a command."""
    text = (
        "`coordinator/bin/tests` is in pytest `testpaths` and\n"
        '`python_files = ["test_*.py"]`, so hyphenated names are never '
        "collected."
    )
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_no_match_narrative_adjective_mention(repo):
    """Case 2 of the repro: a runner name used adjectivally in a heading,
    with no execution verb anywhere on the line."""
    text = "# Task — re-port the loader as a pytest oracle"
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_no_match_narrative_noun_phrase_mention(repo):
    """Case 3 of the repro: a runner name inside a noun phrase describing
    what a gate accepts, not an instruction to run anything."""
    text = (
        "The gate can only accept a confirmation backed by a re-runnable "
        "pytest node id. So we need genuine standing coverage that pins "
        "the property, not a rubber stamp."
    )
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_still_blocks_unscoped_run_instruction(repo):
    """Must-still-block case 1: a genuine imperative instruction, with an
    ordinary-English tail after the runner, must still classify Tier U."""
    text = "Then run python3 -m pytest to check everything still works."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].detected == "pytest"
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_unscoped_verify_instruction(repo):
    """Must-still-block case 2: an unscoped verify-with instruction whose
    trailing argument is the repo's own testpaths root -- scoped-looking
    but actually the whole suite -- must still classify Tier U."""
    text = "Verify with pytest coordinator_core/ before reporting back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].position == "imperative"


def test_bare_line_is_command_shaped_direct_cases():
    """Direct unit coverage of the gating helper -- the exact prefixes from
    the repro/must-block corpus, pinned independent of any surrounding
    classify_text plumbing."""
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


# ---------------------------------------------------------------------------
# 2026-07-25 review (guard-precision slice, coordinator:code-reviewer,
# state/subagent-share/2d4d6703-83aa-44c5-83f9-169d0367193d/
# coordinatorcode-reviewer-208dd00a.md) -- Findings 1-4: concrete evasion
# shapes constructed against the clause-scoping / cue-vocabulary / lead-
# strip mechanisms above. Each direct-case pin here documents the class of
# behavior fixed, not just the one reported bug-report string, per
# Finding 5.
# ---------------------------------------------------------------------------

def test_bare_line_is_command_shaped_colon_headed_cue(repo):
    """Finding 1 (P0): a colon-headed label instruction must not discard
    the cue word into the segment before the (former) clause-boundary
    split -- ``:`` is no longer a clause boundary."""
    assert guard._bare_line_is_command_shaped("Run: ") is True
    assert guard._bare_line_is_command_shaped("Verify: ") is True
    assert guard._bare_line_is_command_shaped("Command: ") is False
    matches = guard.classify_text("Run: pytest before you report back.", cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


def test_bare_line_is_command_shaped_broadened_cue_vocabulary():
    """Finding 2 (P0): ordinary command-issuing English outside the
    original 10-verb closed set must now be detected."""
    assert guard._bare_line_is_command_shaped("please ") is True
    assert guard._bare_line_is_command_shaped("just do ") is True
    assert guard._bare_line_is_command_shaped("kick off ") is True
    assert guard._bare_line_is_command_shaped("start ") is True


def test_bare_line_is_command_shaped_lettered_list_marker():
    """Finding 3 (P1): a lettered ordered-list marker (``a.``, ``b)``) must
    be stripped the same way a numeric marker (``1.``, ``2)``) already is."""
    assert guard._bare_line_is_command_shaped("a. ") is True
    assert guard._bare_line_is_command_shaped("b) ") is True
    # Not stripped when the "marker" is actually a real word's leading
    # letter -- only a single letter directly followed by "."/")" counts.
    assert guard._bare_line_is_command_shaped("Reference ") is False


def test_bare_line_is_command_shaped_markdown_markers_not_command():
    """Finding 4 (P2): markdown heading/blockquote markers are structural
    prose characters on a bare line, not shell-prompt lead-ins."""
    assert guard._bare_line_is_command_shaped("# ") is False
    assert guard._bare_line_is_command_shaped("> ") is False


def test_classify_text_no_match_bare_markdown_heading_names_runner(repo):
    """Full-pipeline pin for Finding 4: a bare markdown heading naming the
    runner as its very first word must not classify as a command."""
    text = "# pytest configuration notes"
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_no_match_bare_blockquote_names_runner(repo):
    """Full-pipeline pin for Finding 4: a bare blockquote naming the runner
    as its very first word must not classify as a command."""
    text = "> pytest already covers this."
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_text_still_blocks_colon_headed_instruction(repo):
    """Full-pipeline pin for Finding 1: a colon-headed label instruction
    naming the whole suite must still classify Tier U."""
    text = "Verify: pytest coordinator_core/ before reporting back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_non_closed_set_imperative(repo):
    """Full-pipeline pin for Finding 2: an ordinary command-issuing phrase
    outside the original closed verb set must still classify Tier U."""
    text = "Please pytest the whole tree before you report back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_lettered_list_command(repo):
    """Full-pipeline pin for Finding 3: a lettered-list command line must
    still classify Tier U."""
    text = "a. Scope your tests.\nb. pytest\nc. Report back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "imperative"


# ---------------------------------------------------------------------------
# 2026-07-25 defect A -- imperative cue must be CLAUSE-scoped, not
# whole-prefix. Repro: cross-repo/inbox/2026-07-25-doe-claude-em-dispatch-
# suite-classifier-two-live-defects.md.
# ---------------------------------------------------------------------------

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
    """Full-pipeline pin: the defect-A sentence verbatim must not classify
    -- the ``run`` in "may run the test tier" is part of an earlier
    prohibition sentence, not an instruction governing the later mention of
    ``pytest``."""
    text = (
        "Neither consumer may run the test tier or block the ceremony. "
        "A start ceremony that invokes pytest is a several-minute stall on "
        "every session boot."
    )
    assert guard.classify_text(text, cwd=str(repo)) == []


# ---------------------------------------------------------------------------
# 2026-07-25 defect B -- ``make``'s suite target must be the FIRST non-flag,
# non-assignment positional, not any positional anywhere in the segment.
# Zero prior ``make`` coverage existed in this corpus before this block.
# ---------------------------------------------------------------------------

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
    """Defect B repro: ``make`` used as an ordinary English verb, with
    ``test`` appearing later in the segment as an unrelated noun, must not
    be misread as ``make test`` -- the suite target must be the FIRST
    positional, and here it is "the", not "test"."""
    assert guard.classify_command(
        "make the exemplar useless: something about which branch the test covers.",
        cwd=str(repo),
    ) == []


def test_classify_command_make_change_and_add_a_test_no_match(repo):
    assert guard.classify_command(
        "make the change and add a test.", cwd=str(repo)
    ) == []


def test_classify_make_direct_adjacency_unit_cases():
    """Direct unit coverage of ``_classify_make``'s adjacency requirement."""
    assert guard._classify_make(["test"]) == "make test"
    assert guard._classify_make(["-j4", "test"]) == "make test"
    assert guard._classify_make(["CC=gcc", "check"]) == "make check"


# ---------------------------------------------------------------------------
# 2026-07-26 defect -- a bare-word ``do`` cue fired on the ordinary English
# negator lead-in "do not <verb other than run>", licensing an unrelated
# later ``make``/suite-target mention on the same clause as command-shaped.
# ``_NEGATION_RE``'s ``\bdo not run\b`` marker never fires here because its
# governing verb is "weaken", not "run" -- this is not a mislabeled negated
# match, it is a match that should never have been detected at all. This
# dispatch was itself blocked by the defect on its first attempt.
# ---------------------------------------------------------------------------

def test_bare_line_is_command_shaped_do_not_non_run_verb_no_match():
    """Direct unit pin: a bare ``do`` cue must not fire when immediately
    followed by ``not`` -- "do not weaken ..." is a prohibition, not an
    imperative licensing the runner mention later in the clause."""
    prefix = "do not weaken the guard to "
    assert guard._bare_line_is_command_shaped(prefix) is False


def test_classify_text_no_match_do_not_weaken_guard_repro(repo):
    """Full-pipeline pin for the live repro: 'do not weaken the guard to
    make tests pass' must not classify -- ``make`` here is a suite-target-
    adjacent mention inside a prohibition sentence about the guard itself,
    not an instruction to run ``make tests``."""
    text = "do not weaken the guard to make tests pass"
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_bare_line_is_command_shaped_do_not_run_still_matches():
    """The veto is narrow: "do not run" still carries real signal via the
    independent ``run`` cue, unaffected by the ``do`` lookahead."""
    prefix = "do not run "
    assert guard._bare_line_is_command_shaped(prefix) is True


def test_classify_text_still_blocks_do_pytest_instruction(repo):
    """The veto must not over-broaden: a genuine ``do <runner>`` imperative
    (no intervening "not") stays detected."""
    text = "please do pytest the whole tree"
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].detected == "pytest"
    assert guard._classify_make(["the", "exemplar", "useless", "test"]) is None
    assert guard._classify_make(["the", "change", "and", "add", "a", "test"]) is None


# ---------------------------------------------------------------------------
# Pin (not a fix, per 50f0def8): a ``|``-delimited prose enum must not be
# shredded by the compound-command splitter into a bare, unscoped-looking
# runner segment.
# ---------------------------------------------------------------------------

def test_classify_text_pipe_delimited_prose_enum_not_shredded_into_bare_runner(repo):
    """A dispatch brief legitimately pinning a ``|``-delimited contract enum
    (inline code, matching the real trigger reported alongside defects A/B)
    must not have its own ``|`` characters mistaken for compound-command
    segment separators, which would shred it into a bare, unscoped-looking
    ``pytest`` segment. Fixed by 50f0def8 -- this is a pin, not a new fix."""
    text = (
        "The dispatch brief pins a contract enum "
        "`runner: pytest | node-test | bats | unknown` for this wave."
    )
    assert guard.classify_text(text, cwd=str(repo)) == []


# ---------------------------------------------------------------------------
# Tier discrimination via classify_text
# ---------------------------------------------------------------------------

def test_classify_text_tier_f_vs_u(repo, monkeypatch):
    """Updated 2026-07-25 for R1 (cross-repo/inbox/2026-07-25-doe-claude-
    em-validate-tier-u-shape-ruling.md): the original fixture command here
    ("python3 -m pytest coordinator_core/") pointed exactly at the pinned
    testpaths root -- an unscoped-runner-invocation SHAPE -- so it now
    correctly classifies Tier U, not Tier F (see
    test_classify_command_unscoped_fast_cmd_match_now_tier_u). Switched to
    a genuinely scoped fast_test_cmd so this test still covers the
    legitimate Tier F route alongside the Tier U default."""
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


# ---------------------------------------------------------------------------
# Tier-T scoped invocations produce no matches
# ---------------------------------------------------------------------------

def test_classify_text_scoped_invocation_no_match(repo):
    text = "Run `pytest coordinator_core/frontmatter/tests/test_x.py` for your chunk."
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_classify_command_scoped_node_id_no_match(repo):
    assert guard.classify_command(
        "pytest coordinator_core/frontmatter/tests/test_x.py::test_case", cwd=str(repo)
    ) == []


# ---------------------------------------------------------------------------
# check() regression -- unchanged by this refactor
# ---------------------------------------------------------------------------

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
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    out = guard.check(_payload(
        "pytest coordinator_core/frontmatter/tests/test_x.py", repo, agent_id="a0123456789abcdef"
    ))
    assert out is None


def test_check_still_denies_unscoped_subagent_command(repo, monkeypatch):
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    out = guard.check(_payload("pytest", repo, agent_id="a0123456789abcdef"))
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("Run the tests you actually touched:")


# ---------------------------------------------------------------------------
# 2026-07-26 P3 regression -- "re-verify" false-positive on a dispatch-brief
# sentence instructing a subagent NOT to redo work already confirmed.
# Repro: state/bug-backlog/2026-07-26-dispatch-suite-guard-classify-text-
# false-bd5afe033da4.yaml. Root cause: ``_IMPERATIVE_CUE_RE``'s bare
# ``\bverify\b`` alternative matched inside the compound word "re-verify"
# (``\b`` fires at the hyphen/letter boundary same as at whitespace), so a
# report-only, read-only subagent brief was misclassified as command-shaped
# even though the sentence opens with "Do NOT" and the runner mention sits
# deep inside an unrelated parenthetical list. Fixed via a negative
# lookbehind scoped to ``verify``/``verifying`` only (see the cue-list
# comment) -- NOT via ``_NEGATION_RE`` (that marker requires "do not run",
# not bare "do not", and per the module/consumer contract, negation only
# relabels an already-detected match's ``position``; it does not gate
# detection). The correct fix is that no match is produced at all -- the
# consumer (DoE's block-dispatch-suite-invocation.py) has nothing to see.
# ---------------------------------------------------------------------------

def test_classify_text_no_match_re_verify_settled_claims_repro(repo):
    """Exact repro sentence: no match should be produced at all -- the
    bare-line pass must never reach command-shape classification here, not
    merely mark the (nonexistent) match "negated". This is a stronger bar
    than position-relabeling because ``check()``'s only override is a
    blast-radius-wide repo sentinel or an env var, and DoE's consumer gates
    exclusively on ``position == "imperative"`` -- a spurious match with any
    OTHER position would still show up in ``classify_text``'s return value
    and could still be mishandled by a caller that doesn't filter by
    position, whereas an empty match list is unambiguous."""
    text = (
        "Do NOT re-verify claims your prior pass already confirmed clean "
        "(769 lines, grep→0, apply_base's 4 consumers, the pytest "
        "result, the forwarders). Those are settled."
    )
    assert guard.classify_text(text, cwd=str(repo)) == []


def test_bare_line_is_command_shaped_re_verify_prefix_not_command_shaped():
    """Direct unit pin on the gating helper: the exact prefix (everything
    before "pytest" on the repro line) must not be judged command-shaped."""
    prefix = (
        "Do NOT re-verify claims your prior pass already confirmed clean "
        "(769 lines, grep→0, apply_base's 4 consumers, the "
    )
    assert guard._bare_line_is_command_shaped(prefix) is False


def test_imperative_cue_re_does_not_match_re_verify():
    """Pins the exact regex-level defect: ``verify``/``verifying`` must not
    fire on the compound "re-verify", "re-verifying"."""
    assert guard._IMPERATIVE_CUE_RE.search("re-verify") is None
    assert guard._IMPERATIVE_CUE_RE.search("re-verifying") is None


def test_classify_text_no_match_re_verify_short_form(repo):
    """A shorter, less parenthetical-heavy variant of the same shape --
    guards against a fix that only special-cases the exact repro string."""
    text = "Do not re-verify the pytest result, it is already settled."
    assert guard.classify_text(text, cwd=str(repo)) == []


# ---------------------------------------------------------------------------
# True-positive acceptance bar -- the fix above must not weaken real
# detection. Equal-weight acceptance criteria per the fix's own mandate.
# ---------------------------------------------------------------------------

def test_classify_text_still_blocks_bare_pytest_instruction(repo):
    """A genuinely bare, unscoped ``pytest`` instruction must still block."""
    text = "Once your change lands, run pytest to confirm nothing broke."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].detected == "pytest"
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_python_module_pytest_instruction(repo):
    """``python -m pytest coordinator/tests`` (unscoped module invocation)
    must still block."""
    text = "Please run python -m pytest coordinator_core/ before you report back."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_npm_test_instruction(repo):
    """``npm test`` (unscoped JS suite invocation) must still block."""
    text = "Kick off npm test once the build finishes."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].detected == "npm test"
    assert matches[0].position == "imperative"


def test_classify_text_still_blocks_fenced_multiline_suite_command(repo):
    """A fenced multi-line block containing a genuine suite-shaped command
    must still classify Tier U / imperative."""
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
    """The lookbehind fix is scoped to ``verify``/``verifying`` only --
    "re-run pytest" genuinely means "invoke the runner again" and must stay
    real signal, not be swept up by the same exclusion."""
    text = "Please re-run pytest to confirm the flake is gone."
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].tier == "U"
    assert matches[0].detected == "pytest"
    assert matches[0].position == "imperative"


def test_classify_text_negation_on_preceding_line_still_flips_position(repo):
    """Second-order case: a genuine suite-shaped command whose negation
    marker sits on the line BEFORE it (not the same line) must still
    classify as a match (detection is not gated by negation) but with
    ``position == "negated"`` -- pre-existing lookback behavior, pinned
    here alongside the re-verify fix so the two mechanisms (command-shape
    gating vs. negation position-labeling) aren't conflated."""
    text = "Do not run this:\npytest -v"
    matches = guard.classify_text(text, cwd=str(repo))
    assert len(matches) == 1
    assert matches[0].position == "negated"


# ---------------------------------------------------------------------------
# position discrimination -- "reported" (2026-07-28 field report)
# ---------------------------------------------------------------------------
#
# Field-report repro: "They stated plainly they could not run pytest to
# confirm." classified position="imperative" (denying) despite being pure
# reported speech about someone ELSE's inability to run something -- no
# instruction anywhere in the sentence. ``_NEGATION_RE`` could not fix this:
# it only recognizes "do not run"/"don't run"/"never run", not
# modal-capability negation ("could not run") or past-tense reporting
# frames ("they stated"). The downstream consumer (DoE-claude's
# ``block-dispatch-suite-invocation.py``) denies a dispatch iff any match
# has ``position == "imperative"``, so this false positive blocked
# legitimate Agent dispatches.

def test_classify_text_reported_speech_field_report_repro(repo):
    """Exact sentence from the 2026-07-28 field report."""
    text = "They stated plainly they could not run pytest to confirm."
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, "expected the reported-speech pytest mention to still be a match"
    assert all(m.position == "reported" for m in matches)
    assert all(m.position != "imperative" for m in matches)


@pytest.mark.parametrize("phrase", [
    # These bare (non-fenced, non-inline-code) phrasings keep "run" as the
    # modal-negation's governing verb, which is what ALSO satisfies the
    # pre-existing bare-line command-shape gate (``_bare_line_is_command_
    # shaped``, unrelated to this fix) -- exactly the shape of the field
    # report's own repro. This is real coverage, not an artifact: it is
    # precisely the shape that produced the original false "imperative".
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
    # "ran" (past tense) is not itself a member of the bare-line command-
    # shape gate's cue vocabulary (only "run"/"running" are), so this
    # variant is exercised via inline code instead -- fenced/inline spans
    # are classified unconditionally, without needing a cue to look
    # command-shaped in the first place.
    "They never ran `pytest` against that branch.",
])
def test_classify_text_reported_modal_capability_negation_variants(repo, phrase):
    """Every modal/capability-negation shape named in the field report --
    each must classify "reported", never "negated" and never a raw base
    position ("imperative"/"inline_code") that a caller could mistake for
    an instruction."""
    matches = guard.classify_text(phrase, cwd=str(repo))
    assert matches, f"expected a pytest match in: {phrase!r}"
    assert all(m.position == "reported" for m in matches), phrase


@pytest.mark.parametrize("phrase", [
    # Backticked so the match is emitted via the inline-code pass, which
    # (unlike the bare-line pass) never gates detection on an imperative
    # cue being present in the preceding prefix -- these reporting frames
    # ("they stated", "they said", "reported that", "noted that") contain
    # no run/execute/invoke cue of their own, so a bare (non-code) mention
    # here would not even reach the classifier at all (a separate,
    # pre-existing gate, not something this fix changes).
    "They stated that `pytest` could not confirm the fix.",
    "They said `pytest` never ran in CI last night.",
    "They reported that `pytest` failed to run in CI.",
    "The QA lead noted that `pytest` never ran in CI.",
    "Reported that `pytest` never ran during the outage.",
])
def test_classify_text_reported_speech_framing_variants(repo, phrase):
    """Past-tense reporting frames ("they said/stated/reported", "reported
    that", "noted that") -- narrative claims about a run, not instructions."""
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
    # No match at or after the second command may be "reported"/"negated" as a
    # side effect of the reported-speech window reaching too far forward.
    #
    # SCOPE NOTE (2026-07-28): this assertion read "no match ANYWHERE in the
    # text is reported/negated", justified by "the only match here is the
    # second command". That premise was itself a bug -- the first sentence's
    # "pytest." tokenized as argv[0] == "pytest." and was silently dropped, so
    # the first clause contributed no match to be labelled. With token-final
    # sentence punctuation now normalized (``_strip_sentence_punctuation``) it
    # does match, and "They could not run pytest" is reported speech, so
    # "reported" is the correct label for it and non-denying either way. The
    # assertion is therefore scoped forward to what it was always about --
    # non-suppression of the LATER command -- rather than relaxed.
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
    """Recall floor: ordinary true-positive imperative shapes (bare
    ``pytest``, ``python -m pytest``, ``npm test``, a fenced multi-line
    block, ``re-run pytest``) must be entirely unaffected by the new
    "reported" cue set -- this fix must not be achievable by making the
    guard quieter."""
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


# ---------------------------------------------------------------------------
# 2026-07-28 -- structural clause-head predicate (``_cue_is_clause_head``),
# replacing the bag-of-words position="imperative" call for a bare-line cue
# match. Four independent false-positive denials landed in five days on the
# lexical-only leg (``_IMPERATIVE_CUE_RE.search(clause)`` with no notion of
# what governs the runner token); this table pins the discriminator: a real
# imperative has the cue AS its clause head (no subject, no auxiliary/modal/
# copula precedes it), every repro instead has one directly governing the
# cue. Per DR-088 layer 2's negative spec, a withheld "imperative" is
# EMITTED as "descriptive", never dropped -- so every case here still
# asserts a non-empty match list, only the ``position`` value differs.
# ---------------------------------------------------------------------------

_CLAUSE_HEAD_CASES = [
    # (clause text ending right before the runner token, expected _cue_is_clause_head)
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
    # Round-2 gap corpus (2026-07-28): adjacency-only licensed all three of
    # these, since none has the aux/modal/copula word directly touching the
    # cue -- an intervening manner adverb in the first two, no auxiliary at
    # all in the third (a bare subject + finite verb, the same sentence
    # shape as the reported live incident, merely de-progressivized). The
    # broad "any substantive leftover blocks" predicate catches all three
    # without a wider lexicon -- presence of a subject is what discriminates,
    # not which word happens to sit next to the cue.
    ("It's a never-say-die attitude toward running ", False),
    # Polarity flip (2026-07-28, PM-authorized): "toward running pytest" is
    # a gerund object of a preposition, not a governed finite verb -- a
    # governing preposition is exactly the kind of leftover substance this
    # predicate exists to detect. This case previously expected True; that
    # was itself the false-positive class this fix eliminates, not a recall
    # case to protect. Do NOT flip it back to True.
]


@pytest.mark.parametrize("clause,expected", _CLAUSE_HEAD_CASES)
def test_cue_is_clause_head_direct_cases(clause, expected):
    """Direct unit coverage of the structural predicate -- every false-
    positive repro (subject/modal/copula governing the cue) plus every
    true-positive shape (cue as clause head, modulo list markers, a
    fronted adverbial phrase set off by a comma, and an attached ``re-``
    prefix)."""
    assert guard._cue_is_clause_head(clause) is expected, clause


_FALSE_POSITIVE_DENIAL_REPROS = [
    "Neither consumer may run pytest directly.",
    "other sessions are running pytest against this shared worktree, so a "
    "wide run may show flakes.",
    # Round-2 gap corpus (2026-07-28): these three still classified
    # "imperative" under the adjacency-only predicate -- an intervening
    # manner adverb defeats an aux-adjacency check, and the third has no
    # auxiliary at all (subject + bare finite verb), the same sentence
    # shape as the incident that triggered this work, rephrased out of the
    # progressive.
    "other sessions were repeatedly running pytest on this branch.",
    "CI is currently running pytest against main.",
    "Peer sessions run pytest on a shared worktree.",
]


@pytest.mark.parametrize("text", _FALSE_POSITIVE_DENIAL_REPROS)
def test_classify_text_subject_modal_or_copula_governed_mention_is_descriptive(
    text, repo,
):
    """Full-pipeline pin for the 2026-07-28 false-positive class: a bare-line
    mention where a subject noun phrase plus a modal ("may run") or a
    copula/progressive ("are running") governs the cue must NOT deny --
    the match is still returned (never suppressed, DR-088 layer 2 negative
    spec), labeled ``"descriptive"`` rather than ``"imperative"``."""
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
    """Recall guard: every clause-initial imperative shape (the cue itself
    is the clause head, modulo list marker / leading adverb / attached
    ``re-`` prefix) must still classify ``"imperative"`` under the new
    structural predicate -- this is what stops the fix from overshooting
    into new false negatives."""
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, f"expected a match for: {text!r}"
    assert any(m.position == "imperative" for m in matches), text


# ---------------------------------------------------------------------------
# 2026-07-28 -- token-final sentence punctuation (``_strip_sentence_punctuation``).
#
# Pre-existing recall hole, found while validating the clause-head predicate
# and fixed separately: prose ends sentences with punctuation, argv does not.
# "run pytest." tokenized to ``argv[0] == "pytest."``, matched no known
# runner, and the match was dropped entirely -- so layer 2 fired on the
# sloppily-punctuated half of its input and missed the well-punctuated half.
# ---------------------------------------------------------------------------

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
    """An ordinary written instruction must still deny when the sentence is
    punctuated -- the single most natural way to write the very instruction
    this guard exists to catch."""
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, f"expected a match for: {text!r}"
    assert any(m.position == "imperative" for m in matches), text


@pytest.mark.parametrize("text", [
    "run pytest tests/test_foo.py.",
    "run pytest tests/test_foo.py::test_bar.",
])
def test_sentence_punctuation_strip_preserves_path_scoping(repo, text):
    """Punctuation INSIDE a token is load-bearing argv content, not sentence
    punctuation: a scoped invocation must stay scoped (and so stay allowed)
    rather than having its path mangled into a broad run."""
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


# ---------------------------------------------------------------------------
# 2026-07-28 -- ``_PROSE_NEGATIVE_RE`` is NOT dead code.
#
# It reads as redundant under the clause-head predicate, and a review pass
# proposed removing it: every example in its own docstring is independently
# caught by ``_bare_line_is_command_shaped``'s cosmetic-lead fallback, and
# disabling it leaves the whole bash_guards suite green. Both observations
# are true and the conclusion is still wrong -- they only probe clauses with
# NO imperative cue, where the fallback is what was answering all along.
#
# Its load-bearing case is a clause where a cue and a prose-negative shape
# co-occur AND the cue is clause-initial, so neither the fallback nor the
# clause-head predicate withholds: "run counts is in pytest testpaths" has
# "run" as its first word, but "pytest" there is the object of a copula, not
# the thing being run. Without this gate that clause classifies "imperative"
# and denies -- a false positive of exactly the class this file exists to
# prevent. Pinned so the redundancy hypothesis cannot land as a removal.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "run counts is in pytest testpaths",
    "we run it as a pytest oracle",
    "you can run that, backed by a re-runnable pytest node id",
])
def test_prose_negative_gate_survives_a_clause_initial_cue(repo, text):
    """A prose-negative shape must still suppress even when an imperative cue
    sits at the head of the same clause -- the one configuration in which no
    other mechanism in this classifier withholds the denying label."""
    matches = guard.classify_text(text, cwd=str(repo))
    assert all(m.position != "imperative" for m in matches), (
        f"{text!r} classified imperative -- _PROSE_NEGATIVE_RE regressed"
    )


# ---------------------------------------------------------------------------
# 2026-07-28 review (P1) -- ``\n`` briefly added to ``_CLAUSE_BOUNDARY_RE``
# broke cross-line reported-speech detection: that regex is SHARED with
# ``_REPORTED_SPEECH_RE.search(clause)``, which is deliberately designed to
# reach backwards across a line break (see the negation-lookback passes'
# own cross-line comments). Reverted; line-scoping now lives locally inside
# ``_cue_is_clause_head``, on the pre-cue text only, so it cannot again
# blind the reported-speech check to a modal-negation cue split across a
# soft-wrapped line.
# ---------------------------------------------------------------------------

def test_classify_text_reported_speech_survives_line_break_before_cue(repo):
    """The exact P1 repro from the 2026-07-28 review: a modal-negation cue
    ("could not") on one line and its governed verb ("run") on the next must
    still classify "reported", not fall through to a false "imperative" --
    the false-positive class this whole module exists to eliminate."""
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


# ---------------------------------------------------------------------------
# 2026-07-28 review (P3) -- ``_strip_sentence_punctuation`` blanked
# punctuation INSIDE a quoted argv token when that punctuation was itself
# followed by whitespace, corrupting the quoted expression's content before
# it reached the tokenizer. Fixed by making the strip quote-aware.
# ---------------------------------------------------------------------------

def test_strip_sentence_punctuation_preserves_quoted_internal_punctuation(repo):
    """A colon inside a quoted ``-m`` expression, followed by whitespace,
    must survive verbatim -- only genuinely sentence-final punctuation
    outside any quoted span is blanked."""
    text = "run pytest -m 'not slow: fast'."
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, "expected a match"
    assert matches[0].matched_text == "pytest -m 'not slow: fast'"
    assert matches[0].position == "imperative"


# ---------------------------------------------------------------------------
# 2026-07-28 review (P3, deliberate gap) -- a fronted adverbial with no
# comma is NOT measured off by ``_FRONTED_ADVERBIAL_BOUNDARY_RE`` (comma-only
# by construction), so a real imperative in this shape is demoted to
# "descriptive" rather than promoted to "imperative". This is pinned as a
# DECISION, not an oversight: widening the predicate to strip a leading
# subordinator-headed phrase would also strip it from a genuinely
# declarative clause ("After the peer sessions run pytest nightly, the
# dashboard updates") and flip THAT to a false "imperative" -- trading a
# cheap false negative (layer 3's identity leg fail-CLOSES on real argv and
# never consults this path) for the expensive false positive this module
# exists to eliminate. Do NOT "fix" this by stripping fronted adverbials.
# ---------------------------------------------------------------------------

def test_position_fronted_adverbial_without_comma_stays_descriptive_deliberate_gap(repo):
    """Pins the current, deliberately-unfixed behavior: a real imperative
    fronted by a comma-less adverbial phrase classifies "descriptive", never
    a false "imperative" -- see the rationale block above and
    ``_cue_is_clause_head``'s own docstring note."""
    text = "After merging your change run pytest to confirm."
    matches = guard.classify_text(text, cwd=str(repo))
    assert matches, "expected the pytest mention to still be a match"
    assert all(m.position == "descriptive" for m in matches)
    assert all(m.position != "imperative" for m in matches)


# ---------------------------------------------------------------------------
# classify_command_precision / classify_text_precision / PrecisionMatch
# (DR-088 R9 layer-2 seam -- cross-repo/inbox/2026-07-28-market-
# intelligence-em-dispatched-agent-scoped-test-breadth.md, DoE-claude repo)
#
# ``classify_command``/``classify_text`` report nothing for a SCOPED-looking
# pytest invocation ("run pytest over tests/acquisition/") -- that is exactly
# the shape R9's precision leg refuses once the dispatched agent tries to
# run it, so the layer-2 hook needs a distinct API to see it coming.
# ---------------------------------------------------------------------------

@pytest.fixture
def precision_repo(repo):
    """``repo`` plus a real on-disk directory to name as a positional, so
    ``_pytest_directory_args``'s ``os.path.isdir`` check has something to
    find -- same discipline as ``check_test_suite_invocation.py``'s own
    ``repo_with_test_dir`` fixture."""
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
    """A bare, unscoped ``pytest`` is Tier U -- ``classify_command``'s own
    business. The precision API must report nothing for it: restating an
    already-suite-shaped match here would duplicate, not extend,
    ``classify_command``'s coverage."""
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
    """A dispatch brief instructing the whole suite is
    ``classify_text``'s business, never this API's."""
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
