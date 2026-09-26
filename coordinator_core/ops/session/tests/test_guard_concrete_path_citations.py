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
    hits = detect_in_text(r"the repo lives at X:\example-game-workbench-repo on that box")
    assert any(f.rule == "drive-letter" for f in hits)


def test_https_url_not_flagged_as_drive_letter() -> None:
    hits = detect_in_text("see https://example.com/x for details")
    assert "drive-letter" not in _rules(hits)
    assert not hits


def test_posix_home_real_username_flagged() -> None:
    hits = detect_in_text("/Users/realperson/X/claude-klabauter")
    assert any(f.rule == "posix-home" for f in hits)


def test_posix_home_placeholders_not_flagged() -> None:
    lines = (
        "/Users/<user>/x",
        "/Users/alice/x",
        "/Users/bob/x",
        "/Users/username/x",
        "/Users/you/x",
        "/Users/me/x",
        "/Users/foo/x",
        "/Users/bar/x",
        "/Users/baz/x",
        "/Users/test/x",
        "/Users/example/x",
        "/Users/someone/x",
        "/Users/operator/x",
        "/Users/yourname/x",
        "/Users/.../x",
        "/Users/$USER/x",
        "/Users/${USER}/x",
    )
    for line in lines:
        assert not detect_in_text(line), f"false positive on placeholder line: {line!r}"


def test_posix_home_wsl_shape_flagged() -> None:
    hits = detect_in_text("cloned to /mnt/c/Users/realperson/src/thing")
    assert any(f.rule == "posix-home" for f in hits)
    assert any(
        f.matched.startswith("/mnt/c/Users/") for f in hits if f.rule == "posix-home"
    ), "the reported match should quote the whole citation, not a suffix"


def test_posix_home_gitbash_shape_flagged() -> None:
    hits = detect_in_text("cloned to /c/Users/realperson/src/thing")
    assert any(f.rule == "posix-home" for f in hits)
    assert any(
        f.matched.startswith("/c/Users/") for f in hits if f.rule == "posix-home"
    ), "the reported match should quote the whole citation, not a suffix"


def test_posix_home_wsl_gitbash_placeholder_segment_still_exempt() -> None:
    for line in (
        "/mnt/c/Users/<username>/x",
        "/c/Users/$USER/x",
    ):
        assert not detect_in_text(line), f"false positive on placeholder line: {line!r}"


def test_posix_home_user_segment_is_concrete_not_a_placeholder() -> None:
    hits = detect_in_text("/Users/user/X/claude-klabauter")
    assert any(f.rule == "posix-home" for f in hits)


def test_posix_home_username_segment_stays_a_placeholder() -> None:
    assert not detect_in_text("/Users/username/X/project")


def test_midpath_drive_letter_lookalike_not_flagged() -> None:
    for line in (
        "docs/c/Users/realperson/x is a relative path, not a root",
        "pkg/mnt/d/home/realperson/x is likewise relative",
    ):
        assert not detect_in_text(line), f"false positive on relative line: {line!r}"


def test_ordinary_prose_with_slashes_not_flagged() -> None:
    for line in (
        "see https://example.com/c/Users/docs for the write-up",
        "the users of this tool run it on macOS and on WSL alike",
    ):
        assert not detect_in_text(line), f"false positive on prose line: {line!r}"


def test_settings_home_anchor_not_flagged() -> None:
    hits = detect_in_text("${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json")
    assert not hits


def test_unc_path_flagged() -> None:
    hits = detect_in_text(r"copy from \\buildhost\share\artifacts")
    assert any(f.rule == "unc" for f in hits)


def test_mixed_separators_flagged() -> None:
    hits = detect_in_text(r"C:\Users/foo\bar is the real path")
    assert any(f.rule == "mixed-separators" for f in hits)


def test_ellipsis_exempts_a_true_shape_illustration() -> None:
    hits = detect_in_text(r"see X:\...\topic.md for the shape")
    assert not hits


def test_trailing_ellipsis_after_a_real_segment_still_flagged() -> None:
    hits = detect_in_text(r"the repo lives at C:\Users\example-operator\project\...")
    assert any(f.rule == "drive-letter" for f in hits)


