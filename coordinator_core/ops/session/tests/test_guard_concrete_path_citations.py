"""
coordinator_core.ops.session.tests.test_guard_concrete_path_citations

Coverage:
  (a) Windows drive-letter root flagged, `https://` NOT flagged (the
      historical false-positive this fleet has hit before -- regression).
  (b) POSIX home path with a real username flagged, including the WSL
      (`/mnt/<drive>/Users/...`) and git-bash (`/<drive>/Users/...`)
      spellings; placeholder segments NOT flagged — the angle-bracket form
      <username>, the word-list forms (alice/bob/username/...), the ellipsis
      "...", and the runtime-substitution forms ($USER and ${USER}). The
      bare word `user` is NOT a placeholder (a real Windows default account).
  (c) UNC path flagged.
  (d) mixed separators within one anchored token flagged.
  (e) `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`-style settings-home anchors NOT
      flagged.
  (f) `abs-path-ok: <reason>` exempts a line; a bare `abs-path-ok:` with no
      reason does NOT exempt it.
  (g) `new_violations` only surfaces citations introduced by a write, never
      pre-existing ones sitting untouched elsewhere in the file.

Every offending literal below carries a same-line `abs-path-ok:` marker with
a reason so THIS file itself can be written past the live
`write_guards.guard_concrete_path_citations` guard -- see that guard's own
module docstring: it scans the raw source text of a write, and a test fixture
full of synthetic offending paths is exactly the "genuine reason to keep a
literal quotable" case the marker exists for. The marker is a same-line
comment outside the string literal in every case, so it has no effect on the
actual string values under test.
"""
from __future__ import annotations

from coordinator_core.ops.session.guard_concrete_path_citations import (
    detect_in_text,
    new_violations,
)


def _rules(findings):
    return {f.rule for f in findings}


def test_drive_letter_flagged() -> None:
    hits = detect_in_text(r"the repo lives at X:\example-game-workbench-repo on that box")  # abs-path-ok: synthetic test fixture
    assert any(f.rule == "drive-letter" for f in hits)


def test_https_url_not_flagged_as_drive_letter() -> None:
    hits = detect_in_text("see https://example.com/x for details")
    assert "drive-letter" not in _rules(hits)
    assert not hits


def test_posix_home_real_username_flagged() -> None:
    hits = detect_in_text("/Users/realperson/X/claude-klabauter")  # abs-path-ok: synthetic test fixture
    assert any(f.rule == "posix-home" for f in hits)


def test_posix_home_placeholders_not_flagged() -> None:
    lines = (
        "/Users/<user>/x",  # abs-path-ok: synthetic test fixture
        "/Users/alice/x",  # abs-path-ok: synthetic test fixture
        "/Users/bob/x",  # abs-path-ok: synthetic test fixture
        "/Users/username/x",  # abs-path-ok: synthetic test fixture
        "/Users/you/x",  # abs-path-ok: synthetic test fixture
        "/Users/me/x",  # abs-path-ok: synthetic test fixture
        "/Users/foo/x",  # abs-path-ok: synthetic test fixture
        "/Users/bar/x",  # abs-path-ok: synthetic test fixture
        "/Users/baz/x",  # abs-path-ok: synthetic test fixture
        "/Users/test/x",  # abs-path-ok: synthetic test fixture
        "/Users/example/x",  # abs-path-ok: synthetic test fixture
        "/Users/someone/x",  # abs-path-ok: synthetic test fixture
        "/Users/operator/x",  # abs-path-ok: synthetic test fixture
        "/Users/yourname/x",  # abs-path-ok: synthetic test fixture
        "/Users/.../x",  # abs-path-ok: synthetic test fixture
        "/Users/$USER/x",  # abs-path-ok: synthetic test fixture
        "/Users/${USER}/x",  # abs-path-ok: synthetic test fixture
    )
    for line in lines:
        assert not detect_in_text(line), f"false positive on placeholder line: {line!r}"


