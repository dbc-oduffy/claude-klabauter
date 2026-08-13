"""
coordinator_core.ops.session.tests.test_guard_foreign_platform_paths

Coverage (AC-5 of the originating dispatch):
  (a) clean POSIX config -> no findings.
  (b) Windows-shaped paths on a POSIX host -> findings, shape tagged.
  (c) POSIX-shaped paths on a Windows host -> findings, shape tagged.
  (d) mixed (both shapes present, single host) -> only the foreign shape flags.
  (e) malformed / absent settings.json -> silent empty-string banner, never raises.

Also covers the `suggested` correction path (via `.doe-root`) and the
shape-regex false-positive guards (URLs, UNC paths must NOT match).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.session.guard_foreign_platform_paths import (
    detect_foreign_platform_paths,
    detect_foreign_platform_paths_in_prose,
    evaluate_foreign_platform_paths,
    format_banner,
    format_prose_banner,
)


def _posix_settings():
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {
                            "command": "python3 /Users/alice/X/example-doctrine-repo/coordinator/hooks/scripts/foo.py",
                        }
                    ]
                }
            ]
        },
        "extraKnownMarketplaces": {
            "example-retrieval-repo": {"source": {"source": "directory", "path": "/Users/alice/X/example-retrieval-repo"}}
        },
    }


def _windows_corrupted_settings():
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {
                            "command": "python3 X:/example-doctrine-repo/coordinator/hooks/scripts/foo.py",
                        }
                    ]
                }
            ]
        },
        "extraKnownMarketplaces": {
            "example-game-workbench-repo": {
                "source": {"source": "directory", "path": "C:\\Users\\alice\\.claude\\plugins\\example-game-workbench-repo"}
            }
        },
    }


# --- (a) clean POSIX config on a POSIX host -----------------------------------


def test_clean_posix_on_posix_host_no_findings():
    findings = detect_foreign_platform_paths(_posix_settings(), host_is_windows=False)
    assert findings == []


# --- (b) Windows-shaped paths on a POSIX host ---------------------------------


def test_windows_paths_on_posix_host_flagged():
    findings = detect_foreign_platform_paths(_windows_corrupted_settings(), host_is_windows=False)
    assert len(findings) == 2
    shapes = {f.shape for f in findings}
    assert shapes == {"windows-drive-path-on-posix-host"}
    pointers = {f.pointer for f in findings}
    assert any("command" in p for p in pointers)
    assert any("path" in p for p in pointers)


# --- (c) POSIX-shaped paths on a Windows host ---------------------------------


def test_posix_paths_on_windows_host_flagged():
    findings = detect_foreign_platform_paths(_posix_settings(), host_is_windows=True)
    assert len(findings) == 2
    shapes = {f.shape for f in findings}
    assert shapes == {"posix-path-on-windows-host"}


def test_clean_windows_on_windows_host_no_findings():
    windows_native = {
        "hooks": {"PreToolUse": [{"hooks": [{"command": "python3 C:/Users/alice/example-doctrine-repo/coordinator/hooks/scripts/foo.py"}]}]},
    }
    findings = detect_foreign_platform_paths(windows_native, host_is_windows=True)
    assert findings == []


# --- (d) mixed shapes, single host --------------------------------------------


def test_mixed_shapes_posix_host_only_windows_shape_flagged():
    mixed = {
        "a": "python3 /Users/alice/X/example-doctrine-repo/coordinator/hooks/scripts/ok.py",
        "b": "python3 X:/example-doctrine-repo/coordinator/hooks/scripts/bad.py",
    }
    findings = detect_foreign_platform_paths(mixed, host_is_windows=False)
    assert len(findings) == 1
    assert findings[0].pointer == "/b"


# --- (e) malformed / absent settings.json -------------------------------------


def test_absent_settings_file_returns_empty_banner(tmp_path):
    missing = tmp_path / "settings.json"
    assert evaluate_foreign_platform_paths(missing) == ""


def test_malformed_json_returns_empty_banner_not_raise(tmp_path):
    bad = tmp_path / "settings.json"
    bad.write_text("{not valid json", encoding="utf-8")
    assert evaluate_foreign_platform_paths(bad) == ""


# --- shape-regex false-positive guards ----------------------------------------


def test_url_and_unc_paths_do_not_false_positive():
    urls = {
        "a": "https://github.com/dbc-oduffy/coordinator-claude",
        "b": "//server/share/some/path",
        "c": "http://localhost:8080/foo",
    }
    assert detect_foreign_platform_paths(urls, host_is_windows=False) == []
    assert detect_foreign_platform_paths(urls, host_is_windows=True) == []


# --- correction / suggestion path ---------------------------------------------


def test_suggestion_derived_from_doe_root(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".doe-root").write_text("/Users/alice/X/example-doctrine-repo", encoding="utf-8")
    settings = config_dir / "settings.json"
    import json

    settings.write_text(
        json.dumps(_windows_corrupted_settings()),
        encoding="utf-8",
    )

    banner = evaluate_foreign_platform_paths(settings, config_dir=config_dir, host_is_windows=False)
    assert "FOREIGN-PLATFORM PATH(S) DETECTED" in banner
    assert "/Users/alice/X/example-doctrine-repo/coordinator/hooks/scripts/foo.py" in banner


def test_no_doe_root_suggestion_is_none(tmp_path):
    findings = detect_foreign_platform_paths(
        _windows_corrupted_settings(), host_is_windows=False, local_coordinator_root=None
    )
    assert all(f.suggested is None for f in findings)


# --- banner text sanity --------------------------------------------------------


def test_format_banner_empty_findings_is_empty_string():
    assert format_banner([], "settings.json") == ""


def test_format_banner_names_offending_keys():
    findings = detect_foreign_platform_paths(_windows_corrupted_settings(), host_is_windows=False)
    banner = format_banner(findings, "/Users/alice/.claude/settings.json")
    assert "settings.json" in banner
    assert "X:/example-doctrine-repo" in banner
    assert "DETECT-ONLY" in banner


# --- Cross-platform env-var-reference-syntax shape ------------------------------
#
# Coverage for the 2026-07-29 follow-up: `hook_root_env_expr()` resolves
# PER-GENERATING-MACHINE and carries no drive letter / POSIX root, so it is
# invisible to the path-shape regexes above. See module docstring "Detection
# is SHAPE-based" § the env-var-reference-syntax paragraph.


def _hooks_with_command(command: str):
    return {"hooks": {"PreToolUse": [{"hooks": [{"command": command}]}]}}


def test_windows_env_var_syntax_on_posix_host_flagged():
    data = _hooks_with_command('[ -n "$env:COORDINATOR_CONTENT_ROOT" ] && exit 127')
    findings = detect_foreign_platform_paths(data, host_is_windows=False)
    assert len(findings) == 1
    assert findings[0].shape == "windows-env-var-syntax-on-posix-host"
    assert findings[0].pointer.endswith("/command")


def test_posix_bare_dollar_var_on_windows_host_flagged():
    data = _hooks_with_command(
        '[ -n "$COORDINATOR_CONTENT_ROOT" ] && exec python3 "$COORDINATOR_CONTENT_ROOT/foo.py"'
    )
    findings = detect_foreign_platform_paths(data, host_is_windows=True)
    assert len(findings) == 1
    assert findings[0].shape == "posix-env-var-syntax-on-windows-host"


def test_posix_braced_dollar_var_on_windows_host_flagged():
    # No leading-slash tail after the brace -- that shape would also trip
    # the pre-existing (untouched) POSIX-path-on-Windows-host check, which
    # is a separate, correctly-firing finding class, not a regression here.
    data = _hooks_with_command('echo "root=${COORDINATOR_CONTENT_ROOT}"')
    findings = detect_foreign_platform_paths(data, host_is_windows=True)
    assert len(findings) == 1
    assert findings[0].shape == "posix-env-var-syntax-on-windows-host"


def test_real_generated_windows_command_via_shared_helpers_roundtrips_both_directions():
    """The exact probe named by the dispatch: build real hook commands with
    `hook_root_env_expr`/`wrap_hook_command_guarded` (read-only, no config
    touched) and confirm the detector now finds the cross-platform case in
    BOTH directions -- this probe returned ZERO findings before this fix."""
    from coordinator_core.install._shared import (
        hook_root_env_expr,
        wrap_hook_command_guarded,
    )

    # Each platform's command is built with ITS OWN env_expr, exactly as the
    # real installer does (`hook_root_env_expr(windows=<the machine
    # generating this settings.json>)`) -- feeding one platform's env_expr
    # into the other's `wrap_hook_command_guarded(windows=...)` call would
    # be a test-harness misuse, not the real cross-machine-sync scenario
    # under test (which is: a WHOLE command generated correctly on one
    # machine, then read unmodified on the other).
    posix_command = wrap_hook_command_guarded(
        f"python3 {hook_root_env_expr(windows=False)}/hooks/scripts/foo.py", windows=False
    )
    windows_command = wrap_hook_command_guarded(
        f"python3 {hook_root_env_expr(windows=True)}/hooks/scripts/foo.py", windows=True
    )
    assert hook_root_env_expr(windows=False) in posix_command
    assert hook_root_env_expr(windows=True) in windows_command

    # A macOS/Linux host receiving the Windows-generated command -> flagged.
    findings_posix_host = detect_foreign_platform_paths(
        _hooks_with_command(windows_command), host_is_windows=False
    )
    assert len(findings_posix_host) == 1
    assert findings_posix_host[0].shape == "windows-env-var-syntax-on-posix-host"

    # A Windows host receiving the POSIX-generated command -> flagged.
    findings_windows_host = detect_foreign_platform_paths(
        _hooks_with_command(posix_command), host_is_windows=True
    )
    assert len(findings_windows_host) == 1
    assert findings_windows_host[0].shape == "posix-env-var-syntax-on-windows-host"

    # Native (matching-platform) commands on their own platform -> silent.
    assert detect_foreign_platform_paths(
        _hooks_with_command(posix_command), host_is_windows=False
    ) == []
    assert detect_foreign_platform_paths(
        _hooks_with_command(windows_command), host_is_windows=True
    ) == []


# --- NOT-detected: the side that matters -----------------------------------------


def test_correct_platform_env_var_syntax_not_flagged():
    posix_data = _hooks_with_command('[ -n "$COORDINATOR_CONTENT_ROOT" ] && exit 127')
    assert detect_foreign_platform_paths(posix_data, host_is_windows=False) == []
    windows_data = _hooks_with_command('if ($env:COORDINATOR_CONTENT_ROOT) { exit 0 }')
    assert detect_foreign_platform_paths(windows_data, host_is_windows=True) == []


def test_env_var_syntax_in_non_command_field_not_flagged():
    """`$env:` / bare `$VAR` sitting in a description/prose field (not an
    executable `command` string) is not a functional break -- must not fire."""
    data = {
        "hooks": {
            "PreToolUse": [
                {
                    "description": "On Windows use $env:COORDINATOR_CONTENT_ROOT; "
                    "on POSIX use $COORDINATOR_CONTENT_ROOT instead.",
                    "hooks": [{"command": "python3 /Users/x/example-doctrine-repo/foo.py"}],
                }
            ]
        }
    }
    findings_posix = detect_foreign_platform_paths(data, host_is_windows=False)
    findings_windows = detect_foreign_platform_paths(data, host_is_windows=True)
    assert all("description" not in f.pointer for f in findings_posix)
    assert all("description" not in f.pointer for f in findings_windows)


def test_url_scheme_not_flagged_as_env_var():
    data = _hooks_with_command("curl https://example.com/path && exit 0")
    assert detect_foreign_platform_paths(data, host_is_windows=False) == []
    assert detect_foreign_platform_paths(data, host_is_windows=True) == []


def test_dollar_used_arithmetically_or_literally_not_flagged():
    data = _hooks_with_command("echo 'cost is $5.00 today' && exit 0")
    assert detect_foreign_platform_paths(data, host_is_windows=False) == []
    assert detect_foreign_platform_paths(data, host_is_windows=True) == []


def test_windows_path_in_non_command_field_not_flagged_as_env_var_shape():
    """A Windows-shaped path sitting in a comment/docstring-ish field (not a
    `command` pointer) must not trip the NEW env-var-shape check (the
    pre-existing path-shape check is untouched behavior, out of scope here)."""
    data = {
        "notes": "See X:/example-doctrine-repo/coordinator/hooks/scripts/foo.py for context.",
        "hooks": {"PreToolUse": [{"hooks": [{"command": "python3 /Users/x/example-doctrine-repo/foo.py"}]}]},
    }
    findings = detect_foreign_platform_paths(data, host_is_windows=False)
    env_shapes = {"windows-env-var-syntax-on-posix-host", "posix-env-var-syntax-on-windows-host"}
    assert all(f.shape not in env_shapes for f in findings)


# --- Prose scan (CLAUDE.md / CLAUDE.local.md) -----------------------------
#
# Coverage for the 2026-07-30 follow-up dispatch: the settings.json leg above
# is the machine-executable surface; this leg is the agent-readable surface
# (CLAUDE.md / CLAUDE.local.md prose), which legitimately DISCUSSES the very
# path shapes the guard exists to catch. See
# `detect_foreign_platform_paths_in_prose`'s own docstring for the false-
# positive rationale this class of test exercises.


def test_prose_leak_fires_on_asserted_repo_path():
    """The exact shape from the 2026-07-30 incident: a sibling-repo-map list
    entry asserting a specific hyphenated repo name after a drive letter."""
    text = "- `X:\\some-repo` — description of the repo"
    findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=False)
    assert len(findings) == 1
    assert findings[0].line == 1
    assert findings[0].shape == "windows-drive-path-on-posix-host"


def test_prose_leak_banner_names_file_line_and_remedy():
    text = "line one\n- `X:\\example-game-workbench-repo` — UE5 workbench\nline three"
    findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=False)
    assert len(findings) == 1
    assert findings[0].line == 2
    banner = format_prose_banner(findings, "CLAUDE.local.md (staged)")
    assert "CLAUDE.local.md (staged)" in banner
    assert "LINE:     2" in banner
    assert "machine-local get repos" in banner


def test_real_bare_root_illustration_line_stays_quiet():
    """The bare-root half of the two lines quoted in this guard's own
    dispatch brief, copied verbatim from the live `~/.claude/CLAUDE.local.md`
    -- a platform illustration with no trailing segment, must not fire."""
    line_one = (
        "**This list names repos, never paths — resolve every path at read "
        "time.** The checkout root differs per machine and per platform "
        "(`X:\\` on Windows-native, `/x/` under WSL/Git-Bash, `~/X/` on "
        "macOS), so any literal path written here is wrong on most machines "
        "that read it."
    )
    assert detect_foreign_platform_paths_in_prose(line_one, host_is_windows=False) == []


def test_real_location_claim_line_now_fires():
    """The companion line from the same dispatch brief IS a location claim
    (a per-machine directory named after the drive letter) and, under the
    structural any-segment-fires rule, correctly fires -- this is the case
    the brief calls out as needing a `foreign-path-ok` marker from the EM,
    not a heuristic exemption."""
    line_two = (
        "are names, not paths; the containing directory is per-machine "
        "(`E:\\dev` on the Windows machines that carry them) and several "
        "exist on no other host at all."
    )
    findings = detect_foreign_platform_paths_in_prose(line_two, host_is_windows=False)
    assert len(findings) == 1
    assert findings[0].shape == "windows-drive-path-on-posix-host"


def test_bare_drive_root_mention_not_flagged():
    text = "The checkout root is `X:\\` on Windows-native machines."
    assert detect_foreign_platform_paths_in_prose(text, host_is_windows=False) == []


def test_bare_forward_slash_drive_root_mention_not_flagged():
    text = "The checkout root is `X:/` on Windows-native machines."  # abs-path-ok: bare-root test fixture, not a real path
    assert detect_foreign_platform_paths_in_prose(text, host_is_windows=False) == []


def test_generic_single_word_segment_now_fires():
    """A hyphen-free, single-word segment used to be invisible to the old
    naming-convention heuristic; the structural rule fires on it, matching
    the real leaked lines (`experiments`, `example-os-repo`) this fix targets."""
    text = "The scratch root is `C:\\temp` on that machine."
    findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=False)
    assert len(findings) == 1


def test_hyphenated_segment_still_fires_even_short():
    text = "checked out at `X:\\my-repo` on that box"
    findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=False)
    assert len(findings) == 1


# --- Corpus regression: the two real fleet repos that motivated this fix ------
#
# `experiments` and `example-os-repo` are real, hyphen-free fleet repo names that were
# among the leaked lines in the file this guard was built to protect. The old
# hyphen/underscore-shaped heuristic missed both; the structural any-segment
# rule must catch both, alongside the hyphenated names it already caught.


def test_single_word_repo_name_experiments_fires():
    text = "- `X:\\experiments` — controlled experiments."  # abs-path-ok: real leaked-corpus fixture under test
    findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=False)
    assert len(findings) == 1
    banner = format_prose_banner(findings, "CLAUDE.local.md")
    assert "CLAUDE.local.md" in banner
    assert "LINE:     1" in banner
    assert "machine-local get repos" in banner


def test_single_word_repo_name_example_os_repo_fires():
    text = "- `X:\\example-os-repo` — Example Interactive repo."  # abs-path-ok: real leaked-corpus fixture under test
    findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=False)
    assert len(findings) == 1
    banner = format_prose_banner(findings, "CLAUDE.local.md")
    assert "CLAUDE.local.md" in banner
    assert "LINE:     1" in banner
    assert "machine-local get repos" in banner


def test_hyphenated_repo_name_example_game_workbench_repo_fires():
    text = "- `X:\\example-game-workbench-repo` — UE5 workbench."  # abs-path-ok: real leaked-corpus fixture under test
    findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=False)
    assert len(findings) == 1
    banner = format_prose_banner(findings, "CLAUDE.local.md")
    assert "CLAUDE.local.md" in banner
    assert "LINE:     1" in banner
    assert "machine-local get repos" in banner


def test_forward_slash_repo_name_coordinator_claude_fires():
    text = "- `X:/coordinator-claude` — OSS publish target."  # abs-path-ok: real leaked-corpus fixture under test
    findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=False)
    assert len(findings) == 1
    banner = format_prose_banner(findings, "CLAUDE.local.md")
    assert "CLAUDE.local.md" in banner
    assert "LINE:     1" in banner
    assert "machine-local get repos" in banner


def test_multi_segment_nested_path_example_sim_repo_fires():
    text = "checked out at `E:\\dev\\example-sim-repo` on that box"  # abs-path-ok: synthetic nested-segment fixture under test
    findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=False)
    assert len(findings) == 1
    banner = format_prose_banner(findings, "CLAUDE.local.md")
    assert "CLAUDE.local.md" in banner
    assert "LINE:     1" in banner
    assert "machine-local get repos" in banner


def test_prose_url_not_flagged():
    text = "See https://github.com/dbc-oduffy/coordinator-claude for the repo."
    assert detect_foreign_platform_paths_in_prose(text, host_is_windows=False) == []


def test_prose_inline_allow_marker_suppresses_detection():
    text = (
        "- `X:\\some-real-leak` — this would normally fire "
        "<!-- foreign-path-ok: deliberate doctrine illustration -->"
    )
    assert detect_foreign_platform_paths_in_prose(text, host_is_windows=False) == []


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- foreign-path-ok: markdown comment -->",
        "# foreign-path-ok: python or toml comment",
        "rem foreign-path-ok: windows batch comment",
        "// foreign-path-ok: c-style comment",
        "[foreign-path-ok: bracketed, as used in a .tmpl header]",
    ],
    ids=["html", "hash", "rem", "slashes", "bracket"],
)
def test_prose_allow_marker_works_in_any_comment_syntax(marker):
    """The TOKEN is the marker, not the wrapper.

    A marker only spellable as an HTML comment silently fails in every
    non-markdown file an author might need to mark -- a .py docstring, a .toml
    sample, a Windows .cmd header. That failure is invisible: the line reads as
    marked to a human and as unmarked to the guard.
    """
    text = f"- `X:\\some-real-leak` — deliberate mention {marker}"
    assert detect_foreign_platform_paths_in_prose(text, host_is_windows=False) == []


def test_prose_allow_marker_only_suppresses_its_own_line():
    text = (
        "- `X:\\some-real-leak` — this would normally fire "
        "<!-- foreign-path-ok: doctrine -->\n"
        "- `X:\\another-real-leak` — no marker on this line"
    )
    findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=False)
    assert len(findings) == 1
    assert findings[0].line == 2


def test_prose_posix_leak_on_windows_host_fires():
    text = "checked out at `/Users/alice/X/some-repo` on that box"
    findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=True)
    assert len(findings) == 1
    assert findings[0].shape == "posix-path-on-windows-host"


def test_prose_scan_native_host_shape_stays_quiet():
    """A POSIX-shaped path scanned on a POSIX host (native, not foreign) must
    not fire -- mirrors the settings.json leg's own host-conditioning."""
    text = "checked out at `/Users/alice/X/some-repo` on that box"
    assert detect_foreign_platform_paths_in_prose(text, host_is_windows=False) == []