def test_mixed_separators_flagged_even_with_placeholder_first_segment() -> None:
    """A mixed-separator token cannot be correct on any platform regardless
    of whether its first segment reads as a placeholder -- regression for
    the false negative where the `WIN_DRIVE_RE` anchor silently reused
    `drive-letter`'s placeholder exemption, contradicting the module's own
    documented invariant."""
    hits = detect_in_text(r"C:\test\project/sub\file is wrong on any platform")
    assert any(f.rule == "mixed-separators" for f in hits)


def test_unc_escape_artifact_not_flagged() -> None:
    hits = detect_in_text(r'"cmd": "\\sizing\n"')
    assert not any(f.rule == "unc" for f in hits)


def test_mixed_separators_escape_artifact_not_flagged() -> None:
    hits = detect_in_text(
        r'"path": "X:/DoE-claude/coordinator/skills/x/SKILL.md\r\n"'
    )
    assert not any(f.rule == "mixed-separators" for f in hits)


def test_mixed_separators_gated_cut_does_not_break_real_path() -> None:
    hits = detect_in_text(r"C:\test\project/sub\file is wrong on any platform")
    matches = [f.matched for f in hits if f.rule == "mixed-separators"]
    assert any(m == r"C:\test\project/sub\file" for m in matches)


def test_well_known_root_does_not_swallow_a_concrete_segment_further_along() -> None:
    hits = detect_in_text(r"see C:\Windows\Users\realperson\notes.txt for the file")
    assert any(f.rule == "drive-letter" for f in hits)


def test_drive_rooted_user_profile_has_a_satisfiable_placeholder_form() -> None:
    assert "drive-letter" not in _rules(detect_in_text(r"see C:\Users\<username>\notes.txt"))
    assert "drive-letter" not in _rules(detect_in_text("see C:/Users/<username>/notes.txt"))


def test_drive_rooted_user_profile_placeholder_word_also_exempt() -> None:
    assert "drive-letter" not in _rules(detect_in_text(r"C:\Users\alice\src"))
    assert "drive-letter" not in _rules(detect_in_text(r"C:\Users\$USER\src"))
    assert "drive-letter" not in _rules(detect_in_text("C:/home/${USER}/src"))


def test_drive_rooted_user_profile_concrete_account_still_flagged() -> None:
    assert "drive-letter" in _rules(detect_in_text(r"C:\Users\realperson"))
    assert "drive-letter" in _rules(detect_in_text("C:/Users/realperson/project/x.md"))
    assert "drive-letter" in _rules(detect_in_text("D:/home/realperson/src"))


def test_bare_drive_rooted_users_root_stays_flagged() -> None:
    assert "drive-letter" in _rules(detect_in_text(r"copy it to C:\Users\ on that box"))
    assert "drive-letter" in _rules(detect_in_text("copy it to C:/Users on that box"))


def test_nested_user_profile_under_well_known_root_unaffected() -> None:
    assert "drive-letter" in _rules(detect_in_text(r"C:\Windows\Users\realperson\x"))
    assert "drive-letter" not in _rules(detect_in_text(r"C:\Windows\Users\<username>\x"))


def test_well_known_root_alone_stays_exempt() -> None:
    hits = detect_in_text(r"binaries live under C:\Windows\System32")
    assert "drive-letter" not in _rules(hits)


def test_marker_embedded_mid_word_does_not_exempt_the_line() -> None:
    line = (
        "notabs-path-ok: nothing here justifies "
        + r"C:\Users\realperson\notes.txt"
    )
    hits = detect_in_text(line)
    assert any(f.rule == "drive-letter" for f in hits)


def test_marker_with_reason_exempts_line() -> None:
    line = r"X:\example-game-workbench-repo  # abs-path-ok: quoting the 2026-07-28 incident"
    assert not detect_in_text(line)


def test_bare_marker_without_reason_does_not_exempt() -> None:
    trailing_colon_no_reason = "abs-path-ok:"
    line = "X:\\example-game-workbench-repo  # " + trailing_colon_no_reason
    hits = detect_in_text(line)
    assert any(f.rule == "drive-letter" for f in hits)


def test_new_violations_ignores_unchanged_legacy_citation() -> None:
    before = "legacy: X:\\some-repo\nunrelated line\n"
    after = "legacy: X:\\some-repo\nunrelated line, now edited\n"
    assert new_violations(before, after) == []


