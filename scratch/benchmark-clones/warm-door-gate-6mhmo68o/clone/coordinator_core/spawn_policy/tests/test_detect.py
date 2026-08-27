"""Tests for coordinator_core.spawn_policy.detect.

Fixtures are inline source strings — this module owns its own fixtures per
the C1 brief; it does not depend on C2's carve-out register landing.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from coordinator_core.spawn_policy.detect import (
    ExcludedReport,
    SpawnKind,
    SpawnParseError,
    is_test_tree_site,
    sites_in_source,
    site_key,
    walk_repo,
)


def _src(text: str) -> str:
    return textwrap.dedent(text)


def test_subprocess_run_shell_binary_literal():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["bash", "script.sh"])
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    site = sites[0]
    assert site.kind == SpawnKind.SHELL_BINARY
    assert site.argv0 == "bash"
    assert site.enclosing == "f"
    assert site.ordinal == 0


def test_powershell_exe_suffix_is_shell_binary():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["powershell.exe", "-Command", "x"])
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.SHELL_BINARY
    assert sites[0].argv0 == "powershell.exe"


def test_pwsh_exe_suffix_is_shell_binary():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["pwsh.exe", "-c", "x"])
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_plain_spawn_non_shell_binary():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["git", "status"])
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN
    assert sites[0].argv0 == "git"


def test_shell_true_literal():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run("echo hi", shell=True)
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.SHELL_TRUE


def test_shell_true_via_variable():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f(use_shell):
                subprocess.run(["git", "status"], shell=use_shell)
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.SHELL_TRUE


def test_shell_true_via_kwargs_spread():
    # Defect 2: opaque **kwargs forwarding with no explicit `shell=` at the
    # call site is SHELL_UNKNOWN, not SHELL_TRUE — a `shell=` value could be
    # present but is not statically visible. See test_kwargs_forwarding_*
    # below for the full fixture set this EM ruling covers.
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f(opts):
                subprocess.run(["git", "status"], **opts)
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.SHELL_UNKNOWN


def test_shell_false_literal_is_not_shell_true():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["git", "status"], shell=False)
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN


def test_os_system_is_inherent_shell_true():
    sites = sites_in_source(
        _src(
            """
            import os

            def f():
                os.system("echo hi")
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.SHELL_TRUE
    assert sites[0].argv0 == "echo"


