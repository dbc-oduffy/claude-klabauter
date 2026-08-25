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


def test_unc_escape_artifact_not_flagged() -> None:
    """A JSON-escaped `\\\\sizing\\n` -- the backslash of the `\\n` escape
    misread as the UNC share separator, no real UNC path present. Regression
    for the false positive `_UNC_RE` picked up before the escape-letter
    lookahead was added to its share segment."""
    # Review: coordinator:code-reviewer -- original fixture had 4 leading
    # backslashes, which `_UNC_RE` never matches under old or new pattern
    # (vacuous regression coverage). 2 backslashes is the real JSON-escaped
    # byte sequence the guard false-positived on.
    hits = detect_in_text(r'"cmd": "\\sizing\n"')  # abs-path-ok: synthetic test fixture
    assert not any(f.rule == "unc" for f in hits)


def test_mixed_separators_escape_artifact_not_flagged() -> None:
    """`X:/DoE-claude/coordinator/skills/x/SKILL.md\\r\\n` -- the trailing
    JSON-escaped `\\r\\n` supplies the sole backslash, no real mixed-
    separator path present. The genuine hit is `posix-home`/`drive-letter`
    territory, not `mixed-separators`."""
    hits = detect_in_text(
        r'"path": "X:/DoE-claude/coordinator/skills/x/SKILL.md\r\n"'
    )  # abs-path-ok: synthetic test fixture
    assert not any(f.rule == "mixed-separators" for f in hits)


def test_mixed_separators_gated_cut_does_not_break_real_path() -> None:
    """The escape-cut gate must not truncate a REAL path token merely
    because a segment starts with an escape letter (`test` starts with `t`).
    `C:\\test\\project/sub\\file` must still fire `mixed-separators` in
    full, not get chopped at `\\t`."""
    hits = detect_in_text(r"C:\test\project/sub\file is wrong on any platform")  # abs-path-ok: synthetic test fixture
    matches = [f.matched for f in hits if f.rule == "mixed-separators"]
    assert any(m == r"C:\test\project/sub\file" for m in matches)


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
# `dead-registry-rung` (ported from DoE-claude's former Rule B) -- see the
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


def test_capture_data_jsonl_under_audits_data_is_exempt() -> None:
    """A `.jsonl` capture dump under `state/audits/data/` has no comment
    syntax, so the `abs-path-ok:` escape hatch is unusable -- format-scoped
    exemption, not the directory-prefix class above."""
    text = '{"cwd": "/Users/realperson/X/claude-klabauter"}\n'  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="state/audits/data/2026-08-13-capture.jsonl")
    assert not hits


def test_capture_data_json_under_recovery_is_exempt() -> None:
    text = '{"cwd": "/Users/realperson/X/claude-klabauter"}\n'  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="state/recovery/2026-08-13-snapshot.json")
    assert not hits


def test_capture_data_hand_authored_md_under_recovery_is_not_exempt() -> None:
    """The negative-spec case, and the point of the change: a `.md` file
    under `state/recovery/` keeps its comment syntax and the `abs-path-ok:`
    escape hatch, so it stays fully in scope."""
    text = "the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="state/recovery/2026-08-13-findings.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_capture_data_hand_authored_py_under_recovery_is_not_exempt() -> None:
    """Same negative-spec case for `.py` -- also keeps comment syntax and
    stays in scope under `state/recovery/`."""
    text = "the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="state/recovery/probe.py")
    assert any(f.rule == "posix-home" for f in hits)


def test_json_outside_prefix_is_exempt_on_format_alone() -> None:
    """`.json` has no comment syntax at any path, so the prefix pairing was
    only producing undischargeable reds outside the two capture directories.
    Superseded the earlier assertion that format alone is not enough."""
    text = '{"cwd": "/Users/realperson/X/claude-klabauter"}\n'  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="state/handoffs/some-handoff.json")
    assert not hits


def test_jsonl_outside_prefix_is_exempt_on_format_alone() -> None:
    """The sent-ledger case DoE-claude named: `.jsonl` matching
    `_CAPTURE_DATA_EXTENSIONS` exactly but living outside every prefix."""
    text = '{"delivery_commit_reason": "fatal: /Users/realperson/X/claude-klabauter/x"}\n'  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="state/memo-outbox/sent-ledger.jsonl")
    assert not hits