def test_new_violations_flags_freshly_introduced_citation() -> None:
    before = "legacy: X:\\some-repo\n"
    after = "legacy: X:\\some-repo\nnew one: /Users/realperson/x\n"
    new = new_violations(before, after)
    assert len(new) == 1
    assert new[0].rule == "posix-home"


def test_new_violations_flags_a_duplicate_of_an_existing_citation() -> None:
    before = "X:\\some-repo\n"
    after = "X:\\some-repo\nX:\\some-repo\n"
    new = new_violations(before, after)
    assert len(new) == 1


def test_dead_registry_rung_flagged_in_shell_live_use() -> None:
    hits = detect_in_text(
        'source "~/.claude/machine-local/env.sh"\n',
        filename="setup.sh",
    )
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_dead_registry_rung_flags_home_spelling_too() -> None:
    hits = detect_in_text(
        'source "$HOME/.claude/machine-local/env.sh"\n',
        filename="setup.sh",
    )
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_hardcoded_plugin_exec_path_flagged() -> None:
    hits = detect_in_text(
        'run "~/.claude/plugins/coordinator-claude/coordinator/bin/foo"\n',
        filename="setup.sh",
    )
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_dead_registry_rung_not_flagged_in_python_docstring() -> None:
    text = (
        '"""\n'
        "module doc mentioning ~/.claude/machine-local as prior art.\n"
        '"""\n'
    )
    hits = detect_in_text(text, filename="mod.py")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_not_flagged_in_argparse_help_text() -> None:
    text = (
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--x', help='replaces hardcoded ~/.claude/machine-local reads')\n"
    )
    hits = detect_in_text(text, filename="mod.py")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_flagged_in_live_python_use() -> None:
    text = (
        "import os\n"
        "open(os.path.expanduser('~/.claude/machine-local/env.sh'))\n"
    )
    hits = detect_in_text(text, filename="mod.py")
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_dead_registry_rung_not_flagged_on_echo_line() -> None:
    text = 'echo "see ~/.claude/machine-local/env.sh" >&2\n'
    hits = detect_in_text(text, filename="setup.sh")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_not_flagged_on_comment_line() -> None:
    text = "# see ~/.claude/machine-local/env.sh for the old shape\n"
    hits = detect_in_text(text, filename="setup.sh")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_not_flagged_inside_backticks() -> None:
    text = "see `~/.claude/machine-local/env.sh` for the dead rung\n"
    hits = detect_in_text(text, filename="doc.md")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_exempted_by_marker() -> None:
    text = "source ~/.claude/machine-local/env.sh  # abs-path-ok: incident writeup quoting the real path\n"
    hits = detect_in_text(text, filename="setup.sh")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_skipped_on_unparseable_python() -> None:
    text = "def broken(:\n    ~/.claude/machine-local\n"
    hits = detect_in_text(text, filename="mod.py")
    assert "dead-registry-rung" not in _rules(hits)


def test_review_trail_diff_transcript_is_exempt() -> None:
    text = "-legacy: X:\\some-repo\n+legacy: repo-alias:some-repo\n"
    hits = detect_in_text(text, filename="state/review-trail/diffs/corpus-path-sweep.diff")
    assert not hits


def test_subagent_share_sidecar_is_exempt() -> None:
    text = "Finding: drive-letter citation at foo.md:3 -- `X:\\Users\\realperson\\notes.txt`\n"
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
        "**Working directory:** X:\\some-repo\n"
        "-legacy: C:\\Users\\realperson\\.claude\n"
    )
    assert not detect_in_text(text, filename="state/review-findings/20260627T120301Z/deps.md")
    assert not detect_in_text(
        text, filename="state/review-findings/2026-06-01-weekly/diff.patch"
    )


def test_review_trail_hand_authored_note_is_not_exempt() -> None:
    text = "the repo lives at X:\\example-game-workbench-repo\n"
    hits = detect_in_text(
        text, filename="state/review-trail/2026-07-01-boundary-union-finding.md"
    )
    assert any(f.rule == "drive-letter" for f in hits)


def test_evidence_artifact_exemption_is_prefix_scoped_not_ambient() -> None:
    text = "the repo lives at X:\\example-game-workbench-repo\n"
    hits = detect_in_text(text, filename="state/handoffs/some-handoff.md")
    assert any(f.rule == "drive-letter" for f in hits)