def test_posix_home_wsl_shape_flagged() -> None:
    """WSL spells a Windows home directory under a POSIX-looking root. The
    citation names one operator's box exactly as much as the bare
    `/Users/<name>` form -- regression for the lookbehind that let the
    drive letter block the anchor."""
    hits = detect_in_text("cloned to /mnt/c/Users/realperson/src/thing")  # abs-path-ok: synthetic test fixture
    assert any(f.rule == "posix-home" for f in hits)
    assert any(
        f.matched.startswith("/mnt/c/Users/") for f in hits if f.rule == "posix-home"
    ), "the reported match should quote the whole citation, not a suffix"


def test_posix_home_gitbash_shape_flagged() -> None:
    """git-bash's `/c/Users/<name>` -- the same gap, one root shorter."""
    hits = detect_in_text("cloned to /c/Users/realperson/src/thing")  # abs-path-ok: synthetic test fixture
    assert any(f.rule == "posix-home" for f in hits)
    assert any(
        f.matched.startswith("/c/Users/") for f in hits if f.rule == "posix-home"
    ), "the reported match should quote the whole citation, not a suffix"


def test_posix_home_wsl_gitbash_placeholder_segment_still_exempt() -> None:
    """Widening the anchor must not cost the placeholder exemption: the
    WSL/git-bash roots carry a placeholder user segment in documentation
    the same way the bare root does."""
    for line in (
        "/mnt/c/Users/<username>/x",  # abs-path-ok: synthetic test fixture
        "/c/Users/$USER/x",  # abs-path-ok: synthetic test fixture
    ):
        assert not detect_in_text(line), f"false positive on placeholder line: {line!r}"


def test_posix_home_user_segment_is_concrete_not_a_placeholder() -> None:
    """`User` is a real Windows default-account name, so a citation whose
    home segment is the bare word `user` is genuinely machine-specific, not
    a worked example -- regression for the placeholder-word list that
    exempted it."""
    hits = detect_in_text("/Users/user/X/claude-klabauter")  # abs-path-ok: synthetic test fixture
    assert any(f.rule == "posix-home" for f in hits)


def test_posix_home_username_segment_stays_a_placeholder() -> None:
    """`username` was never in dispute and must stay exempt -- guards the
    other half of the `user`/`username` split."""
    assert not detect_in_text("/Users/username/X/project")  # abs-path-ok: synthetic test fixture


def test_midpath_drive_letter_lookalike_not_flagged() -> None:
    """A single-letter directory sitting mid-path before `Users` is not a
    WSL/git-bash root -- the widened anchor must still require the letter
    root to start the token, or every relative path with a one-letter
    directory becomes a finding."""
    for line in (
        "docs/c/Users/realperson/x is a relative path, not a root",
        "pkg/mnt/d/home/realperson/x is likewise relative",
    ):
        assert not detect_in_text(line), f"false positive on relative line: {line!r}"


def test_ordinary_prose_with_slashes_not_flagged() -> None:
    """Plain prose and a URL that merely contain slashes and the word
    'users' pick up no finding from the widened anchor."""
    for line in (
        "see https://example.com/c/Users/docs for the write-up",
        "the users of this tool run it on macOS and on WSL alike",
    ):
        assert not detect_in_text(line), f"false positive on prose line: {line!r}"


def test_settings_home_anchor_not_flagged() -> None:
    hits = detect_in_text("${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json")
    assert not hits


def test_unc_path_flagged() -> None:
    hits = detect_in_text(r"copy from \\buildhost\share\artifacts")  # abs-path-ok: synthetic test fixture
    assert any(f.rule == "unc" for f in hits)


def test_mixed_separators_flagged() -> None:
    hits = detect_in_text(r"C:\Users/foo\bar is the real path")  # abs-path-ok: synthetic test fixture
    assert any(f.rule == "mixed-separators" for f in hits)