def test_os_popen_is_inherent_shell_true():
    sites = sites_in_source(
        _src(
            """
            import os

            def f():
                os.popen("echo hi")
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.SHELL_TRUE


def test_asyncio_create_subprocess_shell_is_shell_true():
    sites = sites_in_source(
        _src(
            """
            import asyncio

            async def f():
                await asyncio.create_subprocess_shell("echo hi")
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.SHELL_TRUE


def test_asyncio_create_subprocess_exec_is_plain():
    sites = sites_in_source(
        _src(
            """
            import asyncio

            async def f():
                await asyncio.create_subprocess_exec("git", "status")
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN
    assert sites[0].argv0 == "git"


def test_pty_spawn():
    sites = sites_in_source(
        _src(
            """
            import pty

            def f():
                pty.spawn(["bash"])
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_os_execv_execvp():
    sites = sites_in_source(
        _src(
            """
            import os

            def f():
                os.execv("/bin/bash", ["bash"])
                os.execvp("git", ["git", "status"])
            """
        ),
        "m.py",
    )
    assert len(sites) == 2
    assert sites[0].kind == SpawnKind.SHELL_BINARY
    assert sites[0].argv0 == "bash"
    assert sites[1].kind == SpawnKind.PLAIN_SPAWN
    assert sites[1].argv0 == "git"


def test_os_execve_recognized():
    """`os.execve(path, argv, env)` has the same argv-bearing-first-positional
    shape as the already-tracked `os.execv` and was the sole gap left by
    fb813252f's stdlib-exec enumeration."""
    sites = sites_in_source(
        _src(
            """
            import os

            def f():
                os.execve("/bin/bash", ["bash"], {})
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_BINARY
    assert sites[0].argv0 == "bash"


def test_os_spawnl_family_recognized():
    """`os.spawnl*` (mode, path, *args) is mode-first like `os.spawnv*`, but
    was entirely absent from `_RECOGNIZED` — the exhaustive stdlib exec/spawn
    sweep this finding requested surfaced it alongside `os.execve`."""
    sites = sites_in_source(
        _src(
            """
            import os

            def f():
                os.spawnl(os.P_WAIT, "/bin/bash", "bash")
                os.spawnle(os.P_WAIT, "/bin/git", "git", "status", {})
                os.spawnlp(os.P_WAIT, "git", "git", "status")
                os.spawnlpe(os.P_WAIT, "git", "git", "status", {})
            """
        ),
        "m.py",
    )
    assert len(sites) == 4
    assert sites[0].kind == SpawnKind.SHELL_BINARY
    assert sites[0].argv0 == "bash"
    assert sites[1].kind == SpawnKind.PLAIN_SPAWN
    assert sites[1].argv0 == "git"
    assert sites[2].kind == SpawnKind.PLAIN_SPAWN
    assert sites[2].argv0 == "git"
    assert sites[3].kind == SpawnKind.PLAIN_SPAWN
    assert sites[3].argv0 == "git"


def test_aliased_module_import():
    sites = sites_in_source(
        _src(
            """
            import subprocess as sp

            def f():
                sp.run(["bash"])
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_aliased_from_import():
    sites = sites_in_source(
        _src(
            """
            from subprocess import Popen as P

            def f():
                P(["bash"])
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_module_level_constant_argv0():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            BASH = "bash"

            def f():
                subprocess.run([BASH, "script.sh"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "bash"
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_fstring_argv0_static_prefix_resolves():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run([f"bash", "script.sh"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "bash"


def test_fstring_argv0_dynamic_is_unresolvable():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f(name):
                subprocess.run([f"{name}", "script.sh"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "<dynamic>"
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN


def test_list_concat_argv0():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f(extra):
                subprocess.run(["bash"] + extra)
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "bash"
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_unresolvable_dynamic_argv0_still_reported():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f(cmd):
                subprocess.run(cmd)
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].argv0 == "<dynamic>"
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN


def test_comment_and_docstring_are_never_sites():
    sites = sites_in_source(
        _src(
            '''
            import subprocess

            def f():
                """Mentions subprocess.run(["bash"]) but does not call it."""
                # subprocess.run(["bash"]) -- also just a comment
                pass
            '''
        ),
        "m.py",
    )
    assert sites == []


def test_local_helper_indirection_one_hop():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def _run(a):
                subprocess.run(a)

            def f():
                _run(["bash", "script.sh"])
            """
        ),
        "m.py",
    )
    # one site inside _run itself (dynamic — argv is a bare param there)
    # plus one resolved site at the call site inside f.
    helper_sites = [s for s in sites if s.enclosing == "_run"]
    caller_sites = [s for s in sites if s.enclosing == "f"]
    assert len(helper_sites) == 1
    assert helper_sites[0].argv0 == "<dynamic>"
    assert len(caller_sites) == 1
    assert caller_sites[0].argv0 == "bash"
    assert caller_sites[0].kind == SpawnKind.SHELL_BINARY


def test_local_helper_indirection_does_not_crash_on_deeper_chains():
    # a second hop (g -> _run) is out of scope but must not crash.
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def _run(a):
                subprocess.run(a)

            def g(a):
                _run(a)

            def f():
                g(["bash"])
            """
        ),
        "m.py",
    )
    # No crash; the outer f() -> g() call is not a recognized spawn site
    # because g is not itself a resolved one-hop helper of a recognized call.
    assert all(s.path == "m.py" for s in sites)


def test_class_method_enclosing_name():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            class Cls:
                def meth(self):
                    subprocess.run(["bash"])
            """
        ),
        "m.py",
    )
    assert sites[0].enclosing == "Cls.meth"


def test_module_level_call_enclosing_is_module():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            subprocess.run(["bash"])
            """
        ),
        "m.py",
    )
    assert sites[0].enclosing == "<module>"


def test_ordinal_is_per_enclosing_source_order():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["bash"])
                subprocess.run(["git"])
            """
        ),
        "m.py",
    )
    assert [s.ordinal for s in sites] == [0, 1]


def test_lineno_not_part_of_site_key():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["bash"])
            """
        ),
        "m.py",
    )
    key = site_key(sites[0])
    assert key == ("m.py", "f", "bash", 0)
    assert len(key) == 4


def test_argv_digest_differs_for_different_argv_shapes():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["bash", "a.sh"])
                subprocess.run(["bash", "b.sh"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv_digest != sites[1].argv_digest


def test_sites_in_source_raises_on_parse_failure():
    with pytest.raises(SpawnParseError) as exc_info:
        sites_in_source("def f(:\n    pass", "broken.py")
    assert exc_info.value.path == "broken.py"
    assert exc_info.value.detail


def test_sites_in_source_never_swallows_syntax_error_as_clean():
    # The exact inherited bug: `except SyntaxError: return []` (or False).
    # There must be no path through sites_in_source that reports an
    # unparseable file as having zero sites instead of raising.
    with pytest.raises(SpawnParseError):
        sites_in_source("this is not ) python (", "broken2.py")


def test_walk_repo_excludes_scratch_but_counts_suppressed(tmp_path: pathlib.Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "real.py").write_text(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["bash"])
            """
        )
    )
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    (scratch_dir / "throwaway.py").write_text(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["bash"])
                subprocess.run(["bash"])
            """
        )
    )

    sites, excluded = walk_repo(tmp_path)

    assert len(sites) == 1
    assert sites[0].path == "pkg/real.py"
    assert isinstance(excluded, ExcludedReport)
    assert excluded.paths == ["scratch"]
    # suppressed_site_count counts excluded FILES, not sites: excluded files
    # are never parsed (see test_walk_repo_excludes_dir_with_invalid_syntax
    # below for why), so a per-site count is not knowable without parsing.
    assert excluded.suppressed_site_count == 1


def test_walk_repo_default_exclude_covers_scratchpad(tmp_path: pathlib.Path):
    scratchpad_dir = tmp_path / "scratchpad"
    scratchpad_dir.mkdir()
    (scratchpad_dir / "x.py").write_text(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["bash"])
            """
        )
    )

    sites, excluded = walk_repo(tmp_path)

    assert sites == []
    assert excluded.paths == ["scratchpad"]
    assert excluded.suppressed_site_count == 1


