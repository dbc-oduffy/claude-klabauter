"""test_gen_launcher_shim.py — unit coverage for the whoami-bootstrap
launcher class.

Renamed from a `.test.py`-suffixed file; the body already used
unittest.TestCase with genuine `test_*` methods (directly pytest-collectable)
so no harness conversion was needed — this was a pure rename.

Spec backlink: docs/plans/2026-07-21-macos-first-class-invocation.md § C1, C9.

SCOPE: the whoami-bootstrap class, plus the .cmd/.ps1 interpreter-ladder
invariants that are properties of the EMITTED BODY rather than of the on-disk
launcher/entrypoint pairing (see BakedInterpreterExistenceGateTests below).
On-disk twin parity — every entrypoint has a launcher, every launcher targets
a real file — stays in coordinator_core/test_bin_launcher_parity.py.

RETIRED 2026-07-28: this file used to also cover ensure_unix_shebang() /
ensure_unix_invocable() — gen-launcher-shim.py's `--ensure-unix` mode, which
stamped a `#!/usr/bin/env python3` shebang + chmod +x onto a bin/*.py
entrypoint. That mode was removed (see gen-launcher-shim.py's own module
docstring § RETIRED): PM ruling 2026-07-28 reclassified exactly that shape
(env-stripped shebang, mode 100755) as a POSIX-only-execution portability
defect, so a generator that manufactured it on every install was fighting
the guard it should have satisfied. Removed the EnsureUnixShebangTests and
EnsureUnixInvocableTests classes along with the retired functions — no
coverage of live behavior was lost, since the functions no longer exist.

Also exercises render_whoami_bootstrap() / generate(whoami_bootstrap=True) —
the single named PEP-668 exception (C9): the emitted body must be a valid
bare-python3-shebanged script that resolves COORDINATOR_PYTHON and
subprocess-execs `<pin> -m coordinator_whoami ...`, NEVER importing
coordinator_whoami into its own process (D2-26), and must fail loud (exit
127) when no valid pin resolves.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Real-process spawn is load-bearing: the whoami-bootstrap tests exec the
# EMITTED bootstrap body as a real `sys.executable` subprocess (per module
# docstring, C9) to prove it never bare-imports coordinator_whoami into its
# own process -- an in-process mock cannot exhibit that distinction.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_MODULE_PATH = Path(__file__).with_name("gen-launcher-shim.py")
_spec = importlib.util.spec_from_file_location("gen_launcher_shim", _MODULE_PATH)
gls = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["gen_launcher_shim"] = gls  # dataclass needs module registered for annotation resolution
_spec.loader.exec_module(gls)


class WhoamiBootstrapTests(unittest.TestCase):
    """Coverage for C9's single named PEP-668 exception (see module
    docstring § WHOAMI-BOOTSTRAP EXCEPTION in gen-launcher-shim.py).
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def _write_fake_pin(self, argv_sink: Path) -> Path:
        """A fake `<venv-python>` stub: answers the `-c "import sys"`
        validation probe with exit 0, and for any `-m <module> ...`
        invocation records argv (as one line per arg) to `argv_sink`
        instead of actually importing anything — proves the bootstrap
        subprocess-execs rather than bare-importing (D2-26) without
        depending on a real coordinator_whoami install.

        The bootstrap spawns this path as argv[0], so the pin must be a
        DIRECTLY launchable executable — which is platform-shaped: POSIX
        resolves the shebang (given the exec bit), Windows resolves argv[0]
        through PATHEXT and cannot launch a shebang'd script at all. Both
        arms run the identical payload against the identical assertions;
        this is a launcher branch, never a coverage branch.
        """
        payload = self.tmp / "fake-venv-python.py"
        payload.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "argv = sys.argv[1:]\n"
            "if argv == ['-c', 'import sys']:\n"
            "    sys.exit(0)\n"
            f"with open({str(argv_sink)!r}, 'w') as fh:\n"
            "    fh.write('\\n'.join(argv))\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        if os.name == "nt":
            # python-direct .cmd launcher, the shape render_cmd() below emits
            # for every bin/ entrypoint (baked-interpreter fast path — no bash,
            # no PATH probe). %* forwards argv with the caller's quoting intact.
            pin = self.tmp / "fake-venv-python.cmd"
            with open(pin, "w", encoding="utf-8", newline="\r\n") as fh:
                fh.write(
                    "@echo off\n"
                    f'"{sys.executable}" "{payload}" %*\n'
                    "exit /b %ERRORLEVEL%\n"
                )
        else:
            pin = payload
            os.chmod(pin, 0o755)
        return pin

    def test_render_is_syntactically_valid_python(self) -> None:
        body = gls.render_whoami_bootstrap("coordinator-whoami")
        ast.parse(body)  # raises SyntaxError if malformed
        self.assertTrue(body.startswith("#!/usr/bin/env python3\n"))

    def test_render_never_bare_imports_the_module(self) -> None:
        body = gls.render_whoami_bootstrap("coordinator-whoami")
        self.assertNotIn("import coordinator_whoami", body)
        self.assertIn("subprocess", body)
        self.assertIn("-m", body)

    def test_generate_writes_executable_bootstrap_plus_cmd(self) -> None:
        written = gls.generate(
            "coordinator-whoami", self.tmp, ps1=True, whoami_bootstrap=True
        )
        names = {p.name for p in written}
        self.assertEqual(
            names, {"coordinator-whoami", "coordinator-whoami.cmd", "coordinator-whoami.ps1"}
        )
        bootstrap = self.tmp / "coordinator-whoami"
        mode = stat.S_IMODE(bootstrap.stat().st_mode)
        self.assertTrue(mode & stat.S_IXUSR, "bootstrap must be chmod +x")

    def test_generate_without_flag_omits_bootstrap(self) -> None:
        written = gls.generate("coordinator-whoami", self.tmp)
        names = {p.name for p in written}
        self.assertNotIn("coordinator-whoami", names)
        self.assertIn("coordinator-whoami.cmd", names)

    def test_bootstrap_subprocess_execs_pinned_interpreter_via_env(self) -> None:
        argv_sink = self.tmp / "argv.txt"
        pin = self._write_fake_pin(argv_sink)
        bootstrap = self.tmp / "coordinator-whoami"
        bootstrap.write_text(
            gls.render_whoami_bootstrap("coordinator-whoami"), encoding="utf-8"
        )
        bootstrap.chmod(0o755)

        env = dict(os.environ)
        env["COORDINATOR_PYTHON"] = str(pin)
        result = subprocess.run(
            [sys.executable, str(bootstrap), "--probe", "arg"],
            env=env,
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = argv_sink.read_text(encoding="utf-8").splitlines()
        self.assertEqual(recorded, ["-m", "coordinator_whoami", "--probe", "arg"])

    def test_bootstrap_fails_loud_with_no_resolvable_pin(self) -> None:
        bootstrap = self.tmp / "coordinator-whoami"
        bootstrap.write_text(
            gls.render_whoami_bootstrap("coordinator-whoami"), encoding="utf-8"
        )
        bootstrap.chmod(0o755)

        env = dict(os.environ)
        env["COORDINATOR_PYTHON"] = ""
        env["PATH"] = str(self.tmp)  # no machine-local on PATH, no lib-relative sibling
        result = subprocess.run(
            [sys.executable, str(bootstrap)],
            env=env,
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
        self.assertEqual(result.returncode, 127)
        self.assertIn("coordinator.python", result.stderr)

    def test_bootstrap_fails_loud_on_invalid_pin(self) -> None:
        bad_pin = self.tmp / "not-a-real-interpreter"
        bad_pin.write_text("not an executable\n", encoding="utf-8")
        bad_pin.chmod(0o755)
        bootstrap = self.tmp / "coordinator-whoami"
        bootstrap.write_text(
            gls.render_whoami_bootstrap("coordinator-whoami"), encoding="utf-8"
        )
        bootstrap.chmod(0o755)

        env = dict(os.environ)
        env["COORDINATOR_PYTHON"] = str(bad_pin)
        result = subprocess.run(
            [sys.executable, str(bootstrap)],
            env=env,
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
        self.assertEqual(result.returncode, 127)


class Ps1DefaultTests(unittest.TestCase):
    """Regression coverage for generate()'s `ps1` default flip (False->True,
    2026-08-07 argv-fidelity chunk C2). Prior coverage only asserted .cmd
    presence with the flag omitted; it never asserted the new default
    actually emits the .ps1 sibling, so the flip itself had no direct
    regression test."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_generate_emits_ps1_by_default_when_flag_omitted(self) -> None:
        written = gls.generate("queue-triage", self.tmp)
        names = {p.name for p in written}
        self.assertIn("queue-triage.ps1", names)
        self.assertIn("queue-triage.cmd", names)
        self.assertTrue((self.tmp / "queue-triage.ps1").is_file())

    def test_generate_suppresses_ps1_with_explicit_false(self) -> None:
        written = gls.generate("queue-triage", self.tmp, ps1=False)
        names = {p.name for p in written}
        self.assertNotIn("queue-triage.ps1", names)
        self.assertIn("queue-triage.cmd", names)
        self.assertFalse((self.tmp / "queue-triage.ps1").exists())


class BakedInterpreterExistenceGateTests(unittest.TestCase):
    """The baked-interpreter rung must be existence-gated in BOTH dialects.

    Regression coverage for a Windows-portability defect: render_cmd() jumped
    straight to :run_baked on any non-empty %_py%, and render_ps1() invoked
    $_pybin on any non-empty string — neither asked whether the path still
    existed. A `~/.claude` synced between a Mac and a Windows box (or any
    moved/uninstalled interpreter) therefore produced a launcher that
    invoked a path existing under no spelling and failed obscurely, with the
    `where python.exe` / `py -3` rungs below it unreachable.

    The shape asserted here is the one
    `coordinator_core.install.substrate`'s sibling agent-helper forwarder
    emitter already shipped (`if not "%_py%"=="" if exist "%_py%"` +
    `set "_py="`) — these are two emitters of one ladder, not two dialects.

    BAKED is a sentinel, not a real interpreter path: every assertion below
    is over the EMITTED TEXT, and a concrete absolute path here would be
    both host-specific and a concrete-path-citation guard violation.
    """

    BAKED = "<baked-interpreter-sentinel>"

    def test_cmd_gates_the_baked_rung_on_existence(self) -> None:
        body = gls.render_cmd("queue-triage", python_bin_token=self.BAKED)
        self.assertIn(
            'if not "%_py%"=="" if exist "%_py%" goto :run_baked',
            body,
            "baked .cmd rung must be existence-gated, not merely non-empty-gated",
        )

    def test_cmd_clears_a_rejected_bake_before_the_fallback_rungs(self) -> None:
        """A rejected bake must not leak into :run_baked via the PATH rung."""
        body = gls.render_cmd("queue-triage", python_bin_token=self.BAKED)
        gate_at = body.index('if exist "%_py%" goto :run_baked')
        clear_at = body.index('set "_py="\n', gate_at)
        # The executable rung, not the `where python.exe` mention in the
        # header REM block (which precedes the whole ladder).
        where_at = body.index("'where python.exe 2^>nul'")
        self.assertLess(clear_at, where_at)

    def test_cmd_still_falls_through_to_the_discovery_ladder(self) -> None:
        body = gls.render_cmd("queue-triage", python_bin_token=self.BAKED)
        self.assertIn("where python.exe", body)
        self.assertIn("where py >nul 2>&1", body)
        self.assertIn("goto :run_py3", body)
        self.assertIn("py -3 ", body)
        self.assertIn("exit /b 127", body)

    def test_ps1_gates_the_baked_rung_on_test_path(self) -> None:
        body = gls.render_ps1("queue-triage", python_bin_token=self.BAKED)
        self.assertIn(
            "if ($_pybin -ne '' -and -not (Test-Path -LiteralPath $_pybin)) "
            "{ $_pybin = '' }",
            body,
            "baked .ps1 rung must be Test-Path-gated, not merely non-empty-gated",
        )

    def test_ps1_test_path_gate_precedes_the_baked_invocation(self) -> None:
        body = gls.render_ps1("queue-triage", python_bin_token=self.BAKED)
        self.assertLess(
            body.index("Test-Path -LiteralPath $_pybin"),
            body.index("& $_pybin $_entry @args"),
        )

    def test_ps1_still_falls_through_to_the_discovery_ladder(self) -> None:
        body = gls.render_ps1("queue-triage", python_bin_token=self.BAKED)
        self.assertIn("Get-Command python.exe", body)
        self.assertIn("Get-Command py ", body)
        self.assertIn("-3 $_entry @args", body)
        self.assertIn("exit 127", body)

    def test_unsubstituted_token_self_detection_survives(self) -> None:
        """The token self-clear must still precede the existence gate — an
        uninstalled artifact carries the literal token, which is not a path.
        """
        cmd = gls.render_cmd("queue-triage")
        self.assertIn(f'if "%_py%"=="{gls.PYTHON_BIN_TOKEN}" set "_py="', cmd)
        self.assertLess(
            cmd.index(f'if "%_py%"=="{gls.PYTHON_BIN_TOKEN}"'),
            cmd.index('if exist "%_py%"'),
        )
        ps1 = gls.render_ps1("queue-triage")
        self.assertIn(
            f"if ($_pybin -eq '{gls.PYTHON_BIN_TOKEN}') {{ $_pybin = '' }}", ps1
        )
        self.assertLess(
            ps1.index(f"if ($_pybin -eq '{gls.PYTHON_BIN_TOKEN}')"),
            ps1.index("Test-Path -LiteralPath $_pybin"),
        )

    def test_quoting_of_the_baked_value_is_unchanged(self) -> None:
        """Generated-launcher quoting bugs are silent — pin both dialects."""
        cmd = gls.render_cmd("queue-triage", python_bin_token=self.BAKED)
        self.assertIn(f'set "_py={self.BAKED}"', cmd)
        self.assertIn('"%_py%" "%~dp0queue-triage" %*', cmd)
        ps1 = gls.render_ps1("queue-triage", python_bin_token=self.BAKED)
        self.assertIn(f"$_pybin = '{self.BAKED}'", ps1)


class SpecBacklinkTests(unittest.TestCase):
    """Coverage for the spec-backlink mechanism (gen-launcher-shim.py
    § SPEC BACKLINKS).

    Spec backlink: pln-claude-klabauter-pure-python-shop-retire-0f8aee § C7

    The obligation being discharged: CLAUDE.md requires spec backlinks (the
    "RAG-bait exception" to the no-inline-comments rule), while
    coordinator_core/test_bin_launcher_parity.py byte-compares every launcher
    against live generator output. Before this mechanism the two were flatly
    incompatible — a hand-added backlink was destroyed by the next
    regeneration and the guard enforced the destruction.
    """

    ENTRY = "bin/claude-klabauter-doctor-probe.py"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def _fixture_registry(self, body: str) -> Path:
        path = self.tmp / "fixture-backlinks.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_declared_backlink_appears_in_both_dialects(self) -> None:
        """One declaration, two comment dialects: `REM` for .cmd, `#` for
        .ps1. A dialect slip is silent — a `#` line in a .cmd is not a comment
        at all, it is a command cmd.exe will try to run."""
        value = "docs/plans/whatever.md § C7"
        cmd = gls.render_cmd("claude-klabauter-doctor-probe.py", spec_backlink=value)
        ps1 = gls.render_ps1("claude-klabauter-doctor-probe.py", spec_backlink=value)
        self.assertIn(f"REM Spec backlink: {value}\n", cmd)
        self.assertIn(f"# Spec backlink: {value}\n", ps1)
        # Dialect purity, not merely presence.
        self.assertNotIn(f"# Spec backlink: {value}", cmd)
        self.assertNotIn(f"REM Spec backlink: {value}", ps1)
        # The line lands INSIDE the header comment block, before the ladder.
        self.assertLess(cmd.index("Spec backlink:"), cmd.index('set "_py='))
        self.assertLess(ps1.index("Spec backlink:"), ps1.index("$_pybin ="))

    def test_undeclared_entry_emits_no_backlink_line_and_identical_bytes(self) -> None:
        """The no-churn property, asserted rather than assumed: an entrypoint
        with no registry row must render byte-for-byte what it rendered before
        the mechanism existed. This is what kept the 2026-08-03 fleet
        regeneration from having to run a second time over ~410 launchers."""
        for render in (gls.render_cmd, gls.render_ps1):
            with self.subTest(render=render.__name__):
                default = render("queue-triage")
                explicit_none = render("queue-triage", spec_backlink=None)
                self.assertNotIn("Spec backlink:", default)
                self.assertEqual(default, explicit_none)
                # And an empty/whitespace-free falsy value cannot sneak a bare
                # "Spec backlink:" header in with nothing after it.
                self.assertEqual(default, render("queue-triage", spec_backlink=""))

    def test_registry_lookup_is_keyed_by_repo_relative_entrypoint_path(self) -> None:
        """Keying on the ENTRYPOINT path (not the launcher filename) is what
        makes one row cover both the .cmd and the .ps1 twin."""
        registry = self._fixture_registry(
            '[backlinks]\n"bin/fake-tool.py" = "docs/plans/fake.md § C1"\n'
        )
        self.assertEqual(
            gls.spec_backlink_for_entry_path("bin/fake-tool.py", registry),
            "docs/plans/fake.md § C1",
        )
        self.assertIsNone(
            gls.spec_backlink_for_entry_path("bin/fake-tool.cmd", registry)
        )
        self.assertIsNone(gls.spec_backlink_for_entry_path("bin/other.py", registry))

    def test_missing_registry_is_not_an_error(self) -> None:
        """An installed tree that never shipped the registry must still
        generate launchers, not raise."""
        self.assertEqual(gls.load_spec_backlinks(self.tmp / "absent.toml"), {})

    def test_out_of_repo_out_dir_has_no_registry_identity(self) -> None:
        """A launcher generated outside this repo cannot be keyed, so it
        carries no declared backlink — the reason every existing generator
        test writing into tmp_path is unaffected by this mechanism."""
        self.assertIsNone(gls.entry_rel_path("fake-tool.py", self.tmp))
        self.assertEqual(
            gls.entry_rel_path("claude-klabauter-doctor-probe.py", gls.REPO_ROOT / "bin"),
            "bin/claude-klabauter-doctor-probe.py",
        )

    def test_real_registry_declares_the_doctor_probe_backlink(self) -> None:
        """Regression naming the specific launcher whose hand-added backlink
        the fleet regeneration sweep destroyed."""
        declared = gls.spec_backlink_for_entry_path(self.ENTRY)
        self.assertIsNotNone(declared, f"{self.ENTRY} lost its registry row")
        plan_ref, _, section = declared.partition(" § ")
        self.assertTrue(
            (gls.REPO_ROOT / plan_ref).is_file(),
            f"backlink names a nonexistent plan file: {plan_ref}",
        )
        self.assertEqual(section, "C7")

    def test_every_registry_value_is_a_single_nonempty_line(self) -> None:
        """A newline in a value escapes the comment block and emits executable
        text into a launcher body — a silent, Windows-only breakage."""
        entries = gls.load_spec_backlinks()
        self.assertTrue(entries, "registry is empty; the mechanism proves nothing")
        for key, value in entries.items():
            with self.subTest(entry=key):
                self.assertTrue(value.strip(), f"{key} has an empty backlink")
                self.assertNotIn("\n", value)
                self.assertTrue(
                    key.endswith((".py", ".sh")) or "." not in key.rsplit("/", 1)[-1],
                    f"{key} is not an entrypoint filename (keys are entrypoints, "
                    "not launchers)",
                )

    def test_the_committed_launcher_carries_its_declared_backlink(self) -> None:
        """Real-tree, read-only: the declaration and the shipped bytes agree.

        The discharge test made concrete — nobody passed this value to the
        regeneration command; `generate()` resolved it from the registry. Read
        rather than regenerated on purpose: a test that rewrites a tracked file
        to prove the writer works is a test with a side effect, and the
        byte-equivalence half is already covered fleet-wide by
        coordinator_core/test_bin_launcher_parity.py.
        """
        declared = gls.spec_backlink_for_entry_path(self.ENTRY)
        body = (gls.REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.cmd").read_bytes()
        self.assertIn(
            f"REM Spec backlink: {declared}".encode("utf-8"),
            body,
            "bin/claude-klabauter-doctor-probe.cmd does not carry its declared backlink; "
            "regenerate: python3 coordinator/bin/gen-launcher-shim.py "
            "claude-klabauter-doctor-probe.py --dir bin",
        )

    def test_generate_resolves_the_declaration_itself(self) -> None:
        """`generate()` must not require the backlink as an argument. Proven
        against a tmp out_dir keyed through a fixture registry, so nothing in
        the real tree is written."""
        entry = "fake-tool.py"
        (self.tmp / entry).write_text("if __name__ == '__main__':\n    pass\n")
        # Out-of-repo out_dir: no registry identity, so no backlink line.
        gls.generate(entry, self.tmp)
        self.assertNotIn(
            "Spec backlink:", (self.tmp / "fake-tool.cmd").read_text(encoding="utf-8")
        )
        # ...and the in-repo resolution path composes to the registry key that
        # the .cmd on disk was actually written from.
        self.assertEqual(
            gls.entry_rel_path("claude-klabauter-doctor-probe.py", gls.REPO_ROOT / "bin"),
            self.ENTRY,
        )


if __name__ == "__main__":
    sys.exit(unittest.main())