def test_patch_outside_prefix_is_not_exempt() -> None:
    """Negative spec: `.patch`/`.diff` stay prefix-paired. A diff transcribes
    lines from files that DO have comment syntax, so it does not join the
    comment-syntax-free class."""
    text = "+the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="state/handoffs/some-change.patch")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_scalar_exempt_via_content_model_not_extension() -> None:
    """Superseded by classes 2/3, and corrected by the 2026-08-14 spike: a
    YAML PLAIN scalar's trailing `# abs-path-ok: <reason>` is legal YAML
    and leaves the parsed value byte-identical, so a plain scalar is NOT a
    position where the hatch is unusable -- it stays fully in scope,
    marker required, same as anywhere else."""
    text = "decision_note: failed at /Users/realperson/X/claude-klabauter/x\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="state/sizings/some-sizing.yaml")
    assert any(f.rule == "posix-home" for f in hits)


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
    hits = detect_in_text(text, filename="evidence.yaml")
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_dead_registry_rung_json_description_field_not_flagged() -> None:
    """A JSON `"description"` field narrating the dead rung is documentary,
    same reasoning as the YAML case -- JSON strings never need continuation
    tracking since a raw newline can't appear inside one."""
    text = '{\n  "description": "True when ~/.claude/plugins/coordinator/CLAUDE.md exists."\n}\n'  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="schema.json")
    assert "dead-registry-rung" not in _rules(hits)


# ---------------------------------------------------------------------------
# Classes 2/3 -- position-scoped content model (YAML scalars, markdown code
# blocks). See the module docstring's "Position-scoped exemptions" section.
# ---------------------------------------------------------------------------


def test_yaml_plain_scalar_still_fires() -> None:
    """Negative spec: a plain (unquoted) YAML scalar keeps a lossless
    `abs-path-ok:` hatch (a trailing `# abs-path-ok: r` is legal YAML and
    leaves the value byte-identical), so it must stay fully in scope."""
    text = "note: the failure was at /Users/realperson/X/claude-klabauter/x\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_single_quoted_scalar_still_fires() -> None:
    """Negative spec: a single-quoted YAML scalar's hatch is equally
    lossless -- stays in scope."""
    text = "note: 'the failure was at /Users/realperson/X/claude-klabauter/x'\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_double_quoted_scalar_still_fires() -> None:
    """Negative spec: a double-quoted YAML scalar's hatch is equally
    lossless -- stays in scope."""
    text = 'note: "the failure was at /Users/realperson/X/claude-klabauter/x"\n'  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_path_shaped_mapping_key_still_fires() -> None:
    """Negative spec / the exact defect the spike caught: a `<path>: value`
    mapping line where the path is the KEY, not the value, must stay
    flagged -- a key is never a protected value position regardless of the
    value's own style."""
    text = "/Users/realperson/X/claude-klabauter: value\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_folded_block_scalar_exempt() -> None:
    text = (
        "note: >-\n"
        "  the failure was at /Users/realperson/X/claude-klabauter/x\n"  # abs-path-ok: synthetic test fixture
    )
    assert not detect_in_text(text, filename="entry.yaml")


def test_yaml_literal_block_scalar_exempt() -> None:
    text = (
        "note: |\n"
        "  the failure was at /Users/realperson/X/claude-klabauter/x\n"  # abs-path-ok: synthetic test fixture
    )
    assert not detect_in_text(text, filename="entry.yaml")