def test_walk_repo_propagates_parse_error(tmp_path: pathlib.Path):
    (tmp_path / "broken.py").write_text("def f(:\n    pass")
    with pytest.raises(SpawnParseError):
        walk_repo(tmp_path)


def test_walk_repo_excludes_dir_with_invalid_syntax_without_crashing(
    tmp_path: pathlib.Path,
):
    # Defect 3: an excluded dir must never be parsed at all, so a
    # syntactically invalid file inside it cannot crash the walk.
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "real.py").write_text(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["bash"])
            """
        )
    )
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    (scratch_dir / "invalid.py").write_text("def f(:\n    pass")

    sites, excluded = walk_repo(tmp_path)

    assert len(sites) == 1
    assert sites[0].path == "pkg/real.py"
    assert excluded.paths == ["scratch"]
    assert excluded.suppressed_site_count == 1


def test_sites_in_source_still_raises_for_non_excluded_invalid_file():
    # The fail-loud contract for non-excluded files must survive intact —
    # this fix narrows WHICH files get parsed, it does not add a swallow path.
    with pytest.raises(SpawnParseError):
        sites_in_source("def f(:\n    pass", "pkg/broken.py")


# --- Extensionless naked-Python discovery (P1: coordinator/bin/ shape) -----


def test_walk_repo_discovers_extensionless_shebang_script(tmp_path: pathlib.Path):
    script = tmp_path / "my-tool"
    script.write_text(
        "#!/usr/bin/env python3\n"
        + _src(
            """
            import subprocess

            def f():
                subprocess.run(["bash", "-c", "x"])
            """
        )
    )

    sites, excluded = walk_repo(tmp_path)

    assert len(sites) == 1
    assert sites[0].path == "my-tool"
    assert sites[0].kind == SpawnKind.SHELL_BINARY
    assert excluded.suppressed_site_count == 0


def test_walk_repo_discovers_bare_python_shebang_no_version(tmp_path: pathlib.Path):
    script = tmp_path / "another-tool"
    script.write_text(
        "#!/usr/bin/python\n"
        + _src(
            """
            import subprocess

            def f():
                subprocess.run(["git", "status"])
            """
        )
    )

    sites, _ = walk_repo(tmp_path)

    assert len(sites) == 1
    assert sites[0].path == "another-tool"


def test_walk_repo_ignores_extensionless_non_shebang_file(tmp_path: pathlib.Path):
    (tmp_path / "README").write_text("not a script, just text\n")

    sites, _ = walk_repo(tmp_path)

    assert sites == []


def test_walk_repo_ignores_non_python_shebang(tmp_path: pathlib.Path):
    script = tmp_path / "run.sh"
    script.write_text('#!/bin/bash\necho "subprocess.run(x)"\n')

    sites, _ = walk_repo(tmp_path)

    assert sites == []


def test_walk_repo_shebang_probe_survives_binary_file(tmp_path: pathlib.Path):
    # A binary file with no suffix must not crash the walk or its shebang probe.
    (tmp_path / "blob").write_bytes(bytes(range(256)))

    sites, _ = walk_repo(tmp_path)

    assert sites == []


def test_walk_repo_excludes_extensionless_script_in_excluded_dir(
    tmp_path: pathlib.Path,
):
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    (scratch_dir / "naked-tool").write_text(
        "#!/usr/bin/env python3\n"
        + _src(
            """
            import subprocess

            def f():
                subprocess.run(["bash"])
            """
        )
    )

    sites, excluded = walk_repo(tmp_path)

    # Directory-level exclusion applies before any read, same as for *.py —
    # the excluded extensionless script is never opened, so it does not
    # inflate suppressed_site_count either (that count is *.py-file-only,
    # a pre-existing contract this fix does not change).
    assert sites == []
    assert excluded.paths == ["scratch"]
    assert excluded.suppressed_site_count == 0


# --- Defect 2: SHELL_UNKNOWN for opaque **kwargs forwarding ---------------


def test_kwargs_forwarding_wrapper_is_shell_unknown():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def helper(a, **kw):
                return subprocess.run(a, **kw)
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_UNKNOWN


def test_caller_of_kwargs_wrapper_classified_on_own_evidence():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def helper(a, **kw):
                return subprocess.run(a, **kw)

            def viahelper():
                helper(["git", "log"], capture_output=True)
            """
        ),
        "m.py",
    )
    caller_sites = [s for s in sites if s.enclosing == "viahelper"]
    assert len(caller_sites) == 1
    assert caller_sites[0].argv0 == "git"
    assert caller_sites[0].kind == SpawnKind.PLAIN_SPAWN


