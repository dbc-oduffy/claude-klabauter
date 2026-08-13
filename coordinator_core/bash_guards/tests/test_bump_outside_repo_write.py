"""Tests for coordinator_core.bash_guards.bump_outside_repo_write -- the
Bash-surface OUTSIDE-REPO write-confinement speed bump (C5).

Spec backlink: docs/plans/2026-08-02-write-confinement-guards.md [example-doctrine-repo
repo], chunk C5 "Outside-repo detection, inline-interpreter classification,
sandbox reroute".

THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY -- see the plan's "Design
posture -- passable by construction". These tests verify the bump FIRES for
a plain-bash write-sink target resolving under no git root at all (AC3), for
an inline `python3 -c` payload carrying the same shape (AC4) while a
file-invoked script stays exempt (AC4's negative case, and the install-path
carve-out this module's own docstring names), that the always-allowed
destinations never bump (AC9), that this guard is registered as a
`GuardEntry` with its attributes pinned (AC13/AC19), and that the enumerated
write-sink set is pinned by a fixture (AC20).

No AC10/AC10b-analog (real-chain `offer-git-c` reachability test, see
`test_bump_foreign_repo_write.py`) is needed here: this guard's own
"WRITE-SINK CLASSIFICATION" docstring section deliberately excludes git
subcommands from its candidate set, so a `cd <dir> && git <sub>` shape was
never in scope for it at any chain position -- there is no reachability gap
to test.
"""

from __future__ import annotations

import importlib.util
import ntpath
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.bash_guards import bump_outside_repo_write as guard
from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.bash_guards._write_bump_marker import (
    marker_basename,
    resolve_gitdir,
)
from coordinator_core.bash_guards._write_bump_sink_shapes import WRITE_SINK_BINARIES

#: Same bridge-to-C8 gate as `test_command_tokenizer_length_ceiling.py`'s own
#: `requires_powershell_grammar` -- the cmdlet-detection cases below need
#: `tree-sitter-pwsh` actually importable; C8 (not this dispatch) is what
#: declares it in `[project].dependencies`. Absence is covered separately by
#: the unmarked ImportError->SILENT case in `_dialect.py`'s own test surface,
#: not re-derived here.
_GRAMMAR_PRESENT = all(
    importlib.util.find_spec(name) is not None
    for name in ("tree_sitter", "tree_sitter_pwsh")
)
requires_powershell_grammar = pytest.mark.skipif(
    not _GRAMMAR_PRESENT,
    reason="PowerShell grammar package not installed; C8 declares it in pyproject.toml.",
)

#: C2/[P2] fix -- the six MSYS-round-trip tests below build a REAL git repo
#: on disk and then address it through an MSYS-spelled ABSOLUTE path derived
#: from that repo's own real, host-native drive letter (`_msys_form`, fixed
#: per [P3] to fail loudly rather than fabricate one). A POSIX host's own
#: filesystem paths carry no drive letter to encode, so there is no genuine
#: MSYS spelling of a real POSIX path to round-trip through in the first
#: place -- this is the same class of hardware-gated boundary
#: `test_windows_platform_simulation.py::test_windows_path_resolve_is_
#: hardware_gated` names for `pathlib.Path(...).resolve()`, not something a
#: `_host_is_windows()`/`os.path`->`ntpath` seam can paper over when the
#: underlying git-root resolution still has to walk a REAL filesystem.
#: Skipped explicitly (never silently vacuous) on a non-native-Windows host;
#: the underlying MSYS-decode fix itself (`translate_msys_path`/
#: `resolve_relative`'s Windows branch) is proven host-independently below,
#: via the SAME `_host_is_windows()` seam these six also force, by the
#: `test_translate_msys_path_*`/`test_resolve_relative_*` tests further
#: down -- see those for the deterministic, any-host proof this dispatch
#: was asked to supply.
requires_native_windows_filesystem = pytest.mark.skipif(
    os.name != "nt",
    reason=(
        "MSYS round-trip against a REAL git repo needs a genuine Windows "
        "filesystem with a real drive letter for _msys_form to encode -- "
        "hardware-gated the same way pathlib.Path(...).resolve() is (see "
        "test_windows_platform_simulation.py); the underlying decode fix "
        "is proven host-independently by the test_translate_msys_path_*/"
        "test_resolve_relative_* tests in this file instead."
    ),
)



def _msys_form(p) -> str:
    """MSYS/MinGW drive-mount spelling (`/c/Users/...`) of an absolute
    native path -- the shape Git-for-Windows' bash hands to tools as
    argument expansion, and the exact shape `_write_bump_sink_shapes.
    translate_msys_path`/`resolve_relative` exist to translate correctly.
    Only meaningful for a drive-letter-absolute native path; the caller is
    responsible for handing this a real absolute path.

    Uses `ntpath.splitdrive` explicitly (never bare `os.path.splitdrive`,
    which is `posixpath.splitdrive` off Windows and always returns an empty
    drive) so a genuine Windows-shaped input (e.g. a synthetic `C:\\...`
    string built for the `_host_is_windows()`-seam tests below) decodes
    correctly regardless of which host this suite runs on. `p` must
    genuinely carry a drive letter -- fails LOUDLY (`ValueError`) rather
    than silently emitting a doubled leading slash (`//tmp/...`) when it
    does not, per the C2/[P3] review finding this replaces: a POSIX
    absolute path like `/tmp/x` has no drive letter to encode as an MSYS
    mount, so there is no genuine MSYS spelling to produce for it -- that
    is a caller error, not a shape this helper should paper over."""
    s = str(p)
    drive, rest = ntpath.splitdrive(s)
    letter = drive.rstrip(":").lower()
    if not letter:
        raise ValueError(
            f"_msys_form: {s!r} carries no Windows drive letter -- only "
            "meaningful for a real drive-letter-absolute native path "
            "(see this helper's own docstring)"
        )
    rest_posix = rest.replace("\\", "/")
    return f"/{letter}{rest_posix}"


def _posix(p) -> str:
    """POSIX-slash string form of a path for embedding in a bash
    command-line string -- the tokenizer under test parses commands as
    real bash/POSIX-sh syntax (backslash is an escape character), so a
    native Windows ``str(Path)`` (backslash-separated) embedded directly
    into a ``cmd`` string is not a realistic Bash-tool payload and
    silently corrupts the path once tokenized. Accepts a ``Path`` or a
    plain ``str``."""
    return p.as_posix() if hasattr(p, "as_posix") else str(p).replace("\\", "/")


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


@pytest.fixture()
def env(tmp_path):
    """Anchor repo (the session's own, WITH a git root), a scratch
    non-repo directory to write outside into, and an unregistered fake HOME
    (so `~/.claude` never accidentally matches)."""
    anchor = _init_repo(tmp_path, "anchor")
    outside = tmp_path / "outside-scratch"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return {"anchor": anchor, "outside": outside, "home": home}