def test_ellipsis_exempts_a_true_shape_illustration() -> None:
    """`X:\\...\\topic.md` -- the worked-example shape the ellipsis
    exemption exists for -- stays exempt: nothing before the ellipsis is
    concrete."""
    hits = detect_in_text(r"see X:\...\topic.md for the shape")  # abs-path-ok: synthetic test fixture
    assert not hits


def test_trailing_ellipsis_after_a_real_segment_still_flagged() -> None:
    """A real citation that merely trails off with a natural typographic
    ellipsis must NOT be swallowed by the same exemption that legitimizes a
    worked-example shape -- regression for the false negative where `...`
    appearing anywhere in the token exempted the whole thing."""
    hits = detect_in_text(r"the repo lives at C:\Users\example-operator\project\...")  # abs-path-ok: synthetic test fixture
    assert any(f.rule == "drive-letter" for f in hits)


def test_mixed_separators_flagged_even_with_placeholder_first_segment() -> None:
    """A mixed-separator token cannot be correct on any platform regardless
    of whether its first segment reads as a placeholder -- regression for
    the false negative where the `WIN_DRIVE_RE` anchor silently reused
    `drive-letter`'s placeholder exemption, contradicting the module's own
    documented invariant."""
    hits = detect_in_text(r"C:\test\project/sub\file is wrong on any platform")  # abs-path-ok: synthetic test fixture
    assert any(f.rule == "mixed-separators" for f in hits)


def test_well_known_root_does_not_swallow_a_concrete_segment_further_along() -> None:
    """A well-known Windows system root (`Windows`) is an installation
    convention, not a machine-specific claim -- but a REAL segment further
    along the same token past that root is exactly the citation this guard
    exists to catch, and must not be swallowed by the root-level exemption."""
    hits = detect_in_text(r"see C:\Windows\Users\realperson\notes.txt for the file")  # abs-path-ok: synthetic test fixture
    assert any(f.rule == "drive-letter" for f in hits)


def test_drive_rooted_user_profile_has_a_satisfiable_placeholder_form() -> None:
    """The regression this pair exists for: `_is_win_drive_root_exempt` used
    to test ONLY the first segment after the root, so `Users` -- neither a
    placeholder nor a well-known root -- made BOTH the concrete citation and
    its corrected portable form violations. The rule had no spelling anyone
    could satisfy, and a real citation in `discover_working_repos` was
    stranded: the write-time guard hard-blocked the fix for it."""
    assert "drive-letter" not in _rules(detect_in_text(r"see C:\Users\<username>\notes.txt"))
    assert "drive-letter" not in _rules(detect_in_text("see C:/Users/<username>/notes.txt"))


def test_drive_rooted_user_profile_placeholder_word_also_exempt() -> None:
    """The delegation is to `posix-home`'s OWN placeholder logic, so every
    form that exempts a `posix-home` segment exempts an account segment
    here too -- a word from the list, and a runtime substitution."""
    assert "drive-letter" not in _rules(detect_in_text(r"C:\Users\alice\src"))  # abs-path-ok: synthetic test fixture
    assert "drive-letter" not in _rules(detect_in_text(r"C:\Users\$USER\src"))  # abs-path-ok: synthetic test fixture
    assert "drive-letter" not in _rules(detect_in_text("C:/home/${USER}/src"))  # abs-path-ok: synthetic test fixture


def test_drive_rooted_user_profile_concrete_account_still_flagged() -> None:
    """The negative half -- the fix must not widen the rule. A genuinely
    concrete account name under a drive-rooted profile root is exactly the
    machine-specific citation this guard exists to catch, at any depth."""
    assert "drive-letter" in _rules(detect_in_text(r"C:\Users\realperson"))  # abs-path-ok: synthetic test fixture
    assert "drive-letter" in _rules(detect_in_text("C:/Users/realperson/project/x.md"))  # abs-path-ok: synthetic test fixture
    assert "drive-letter" in _rules(detect_in_text("D:/home/realperson/src"))  # abs-path-ok: synthetic test fixture