def test_format_prose_banner_empty_findings_is_empty_string():
    assert format_prose_banner([], "CLAUDE.md") == ""


# --- String-escape false positive (2026-07-31) --------------------------------
#
# A word ending in a letter, a colon, then a JSON/Python string-escape
# sequence (`\n`, `\t`, `\r`, ...) supplies a bare backslash + one letter --
# indistinguishable from a one-character Windows drive path unless the
# escape-letter shape is excluded. See `_path_shape_regexes.WIN_DRIVE_RE`'s
# own docstring for the chosen fix and its residual, and that module's own
# docstring for the real fleet example this reproduces (example-doctrine-repo's
# `state/cockpit-emission.json`).


def test_word_colon_escaped_newline_does_not_fire():
    text = "some prose ending in a word:\\n(next clause continues)"  # abs-path-ok: escape-sequence false-positive fixture, not a real path
    assert detect_foreign_platform_paths_in_prose(text, host_is_windows=False) == []


def test_word_colon_escaped_tab_does_not_fire():
    text = "some prose ending in a word:\\t(next clause continues)"  # abs-path-ok: escape-sequence false-positive fixture, not a real path
    assert detect_foreign_platform_paths_in_prose(text, host_is_windows=False) == []


def test_word_colon_escaped_carriage_return_does_not_fire():
    text = "some prose ending in a word:\\r(next clause continues)"  # abs-path-ok: escape-sequence false-positive fixture, not a real path
    assert detect_foreign_platform_paths_in_prose(text, host_is_windows=False) == []