@pytest.fixture(autouse=True)
def _clean_bump_env(monkeypatch, tmp_path):
    """Same isolation `test_bump_foreign_repo_write.py` uses -- this module
    also reads `os.environ` directly, never an injected `env` dict.

    `COORDINATOR_SETTINGS_HOME` is `setenv`'d to an isolated `tmp_path`
    subdirectory rather than `delenv`'d (binds AC13, example-doctrine-repo finding-#6-adjacent
    test-isolation gap): with it and `CLAUDE_HOME`/`HOME`/`USERPROFILE` all
    absent, `_settings_home_dir_from_env` returns `""`, which -- now that
    C1/C2 have landed the settings-home anchor -- would make that anchor
    silently no-op in exactly this suite (the one meant to exercise the
    write-confinement guards it now primarily resolves through), and any
    settings-home write this suite's own fixtures trigger would otherwise
    land in the developer's real `~/.coordinator-claude-settings/
    claude-klabauter/`. Isolated per-test via `tmp_path`, matching every
    other isolation root this fixture builds.

    ALSO repoints the SHARED classifier's recognized-temp-root primitives
    (`_write_bump_applicability.tempfile.gettempdir()` AND its
    `_posix_tmp_literal()` seam -- this guard no longer owns a `tempfile`
    reference of its own; it delegates entirely to that shared module's
    `target_is_bare_temp_scratch`) to sentinel directories under `tmp_path`,
    distinct from the fixture repos built elsewhere under the SAME
    `tmp_path`: pytest's own `tmp_path` lives under the REAL system temp dir
    on every platform this suite runs on, so without this repoint every
    "outside repo" candidate this file's other fixtures build would ALSO
    resolve under the real system temp and get silently exempted by this
    guard's own AC9 carve-out -- not a guard bug, a test-isolation
    necessity. `_posix_tmp_literal` specifically must ALSO be repointed
    (not just `gettempdir()`): on a platform where `tmp_path` defaults to
    living directly under `/tmp` (Linux with no `TMPDIR` set), the shared
    classifier's unconditional `os.path.realpath("/tmp")` candidate would
    otherwise still catch every fixture path regardless of the
    `gettempdir()` patch. Example-doctrine-repo finding #6 describes this exact hazard as
    reproducing in this fixture ("patches ONE of two candidates"); it does
    NOT reproduce here -- both `gettempdir()` and `_posix_tmp_literal()`
    were already repointed together before this dispatch (see the two
    `monkeypatch.setattr` calls below), so `gettempdir()` and the `/tmp`
    literal never collapse to one unpatched root on Linux CI. The one test
    that exercises the REAL AC9 system-temp carve-out
    (`test_ac9_system_temp_write_never_bumps`) explicitly re-points both
    back."""
    for var in (
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_HOME",
        "HOME",
        "USERPROFILE",
        "MACHINE_LOCAL_REGISTRY_DIR",
        "TMPDIR",
        "TEMP",
        "TMP",
    ):
        monkeypatch.delenv(var, raising=False)
    isolated_settings_home = tmp_path / "isolated-settings-home"
    isolated_settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(isolated_settings_home))
    fake_system_temp = tmp_path / "not-the-real-system-temp"
    fake_system_temp.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(fake_system_temp))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(fake_system_temp))


def _set_anchor(monkeypatch, env, session_id: str, extra: dict | None = None) -> None:
    """Establishes applicability the same way a real session does -- a
    settings-home `write_session_start_record`, not `CLAUDE_PROJECT_DIR`
    (example-doctrine-repo finding #5 / AC5). Mirrors `test_bump_foreign_repo_write._set_anchor`
    exactly -- see that helper's own docstring for the full rationale.
    `CLAUDE_PROJECT_DIR` is left unset throughout; `HOME` is still set (to an
    unrelated scratch dir) so `_anchor_is_under_claude_home` resolves to "not
    under" rather than fail-opening on an unresolvable home."""
    monkeypatch.setenv("HOME", str(env["home"]))
    for k, v in (extra or {}).items():
        monkeypatch.setenv(k, v)
    session_start.write_session_start_record(session_id, launch_cwd=str(env["anchor"]))


# ---------------------------------------------------------------------------
# AC3 -- a plain-bash write resolving to no git repo bumps.
# ---------------------------------------------------------------------------


def test_ac3_cp_to_outside_repo_target_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-1")
    src = env["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = env["outside"] / "newfile.txt"
    cmd = f"cp {_posix(src)} {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-1", str(env["anchor"]), {})

    assert result is not None
    assert "hookSpecificOutput" in result


def test_ac3_output_redirection_to_outside_repo_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-2")
    dest = env["outside"] / "redir.txt"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-2", str(env["anchor"]), {})

    assert result is not None


# ---------------------------------------------------------------------------
# 2026-08-13 `/dev/null` redirect false-positive fix -- see
# `_write_bump_sink_shapes._DEVNULL_TARGET`'s own docstring.
# ---------------------------------------------------------------------------