def test_yaml_comment_line_still_fires() -> None:
    """A `#`-led comment line never matches the key-line pattern, so it is
    never part of a YAML scalar -- the hatch works there like anywhere
    else, and the line stays fully in scope."""
    text = "# see /Users/realperson/X/claude-klabauter/x for the shape\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_frontmatter_value_still_fires() -> None:
    """Negative spec, reversing the memo's original class-2 ask (spike
    verdict item 3): a frontmatter value is a YAML scalar like any other --
    the hatch is lossless for it, so frontmatter values stay fully in
    scope, marker required."""
    text = (
        "---\n"
        "note: the failure was at /Users/realperson/X/claude-klabauter/x\n"  # abs-path-ok: synthetic test fixture
        "---\n"
        "prose after frontmatter\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_prose_after_frontmatter_still_fires() -> None:
    text = (
        "---\n"
        "title: fine\n"
        "---\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_fenced_backtick_block_exempt() -> None:
    text = (
        "prose before\n"
        "```\n"
        "cd /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
        "```\n"
        "prose after\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_fenced_tilde_block_exempt() -> None:
    text = (
        "prose before\n"
        "~~~\n"
        "cd /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
        "~~~\n"
        "prose after\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_indented_code_block_exempt() -> None:
    text = "prose\n\n    cd /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_prose_between_two_fences_still_fires() -> None:
    text = (
        "```\ncode block one\n```\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
        "```\ncode block two\n```\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_unterminated_fence_does_not_swallow_rest_of_file() -> None:
    """An unterminated fence opens NO exempt span -- the citation after it
    must still fire, not be silently swallowed as "inside code"."""
    text = (
        "```\n"
        "unterminated code block\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_horizontal_rule_dash_is_not_frontmatter() -> None:
    """A `---` that is NOT the first line of the file is an ordinary
    markdown horizontal rule, not a frontmatter fence -- content after it
    must not be swallowed as if it were YAML."""
    text = (
        "# Title\n"
        "\n"
        "---\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_unterminated_frontmatter_does_not_swallow_rest_of_file() -> None:
    text = (
        "---\n"
        "note: fine\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_py_string_literal_path_still_fires() -> None:
    """`.py` is not a covered class for classes 2/3 -- a path in a Python
    string is ordinary corpus debt, still fully in scope."""
    text = "path = '/Users/realperson/X/claude-klabauter'\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="mod.py")
    assert any(f.rule == "posix-home" for f in hits)


def test_class1_json_still_exempt_after_classes_2_3() -> None:
    text = '{"cwd": "/Users/realperson/X/claude-klabauter"}\n'  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="state/handoffs/some-handoff.json")
    assert not hits


# ---------------------------------------------------------------------------
# Positional exemption -- 2026-08-14 spike-proven correction (block scalars
# only, code blocks; every other position from the earlier revision reverts
# to fully in scope). See docs/research/spike-verdicts/2026-08-14-
# positional-parse-for-abs-path-ok-discharge.md.
# ---------------------------------------------------------------------------


def test_yaml_folded_dash_chomping_block_scalar_exempt() -> None:
    """`>-` (folded, strip chomping) normalizes to PyYAML style `>` like any
    other folded-scalar spelling -- still protected."""
    text = (
        "note: >-\n"
        "  the failure was at /Users/realperson/X/claude-klabauter/x\n"  # abs-path-ok: synthetic test fixture
    )
    assert not detect_in_text(text, filename="entry.yaml")


def test_md_fence_info_string_still_exempt() -> None:
    """A fenced block carrying an info string (` ```bash `) is still a
    fence -- the info string doesn't change the protected-span shape."""
    text = (
        "prose before\n"
        "```bash\n"
        "cd /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
        "```\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_longer_fence_not_closed_by_shorter_run() -> None:
    """A 5-backtick opener is NOT closed by a 3-backtick line -- CommonMark
    requires the closer to be AT LEAST as long as the opener, so the
    3-tick line is literal fence content, not a close. The block only
    closes at the later 5-backtick line, and the citation between them
    stays protected throughout."""
    text = (
        "`````\n"
        "```\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
        "`````\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_mixed_char_fence_not_closed() -> None:
    """Negative spec: a `~~~` fence must NOT be closed by a ``` ``` ``` line
    -- the two fence characters are not interchangeable. Content is treated
    as still inside the open `~~~` fence, so it stays protected."""
    text = (
        "~~~\n"
        "cd /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
        "```\n"
        "~~~\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_html_comment_inside_fence_exempt() -> None:
    """An HTML comment marker sitting inside a fenced block renders as
    literal text in the transcript -- the position is still protected."""
    text = (
        "```\n"
        "<!-- the repo lives at /Users/realperson/X/claude-klabauter -->\n"  # abs-path-ok: synthetic test fixture
        "```\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_yaml_malformed_keeps_firing() -> None:
    """Negative spec: malformed YAML (unparseable by `yaml.compose_all`)
    yields NO protected spans at all -- every uncertainty resolves to
    keep-firing, never to guessing at a boundary in broken input."""
    text = (
        "note: [unclosed flow sequence\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_tab_indented_keeps_firing() -> None:
    """Negative spec: YAML forbids tabs for indentation, so a tab-indented
    document fails to compose -- keep-firing, not a guessed boundary."""
    text = "note:\n\t- the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_four_space_lazy_list_continuation_still_fires() -> None:
    """Negative spec: a 4-space-indented line that is a LAZY CONTINUATION of
    an open list item's paragraph text is not indented code (CommonMark) --
    it must stay in scope, not be swallowed as a code block."""
    text = (
        "- item one starts here\n"
        "    continues at /Users/realperson/X/claude-klabauter as a lazy line\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_lazy_list_continuation_after_blank_line_still_fires() -> None:
    """The case a preceded-by-blank rule alone gets WRONG. With a blank line
    between the marker and the indented text, "previous line was blank" is
    satisfied, so a threshold-free scanner reads this as code and silently
    exempts a real citation. Inside a list item the code threshold rebases to
    the item's content indent plus four, and four spaces does not clear it."""
    text = (
        "- item one starts here\n"
        "\n"
        "    /Users/realperson/X/claude-klabauter is item text, not code\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_ordered_list_continuation_after_blank_line_still_fires() -> None:
    """Same rebasing for an ordered marker -- the width of `1. ` sets the
    threshold, so the rule cannot be special-cased to bullets."""
    text = (
        "1. item one starts here\n"
        "\n"
        "    /Users/realperson/X/claude-klabauter is item text, not code\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_genuine_indented_code_inside_list_item_is_exempt() -> None:
    """The other side of the rebasing: six spaces DOES clear `- `'s content
    indent plus four, so it is real indented code inside the item and the
    marker is unusable there."""
    text = (
        "- item one starts here\n"
        "\n"
        "      /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    )
    assert not detect_in_text(text, filename="doc.md")


def test_md_deeply_nested_list_continuation_still_fires() -> None:
    """The defect a 0-3 indent cap on the list-marker pattern produced. At
    three-plus levels the markers themselves sit past column 3, so a capped
    pattern stops matching, the threshold freezes at the last recognised
    level, and the deep item's own paragraph starts clearing a stale-low bar
    -- silently exempted as code. Item d's content indent is 8, so code needs
    12; this text at 8 is prose and must stay in scope."""
    text = (
        "- a\n"
        "  - b\n"
        "    - c\n"
        "      - d\n"
        "\n"
        "        /Users/realperson/X/claude-klabauter is item text\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_genuine_code_in_deeply_nested_item_is_exempt() -> None:
    """The other side of the same rebasing: twelve columns DOES clear item
    d's content indent plus four, so it is real indented code and the marker
    is unusable there. Guards against fixing the case above by disabling the
    rebasing outright."""
    text = (
        "- a\n"
        "  - b\n"
        "    - c\n"
        "      - d\n"
        "\n"
        "            /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    )
    assert not detect_in_text(text, filename="doc.md")


def test_md_wide_ordered_marker_continuation_still_fires() -> None:
    """A two-digit ordered marker is four columns wide, so `10. `'s content
    indent is 4 and code needs 8. Four spaces is item text -- the threshold
    is computed from the marker's real width, never assumed."""
    text = (
        "10. item one starts here\n"
        "\n"
        "    /Users/realperson/X/claude-klabauter is item text\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="posix.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_blockquote_nested_list_falls_back_to_firing() -> None:
    """Blockquote-nested lists are not tracked. Documenting the behaviour
    rather than claiming coverage: the `> ` prefix means the line never
    matches the indented-code shape at all, so it keeps firing. That is the
    safe direction (a citation stays reportable), and this test pins it so a
    later change cannot quietly flip it into an exemption."""
    text = (
        "> - item one\n"
        ">\n"
        ">     /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_indented_code_after_list_closes_is_exempt() -> None:
    """A non-blank line at the margin closes the list item, so the threshold
    drops back to the document's own four columns."""
    text = (
        "- item one\n"
        "\n"
        "prose at the margin closes the list\n"
        "\n"
        "    /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    )
    assert not detect_in_text(text, filename="doc.md")


def test_md_thematic_break_not_mistaken_for_frontmatter() -> None:
    """Negative spec: a `---` thematic break appearing anywhere other than
    line 1 is ordinary markdown, never treated as a frontmatter open --
    content after it stays in scope (duplicate coverage of the existing
    horizontal-rule test, phrased against the AC's own wording)."""
    text = (
        "prose\n"
        "\n"
        "---\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_setext_heading_underline_not_mistaken_for_frontmatter() -> None:
    """Negative spec: a setext heading underline (`---` directly beneath a
    heading's text, line 1 not being the file's own first line) must not be
    mistaken for a frontmatter delimiter or a code fence -- content after
    it stays in scope."""
    text = (
        "Heading Text\n"
        "---\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"  # abs-path-ok: synthetic test fixture
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_inline_code_span_still_fires() -> None:
    """Negative spec: a single-backtick inline code span is not a fenced or
    indented code BLOCK -- it stays fully in scope."""
    text = "see `/Users/realperson/X/claude-klabauter` for the shape\n"  # abs-path-ok: synthetic test fixture
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)