def test_bare_drive_rooted_users_root_stays_flagged() -> None:
    """A profile root with NO account segment behind it is not exempt: the
    drive root is one operator's mapping and machine-specific in its own
    right (same reasoning as a bare drive root with nothing after it), and
    there is no account name to adjudicate. `_posix_home_account_segment`
    returns None here rather than an empty-string segment that could read as
    a placeholder."""
    assert "drive-letter" in _rules(detect_in_text(r"copy it to C:\Users\ on that box"))  # abs-path-ok: synthetic test fixture
    assert "drive-letter" in _rules(detect_in_text("copy it to C:/Users on that box"))  # abs-path-ok: synthetic test fixture


def test_nested_user_profile_under_well_known_root_unaffected() -> None:
    """The user-profile branch fires only on the FIRST segment after the
    root; a profile root nested deeper still goes through the well-known-root
    path's own nested-`Users` check, which stays intact."""
    assert "drive-letter" in _rules(detect_in_text(r"C:\Windows\Users\realperson\x"))  # abs-path-ok: synthetic test fixture
    assert "drive-letter" not in _rules(detect_in_text(r"C:\Windows\Users\<username>\x"))


def test_well_known_root_alone_stays_exempt() -> None:
    """The well-known-root exemption still holds when nothing concrete
    follows it -- this is the legitimate case the exemption exists for."""
    hits = detect_in_text(r"binaries live under C:\Windows\System32")  # abs-path-ok: synthetic test fixture
    assert "drive-letter" not in _rules(hits)