def test_devnull_redirect_does_not_bump(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-devnull-1")
    cmd = 'while pgrep -f "x" > /dev/null; do sleep 20; done'

    result = guard.check_bump_outside_repo_write(cmd, "sess-devnull-1", str(env["anchor"]), {})

    assert result is None


def test_devnull_stderr_redirect_does_not_bump(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-devnull-2")
    cmd = "grep foo bar 2>/dev/null"

    result = guard.check_bump_outside_repo_write(cmd, "sess-devnull-2", str(env["anchor"]), {})

    assert result is None


def test_devnull_redirect_plus_genuine_outside_write_still_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-devnull-3")
    dest = env["outside"] / "compound.txt"
    cmd = f"echo hi > /dev/null; cp {_posix(env['anchor'] / 'src.txt')} {_posix(dest)}"
    (env["anchor"] / "src.txt").write_text("x\n", encoding="utf-8")

    result = guard.check_bump_outside_repo_write(cmd, "sess-devnull-3", str(env["anchor"]), {})

    assert result is not None
    assert "hookSpecificOutput" in result


# ---------------------------------------------------------------------------
# C4e (2026-08-07, guard-dialect-coverage.md row 15) -- PowerShell dialect
# gate. This guard's write-sink verb table lives in `_write_bump_sink_
# shapes.py`, outside C4e's owned-file scope -- see `check_bump_outside_
# repo_write`'s own "DIALECT GATE" comment. It declares SILENT for
# PowerShell rather than mis-tokenizing with the bash-only shlex path;
# `tool_name` absent/"Bash" (every test above, `payload={}`) is unaffected
# (AC4).
# ---------------------------------------------------------------------------


def test_powershell_dialect_declines_and_records_silent(env, monkeypatch):
    from coordinator_core.bash_guards import _verdict

    _set_anchor(monkeypatch, env, "sess-ps")
    dest = env["outside"] / "redir.txt"
    cmd = f"echo hi > {_posix(dest)}"

    with _verdict.collecting() as silences:
        result = guard.check_bump_outside_repo_write(
            cmd, "sess-ps", str(env["anchor"]), {"tool_name": "PowerShell"}
        )

    assert result is None
    assert any(s.guard_name == "bump-outside-repo-write" for s in silences)


# ---------------------------------------------------------------------------
# Follow-up dispatch (2026-08-07, guard-dialect-coverage.md row 15): the
# blanket PowerShell SILENT above is now real detection for the cmdlet-shaped
# write table (`New-Item`/`Set-Content`/`Add-Content`/`Copy-Item`/
# `Move-Item`/`Out-File`/`Tee-Object`) -- see `_write_bump_sink_shapes.
# PS_WRITE_SINK_CMDLETS`. Every other PowerShell shape (a bare redirect, an
# unrecognized cmdlet) still records SILENT, exercised by
# `test_powershell_dialect_declines_and_records_silent` above (an `echo hi >
# ...` redirect, which matches no cmdlet in this leg's table, unchanged).
# ---------------------------------------------------------------------------


@requires_powershell_grammar
def test_powershell_new_item_to_outside_repo_target_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-ps-new-item")
    dest = env["outside"] / "newfile.txt"
    cmd = f"New-Item -Path {dest} -ItemType File"

    result = guard.check_bump_outside_repo_write(
        cmd, "sess-ps-new-item", str(env["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is not None
    assert "hookSpecificOutput" in result


@requires_powershell_grammar
def test_powershell_new_item_quoted_outside_repo_target_bumps(env, monkeypatch):
    """Reproduction/regression for the quoted-target false-clean: the
    PowerShell tokenizer emits a quoted leaf's raw source span (quotes
    attached, see `_dialect._flatten_powershell_tokens`), and prior to the
    `_write_bump_sink_shapes` extractor-level fix the attached quotes
    defeated `os.path.isabs`/the drive-absolute regex downstream, causing
    the candidate to be silently re-rooted under the session's own repo
    instead of judged as outside it. Double-quoted form."""
    _set_anchor(monkeypatch, env, "sess-ps-new-item-quoted")
    dest = env["outside"] / "newfile.txt"
    cmd = f'New-Item -Path "{dest}" -ItemType File'

    result = guard.check_bump_outside_repo_write(
        cmd, "sess-ps-new-item-quoted", str(env["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is not None
    assert "hookSpecificOutput" in result


@requires_powershell_grammar
def test_powershell_new_item_single_quoted_outside_repo_target_bumps(env, monkeypatch):
    """Same reproduction as above, single-quoted form -- `_strip_ps_quotes`
    covers both quote characters symmetrically."""
    _set_anchor(monkeypatch, env, "sess-ps-new-item-single-quoted")
    dest = env["outside"] / "newfile.txt"
    cmd = f"New-Item -Path '{dest}' -ItemType File"

    result = guard.check_bump_outside_repo_write(
        cmd, "sess-ps-new-item-single-quoted", str(env["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is not None
    assert "hookSpecificOutput" in result


@requires_powershell_grammar
def test_powershell_new_item_quoted_inside_own_repo_target_does_not_bump(env, monkeypatch):
    """Non-regression companion: a quoted target that legitimately resolves
    INSIDE the session's own anchor repo must still NOT bump, exactly as
    the equivalent unquoted case already does not."""
    _set_anchor(monkeypatch, env, "sess-ps-new-item-quoted-inside")
    dest = env["anchor"] / "newfile.txt"
    cmd = f'New-Item -Path "{dest}" -ItemType File'

    result = guard.check_bump_outside_repo_write(
        cmd, "sess-ps-new-item-quoted-inside", str(env["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is None


@requires_powershell_grammar
def test_powershell_copy_item_positional_destination_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-ps-copy-item")
    src = env["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = env["outside"] / "dest.txt"
    cmd = f"Copy-Item {src} {dest}"

    result = guard.check_bump_outside_repo_write(
        cmd, "sess-ps-copy-item", str(env["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is not None


@requires_powershell_grammar
def test_powershell_out_file_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-ps-out-file")
    dest = env["outside"] / "out.txt"
    cmd = f"Get-Date | Out-File -FilePath {dest}"

    result = guard.check_bump_outside_repo_write(
        cmd, "sess-ps-out-file", str(env["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is not None


@requires_powershell_grammar
def test_powershell_tee_object_variable_only_does_not_bump(env, monkeypatch):
    """`Tee-Object -Variable foo` writes to an in-memory PowerShell
    variable, not the filesystem -- must NOT be misread as a write-sink
    candidate (the false-positive this leg's own extraction helper guards
    against -- see `_write_bump_sink_shapes.extract_write_sink_targets_
    powershell`'s `tee-object` branch)."""
    from coordinator_core.bash_guards import _verdict

    _set_anchor(monkeypatch, env, "sess-ps-tee-var")
    cmd = "Get-Date | Tee-Object -Variable foo"

    with _verdict.collecting() as silences:
        result = guard.check_bump_outside_repo_write(
            cmd, "sess-ps-tee-var", str(env["anchor"]), {"tool_name": "PowerShell"}
        )

    assert result is None
    assert any(s.guard_name == "bump-outside-repo-write" for s in silences)


@requires_powershell_grammar
def test_powershell_copy_item_alias_and_cp_are_not_duplicated_here(env, monkeypatch):
    """`cp`/`mv` PowerShell ALIASES are deliberately out of this leg's own
    table (C3's triage: they already fire via alias collision elsewhere) --
    a bare `cp`/`mv` invocation under `tool_name=PowerShell` matches no
    cmdlet in `PS_WRITE_SINK_CMDLETS` and stays SILENT here, never bumped by
    this leg."""
    from coordinator_core.bash_guards import _verdict

    _set_anchor(monkeypatch, env, "sess-ps-cp-alias")
    src = env["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = env["outside"] / "dest.txt"
    cmd = f"cp {src} {dest}"

    with _verdict.collecting() as silences:
        result = guard.check_bump_outside_repo_write(
            cmd, "sess-ps-cp-alias", str(env["anchor"]), {"tool_name": "PowerShell"}
        )

    assert result is None
    assert any(s.guard_name == "bump-outside-repo-write" for s in silences)


def test_ac3_mkdir_to_not_yet_existing_outside_repo_dir_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-3")
    new_dir = env["outside"] / "brand-new-subdir"
    assert not new_dir.exists()
    cmd = f"mkdir -p {_posix(new_dir)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-3", str(env["anchor"]), {})

    assert result is not None


def test_same_repo_write_does_not_bump(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-4")
    src = env["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = env["anchor"] / "dest.txt"
    cmd = f"cp {_posix(src)} {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-4", str(env["anchor"]), {})

    assert result is None


def test_write_into_a_different_git_repo_does_not_bump_here(env, tmp_path, monkeypatch):
    """A target that resolves to SOME git root (even a foreign one) is C4's
    concern, never this guard's -- see module docstring, "PREDICATE"."""
    _set_anchor(monkeypatch, env, "sess-5")
    foreign = _init_repo(tmp_path, "foreign")
    src = env["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = foreign / "dest.txt"
    cmd = f"cp {_posix(src)} {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-5", str(env["anchor"]), {})

    assert result is None


def test_subagent_class_message_names_the_sandbox_route(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-6")
    src = env["anchor"] / "src.txt"
    src.write_text("x\n", encoding="utf-8")
    dest = env["outside"] / "newfile.txt"
    cmd = f"cp {_posix(src)} {_posix(dest)}"
    # `_canonical_agent_id` (subagent_sandbox.engine) only recognizes a
    # bare-hex unnamed-agent id (`^[a-f0-9]{12,}$`) or a named teammate id
    # -- an arbitrary string does not canonicalize and falls back to
    # EM-class, so this must be a real bare-hex shape.
    payload = {"agent_id": "af307f34d8afa24eb"}

    result = guard.check_bump_outside_repo_write(cmd, "sess-6", str(env["anchor"]), payload)

    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "sandbox" in reason.lower() or "state" in reason.lower()


# ---------------------------------------------------------------------------
# § Where the bump does not fire -- session anchor itself has no git repo.
# ---------------------------------------------------------------------------


def test_session_anchor_with_no_git_repo_never_bumps_outside_repo_writes(tmp_path, monkeypatch):
    """AC11's C5 half: a session launched to scaffold a fresh, un-repo'd
    tree writes freely -- outside-repo writes never bump when the SESSION's
    own anchor has no git repo of its own, regardless of target."""
    no_repo_anchor = tmp_path / "fresh-scaffold"
    no_repo_anchor.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    session_start.write_session_start_record("sess-7", launch_cwd=str(no_repo_anchor))
    dest = tmp_path / "elsewhere" / "file.txt"
    dest.parent.mkdir()
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-7", str(no_repo_anchor), {})

    assert result is None


# ---------------------------------------------------------------------------
# AC4 -- inline `python3 -c` payload write sinks bump; file-invoked scripts
# stay exempt.
# ---------------------------------------------------------------------------


def test_ac4_inline_python3_dash_c_write_sink_bumps(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-8")
    dest = env["outside"] / "inline.txt"
    cmd = f'python3 -c "echo hi > {_posix(dest)}"'

    result = guard.check_bump_outside_repo_write(cmd, "sess-8", str(env["anchor"]), {})

    assert result is not None


def test_ac4_inline_python_bundled_short_flag_write_sink_bumps(env, monkeypatch):
    """`-ic` (bundled) is the SAME `-c` shape `_BUNDLED_C_FLAG_RE` matches,
    not only the standalone `-c` spelling."""
    _set_anchor(monkeypatch, env, "sess-9")
    dest = env["outside"] / "bundled.txt"
    cmd = f'python3 -ic "echo hi > {_posix(dest)}"'

    result = guard.check_bump_outside_repo_write(cmd, "sess-9", str(env["anchor"]), {})

    assert result is not None


def test_ac4_inline_bash_dash_c_write_sink_bumps_via_shared_tokenizer_recursion(env, monkeypatch):
    """`bash -c` is not this module's own manual unwrap -- it is already
    auto-recursed by `resolve_command_positions` itself (module docstring,
    "INLINE PYTHON `-c` PAYLOADS")."""
    _set_anchor(monkeypatch, env, "sess-10")
    dest = env["outside"] / "bash-inline.txt"
    cmd = f'bash -c "echo hi > {_posix(dest)}"'

    result = guard.check_bump_outside_repo_write(cmd, "sess-10", str(env["anchor"]), {})

    assert result is not None


def test_ac4_file_invoked_python_script_does_not_bump(env, monkeypatch):
    """`python3 script.py` -- no `-c` at all -- is NOT classified (deferred
    to plan D1). Even though the script's own (unexamined) content might
    write outside the repo, this module never reads file contents."""
    _set_anchor(monkeypatch, env, "sess-11")
    script = env["anchor"] / "script.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    cmd = f"python3 {_posix(script)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-11", str(env["anchor"]), {})

    assert result is None


def test_ac4_file_invoked_install_script_writing_settings_home_is_allowed(env, monkeypatch):
    """The explicit negative case this chunk's own test surface names: a
    file-invoked `bash install.sh` is never classified at all (this module
    never opens the script), so it is allowed regardless of what it writes
    -- distinct from (and a superset of) the AC9 settings-home carve-out."""
    settings_home = env["home"] / ".coordinator-claude-settings"
    settings_home.mkdir()
    _set_anchor(monkeypatch, env, "sess-12")
    install_sh = env["anchor"] / "install.sh"
    install_sh.write_text("#!/bin/sh\necho installing\n", encoding="utf-8")
    cmd = f"bash {_posix(install_sh)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-12", str(env["anchor"]), {})

    assert result is None


# ---------------------------------------------------------------------------
# AC9 -- always-allowed destinations never bump.
# ---------------------------------------------------------------------------


def test_ac9_settings_home_write_never_bumps(env, monkeypatch):
    settings_home = env["home"] / ".coordinator-claude-settings"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    _set_anchor(monkeypatch, env, "sess-13", {"COORDINATOR_SETTINGS_HOME": str(settings_home)})
    dest = settings_home / "bin" / "note.txt"
    dest.parent.mkdir(parents=True)
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-13", str(env["anchor"]), {})

    assert result is None


# ---------------------------------------------------------------------------
# C1 (docs/plans/2026-08-10-carve-claude-out-and-close-the-backslash-bypass.md)
# -- `~/.claude` never bumps on the Bash outside-repo leg either. AC2/AC4.
# ---------------------------------------------------------------------------


def test_ac2_claude_home_write_never_bumps(env, monkeypatch):
    """AC2: a bash command writing into `~/.claude` is not denied by this
    leg. `~/.claude` is a real git checkout on this fleet (per `_init_repo`
    below), so this candidate resolves to SOME git root and is skipped by
    this guard's own "not this guard's predicate" branch BEFORE ever
    reaching `_target_is_always_allowed` -- `bump_foreign_repo_write.py`
    (C4) is the leg whose own carve-out this candidate shape actually
    exercises (see that module's own C1 test). Kept here anyway as AC2's
    literal "not denied by either bash leg" assertion for this destination
    class; `test_target_is_always_allowed_covers_claude_home_when_
    unresolved_as_a_repo` immediately below unit-tests THIS leg's own
    `target_is_under_claude_home` wiring directly, for the (edge, currently
    unobserved on this fleet) case where `~/.claude` resolves to no git
    repo at all."""
    claude_home = _init_repo(tmp_path=env["outside"].parent, name="claude-home-c1")
    monkeypatch.setenv("HOME", str(claude_home))
    monkeypatch.setenv("USERPROFILE", str(claude_home))
    session_start.write_session_start_record("sess-c1-outside", launch_cwd=str(env["anchor"]))

    dest = claude_home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-c1-outside", str(env["anchor"]), {})

    assert result is None


def test_target_is_always_allowed_covers_claude_home_when_unresolved_as_a_repo(
    env, monkeypatch
):
    """Direct unit test of this leg's own `_target_is_always_allowed` wiring
    (AC1/AC3), independent of `check_bump_outside_repo_write`'s upstream
    `target_gitdir is not None: continue` skip -- exercises exactly the
    branch `target_is_under_claude_home` was added to."""
    claude_home = env["home"]  # NOT a git checkout in this fixture
    monkeypatch.setenv("HOME", str(claude_home))
    monkeypatch.setenv("USERPROFILE", str(claude_home))
    target = str(claude_home / ".claude" / "settings.json")

    assert guard._target_is_always_allowed(target, str(env["anchor"]), "sess-x", env=os.environ)


def test_ac9_system_temp_write_never_bumps(env, monkeypatch):
    # Undo `_clean_bump_env`'s fake-tempdir repoint -- this is the one test
    # that exercises the REAL system-temp carve-out.
    monkeypatch.setattr(applicability.tempfile, "gettempdir", tempfile.gettempdir)
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: "/tmp")
    _set_anchor(monkeypatch, env, "sess-14")
    real_tmp = os.path.realpath(tempfile.gettempdir())
    dest_dir = Path(real_tmp) / "write-confinement-ac9-probe"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / "note.txt"
    cmd = f"echo hi > {_posix(dest)}"

    try:
        result = guard.check_bump_outside_repo_write(cmd, "sess-14", str(env["anchor"]), {})
        assert result is None
    finally:
        if dest.exists():
            dest.unlink()
        dest_dir.rmdir()


def test_ac9_subagent_sandbox_root_write_never_bumps(env, monkeypatch):
    """`state/subagent-share/<effective-session-id>/` under the anchor's
    OWN git root, per `_sandbox_root` -- ordinarily inside a repo already
    (so never reaches this guard's "no git root" predicate in the first
    place), but the carve-out is asserted directly here as a property of
    this module's own allow-list logic (module docstring, "ALWAYS-ALLOWED
    DESTINATIONS")."""
    session_id = "sess-15"
    _set_anchor(monkeypatch, env, session_id)
    sandbox = env["anchor"] / "state" / "subagent-share" / session_id
    sandbox.mkdir(parents=True)
    dest = sandbox / "note.txt"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, session_id, str(env["anchor"]), {})

    assert result is None


def test_agent_memory_store_write_never_bumps(env, monkeypatch):
    """A write into `<home>/.claude/projects/<slug>/memory/<file>.md` never
    bumps -- the memory store is Claude Code's own per-project persistent
    scratch, not a sibling repo, even when (as here, under the fake `HOME`
    this suite builds) `~/.claude` resolves to no git repo at all -- this
    guard's own predicate."""
    session_id = "sess-mem-1"
    _set_anchor(monkeypatch, env, session_id)
    memory_dir = env["home"] / ".claude" / "projects" / "-Users-example-operator-X-some-project" / "memory"
    memory_dir.mkdir(parents=True)
    dest = memory_dir / "note.md"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, session_id, str(env["anchor"]), {})

    assert result is None


def test_agent_memory_store_index_write_never_bumps(env, monkeypatch):
    """Same as above for the `MEMORY.md` index file specifically."""
    session_id = "sess-mem-2"
    _set_anchor(monkeypatch, env, session_id)
    memory_dir = env["home"] / ".claude" / "projects" / "-Users-example-operator-X-some-project" / "memory"
    memory_dir.mkdir(parents=True)
    dest = memory_dir / "MEMORY.md"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, session_id, str(env["anchor"]), {})

    assert result is None


# ---------------------------------------------------------------------------
# The marker clears the bump.
# ---------------------------------------------------------------------------


def test_marker_present_for_session_clears_the_bump(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-16")
    anchor_gitdir = resolve_gitdir(str(env["anchor"]))
    assert anchor_gitdir is not None
    (anchor_gitdir / marker_basename("sess-16")).touch()
    dest = env["outside"] / "cleared.txt"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-16", str(env["anchor"]), {})

    assert result is None


def test_marker_for_a_different_session_does_not_clear_this_ones_bump(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-17")
    anchor_gitdir = resolve_gitdir(str(env["anchor"]))
    assert anchor_gitdir is not None
    (anchor_gitdir / marker_basename("some-other-session")).touch()
    dest = env["outside"] / "not-cleared.txt"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-17", str(env["anchor"]), {})

    assert result is not None


# ---------------------------------------------------------------------------
# C5 -- destination-class axis wired through (always DESTINATION_FOREIGN in
# practice: this guard's own predicate guarantees the target resolves to no
# git repo, and a `publish.mirrors.*.path` entry is itself always a real
# repo, so the two can never coincide). Classified explicitly via C1 rather
# than hardcoded -- AC9 asserted first.
# ---------------------------------------------------------------------------


def test_destination_class_kwarg_is_passed_explicitly_as_foreign(env, monkeypatch):
    from coordinator_core.bash_guards import _write_bump_applicability as applicability
    from coordinator_core.bash_guards._write_bump_message import DESTINATION_FOREIGN

    session_id = "sess-destination-class"
    _set_anchor(monkeypatch, env, session_id)

    assert applicability.bump_applies(session_id, cwd=str(env["anchor"])) is True

    captured = []

    def _spy(**kwargs):
        captured.append(kwargs)
        return "stub message -- test double, never asserted on text"

    monkeypatch.setattr(guard, "render_bump_message", _spy)

    dest = env["outside"] / "x.txt"
    cmd = f"echo hi > {_posix(dest)}"
    result = guard.check_bump_outside_repo_write(cmd, session_id, str(env["anchor"]), {})

    assert result is not None
    assert len(captured) == 1
    assert captured[0].get("destination_class") == DESTINATION_FOREIGN


# ---------------------------------------------------------------------------
# Fail-open cases -- unresolvable inputs never bump.
# ---------------------------------------------------------------------------


def test_fail_open_when_session_id_empty(env, monkeypatch):
    # Guard short-circuits on an empty `session_id` before ever resolving an
    # anchor -- no applicability setup needed either way (mirrors C3's own
    # `test_fail_open_when_session_id_empty`).
    dest = env["outside"] / "x.txt"
    cmd = f"echo hi > {_posix(dest)}"

    assert guard.check_bump_outside_repo_write(cmd, "", str(env["anchor"]), {}) is None


def test_fail_open_when_cmd_empty(env, monkeypatch):
    # Guard short-circuits on an empty `cmd` before ever resolving an anchor
    # -- no applicability setup needed either way (mirrors C3's own
    # `test_fail_open_when_cmd_empty`).
    assert guard.check_bump_outside_repo_write("", "sess-18", str(env["anchor"]), {}) is None


def test_fail_open_when_anchor_unresolvable(env, monkeypatch):
    # No CLAUDE_PROJECT_DIR / session-start record at all.
    monkeypatch.setenv("HOME", str(env["home"]))
    dest = env["outside"] / "x.txt"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-19", str(env["anchor"]), {})

    assert result is None


def test_fail_open_when_write_sink_target_has_no_existing_ancestor_at_all():
    """`_nearest_existing_ancestor` (shared with C4 via
    `_write_bump_sink_shapes.nearest_existing_ancestor`) -- a bogus path
    with no real filesystem root resolves to `None` (never raises)."""
    assert guard._nearest_existing_ancestor("") is None


# ---------------------------------------------------------------------------
# AC13 / AC19 -- registered as a GuardEntry, not a call-site patch, with
# every registration attribute pinned explicitly.
# ---------------------------------------------------------------------------


def test_ac13_registered_as_a_guard_entry_in_dispatch_build_guard_chain():
    from coordinator_core.bash_guards import dispatch

    chain = dispatch._build_guard_chain("echo hi", "sess-struct", "/tmp", {}, None, False)
    entries = [e for e in chain if e.name == "bump-outside-repo-write"]

    assert len(entries) == 1


def test_ac19_registration_attributes_pinned_not_left_to_default():
    from coordinator_core.bash_guards import dispatch
    from coordinator_core.bash_guards._advisory_value import AdvisoryValue

    chain = dispatch._build_guard_chain("echo hi", "sess-struct", "/tmp", {}, None, False)
    entry = next(e for e in chain if e.name == "bump-outside-repo-write")

    # `fail_closed=False` -- the OPPOSITE of every neighbouring
    # CONFINEMENT_DENY entry: a crash in this guard must swallow to
    # "allow", never route through the hard-deny crash path.
    assert entry.fail_closed is False
    # `band=ADVISORY_REWRITE`, NOT `CONFINEMENT_DENY` -- the blanket-disarm
    # marker can suppress every band except CONFINEMENT_DENY; registering a
    # deliberately passable bump there would make it the LEAST passable
    # guard in the suite.
    assert entry.band is dispatch.GuardBand.ADVISORY_REWRITE
    # Explicit, never the UNCLASSIFIED default.
    assert entry.advisory_value is not AdvisoryValue.UNCLASSIFIED
    assert entry.advisory_value is AdvisoryValue.NOT_COST_ARGUED


def test_ac19_guard_band_membership_and_advisory_registry_tests_also_pass():
    """This guard's registration is additionally pinned by the package's
    OWN pre-existing structural tests -- see
    `test_guard_band_membership.py::ADVISORY_REWRITE_NAMES` and
    `test_override_route_inventory.py::_NO_OVERRIDE_NOTE_ALLOWLIST`, both
    updated alongside this guard's own registration."""
    from coordinator_core.bash_guards import dispatch
    from coordinator_core.bash_guards._advisory_value import AdvisoryValue

    chain = dispatch._build_guard_chain("echo hi", "sess-struct", "/tmp", {}, None, False)
    for entry in chain:
        assert entry.advisory_value is not None
        if entry.band is dispatch.GuardBand.CONFINEMENT_DENY:
            assert entry.advisory_value is not AdvisoryValue.UNCLASSIFIED


# ---------------------------------------------------------------------------
# AC20 -- the enumerated write-sink set is pinned by a fixture, so a later
# omission reads as a diff against a named list rather than a silent gap.
# Incident-derived where practical -- the two cited incidents
# (`nested-layout-bisect-tmp`, `tasks`, see the plan's own § Problem) were
# both plain directory/file creation, matching `mkdir`/output-redirection
# below directly; the remaining entries are this module's own enumerated
# superset, pinned for drift-detection rather than incident-recreated one
# by one.
# ---------------------------------------------------------------------------


def test_ac20_write_sink_binary_set_is_pinned():
    assert WRITE_SINK_BINARIES == frozenset(
        {"tee", "cp", "mv", "mkdir", "install", "sed", "rsync", "tar"}
    )


# ---------------------------------------------------------------------------
# REGRESSION -- the confirmed live false positive: the harness-designated
# per-session scratchpad is NOT covered by `tempfile.gettempdir()` alone on
# macOS (`TMPDIR` resolves under `/var/folders/...`; the scratchpad lives
# under `/private/tmp`). Uses a REAL session-start record so applicability is
# genuinely True -- a test that passes only because applicability failed open
# proves nothing (see this fix's own dispatch brief). MUST fail against the
# pre-fix module (`_always_allowed_roots`'s single `tempfile.gettempdir()`
# entry, no `/tmp` realpath candidate).
# ---------------------------------------------------------------------------


def test_regression_real_harness_scratchpad_shape_never_bumps(env, monkeypatch):
    # Point the REAL `/tmp` candidate (via the testability seam) at a
    # sentinel standing in for `/private/tmp` -- `gettempdir()` stays
    # repointed to the fixture's OWN fake system temp (simulating the
    # macOS `TMPDIR` divergence: gettempdir() and the scratchpad's real
    # root are two DIFFERENT directories, exactly like the live incident).
    real_tmp_stand_in = env["anchor"].parent / "private-tmp-stand-in"
    real_tmp_stand_in.mkdir()
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(real_tmp_stand_in))

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(env["anchor"]))
    monkeypatch.setenv("HOME", str(env["home"]))
    session_id = "sess-real-scratchpad"
    session_start.write_session_start_record(session_id, launch_cwd=str(env["anchor"]))

    scratchpad = (
        real_tmp_stand_in
        / "claude-501"
        / "-Users-example-operator-X-claude-klabauter"
        / session_id
        / "scratchpad"
    )
    scratchpad.mkdir(parents=True)
    dest = scratchpad / "draft-memo.md"
    cmd = f"cat > {_posix(dest)} <<'E'\nx\nE"

    result = guard.check_bump_outside_repo_write(cmd, session_id, str(env["anchor"]), {})

    assert result is None


def test_regression_shared_classifier_conjunction_git_repo_under_temp_not_exempt(
    env, tmp_path, monkeypatch
):
    """THE CONJUNCTION, unit-tested directly against the shared classifier
    this guard now delegates to: a real checkout under the recognized temp
    root IS a foreign repo and must NOT classify as bare scratch -- an
    unconditional temp exemption would open a hole the size of
    `git clone $TMPDIR/...`. (This guard's OWN candidate loop never even
    reaches the temp-exemption check for a git-resolved target -- it is
    filtered out earlier as "not this guard's concern", C4's territory --
    so the conjunction is exercised here at the classifier level, which is
    where both C5 and C7 actually consume it.)"""
    real_tmp_stand_in = tmp_path / "private-tmp-stand-in-2"
    real_tmp_stand_in.mkdir()
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(real_tmp_stand_in))
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(real_tmp_stand_in))

    foreign = _init_repo(real_tmp_stand_in, "checkout-under-temp")

    assert applicability.target_is_bare_temp_scratch(str(foreign / "dest.txt")) is False


def test_regression_symlinked_tmp_root_resolves_to_same_verdict_as_real(env, tmp_path, monkeypatch):
    """`/tmp` -> `/private/tmp` shape, expressed portably via a real
    symlink: a target reached through the symlink must classify identically
    to one spelled with the resolved real path."""
    real_root = tmp_path / "private-tmp-real"
    real_root.mkdir()
    link_root = tmp_path / "tmp-link"
    try:
        os.symlink(str(real_root), str(link_root), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"symlink creation unavailable on this platform: {exc}")

    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(link_root))
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(real_root))

    monkeypatch.setenv("HOME", str(env["home"]))
    session_start.write_session_start_record("sess-symlink-real", launch_cwd=str(env["anchor"]))
    session_start.write_session_start_record("sess-symlink-link", launch_cwd=str(env["anchor"]))
    scratchpad = real_root / "scratchpad"
    scratchpad.mkdir()

    dest_via_real = real_root / "scratchpad" / "via-real.txt"
    dest_via_link = link_root / "scratchpad" / "via-link.txt"

    result_real = guard.check_bump_outside_repo_write(
        f"echo hi > {dest_via_real}", "sess-symlink-real", str(env["anchor"]), {}
    )
    result_link = guard.check_bump_outside_repo_write(
        f"echo hi > {dest_via_link}", "sess-symlink-link", str(env["anchor"]), {}
    )

    assert result_real is None
    assert result_link is None


# ---------------------------------------------------------------------------
# AC6 -- cross-repo `cwd` drift, applied to the OUTSIDE-repo surface (the
# same scenario `test_bump_foreign_repo_write.py` [C3] pins for the
# cross-repo surface, so the anchor fix is pinned on more than the one
# surface the memo reproduced against). Drifts the live payload `cwd` ACROSS
# a repo boundary -- into an unrelated FOREIGN repo, never merely a
# subdirectory of the anchor repo (the `[subdir]` row in the plan's own
# repro table already passed before this fix; the `[FOREIGN]` row is the
# one this guard's own anchor resolution must now get right too). Uses a
# REAL `write_session_start_record` so applicability is genuinely True, per
# this plan's own repro methodology -- a test that passes only because
# applicability failed open proves nothing.
# ---------------------------------------------------------------------------


def test_ac6_cwd_drifted_to_a_foreign_repo_still_bumps_an_outside_repo_write(
    env, tmp_path, monkeypatch
):
    """Session launches in `env["anchor"]`; the live payload `cwd` later
    drifts to an unrelated FOREIGN repo (never `CLAUDE_PROJECT_DIR`, unset
    throughout -- the production condition the plan's repro table names).
    Pre-C1/C2, `resolve_launch_anchor`'s only anchor source was the in-repo
    record resolved from the LIVE `cwd` (`sessions_dir(cwd)`) -- which,
    once `cwd` has crossed into the foreign repo, addresses a hub where
    this session's record was never written, so the anchor resolves to
    `None`, `bump_applies` fails open, and this guard never even reaches
    its own outside-repo predicate for a target that plainly resolves
    under no git root at all. Post-C1/C2, the settings-home hub is
    cwd-independent and resolves the real anchor regardless of where the
    live `cwd` has drifted to, so the bump fires correctly."""
    monkeypatch.setenv("HOME", str(env["home"]))
    session_id = "sess-ac6-drift"
    session_start.write_session_start_record(session_id, launch_cwd=str(env["anchor"]))

    foreign = _init_repo(tmp_path, "foreign-drift-target")
    dest = env["outside"] / "drifted-write.txt"
    cmd = f"echo hi > {_posix(dest)}"

    result = guard.check_bump_outside_repo_write(cmd, session_id, str(foreign), {})

    assert result is not None


def test_extended_length_prefix_does_not_desync_is_under(monkeypatch, tmp_path):
    """`state/handoffs/2026-08-03-windows-extended-length-prefix-desync.md`
    -- outside-repo surface (C5). Same injected-asymmetry shape as the
    other two surfaces: `os.path.realpath` returns the Windows
    extended-length form for the candidate and the bare form for an
    always-allowed root. Before this fix, `_resolve_and_casefold`'s
    `casefold_path` call preserved the prefix and `_is_under` wrongly
    reported "not under" for the identical directory -- which, on the
    AC9 always-allowed-roots check this helper backs, would wrongly let a
    bump fire against a destination that should have been exempted."""
    real_dir = tmp_path / "always-allowed-root"
    real_dir.mkdir()
    bare_form = str(real_dir)
    prefixed_form = "\\\\?\\" + bare_form

    real_realpath = guard.os.path.realpath

    def fake_realpath(path, *a, **kw):
        if path == "candidate-input":
            return prefixed_form
        if path == "root-input":
            return bare_form
        return real_realpath(path, *a, **kw)

    monkeypatch.setattr(guard.os.path, "realpath", fake_realpath)

    candidate_cf = guard._resolve_and_casefold("candidate-input")
    root_cf = guard._resolve_and_casefold("root-input")
    assert candidate_cf is not None and root_cf is not None
    assert guard._is_under(candidate_cf, root_cf) is True


# ---------------------------------------------------------------------------
# C2 -- AC1: the guard-posix-path-rerooting defect, regression. On Windows,
# handing a POSIX/MSYS-absolute string to bare `os.path` re-roots it onto the
# process's current drive, producing a nonexistent path -- which this guard
# then (wrongly) treats as "resolves under no git root at all" and denies a
# write that is actually INSIDE the session's own repo. Both this guard and
# `bump_foreign_repo_write.py` now share `_write_bump_sink_shapes.
# resolve_relative`, which translates the MSYS spelling to a native path
# BEFORE `os.path.isabs`/`os.path.join`/`os.path.realpath` ever see it.
# ---------------------------------------------------------------------------


@requires_native_windows_filesystem
def test_ac1_msys_absolute_target_inside_own_repo_not_denied(env, monkeypatch):
    from coordinator_core.bash_guards import _write_bump_sink_shapes as sink_shapes

    monkeypatch.setattr(sink_shapes, "_host_is_windows", lambda: True)
    _set_anchor(monkeypatch, env, "sess-ac1")
    dest = env["anchor"] / "msys-inside.txt"
    msys_target = _msys_form(dest)
    cmd = f"echo hi > {msys_target}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-ac1", str(env["anchor"]), {})

    assert result is None, (
        "MSYS-spelled target inside the session's own repo was wrongly "
        "denied -- the guard-posix-path-rerooting defect"
    )


# ---------------------------------------------------------------------------
# C2 -- AC3: the fix must not turn a real bump into a permit. A genuinely
# foreign (no-git-root) target, spelled in the same MSYS form, still bumps.
# ---------------------------------------------------------------------------


@requires_native_windows_filesystem
def test_ac3_msys_absolute_target_genuinely_outside_repo_still_denies(env, monkeypatch):
    from coordinator_core.bash_guards import _write_bump_sink_shapes as sink_shapes

    monkeypatch.setattr(sink_shapes, "_host_is_windows", lambda: True)
    _set_anchor(monkeypatch, env, "sess-ac3-msys")
    dest = env["outside"] / "msys-outside.txt"
    msys_target = _msys_form(dest)
    cmd = f"echo hi > {msys_target}"

    result = guard.check_bump_outside_repo_write(cmd, "sess-ac3-msys", str(env["anchor"]), {})

    assert result is not None


# ---------------------------------------------------------------------------
# C2 -- AC5: both twins agree. Feeding the SAME input to both modules'
# `_resolve_relative` attributes (now both aliases onto the identical lifted
# `_write_bump_sink_shapes.resolve_relative`) must yield identical output --
# trivially true once both alias the same function, which is the correct
# outcome for a parity assertion, not a reason to weaken it.
# ---------------------------------------------------------------------------


def test_ac5_both_twins_resolve_relative_agree():
    from coordinator_core.bash_guards import bump_foreign_repo_write as foreign_guard

    base = "C:\\base\\dir"  # abs-path-ok: illustrative literal, not machine-specific
    for target in ("/c/Users/x/file.txt", "relative/x.txt", "/tmp/x", "", "/c"):
        assert guard._resolve_relative(base, target) == foreign_guard._resolve_relative(
            base, target
        )
    # Both aliases resolve to the literal SAME function object, not merely
    # two independently-behaving functions that happen to agree today.
    assert guard._resolve_relative is foreign_guard._resolve_relative


# ---------------------------------------------------------------------------
# C2 -- [P2] fast-follow: the deterministic, any-host proof the AC1/AC3
# claims rest on. `_host_is_windows()` forced True via monkeypatch (the same
# seam `translate_msys_path`/`resolve_relative` are documented as gated on),
# proving the MSYS-decode Windows branch produces the correct native path on
# EVERY interpreter -- not merely on whichever host happens to already be
# Windows. Pure string-in/string-out, no filesystem touched at all, so
# nothing here is hardware-gated the way the full AC1/AC3/AC7 round-trips
# above are (see `requires_native_windows_filesystem`'s own docstring).
# ---------------------------------------------------------------------------


def test_translate_msys_path_windows_branch_decodes_drive_mount(monkeypatch):
    from coordinator_core.bash_guards import _write_bump_sink_shapes as sink_shapes

    monkeypatch.setattr(sink_shapes, "_host_is_windows", lambda: True)

    assert (
        sink_shapes.translate_msys_path("/c/Users/me/repo/file.txt")
        == "C:\\Users\\me\\repo\\file.txt"
    )


def test_translate_msys_path_identity_off_windows_host(monkeypatch):
    """The IDENTITY branch (the one the six vacuous tests were actually
    exercising pre-fix) -- pinned directly so it stays covered on its own
    terms, distinct from the Windows-branch proof above."""
    from coordinator_core.bash_guards import _write_bump_sink_shapes as sink_shapes

    monkeypatch.setattr(sink_shapes, "_host_is_windows", lambda: False)

    assert (
        sink_shapes.translate_msys_path("/c/Users/me/repo/file.txt")
        == "/c/Users/me/repo/file.txt"
    )


def test_resolve_relative_msys_target_inside_repo_decodes_to_native_child(monkeypatch):
    """AC1-equivalent, host-independent: an MSYS-spelled target that lands
    inside `base` decodes to the correct native child path."""
    from coordinator_core.bash_guards import _write_bump_sink_shapes as sink_shapes

    monkeypatch.setattr(sink_shapes, "_host_is_windows", lambda: True)

    base = "C:\\repo\\anchor"  # abs-path-ok: synthetic literal for the seam-forced branch
    target = "/c/repo/anchor/msys-inside.txt"

    assert sink_shapes.resolve_relative(base, target) == "C:\\repo\\anchor\\msys-inside.txt"


def test_resolve_relative_msys_target_outside_repo_decodes_to_native_sibling(monkeypatch):
    """AC3-equivalent, host-independent: an MSYS-spelled target that lands
    OUTSIDE `base` decodes to the correct native path, and it is not a
    descendant of `base` -- the fix must not turn a real outside-repo write
    into a permit."""
    from coordinator_core.bash_guards import _write_bump_sink_shapes as sink_shapes

    monkeypatch.setattr(sink_shapes, "_host_is_windows", lambda: True)

    base = "C:\\repo\\anchor"  # abs-path-ok: synthetic literal for the seam-forced branch
    target = "/c/outside-scratch/msys-outside.txt"

    resolved = sink_shapes.resolve_relative(base, target)

    assert resolved == "C:\\outside-scratch\\msys-outside.txt"
    assert not resolved.startswith(base)


# ---------------------------------------------------------------------------
# C2 -- AC7: reachability, a 2x2 matrix -- {bump_outside, bump_foreign} x
# {bash, powershell} -- each proven through `evaluate_payload_json`, NOT by
# calling `check()` directly (DR-280: a whole unreachable deny leg once
# survived a fully green suite that bypassed the dispatcher). A
# POSIX/MSYS-absolute in-repo target must round-trip to "not denied" through
# the REAL dispatcher entrypoint on every leg.
# ---------------------------------------------------------------------------


def _dispatch_evaluate(cmd, session_id, cwd, tool_name="Bash"):
    import json as _json

    from coordinator_core.bash_guards import dispatch as _dispatch

    payload = {
        "tool_name": tool_name,
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
    }
    return _dispatch.evaluate_payload_json(_json.dumps(payload))


@requires_native_windows_filesystem
def test_ac7_bump_outside_repo_write_reachable_via_dispatcher_bash(env, monkeypatch):
    from coordinator_core.bash_guards import _write_bump_sink_shapes as sink_shapes

    monkeypatch.setattr(sink_shapes, "_host_is_windows", lambda: True)
    _set_anchor(monkeypatch, env, "sess-ac7-out-bash")
    dest = env["anchor"] / "ac7-out-bash.txt"
    msys_target = _msys_form(dest)
    cmd = f"echo hi > {msys_target}"

    result = _dispatch_evaluate(cmd, "sess-ac7-out-bash", str(env["anchor"]))

    assert result is None


@requires_powershell_grammar
@requires_native_windows_filesystem
def test_ac7_bump_outside_repo_write_reachable_via_dispatcher_powershell(env, monkeypatch):
    from coordinator_core.bash_guards import _write_bump_sink_shapes as sink_shapes

    monkeypatch.setattr(sink_shapes, "_host_is_windows", lambda: True)
    _set_anchor(monkeypatch, env, "sess-ac7-out-ps")
    dest = env["anchor"] / "ac7-out-ps.txt"
    msys_target = _msys_form(dest)
    cmd = f"New-Item -Path {msys_target} -ItemType File"

    result = _dispatch_evaluate(cmd, "sess-ac7-out-ps", str(env["anchor"]), tool_name="PowerShell")

    assert result is None


@requires_native_windows_filesystem
def test_ac7_bump_foreign_repo_write_reachable_via_dispatcher_bash(env, monkeypatch):
    from coordinator_core.bash_guards import _write_bump_sink_shapes as sink_shapes

    monkeypatch.setattr(sink_shapes, "_host_is_windows", lambda: True)
    _set_anchor(monkeypatch, env, "sess-ac7-foreign-bash")
    dest = env["anchor"] / "ac7-foreign-bash.txt"
    msys_target = _msys_form(dest)
    cmd = f"cp {_posix(env['anchor'] / 'README.md')} {msys_target}"

    result = _dispatch_evaluate(cmd, "sess-ac7-foreign-bash", str(env["anchor"]))

    assert result is None


@requires_powershell_grammar
@requires_native_windows_filesystem
def test_ac7_bump_foreign_repo_write_reachable_via_dispatcher_powershell(env, monkeypatch):
    from coordinator_core.bash_guards import _write_bump_sink_shapes as sink_shapes

    monkeypatch.setattr(sink_shapes, "_host_is_windows", lambda: True)
    _set_anchor(monkeypatch, env, "sess-ac7-foreign-ps")
    dest = env["anchor"] / "ac7-foreign-ps.txt"
    msys_target = _msys_form(dest)
    cmd = f"Copy-Item {_posix(env['anchor'] / 'README.md')} {msys_target}"

    result = _dispatch_evaluate(
        cmd, "sess-ac7-foreign-ps", str(env["anchor"]), tool_name="PowerShell"
    )

    assert result is None


# ---------------------------------------------------------------------------
# PowerShell `Set-Location` cwd-tracking parity fix (2026-08-08, mirroring
# `bump_foreign_repo_write.py`'s own fix for backlog row
# `2026-08-07-bump-foreign-repo-write-s-powershell-leg-3254b856d676`, which
# named this guard's identical gap as out of that dispatch's scope). These
# assert through `check_bump_outside_repo_write`'s own real return value (a
# deny envelope, or `None`) -- never by re-reading a constant the test
# itself set -- so a revert of the `Set-Location` tracking fix flips these
# from PASS to FAIL.
# ---------------------------------------------------------------------------


@requires_powershell_grammar
def test_set_location_into_no_repo_dir_then_write_is_detected(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-ps-setloc-outside")
    cmd = f'Set-Location {_posix(env["outside"])}; New-Item -Path file.txt -ItemType File'

    result = guard.check_bump_outside_repo_write(
        cmd, "sess-ps-setloc-outside", str(env["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is not None, (
        "a New-Item write extracted AFTER a Set-Location into a directory "
        "under NO git root must resolve against the CHANGED base and bump "
        "-- the exact parity gap this fix closes"
    )


@requires_powershell_grammar
def test_set_location_into_git_root_dir_then_write_does_not_bump(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-ps-setloc-anchor")
    cmd = f'Set-Location {_posix(env["anchor"])}; New-Item -Path file.txt -ItemType File'

    result = guard.check_bump_outside_repo_write(
        cmd, "sess-ps-setloc-anchor", str(env["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is None, (
        "a Set-Location into a directory that IS under a git root followed "
        "by a write must not bump -- the tracking fix must not turn an "
        "in-repo write into a false bump"
    )


@requires_powershell_grammar
def test_set_location_unresolvable_target_yields_silent_not_deny(env, monkeypatch):
    """A `Set-Location` to a variable-valued (unresolvable) target must
    never guess a base for the write that follows -- the leg goes SILENT
    (via `_verdict.record_silent`) for the rest of the command, and
    `check_bump_outside_repo_write` itself still returns `None` (never a
    manufactured verdict off an untrusted cwd)."""
    from coordinator_core.bash_guards import _verdict

    _set_anchor(monkeypatch, env, "sess-ps-setloc-unresolvable")
    cmd = "Set-Location $SomeUnresolvedVar; New-Item -Path file.txt -ItemType File"

    with _verdict.collecting() as silences:
        result = guard.check_bump_outside_repo_write(
            cmd, "sess-ps-setloc-unresolvable", str(env["anchor"]), {"tool_name": "PowerShell"}
        )

    assert result is None, (
        "an unresolvable Set-Location target must never produce a verdict "
        "off a guessed base"
    )
    assert any(
        s.guard_name == "bump-outside-repo-write" and "Set-Location" in s.reason
        for s in silences
    ), "the unresolvable Set-Location must be recorded SILENT, not merely swallowed"
