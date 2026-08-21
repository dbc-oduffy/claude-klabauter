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


class TestUnrecognizedCallShapesForfeitConclusive:
    """Regression pins for a narrowing found in review of ddf8587d7d01.

    ``conclusive`` was computed only from the calls ``_ast_spawn_kind``
    recognizes, while the leg it retires (``_PY_NO_OP_SUPPRESSION_RE``) is a
    whole-file search with no shape restriction. A clean recognized call could
    therefore earn file-wide "I have seen everything" credit and silence a
    DETACHED_PROCESS carried by an unrecognized call elsewhere in the file —
    the guard going QUIET where it previously fired, which is the one
    direction this module's negative-spec forbids.
    """

    def test_bare_popen_import_with_detached_process_still_fires(self):
        """The reviewer's exact repro — ordinary style, not adversarial."""
        content = (
            "import subprocess\n"
            'subprocess.run(["cmd.exe", "/c", "dir"], creationflags=0x08000000)\n'
            "from subprocess import Popen\n"
            'Popen(["python.exe", "script.py"], creationflags=subprocess.DETACHED_PROCESS)\n'
        )
        assert _fires("mixed_shapes.py", content), (
            "a clean recognized call must not silence a DETACHED_PROCESS "
            "carried by an unrecognized call shape in the same file"
        )

    def test_aliased_module_spawn_with_creationflags_forfeits_conclusive(self):
        content = (
            "import subprocess as sp\n"
            "import subprocess\n"
            'subprocess.run(["cmd.exe", "/c", "dir"], creationflags=0x08000000)\n'
            'sp.Popen(["python.exe", "s.py"], creationflags=subprocess.DETACHED_PROCESS)\n'
        )
        _verdict, conclusive = guard._analyze_py_call_sites(content)
        assert conclusive is False, (
            "an aliased-module spawn carrying creationflags is unaccounted for"
        )
        assert _fires("aliased.py", content)

    def test_subprocess_call_family_with_literal_creationflags_is_now_conclusive(self):
        """``subprocess.call``/``check_output`` were outside ``_ast_spawn_kind``
        at review time, so this exact shape forfeited via the kwarg-keyed
        branch above rather than being analyzed directly. DR-345 Decision 1
        (b) (2026-08-21) widened ``_ast_spawn_kind`` to recognize the
        qualified ``subprocess.call``/``check_call``/``check_output`` forms
        (``coordinator_core.spawn_policy.spawn_names.SPAWN_NAMES_BY_MODULE``),
        so a LITERAL ``creationflags=`` keyword on ``check_output`` is now
        read directly by the RECOGNIZED branch — the call's own
        ``DETACHED_PROCESS`` reference is inspected and accounted for, so the
        view is genuinely complete rather than defensively forfeited. This is
        the strict improvement the widening exists to buy: it fires exactly
        as before, but now because the AST path SAW it, not because the
        coarse regex leg caught what an incomplete AST view missed."""
        content = (
            "import subprocess\n"
            'subprocess.run(["cmd.exe", "/c", "dir"], creationflags=0x08000000)\n'
            'subprocess.check_output(["git", "log"], creationflags=subprocess.DETACHED_PROCESS)\n'
        )
        verdict, conclusive = guard._analyze_py_call_sites(content)
        assert verdict == guard._AST_FIRE
        assert conclusive is True
        assert _fires("callfamily.py", content)

    def test_dict_literal_splat_on_a_recognized_call_still_forfeits_conclusive(self):
        """Integration review's counter-repro to the first fix.

        At review time `subprocess.call` was outside `_ast_spawn_kind`'s set,
        and the `flags = {...}` binding is an `ast.Dict` the Call-only walk
        never visits — so no literal `creationflags=` keyword existed
        anywhere for a kwarg-only check to find, and the clean
        `subprocess.run` above earned conclusiveness on its own while a real
        DETACHED_PROCESS went silent. Closed by also forfeiting on `**`
        splats (`kw.arg is None`).

        DR-345 Decision 1 (b) (2026-08-21) since widened `_ast_spawn_kind` to
        recognize the qualified `subprocess.call` form — this exact repro now
        forfeits via the RECOGNIZED branch's own `has_splat` check instead of
        the `kind is None` branch, since `subprocess.call` is recognized. The
        verdict (still forfeits, still fires) is unchanged; only the
        accounting PATH is. A splat is never resolved to a literal value
        regardless of which branch handles it, so widening call-name
        recognition alone was never going to close this one — named here so
        a future reader does not expect it to.
        """
        content = (
            "import subprocess\n"
            'subprocess.run(["notepad.exe"],\n'
            '    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))\n'
            'flags = {"creationflags": subprocess.DETACHED_PROCESS}\n'
            'subprocess.call(["cmd.exe", "/c", "dir"], **flags)\n'
        )
        _verdict, conclusive = guard._analyze_py_call_sites(content)
        assert conclusive is False, (
            "a dict-literal splat on an unrecognized call must forfeit "
            "conclusiveness — it can convey creationflags invisibly"
        )
        assert _fires("dictsplat.py", content)

    def test_flag_passed_positionally_to_a_wrapper_forfeits_conclusive(self):
        """The structural close: a no-op flag REFERENCE the walk never
        inspected forfeits conclusiveness, whatever syntax carries it.

        This case has no `creationflags` kwarg and no `**` splat anywhere — the
        flag is handed positionally to an unrecognized callable — so both
        earlier kwarg/splat-shaped fixes missed it. Keyed on the AST identifier
        rather than on text, which is what lets it coexist with
        `test_a_mere_mention_no_longer_condemns_a_clean_call_site`.
        """
        content = (
            "import subprocess\n"
            'subprocess.run(["notepad.exe"],\n'
            '    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))\n'
            "def my_popen(argv, cflags):\n"
            "    return _do_spawn(argv, cflags)\n"
            'my_popen(["cmd.exe", "/c", "dir"], subprocess.DETACHED_PROCESS)\n'
        )
        _verdict, conclusive = guard._analyze_py_call_sites(content)
        assert conclusive is False
        assert _fires("wrapper.py", content)

    def test_noop_flag_identifier_sets_stay_in_lockstep(self):
        """`_AST_NO_OP_FLAG_NAMES` and `_PY_NO_OP_SUPPRESSION_RE` are one list
        in two spellings — a spelling added to one alone is invisible to the
        other, the same drift hazard the console-target lockstep test pins."""
        for name in guard._AST_NO_OP_FLAG_NAMES:
            assert guard._PY_NO_OP_SUPPRESSION_RE.search(name), (
                f"{name} forfeits conclusiveness but is invisible to the regex"
            )

    def test_unrecognized_call_without_creationflags_does_not_forfeit(self):
        """The forfeit is keyed on the kwarg, so ordinary unrelated calls do
        not gratuitously destroy conclusiveness — otherwise the mirror-defect
        fix (a prose mention no longer condemning a clean file) would be dead
        in every real module."""
        content = (
            "import subprocess\n"
            'print("hello")\n'
            "len([1, 2, 3])\n"
            'subprocess.run(["git", "push"], creationflags=0x08000000)\n'
        )
        _verdict, conclusive = guard._analyze_py_call_sites(content)
        assert conclusive is True
        assert not _fires("ordinary.py", content)