def test_marker_embedded_mid_word_does_not_exempt_the_line() -> None:
    """A marker token appearing as a bare substring inside a longer word
    must NOT exempt the line -- regression for the naive `line.find()`
    match, which let a spoofed/incidental substring occurrence swallow a
    genuine citation on the same line."""
    line = (
        "notabs-path-ok: nothing here justifies "
        + r"C:\Users\realperson\notes.txt"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(line)
    assert any(f.rule == "drive-letter" for f in hits)


def test_marker_with_reason_exempts_line() -> None:
    line = r"X:\example-game-workbench-repo  # abs-path-ok: quoting the 2026-07-28 incident"  # abs-path-ok: synthetic test fixture
    assert not detect_in_text(line)


def test_bare_marker_without_reason_does_not_exempt() -> None:
    trailing_colon_no_reason = "abs-path-ok:"  # abs-path-ok: assembled at runtime so this source line is not itself a bare marker
    line = "X:\\example-game-workbench-repo  # " + trailing_colon_no_reason  # abs-path-ok: synthetic test fixture, assembled at runtime
    hits = detect_in_text(line)
    assert any(f.rule == "drive-letter" for f in hits)


def test_new_violations_ignores_unchanged_legacy_citation() -> None:
    before = "legacy: X:\\some-repo\nunrelated line\n"  # abs-path-ok: synthetic test fixture
    after = "legacy: X:\\some-repo\nunrelated line, now edited\n"  # abs-path-ok: synthetic test fixture
    assert new_violations(before, after) == []


def test_new_violations_flags_freshly_introduced_citation() -> None:
    before = "legacy: X:\\some-repo\n"  # abs-path-ok: synthetic test fixture
    after = "legacy: X:\\some-repo\nnew one: /Users/realperson/x\n"  # abs-path-ok: synthetic test fixture
    new = new_violations(before, after)
    assert len(new) == 1
    assert new[0].rule == "posix-home"


def test_new_violations_flags_a_duplicate_of_an_existing_citation() -> None:
    before = "X:\\some-repo\n"  # abs-path-ok: synthetic test fixture
    after = "X:\\some-repo\nX:\\some-repo\n"  # abs-path-ok: synthetic test fixture
    new = new_violations(before, after)
    assert len(new) == 1


# ---------------------------------------------------------------------------
# `dead-registry-rung` (ported from example-doctrine-repo's former Rule B) -- see the
# module docstring's own "mention-awareness, per surface" section.
# ---------------------------------------------------------------------------


def test_dead_registry_rung_flagged_in_shell_live_use() -> None:
    hits = detect_in_text(
        'source "~/.claude/machine-local/env.sh"\n',  # abs-path-ok: synthetic test fixture
        filename="setup.sh",
    )
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_dead_registry_rung_flags_home_spelling_too() -> None:
    hits = detect_in_text(
        'source "$HOME/.claude/machine-local/env.sh"\n',  # abs-path-ok: synthetic test fixture
        filename="setup.sh",
    )
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_hardcoded_plugin_exec_path_flagged() -> None:
    hits = detect_in_text(
        'run "~/.claude/plugins/coordinator/bin/foo"\n',  # abs-path-ok: synthetic test fixture
        filename="setup.sh",
    )
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_dead_registry_rung_not_flagged_in_python_docstring() -> None:
    text = (
        '"""\n'
        "module doc mentioning ~/.claude/machine-local as prior art.\n"  # abs-path-ok: synthetic test fixture
        '"""\n'
    )
    hits = detect_in_text(text, filename="mod.py")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_not_flagged_in_argparse_help_text() -> None:
    text = (
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--x', help='replaces hardcoded ~/.claude/machine-local reads')\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="mod.py")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_flagged_in_live_python_use() -> None:
    text = (
        "import os\n"
        "open(os.path.expanduser('~/.claude/machine-local/env.sh'))\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="mod.py")
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_dead_registry_rung_not_flagged_on_echo_line() -> None:
    text = 'echo "see ~/.claude/machine-local/env.sh" >&2\n'  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="setup.sh")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_not_flagged_on_comment_line() -> None:
    text = "# see ~/.claude/machine-local/env.sh for the old shape\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="setup.sh")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_not_flagged_inside_backticks() -> None:
    text = "see `~/.claude/machine-local/env.sh` for the dead rung\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="doc.md")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_exempted_by_marker() -> None:
    text = "source ~/.claude/machine-local/env.sh  # abs-path-ok: incident writeup quoting the real path\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="setup.sh")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_skipped_on_unparseable_python() -> None:
    text = "def broken(:\n    ~/.claude/machine-local\n"  # abs-path-ok: synthetic test fixture, deliberately invalid syntax
    hits = detect_in_text(text, filename="mod.py")
    assert "dead-registry-rung" not in _rules(hits)


# ---------------------------------------------------------------------------
# Evidence-artifact exemption -- `state/review-trail/diffs/` and
# `state/subagent-share/` are machine-generated RECORDS of a citation that
# exists elsewhere, not fresh corpus debt. See the module docstring's
# "Evidence-artifact exemption" section.
# ---------------------------------------------------------------------------


def test_review_trail_diff_transcript_is_exempt() -> None:
    """A frozen diff under `state/review-trail/diffs/` is a transcript of a
    rewrite -- its `-` line legitimately still contains the pre-rewrite
    literal, and flagging it would ask the record to lie about what it
    recorded."""
    text = "-legacy: X:\\some-repo\n+legacy: repo-alias:some-repo\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="state/review-trail/diffs/corpus-path-sweep.diff")
    assert not hits


def test_subagent_share_sidecar_is_exempt() -> None:
    """A reviewer/integrator sidecar under `state/subagent-share/` quotes an
    offending line as the finding itself -- the quotation IS the evidence,
    not a fresh citation the guard hasn't seen yet."""
    text = "Finding: drive-letter citation at foo.md:3 -- `X:\\Users\\realperson\\notes.txt`\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(
        text, filename="state/subagent-share/some-session/coordinatorcode-reviewer-abc.md"
    )
    assert not hits


def test_review_findings_output_is_exempt() -> None:
    """`state/review-findings/` is machine-generated review output that
    TRANSCRIBES paths it did not originate: a `**Working directory:**`
    provenance header stamped in from the invoking environment, and embedded
    `diff.patch` bodies carrying the same frozen `-`/`+` lines the
    `state/review-trail/diffs/` case covers. Same "guaranteed to transcribe,
    not originate" property as its two sibling prefixes."""
    text = (
        "**Working directory:** X:\\some-repo\n"  # abs-path-ok: synthetic test fixture
        "-legacy: C:\\Users\\realperson\\.claude\n"  # abs-path-ok: synthetic test fixture
    )
    assert not detect_in_text(text, filename="state/review-findings/20260627T120301Z/deps.md")
    assert not detect_in_text(
        text, filename="state/review-findings/2026-06-01-weekly/diff.patch"
    )


def test_review_trail_hand_authored_note_is_not_exempt() -> None:
    """The guard's prefix stops at `state/review-trail/diffs/`, NOT the whole
    `state/review-trail/` tree the sibling `fix_concrete_path_citations`
    module covers -- that tree also holds hand-authored `.md` findings, which
    are ordinary corpus debt. This asserts the two prefix sets are
    deliberately different rather than drifted; unifying them would open a
    detect-side hole here."""
    text = "the repo lives at X:\\example-game-workbench-repo\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(
        text, filename="state/review-trail/2026-07-01-boundary-union-finding.md"
    )
    assert any(f.rule == "drive-letter" for f in hits)