def test_shell_true_variable_bound_stays_shell_true():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f(use_shell):
                subprocess.run(["git", "status"], shell=use_shell)
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.SHELL_TRUE


def test_shell_true_literal_still_shell_true():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run("echo hi", shell=True)
            """
        ),
        "m.py",
    )
    assert sites[0].kind == SpawnKind.SHELL_TRUE


# --- Defect 1: single-static-binding local variable resolution ------------


def test_shutil_which_bound_local_variable_resolves_to_shell_binary():
    sites = sites_in_source(
        _src(
            """
            import shutil
            import subprocess

            def f():
                bash = shutil.which("bash")
                subprocess.run([bash, "-lc", "echo hi"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "bash"
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_shutil_which_aliased_module_import_resolves():
    sites = sites_in_source(
        _src(
            """
            import shutil as sh
            import subprocess

            def f():
                bash = sh.which("bash")
                subprocess.run([bash])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "bash"
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_shutil_which_from_import_resolves():
    sites = sites_in_source(
        _src(
            """
            from shutil import which
            import subprocess

            def f():
                bash = which("bash")
                subprocess.run([bash])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "bash"
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_plain_literal_local_binding_resolves():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                name = "git"
                subprocess.run([name, "status"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "git"
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN


def test_two_bindings_stays_dynamic():
    sites = sites_in_source(
        _src(
            """
            import shutil
            import subprocess

            def f(flag):
                exe = shutil.which("bash")
                if flag:
                    exe = shutil.which("sh")
                subprocess.run([exe])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "<dynamic>"
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN


def test_loop_target_binding_stays_dynamic():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f(candidates):
                for exe in candidates:
                    subprocess.run([exe])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "<dynamic>"


def test_function_parameter_argv0_stays_dynamic():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f(bash_path):
                subprocess.run([bash_path, "--version"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "<dynamic>"
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN


# --- Documented gaps: _resolve_argv0 has no case for these node shapes.
# Safe-direction (falls to <dynamic> -> never misclassified as PLAIN_SPAWN's
# opposite), but a class-attribute-held shell binary, subscript lookup, or
# ternary is currently invisible under-detection. Fixtures pin the CURRENT
# (accepted-gap) behavior so a future change is a deliberate, visible diff.


def test_attribute_argv0_falls_to_dynamic():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            class Runner:
                SHELL = "bash"

                def go(self):
                    subprocess.run([self.SHELL, "-c", "x"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "<dynamic>"
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN


def test_subscript_argv0_falls_to_dynamic():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f(argv_map):
                subprocess.run([argv_map["shell"], "-c", "x"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "<dynamic>"
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN


def test_ternary_argv0_falls_to_dynamic():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f(posix):
                subprocess.run(["bash" if posix else "cmd"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "<dynamic>"
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN


# --- Documented over-detection: a helper's own `shell=` param echoing its
# default (`def _run(a, shell=False): subprocess.run(a, shell=shell)`) is a
# Name, not a Constant, so _shell_signal_from_call returns "true"
# unconditionally — every caller through the helper is marked SHELL_TRUE
# even when no caller overrides the default. Over-detection, safe direction;
# fixture confirms the false-positive stays bounded to one hop.


def test_helper_shell_kwarg_echoing_own_default_is_over_detected_as_shell_true():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def _run(a, shell=False):
                subprocess.run(a, shell=shell)

            def f():
                _run(["git", "status"])
            """
        ),
        "m.py",
    )
    caller_sites = [s for s in sites if s.enclosing == "f"]
    assert len(caller_sites) == 1
    # Over-detected: the real default is False and no caller overrides it,
    # but the helper's own `shell=shell` is opaque to the detector.
    assert caller_sites[0].kind == SpawnKind.SHELL_TRUE


# --- Regression: 84cec279 review — false-negative evasions --------------


def test_functools_partial_shell_true_binding_is_detected():
    # Prior bug: `functools.partial(subprocess.run, shell=True)` bound to a
    # module-level name, then called, produced ZERO detected sites — not a
    # downgrade, a complete blind spot.
    sites = sites_in_source(
        _src(
            """
            import functools
            import subprocess

            _run = functools.partial(subprocess.run, shell=True)

            def f():
                _run(["echo", "hi"])
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_TRUE
    assert sites[0].argv0 == "echo"


def test_functools_partial_bound_positional_argv_is_detected():
    sites = sites_in_source(
        _src(
            """
            import functools
            import subprocess

            _run = functools.partial(subprocess.run, ["bash", "-c", "x"])

            def f():
                _run()
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_BINARY
    assert sites[0].argv0 == "bash"


def test_functools_partial_aliased_import_is_detected():
    sites = sites_in_source(
        _src(
            """
            from functools import partial
            import subprocess

            _run = partial(subprocess.run, shell=True)

            def f():
                _run("echo hi")
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_TRUE


def test_functools_partial_call_override_takes_precedence():
    # Real functools semantics: a call-time `shell=` keyword overrides the
    # partial's bound one.
    sites = sites_in_source(
        _src(
            """
            import functools
            import subprocess

            _run = functools.partial(subprocess.run, shell=True)

            def f():
                _run(["git", "status"], shell=False)
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.PLAIN_SPAWN


def test_os_posix_spawn_is_detected():
    sites = sites_in_source(
        _src(
            """
            import os

            def f():
                os.posix_spawn("/bin/sh", ["sh", "-c", "echo hi"], os.environ)
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_BINARY
    assert sites[0].argv0 == "sh"


def test_os_spawnv_is_detected_with_mode_first_argv():
    sites = sites_in_source(
        _src(
            """
            import os

            def f():
                os.spawnv(0, "/bin/sh", ["sh", "-c", "echo hi"])
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_BINARY
    assert sites[0].argv0 == "sh"


def test_os_execlp_and_execvpe_are_detected():
    sites = sites_in_source(
        _src(
            """
            import os

            def f():
                os.execlp("bash", "bash", "-c", "x")
                os.execvpe("git", ["git", "status"], os.environ)
            """
        ),
        "m.py",
    )
    assert len(sites) == 2
    assert sites[0].kind == SpawnKind.SHELL_BINARY
    assert sites[1].kind == SpawnKind.PLAIN_SPAWN
    assert sites[1].argv0 == "git"


def test_binop_string_concat_argv0_resolves_correctly():
    # Prior bug: "ba" + "sh" resolved argv0 to "ba" (a confidently WRONG
    # concrete value), misclassifying a real shell binary as PLAIN_SPAWN.
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["ba" + "sh", "-c", "x"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "bash"
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_binop_string_concat_via_variable_binding_resolves_correctly():
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f():
                b = "ba" + "sh"
                subprocess.run([b, "-c", "x"])
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "bash"
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_list_concat_still_resolves_after_binop_fix(tmp_path=None):
    # Regression guard: the string-concat fix must not break the
    # pre-existing list-concat resolution (`["bash"] + extra`).
    sites = sites_in_source(
        _src(
            """
            import subprocess

            def f(extra):
                subprocess.run(["bash"] + extra)
            """
        ),
        "m.py",
    )
    assert sites[0].argv0 == "bash"
    assert sites[0].kind == SpawnKind.SHELL_BINARY


def test_getattr_dynamic_dispatch_os_system_is_detected():
    sites = sites_in_source(
        _src(
            """
            import os

            def f():
                getattr(os, "system")("echo hi")
            """
        ),
        "m.py",
    )
    assert len(sites) == 1
    assert sites[0].kind == SpawnKind.SHELL_TRUE


def test_dist_no_longer_in_default_exclude():
    from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE

    assert "dist" not in DEFAULT_EXCLUDE


def test_walk_repo_no_longer_excludes_dist_directory(tmp_path: pathlib.Path):
    # 84cec279 finding: `dist` in DEFAULT_EXCLUDE hid this repo's own
    # git-tracked percolate publish-mirror source. `dist` must be walked
    # like any other production directory now.
    dist_dir = tmp_path / "dist" / "mirror-native"
    dist_dir.mkdir(parents=True)
    (dist_dir / "real.py").write_text(
        _src(
            """
            import subprocess

            def f():
                subprocess.run(["bash"])
            """
        )
    )

    sites, excluded = walk_repo(tmp_path)

    assert len(sites) == 1
    assert sites[0].path == "dist/mirror-native/real.py"
    assert "dist" not in excluded.paths


def test_is_test_tree_site_true_for_tests_dir_component():
    assert is_test_tree_site("coordinator_core/spawn_policy/tests/test_detect.py")
    assert is_test_tree_site("coordinator_core/tests/helpers.py")


def test_is_test_tree_site_true_for_test_underscore_basename():
    assert is_test_tree_site("coordinator_core/spawn_policy/detect_test_helpers/test_thing.py")
    assert is_test_tree_site("test_standalone.py")


def test_is_test_tree_site_false_for_benign_production_path():
    assert not is_test_tree_site("coordinator_core/spawn_policy/detect.py")
    assert not is_test_tree_site("coordinator_core/testing/fixtures.py")
