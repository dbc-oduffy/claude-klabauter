"""Per-CALL-SITE detection tests for the console-popup authoring advisory.

Pins the 2026-08-21 fix for the file-wide suppression defect measured in
``state/audits/2026-08-21-detached-process-console-window-storm.md``:

  - FALSE NEGATIVE (the defect): ``_PY_SUPPRESSION_RE`` is a whole-file
    search, so ONE ``creationflags=`` anywhere silenced the guard for every
    OTHER spawn in the same file. Measured across this repo: 65 files blind,
    among them live CLIs such as ``coordinator/bin/publish.py`` (7 suppressed
    spawns standing in front of 9 bare ones). Reproduced order-independently.
  - FALSE POSITIVE (its mirror): ``_PY_NO_OP_SUPPRESSION_RE`` shares that
    file-wide scope, so one ``DETACHED_PROCESS`` line condemned every spawn in
    its file.

The fix layers ``_analyze_py_call_sites`` — an AST walk resolving each call's
OWN argv0 and reading that call's OWN keywords — IN FRONT OF the regex
cascade, never in place of it. Two properties are load-bearing and pinned
here:

  1. The AST path can only make the guard LOUDER (the two verdicts are ORed),
     so it cannot silently narrow the guard's field of view — the failure
     shape the module's negative-spec exists to prevent. The one scoped
     exception is the no-op-suppression leg, which defers to the per-call-site
     verdict only when the AST view is ``conclusive``.
  2. The regex path SURVIVES as the prose fallback. An AST-only detector
     cannot fire on a docstring-only file, and
     ``TestCaseCDocstringOnlyProse::test_write_pure_prose_file_still_surfaces_but_only_as_advisory``
     (DR-077, whole-file context + advisory) pins that pure prose must still
     surface. ``test_docstring_only_prose_still_surfaces`` below re-pins the
     same behaviour from this suite so an AST-side rewrite cannot quietly
     drop it.

Grep anchors: WINDOWS-CONSOLE-POPUP DR-077
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.write_guards import nudge_windows_subprocess_popup as guard


def _fires(file_path: str, content: str) -> bool:
    """True when the popup advisory fires on a Write of ``content``."""
    result = guard.check(
        {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}}
    )
    if result is None:
        return False
    hso = result.get("hookSpecificOutput", {})
    assert "additionalContext" in hso and "permissionDecision" not in hso, (
        "DR-077 part 2: this guard is advisory-class and must never deny"
    )
    return True


class TestFileWideSuppressionNoLongerHidesASibling:
    """The defect proper: a suppressed spawn standing in front of a bare one."""

    def test_suppressed_spawn_does_not_silence_a_bare_sibling(self):
        content = (
            "import subprocess\n"
            'subprocess.run(["git", "status"], creationflags=0x08000000)\n'
            'subprocess.run(["git", "push"])\n'
        )
        assert _fires("live_cli.py", content), (
            "the bare second spawn must surface even though the first is suppressed"
        )

    def test_order_independent_bare_spawn_first(self):
        """The audit reproduced the defect order-independently; so does the fix."""
        content = (
            "import subprocess\n"
            'subprocess.run(["git", "push"])\n'
            'subprocess.run(["git", "status"], creationflags=0x08000000)\n'
        )
        assert _fires("live_cli.py", content)

    def test_every_spawn_suppressed_stays_quiet(self):
        content = (
            "import subprocess\n"
            'subprocess.run(["git", "push"], creationflags=0x08000000)\n'
            'subprocess.Popen(["cmd.exe", "/c", "x"], creationflags=0x08000000)\n'
        )
        assert not _fires("clean.py", content), (
            "a genuinely clean file must not become a new false positive"
        )

    def test_non_console_target_sibling_is_not_policed(self):
        """``ls`` is not a console-subsystem target — resolving argv0 per call
        site must not turn every unsuppressed spawn into a fire."""
        content = (
            "import subprocess\n"
            'subprocess.run(["git", "x"], creationflags=0x08000000)\n'
            'subprocess.run(["ls", "-la"])\n'
        )
        assert not _fires("clean.py", content)

    def test_os_system_console_target_is_unsuppressable_and_fires(self):
        """``os.system`` accepts no creationflags at all, so a console target
        routed through it cannot be suppressed — it is not merely unsuppressed
        here, and a suppressed sibling must not speak for it."""
        content = (
            "import os\n"
            "import subprocess\n"
            'subprocess.run(["git", "x"], creationflags=0x08000000)\n'
            'os.system("git status")\n'
        )
        assert _fires("live_cli.py", content)

    def test_asyncio_bare_form_behind_a_suppressed_sibling_fires(self):
        content = (
            "import asyncio\n"
            "import subprocess\n"
            'subprocess.run(["git", "x"], creationflags=0x08000000)\n'
            "async def f():\n"
            '    await asyncio.create_subprocess_exec("git", "status")\n'
        )
        assert _fires("live_cli.py", content)

    def test_single_quoted_target_resolves_per_call_site(self):
        content = "import subprocess\nsubprocess.Popen(['powershell.exe', '-c', 'x'])\n"
        assert _fires("live_cli.py", content)


class TestNoOpSuppressionIsAlsoPerCallSite:
    """The mirror defect — one ``DETACHED_PROCESS`` line condemning a file."""

    def test_detached_process_at_the_call_site_still_fires(self):
        content = (
            "import subprocess\n"
            'subprocess.Popen(["git", "log"], creationflags=subprocess.DETACHED_PROCESS)\n'
        )
        assert _fires("detached.py", content)

    def test_detached_process_ored_with_create_no_window_still_fires(self):
        """Win32 ignores CREATE_NO_WINDOW alongside DETACHED_PROCESS — the
        belt-and-braces spelling is measured identical to bare detached."""
        content = (
            "import subprocess\n"
            'subprocess.Popen(["git", "log"],\n'
            "    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)\n"
        )
        assert _fires("detached.py", content)

    def test_a_mere_mention_no_longer_condemns_a_clean_call_site(self):
        """A module-level string naming DETACHED_PROCESS is not a spawn flag.
        Pre-fix this condemned the correctly-suppressed spawn below it."""
        content = (
            "import subprocess\n"
            'FLAG_DOC = "DETACHED_PROCESS is a no-op alongside CREATE_NO_WINDOW"\n'
            'subprocess.run(["git", "push"], creationflags=0x08000000)\n'
        )
        assert not _fires("clean.py", content)

    def test_opaque_creationflags_variable_keeps_the_file_wide_leg(self):
        """``creationflags=flags`` is unreadable from its own source, so the
        AST view is NOT conclusive and the coarse file-wide leg must keep its
        say. A detector that cannot see a construct must never be the reason
        the guard goes quiet about it."""
        content = (
            "import subprocess\n"
            "flags = subprocess.DETACHED_PROCESS\n"
            'subprocess.Popen(["git", "log"], creationflags=flags)\n'
        )
        assert _fires("opaque.py", content)


class TestUnreadableConstructsDegradeToTheRegexPath:
    """Never-narrow: anything the AST walk cannot resolve falls back, not silent."""

    def test_kwargs_splat_is_not_treated_as_a_bare_spawn(self):
        content = (
            "import subprocess\n"
            'KW = {"creationflags": 0x08000000}\n'
            'subprocess.run(["git", "push"], **KW)\n'
        )
        assert not _fires("splat.py", content)

    def test_unparseable_content_still_fires_via_the_regex_path(self):
        content = 'import subprocess\ndef f(:\nsubprocess.run(["git", "push"])\n'
        assert _fires("broken.py", content)

    def test_docstring_only_prose_still_surfaces(self):
        """DR-077 Case C, re-pinned from the AST side: an AST-only detector
        finds no call site here, so the regex prose fallback MUST survive.
        Deleting it to make an AST rewrite pass is the failure this guards."""
        content = '"""Docs: we call subprocess.run(["git", "status"]) somewhere."""\n'
        assert _fires("prose.py", content)

    def test_allowlist_marker_still_clears_a_per_call_site_fire(self):
        content = (
            "# popup-intentional-last-resort\n"
            "import subprocess\n"
            'subprocess.run(["git", "push"])\n'
        )
        assert not _fires("escape.py", content)


class TestOversizedFileEditFragments:
    """``coordinator/bin/publish.py`` — the audit's headline example at ~558KB —
    is over ``_MAX_WHOLE_FILE_BYTES``, so ``_read_file_safely`` refuses it and
    the guard sees the raw edit fragment instead of a reconstructed whole file.
    Without the indented-fragment parse retry the fix would not reach the very
    file the defect was named for."""

    def _edit_fires(self, tmp_path: Path, new_string: str) -> bool:
        target = tmp_path / "huge.py"
        target.write_text("x = 1\n" + "# pad\n" * 200, encoding="utf-8")
        result = guard.check(
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(target),
                    "old_string": "ANCHOR_ABSENT_FORCES_FRAGMENT_FALLBACK",
                    "new_string": new_string,
                },
            }
        )
        return result is not None

    def test_bare_spawn_in_an_indented_fragment_fires(self, tmp_path: Path):
        assert self._edit_fires(
            tmp_path,
            '    subprocess.run(["git", "push"], capture_output=True, check=True)\n',
        )

    def test_suppressed_spawn_in_an_indented_fragment_clears(self, tmp_path: Path):
        assert not self._edit_fires(
            tmp_path, '    subprocess.run(["git", "push"], creationflags=0x08000000)\n'
        )


class TestAnalyzerContract:
    """Direct assertions on the analyzer's own two-value contract."""

    def test_no_recognized_call_is_unknown_not_clean(self):
        verdict, conclusive = guard._analyze_py_call_sites("x = 1\n")
        assert verdict == guard._AST_UNKNOWN
        assert conclusive is False

    def test_unresolvable_argv0_is_not_conclusive(self):
        _verdict, conclusive = guard._analyze_py_call_sites(
            "import subprocess\ncmd = ['git']\nsubprocess.run(cmd)\n"
        )
        assert conclusive is False, (
            "a dynamic argv0 must never be reported as a complete view"
        )

    def test_resolved_clean_file_is_conclusive(self):
        verdict, conclusive = guard._analyze_py_call_sites(
            "import subprocess\n"
            'subprocess.run(["git", "push"], creationflags=0x08000000)\n'
        )
        assert verdict == guard._AST_CLEAN
        assert conclusive is True

    def test_ast_targets_stay_in_lockstep_with_the_regex_alternation(self):
        """The two detectors share one console-target list in two spellings.
        Drift means the precise one stops seeing a target the coarse one still
        names — widen both or neither."""
        for name in guard._AST_CONSOLE_TARGET_NAMES:
            assert guard._PY_CONSOLE_TARGET_RE.search(f'"{name}"'), (
                f"{name} is policed per call site but invisible to the regex"
            )