def test_evidence_artifact_exemption_is_prefix_scoped_not_ambient() -> None:
    """Only the named prefixes are exempt -- a citation in an ordinary
    `state/` surface (e.g. a lesson or handoff, read and trusted at session
    start) stays fully in scope. This is an artifact-CLASS carve-out, not an
    ambient "anything under state/" exemption."""
    text = "the repo lives at X:\\example-game-workbench-repo\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="state/handoffs/some-handoff.md")
    assert any(f.rule == "drive-letter" for f in hits)


def test_new_violations_evidence_artifact_filename_exempts_both_sides() -> None:
    """`new_violations` threads `filename` through to both `detect_in_text`
    calls -- a sidecar that quotes a NEW finding in its `after` text must not
    be denied by the write-time guard leg."""
    before = "no findings yet\n"
    after = "Finding: drive-letter citation -- `X:\\Users\\realperson\\notes.txt`\n"  # abs-path-ok: synthetic test fixture
    new = new_violations(
        before, after, filename="state/subagent-share/some-session/coordinatorreview-integrator-abc.md"
    )
    assert new == []


# ---------------------------------------------------------------------------
# `dead-registry-rung` mention-awareness fixes -- Hole 1 (`.py` comments were
# never excluded despite the docstring's claim), Hole 2 (a real Python file
# kept under a `.sh` extension for oracle-parity naming was extension-
# misdetected and got no AST mention-awareness at all), Hole 3 (structured
# YAML/JSON prose fields narrating a defect were flagged as if they were a
# live read).
# ---------------------------------------------------------------------------