class TestSpawnKindRecognizesTheWidenedUniverse:
    """DR-345 Decision 1 (b) (2026-08-21): `_ast_spawn_kind` now positively
    recognizes the full qualified-attribute spawn universe in
    `coordinator_core.spawn_policy.spawn_names.SPAWN_NAMES_BY_MODULE`, not
    only `subprocess.run`/`Popen`/`os.system`/the two `asyncio` forms it
    recognized before. Before this widening, none of the four forms below
    were visible to EITHER detector -- outside `_PY_SUBPROCESS_CALL_RE`'s
    alternation AND outside `_ast_spawn_kind`'s old recognized set -- so an
    unsuppressed console-target call through any of them evaded the guard
    entirely. Each case here fires on the AST path directly (`_AST_FIRE`),
    not via the regex fallback, proving the precise detector -- not just the
    coarse one -- now sees these forms."""

    def test_subprocess_call_console_target_now_fires(self):
        content = 'import subprocess\nsubprocess.call(["cmd.exe", "/c", "dir"])\n'
        verdict, _conclusive = guard._analyze_py_call_sites(content)
        assert verdict == guard._AST_FIRE
        assert _fires("bare_call.py", content)

    def test_subprocess_check_call_console_target_now_fires(self):
        content = 'import subprocess\nsubprocess.check_call(["git", "push"])\n'
        verdict, _conclusive = guard._analyze_py_call_sites(content)
        assert verdict == guard._AST_FIRE
        assert _fires("bare_check_call.py", content)

    def test_subprocess_check_output_console_target_now_fires(self):
        content = 'import subprocess\nsubprocess.check_output(["python.exe", "-V"])\n'
        verdict, _conclusive = guard._analyze_py_call_sites(content)
        assert verdict == guard._AST_FIRE
        assert _fires("bare_check_output.py", content)

    def test_os_popen_console_target_now_fires(self):
        """`os.popen`, like `os.system`, carries no `creationflags` parameter
        at all -- unsuppressable by construction, same as `os.system`."""
        content = 'import os\nos.popen("git status")\n'
        verdict, _conclusive = guard._analyze_py_call_sites(content)
        assert verdict == guard._AST_FIRE
        assert _fires("bare_popen.py", content)

    def test_suppressed_call_family_forms_stay_clean(self):
        content = (
            "import subprocess\n"
            'subprocess.call(["cmd.exe", "/c", "dir"], creationflags=0x08000000)\n'
            'subprocess.check_call(["git", "push"], creationflags=0x08000000)\n'
            'subprocess.check_output(["python.exe", "-V"], creationflags=0x08000000)\n'
        )
        verdict, conclusive = guard._analyze_py_call_sites(content)
        assert verdict == guard._AST_CLEAN
        assert conclusive is True
        assert not _fires("suppressed_call_family.py", content)