def test_real_shapes_still_fire_after_escape_letter_fix():
    """Every real shape named in the fix's constraints must still fire
    exactly as before -- the escape-letter exclusion must not swallow a
    genuine segment that merely STARTS with an escape letter (temp, dev,
    Users)."""
    cases = [
        "- `X:\\some-repo` — description of the repo",  # abs-path-ok: shape-regression fixture, duplicate of an earlier fixture in this file
        "- `X:/some-repo` — description of the repo",  # abs-path-ok: shape-regression fixture, duplicate of an earlier fixture in this file
        "checked out at `E:\\dev\\Thing` on that box",  # abs-path-ok: shape-regression fixture, not a real path
        "checked out at `C:\\Users\\someone\\rest` on that box",  # abs-path-ok: shape-regression fixture, not a real path
        "The scratch root is `C:\\temp` on that machine.",  # abs-path-ok: fixture, segment starts with escape letter 't' but is a real word, duplicate of an earlier fixture in this file
    ]
    for text in cases:
        findings = detect_foreign_platform_paths_in_prose(text, host_is_windows=False)
        assert len(findings) == 1, f"expected exactly one finding for: {text!r}"


def test_bare_drive_root_and_url_stay_quiet_after_escape_letter_fix():
    assert (
        detect_foreign_platform_paths_in_prose(
            "The checkout root is `X:\\` on Windows-native machines.",  # abs-path-ok: bare-root fixture, duplicate of an earlier fixture in this file
            host_is_windows=False,
        )
        == []
    )
    assert (
        detect_foreign_platform_paths_in_prose(
            "See https://github.com/dbc-oduffy/coordinator-claude for the repo.",
            host_is_windows=False,
        )
        == []
    )


def test_detect_never_writes_anything(tmp_path):
    """The guard must never mutate anything it inspects."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    settings = config_dir / "settings.json"
    payload = json.dumps(_hooks_with_command('[ -n "$env:FOO" ] && exit 127'))
    settings.write_text(payload, encoding="utf-8")
    before_mtime = settings.stat().st_mtime
    before_content = settings.read_text(encoding="utf-8")

    banner = evaluate_foreign_platform_paths(settings, config_dir=config_dir, host_is_windows=False)

    assert "windows-env-var-syntax-on-posix-host" in banner
    assert settings.read_text(encoding="utf-8") == before_content
    assert settings.stat().st_mtime == before_mtime