def test_dead_registry_rung_py_comment_is_excluded() -> None:
    """Hole 1 regression -- a plain `#` comment in a real `.py` file must be
    excluded, matching the module docstring's claim (previously only
    docstrings/help-kwargs were excluded; comments were scanned as raw text
    and flagged)."""
    text = "x = 1\n# under ~/.claude/plugins/coordinator-claude/ is one level above a\ny = 2\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="mod.py")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_content_detects_python_under_sh_extension() -> None:
    """Hole 2 regression -- a genuine Python file kept under a `.sh`
    extension (this fleet's bash-oracle-parity naming convention, e.g.
    `dev-sync.sh`) must still get AST-based mention-awareness: its own
    module docstring citing the dead rung documentarily must not be
    flagged, even though `filename` doesn't end in `.py`."""
    text = (
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n"
        '"""\n'
        "Negative-spec: does NOT touch ~/.claude/plugins/marketplaces/ -- cache tree only.\n"  # abs-path-ok: synthetic test fixture
        '"""\n'
    )
    hits = detect_in_text(text, filename="dev-sync.sh")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_py_extension_unparseable_still_skips_whole_file() -> None:
    """The fail-safe (`.py`-extension file with broken syntax gets NO
    `dead-registry-rung` findings at all) still holds after the content-
    based detection change -- it stays gated on the EXTENSION, not on
    whether `ast.parse` happened to succeed."""
    text = "def broken(:\n    ~/.claude/machine-local\n"  # abs-path-ok: synthetic test fixture, deliberately invalid syntax
    hits = detect_in_text(text, filename="mod.py")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_yaml_prose_key_not_flagged() -> None:
    """Hole 3 regression -- a `body:`/`title:` field narrating a defect
    (a lesson or bug-backlog entry) must not be flagged as a live read."""
    text = (
        "created: 2026-07-21\n"
        "body: \"prepare-commit-msg exec's ~/.claude/plugins/coordinator-claude/bin with no guard\"\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="entry.yaml")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_yaml_folded_prose_continuation_not_flagged() -> None:
    """A YAML plain (unquoted) scalar folds across multiple lines with no
    repeated key -- the continuation line must stay covered by the prose
    key it belongs to."""
    text = (
        "- id: doe-L11\n"
        "  title: Grep must cover BOTH the CLAUDE_PLUGIN_ROOT:- fallback\n"
        "    form AND the bare $HOME/.claude/plugins/... form\n"  # abs-path-ok: synthetic test fixture
        "  scope: project\n"
    )
    hits = detect_in_text(text, filename="records.yaml")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_yaml_folded_prose_continuation_word_colon_shape_not_flagged() -> None:
    """A folded `body: |` continuation line that itself starts with a
    `Word:` shape (e.g. "Note:", "Fix:") matches the key-line regex but is
    still folded prose content, not a sibling key -- it must stay exempt.

    Review: coordinatorcode-reviewer-3e4f4e1b -- regression for the
    false-positive gap in `_structured_data_documentary_lines` where such a
    line prematurely ended the active prose continuation.
    """
    text = (
        "created: 2026-08-03\n"
        "title: A narrative record\n"
        "body: |\n"
        "  The registry used to live at ~/.claude/machine-local and callers hardcoded it.\n"  # abs-path-ok: synthetic test fixture
        "  Note: the registry lives at ~/.claude/machine-local, not there anymore.\n"  # abs-path-ok: synthetic test fixture
        "  Fix: route through the resolver instead of ~/.claude/machine-local.\n"  # abs-path-ok: synthetic test fixture
        "status: open\n"
    )
    hits = detect_in_text(text, filename="example.yaml")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_yaml_folded_continuation_ends_at_next_key() -> None:
    """A non-prose key line at the same/shallower indent ends the
    continuation -- a citation AFTER the boundary, under a non-prose key,
    stays flagged."""
    text = (
        "  title: some lesson title that keeps going\n"
        "    and folds onto this line\n"
        "  scope: ~/.claude/machine-local\n"  # abs-path-ok: synthetic test fixture -- "scope" is not a prose key
        "  change_kind: wiki-append\n"
    )
    hits = detect_in_text(text, filename="records.yaml")
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_dead_registry_rung_yaml_non_prose_key_not_swallowed() -> None:
    """A live-looking assertion under a NON-prose key (not `title`/`body`/
    etc.) stays flagged -- the prose-key exemption is a named list, not a
    blanket YAML carve-out."""
    text = "cwd: \"~/.claude/plugins/coordinator-claude/coordinator\"\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="evidence.json")
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_dead_registry_rung_json_description_field_not_flagged() -> None:
    """A JSON `"description"` field narrating the dead rung is documentary,
    same reasoning as the YAML case -- JSON strings never need continuation
    tracking since a raw newline can't appear inside one."""
    text = '{\n  "description": "True when ~/.claude/plugins/coordinator/CLAUDE.md exists."\n}\n'  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="schema.json")
    assert "dead-registry-rung" not in _rules(hits)