class TestKnownResidueSurvivesWideningB:
    """DR-345 Decision 1 (b) (2026-08-21) widened `_ast_spawn_kind` to the
    full `SPAWN_NAMES_BY_MODULE` universe (see `TestSpawnKindRecognizesTheWidenedUniverse`
    below). That widening closes the shapes where an unrecognized CALLEE was
    the only reason a no-op flag reference went unaccounted. It does NOT, and
    per its own module docstring negative-spec CANNOT, close a shape whose
    defect is upstream of call-name recognition: the closing sweep in
    `_analyze_py_call_sites` matches a no-op flag by its literal AST
    IDENTIFIER (`ast.Attribute.attr` / `ast.Name.id`), never by resolving
    what that identifier is bound to. Both cases below still forfeit-should
    (should report non-conclusive) but do not, because the flag never reaches
    the sweep under either of its own real names. Pinned here as an accepted,
    documented gap (module negative-spec, "KNOWN RESIDUE") rather than left
    silently rediscoverable -- these tests exist to FAIL loudly if a future
    change accidentally starts relying on `conclusive` being correct in
    either shape, and to stop a future reader from re-attempting (b) to close
    them, since (b) cannot.
    """

    def test_import_alias_of_a_no_op_flag_is_not_seen_by_the_sweep(self):
        """`from subprocess import DETACHED_PROCESS as DP` then
        `my_popen(argv, DP)` -- confirmed hole, round 3 integration review.
        The literal text `DETACHED_PROCESS` is still present (on the import
        line), so the file-wide regex leg WOULD catch this were it not gated
        behind `not ast_conclusive` -- and `ast_conclusive` is falsely True,
        so the correct verdict is computed by the regex and then discarded.
        `my_popen` is an ordinary, non-adversarial wrapper shape."""
        content = (
            "import subprocess\n"
            'subprocess.run(["cmd.exe", "/c", "dir"], creationflags=0x08000000)\n'
            "from subprocess import DETACHED_PROCESS as DP\n"
            "def my_popen(argv, cflags):\n"
            "    return _do_spawn(argv, cflags)\n"
            'my_popen(["python.exe", "script.py"], DP)\n'
        )
        verdict, conclusive = guard._analyze_py_call_sites(content)
        assert verdict == guard._AST_CLEAN
        assert conclusive is True, (
            "documents the residue: the AST view falsely reports conclusive "
            "-- an aliased DETACHED_PROCESS reference is invisible to the "
            "identifier sweep, which was never widened by (b)"
        )
        assert not _fires("import_alias_residue.py", content), (
            "documents the residue: this DETACHED_PROCESS use goes entirely "
            "unreported -- the regex leg's own correct verdict is discarded "
            "because ast_conclusive is falsely True"
        )

    def test_getattr_by_string_flag_lookup_is_not_seen_by_the_sweep(self):
        """`x = getattr(subprocess, "DETACHED_PROCESS", 0)` then
        `my_popen(argv, x)` -- confirmed hole, round 3 integration review.
        The flag name is an `ast.Constant` string, invisible to the sweep's
        `Attribute`/`Name` `isinstance` branches; the use-site identifier
        `x` carries no flag-shaped name either. `getattr`-by-name is
        ordinary style, not an adversarial construction."""
        content = (
            "import subprocess\n"
            'subprocess.run(["cmd.exe", "/c", "dir"], creationflags=0x08000000)\n'
            'x = getattr(subprocess, "DETACHED_PROCESS", 0)\n'
            "def my_popen(argv, cflags):\n"
            "    return _do_spawn(argv, cflags)\n"
            'my_popen(["python.exe", "script.py"], x)\n'
        )
        verdict, conclusive = guard._analyze_py_call_sites(content)
        assert verdict == guard._AST_CLEAN
        assert conclusive is True, (
            "documents the residue: the AST view falsely reports conclusive "
            "-- a getattr-by-string flag lookup is invisible to the "
            "identifier sweep, which was never widened by (b)"
        )
        assert not _fires("getattr_flag_residue.py", content), (
            "documents the residue: this DETACHED_PROCESS use goes entirely "
            "unreported -- the regex leg's own correct verdict is discarded "
            "because ast_conclusive is falsely True"
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