def test_capture_data_jsonl_under_audits_data_is_exempt() -> None:
    text = '{"cwd": "/Users/realperson/X/claude-klabauter"}\n'
    hits = detect_in_text(text, filename="state/audits/data/2026-08-13-capture.jsonl")
    assert not hits


def test_capture_data_json_under_recovery_is_exempt() -> None:
    text = '{"cwd": "/Users/realperson/X/claude-klabauter"}\n'
    hits = detect_in_text(text, filename="state/recovery/2026-08-13-snapshot.json")
    assert not hits


def test_capture_data_hand_authored_md_under_recovery_is_not_exempt() -> None:
    text = "the repo lives at /Users/realperson/X/claude-klabauter\n"
    hits = detect_in_text(text, filename="state/recovery/2026-08-13-findings.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_capture_data_hand_authored_py_under_recovery_is_not_exempt() -> None:
    text = "the repo lives at /Users/realperson/X/claude-klabauter\n"
    hits = detect_in_text(text, filename="state/recovery/probe.py")
    assert any(f.rule == "posix-home" for f in hits)


def test_json_outside_prefix_is_exempt_on_format_alone() -> None:
    text = '{"cwd": "/Users/realperson/X/claude-klabauter"}\n'
    hits = detect_in_text(text, filename="state/handoffs/some-handoff.json")
    assert not hits


def test_jsonl_outside_prefix_is_exempt_on_format_alone() -> None:
    """The sent-ledger case DoE-claude named: `.jsonl` matching
    `_CAPTURE_DATA_EXTENSIONS` exactly but living outside every prefix."""
    text = '{"delivery_commit_reason": "fatal: /Users/realperson/X/claude-klabauter/x"}\n'
    hits = detect_in_text(text, filename="state/memo-outbox/sent-ledger.jsonl")
    assert not hits


def test_patch_outside_prefix_is_not_exempt() -> None:
    text = "+the repo lives at /Users/realperson/X/claude-klabauter\n"
    hits = detect_in_text(text, filename="state/handoffs/some-change.patch")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_scalar_exempt_via_content_model_not_extension() -> None:
    text = "decision_note: failed at /Users/realperson/X/claude-klabauter/x\n"
    hits = detect_in_text(text, filename="state/sizings/some-sizing.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_new_violations_evidence_artifact_filename_exempts_both_sides() -> None:
    before = "no findings yet\n"
    after = "Finding: drive-letter citation -- `X:\\Users\\realperson\\notes.txt`\n"
    new = new_violations(
        before, after, filename="state/subagent-share/some-session/coordinatorreview-integrator-abc.md"
    )
    assert new == []


def test_dead_registry_rung_py_comment_is_excluded() -> None:
    text = "x = 1\n# under ~/.claude/plugins/coordinator-claude/ is one level above a\ny = 2\n"
    hits = detect_in_text(text, filename="mod.py")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_content_detects_python_under_sh_extension() -> None:
    text = (
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n"
        '"""\n'
        "Negative-spec: does NOT touch ~/.claude/plugins/marketplaces/ -- cache tree only.\n"
        '"""\n'
    )
    hits = detect_in_text(text, filename="dev-sync.sh")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_py_extension_unparseable_still_skips_whole_file() -> None:
    """The fail-safe (`.py`-extension file with broken syntax gets NO
    `dead-registry-rung` findings at all) still holds after the content-
    based detection change -- it stays gated on the EXTENSION, not on
    whether `ast.parse` happened to succeed."""
    text = "def broken(:\n    ~/.claude/machine-local\n"
    hits = detect_in_text(text, filename="mod.py")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_yaml_prose_key_not_flagged() -> None:
    text = (
        "created: 2026-07-21\n"
        "body: \"prepare-commit-msg exec's ~/.claude/plugins/coordinator-claude/bin with no guard\"\n"
    )
    hits = detect_in_text(text, filename="entry.yaml")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_yaml_folded_prose_continuation_not_flagged() -> None:
    text = (
        "- id: doe-L11\n"
        "  title: Grep must cover BOTH the CLAUDE_PLUGIN_ROOT:- fallback\n"
        "    form AND the bare $HOME/.claude/plugins/... form\n"
        "  scope: project\n"
    )
    hits = detect_in_text(text, filename="records.yaml")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_yaml_folded_prose_continuation_word_colon_shape_not_flagged() -> None:
    text = (
        "created: 2026-08-03\n"
        "title: A narrative record\n"
        "body: |\n"
        "  The registry used to live at ~/.claude/machine-local and callers hardcoded it.\n"
        "  Note: the registry lives at ~/.claude/machine-local, not there anymore.\n"
        "  Fix: route through the resolver instead of ~/.claude/machine-local.\n"
        "status: open\n"
    )
    hits = detect_in_text(text, filename="example.yaml")
    assert "dead-registry-rung" not in _rules(hits)


def test_dead_registry_rung_yaml_folded_continuation_ends_at_next_key() -> None:
    text = (
        "  title: some lesson title that keeps going\n"
        "    and folds onto this line\n"
        "  scope: ~/.claude/machine-local\n"
        "  change_kind: wiki-append\n"
    )
    hits = detect_in_text(text, filename="records.yaml")
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_dead_registry_rung_yaml_non_prose_key_not_swallowed() -> None:
    text = "cwd: \"~/.claude/plugins/coordinator-claude/coordinator\"\n"
    hits = detect_in_text(text, filename="evidence.yaml")
    assert any(f.rule == "dead-registry-rung" for f in hits)


def test_dead_registry_rung_json_description_field_not_flagged() -> None:
    text = '{\n  "description": "True when ~/.claude/plugins/coordinator-claude/coordinator/CLAUDE.md exists."\n}\n'
    hits = detect_in_text(text, filename="schema.json")
    assert "dead-registry-rung" not in _rules(hits)


def test_yaml_plain_scalar_still_fires() -> None:
    text = "note: the failure was at /Users/realperson/X/claude-klabauter/x\n"
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_single_quoted_scalar_still_fires() -> None:
    text = "note: 'the failure was at /Users/realperson/X/claude-klabauter/x'\n"
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_double_quoted_scalar_still_fires() -> None:
    text = 'note: "the failure was at /Users/realperson/X/claude-klabauter/x"\n'
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_path_shaped_mapping_key_still_fires() -> None:
    text = "/Users/realperson/X/claude-klabauter: value\n"
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_folded_block_scalar_exempt() -> None:
    text = (
        "note: >-\n"
        "  the failure was at /Users/realperson/X/claude-klabauter/x\n"
    )
    assert not detect_in_text(text, filename="entry.yaml")


def test_yaml_literal_block_scalar_exempt() -> None:
    text = (
        "note: |\n"
        "  the failure was at /Users/realperson/X/claude-klabauter/x\n"
    )
    assert not detect_in_text(text, filename="entry.yaml")


def test_yaml_comment_line_still_fires() -> None:
    text = "# see /Users/realperson/X/claude-klabauter/x for the shape\n"
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_frontmatter_value_still_fires() -> None:
    text = (
        "---\n"
        "note: the failure was at /Users/realperson/X/claude-klabauter/x\n"
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
        "the repo lives at /Users/realperson/X/claude-klabauter\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_fenced_backtick_block_exempt() -> None:
    text = (
        "prose before\n"
        "```\n"
        "cd /Users/realperson/X/claude-klabauter\n"
        "```\n"
        "prose after\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_fenced_tilde_block_exempt() -> None:
    text = (
        "prose before\n"
        "~~~\n"
        "cd /Users/realperson/X/claude-klabauter\n"
        "~~~\n"
        "prose after\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_indented_code_block_exempt() -> None:
    text = "prose\n\n    cd /Users/realperson/X/claude-klabauter\n"
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_prose_between_two_fences_still_fires() -> None:
    text = (
        "```\ncode block one\n```\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"
        "```\ncode block two\n```\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_unterminated_fence_does_not_swallow_rest_of_file() -> None:
    text = (
        "```\n"
        "unterminated code block\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_horizontal_rule_dash_is_not_frontmatter() -> None:
    text = (
        "# Title\n"
        "\n"
        "---\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_unterminated_frontmatter_does_not_swallow_rest_of_file() -> None:
    text = (
        "---\n"
        "note: fine\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_py_string_literal_path_still_fires() -> None:
    text = "path = '/Users/realperson/X/claude-klabauter'\n"
    hits = detect_in_text(text, filename="mod.py")
    assert any(f.rule == "posix-home" for f in hits)


def test_class1_json_still_exempt_after_classes_2_3() -> None:
    text = '{"cwd": "/Users/realperson/X/claude-klabauter"}\n'
    hits = detect_in_text(text, filename="state/handoffs/some-handoff.json")
    assert not hits


def test_yaml_folded_dash_chomping_block_scalar_exempt() -> None:
    text = (
        "note: >-\n"
        "  the failure was at /Users/realperson/X/claude-klabauter/x\n"
    )
    assert not detect_in_text(text, filename="entry.yaml")


def test_md_fence_info_string_still_exempt() -> None:
    text = (
        "prose before\n"
        "```bash\n"
        "cd /Users/realperson/X/claude-klabauter\n"
        "```\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_longer_fence_not_closed_by_shorter_run() -> None:
    text = (
        "`````\n"
        "```\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"
        "`````\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_mixed_char_fence_not_closed() -> None:
    text = (
        "~~~\n"
        "cd /Users/realperson/X/claude-klabauter\n"
        "```\n"
        "~~~\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_md_html_comment_inside_fence_exempt() -> None:
    text = (
        "```\n"
        "<!-- the repo lives at /Users/realperson/X/claude-klabauter -->\n"
        "```\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert not hits


def test_yaml_malformed_keeps_firing() -> None:
    text = (
        "note: [unclosed flow sequence\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"
    )
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_yaml_tab_indented_keeps_firing() -> None:
    text = "note:\n\t- the repo lives at /Users/realperson/X/claude-klabauter\n"
    hits = detect_in_text(text, filename="entry.yaml")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_four_space_lazy_list_continuation_still_fires() -> None:
    """Negative spec: a 4-space-indented line that is a LAZY CONTINUATION of
    an open list item's paragraph text is not indented code (CommonMark) --
    it must stay in scope, not be swallowed as a code block."""
    text = (
        "- item one starts here\n"
        "    continues at /Users/realperson/X/claude-klabauter as a lazy line\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_lazy_list_continuation_after_blank_line_still_fires() -> None:
    text = (
        "- item one starts here\n"
        "\n"
        "    /Users/realperson/X/claude-klabauter is item text, not code\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_ordered_list_continuation_after_blank_line_still_fires() -> None:
    text = (
        "1. item one starts here\n"
        "\n"
        "    /Users/realperson/X/claude-klabauter is item text, not code\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_genuine_indented_code_inside_list_item_is_exempt() -> None:
    text = (
        "- item one starts here\n"
        "\n"
        "      /Users/realperson/X/claude-klabauter\n"
    )
    assert not detect_in_text(text, filename="doc.md")


def test_md_deeply_nested_list_continuation_still_fires() -> None:
    text = (
        "- a\n"
        "  - b\n"
        "    - c\n"
        "      - d\n"
        "\n"
        "        /Users/realperson/X/claude-klabauter is item text\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_genuine_code_in_deeply_nested_item_is_exempt() -> None:
    text = (
        "- a\n"
        "  - b\n"
        "    - c\n"
        "      - d\n"
        "\n"
        "            /Users/realperson/X/claude-klabauter\n"
    )
    assert not detect_in_text(text, filename="doc.md")


def test_md_wide_ordered_marker_continuation_still_fires() -> None:
    text = (
        "10. item one starts here\n"
        "\n"
        "    /Users/realperson/X/claude-klabauter is item text\n"
    )
    hits = detect_in_text(text, filename="posix.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_blockquote_nested_list_falls_back_to_firing() -> None:
    text = (
        "> - item one\n"
        ">\n"
        ">     /Users/realperson/X/claude-klabauter\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_indented_code_after_list_closes_is_exempt() -> None:
    text = (
        "- item one\n"
        "\n"
        "prose at the margin closes the list\n"
        "\n"
        "    /Users/realperson/X/claude-klabauter\n"
    )
    assert not detect_in_text(text, filename="doc.md")


def test_md_thematic_break_not_mistaken_for_frontmatter() -> None:
    text = (
        "prose\n"
        "\n"
        "---\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_setext_heading_underline_not_mistaken_for_frontmatter() -> None:
    text = (
        "Heading Text\n"
        "---\n"
        "the repo lives at /Users/realperson/X/claude-klabauter\n"
    )
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)


def test_md_inline_code_span_still_fires() -> None:
    text = "see `/Users/realperson/X/claude-klabauter` for the shape\n"
    hits = detect_in_text(text, filename="doc.md")
    assert any(f.rule == "posix-home" for f in hits)
