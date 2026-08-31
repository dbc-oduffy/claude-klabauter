"""Regression tests for coordinator.bin.lib.git_hook_install's D3 fix
(2026-07-28, break-class): the unresolvable-interpreter case in the
generated hook shims used to be a silent `[ -n "$_PY" ] || exit 0` — zero
stderr output, asymmetric with the missing-SCRIPT branch two lines below it,
which already prints a loud "commits are NOT being auto-pushed" WARNING.
These tests pin that both cases now announce themselves identically.

See git_hook_install.py's own module docstring (Behavior section) for the
full contract this guards.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import git_hook_install as ghi  # noqa: E402
from git_hook_install import _append_block, _ml_get, _shim_body  # noqa: E402


def _make_tool_bindir(tmp_path: Path, tools: dict) -> str:
    """Directory containing ONLY symlinks to `tools` (name -> real absolute
    path) — see coordinator_core/ops/test_install_publish_repo_precommit_hook.py's
    identically-named helper for why reusing a real tool's parent directory
    is unsafe (it can smuggle in other binaries that happen to live next to
    the one you wanted reachable)."""
    bindir = tmp_path / "tool-bindir"
    bindir.mkdir(exist_ok=True)
    for name, real_path in tools.items():
        link = bindir / name
        if not link.exists():
            link.symlink_to(real_path)
    return str(bindir)


def _sh() -> str:
    """Absolute path to a POSIX `sh`, or skip the calling test.

    NEGATIVE SPEC: never the literal `/bin/sh`. Windows is first-class for this
    suite and has no such path -- git ships `sh` under its own usr/bin -- so a
    hard-coded invocation dies with WinError 2 before the emitted hook body
    under test is ever read, failing identically whatever the code under test
    does. The emitted bodies' own `#!/bin/sh` shebangs are unaffected: git
    resolves those through its bundled shell, and every call site here invokes
    the interpreter explicitly rather than relying on the shebang.
    """
    sh = shutil.which("sh")
    if not sh:
        import pytest

        pytest.skip("no POSIX sh resolvable on PATH in this environment")
    return sh


def _sh_path(name: str) -> str:
    result = subprocess.run([_sh(), "-c", f"command -v {name}"], capture_output=True, text=True)
    path = result.stdout.strip()
    if not path:
        import pytest

        pytest.skip(f"{name} not found on PATH in this environment")
    return path


def _no_python_path(tmp_path: Path) -> str:
    """A PATH with sh reachable but no python3/python/py binary resolvable."""
    return _make_tool_bindir(tmp_path, {"sh": _sh()})


_UNRESOLVABLE_BAKED_PY = "/nonexistent/coordinator-test/python"


def _with_unresolvable_interpreter(body: str) -> str:
    """Point the baked `_PY=` rung at a path that cannot exist.

    Sanitizing PATH alone stopped being enough at 304a1bc30: `_shim_body` now
    bakes an ABSOLUTE `sys.executable` and only falls back to the `$PATH` walk
    when `[ -x "$_PY" ]` fails, so a body under a python-free PATH still finds
    the real interpreter and runs the hook for real. Both rungs have to miss
    for the WARNING branch these tests pin to be reachable at all -- this
    handles the baked one, `_no_python_path` handles the walk.
    """
    return re.sub(r'^(_PY=")[^"]*(")$', r"\1" + _UNRESOLVABLE_BAKED_PY + r"\2", body, flags=re.M)


# ---------------------------------------------------------------------------
# _shim_body — fresh-install / self-heal shim
# ---------------------------------------------------------------------------

def test_shim_body_missing_interpreter_message_present_in_source():
    body = _shim_body("/fake/coord/bin", "coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"')
    assert "no python3/python/py interpreter found on PATH" in body
    assert "commits are NOT being auto-pushed / annotated by this hook" in body
    # Still exits 0 — a push helper must never block a commit (D3: loud, not fail-closed).
    assert 'exit 0; }' in body


def test_shim_body_missing_interpreter_blocks_loudly_at_runtime(tmp_path):
    body = _shim_body("/fake/coord/bin", "coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"')
    hook = tmp_path / "post-commit"
    hook.write_text(_with_unresolvable_interpreter(body), encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = _no_python_path(tmp_path)
    result = subprocess.run([_sh(), str(hook)], capture_output=True, text=True, env=env)

    assert result.returncode == 0  # never fail-closed
    assert "WARNING" in result.stderr
    assert "no python3/python/py interpreter found on PATH" in result.stderr


def test_shim_body_missing_interpreter_and_missing_script_read_the_same_shape():
    """The interpreter-missing and script-missing WARNING branches must use
    the same wording contract ("[coordinator] WARNING: hook installed but
    ... commits are NOT being auto-pushed / annotated by this hook") so an
    operator scanning stderr recognizes both as the same class of problem."""
    body = _shim_body("/fake/coord/bin", "coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"')
    assert body.count("[coordinator] WARNING: hook installed but") == 2
    assert body.count("commits are NOT being auto-pushed / annotated by this hook") == 2


# ---------------------------------------------------------------------------
# _HOOK_GEN_STAMP <-> emitted body shape coupling (AC3, plan
# 2026-08-14-hook-currency-stops-resting-on-a-comment.md). A checksum over
# `_shim_body`'s output for a fixed input pins TODAY's shape — the whole
# point of this test is that it goes RED the moment `_shim_body` grows a new
# rung (or drops one, or reorders a line) without `_HOOK_GEN_STAMP` in
# git_hook_install.py being bumped alongside it. The failure message says
# what to do, not merely that a checksum moved: bump the stamp, then update
# _EXPECTED_BODY_SHAPE_CHECKSUM here to match.
#
# THE BAKED INTERPRETER PATH IS NORMALIZED OUT BEFORE HASHING (2026-08-25, gen
# 5). `_shim_body` now interpolates `py_probe_sh.baked_python_lines`, which
# embeds THIS machine's `sys.executable`. That literal is machine state, not
# body SHAPE: hashing it would make this test pass only on the box that last
# updated the constant and fail on every other one — including the fleet floor
# (a MacBook), where the path is not even the same shape. `_normalize_baked_py`
# replaces the assigned value with a fixed placeholder so the checksum still
# goes red for a new/dropped/reordered rung — the thing this test exists to
# catch — and stays green across machines. It deliberately does NOT elide the
# whole line: the `_PY="..."` assignment and its `[ -x ]` self-heal sibling are
# rungs, and losing either must still be caught.
# ---------------------------------------------------------------------------

_EXPECTED_BODY_SHAPE_CHECKSUM = "98cd38f8e25a78cb40e0990769cb89b62d90e49d1ab869fe78befb81c2cb67cc"

_BAKED_PY_PLACEHOLDER = "<BAKED-INTERPRETER>"


def _normalize_baked_py(body: str) -> str:
    """Replace the machine-specific baked interpreter path with a placeholder.

    Keeps the assignment line itself in the hashed text — only its VALUE is
    normalized — so a dropped or reordered interpreter rung still moves the
    checksum.
    """
    return re.sub(
        r'^(_PY=")[^"]*(")$',
        r"\1" + _BAKED_PY_PLACEHOLDER + r"\2",
        body,
        flags=re.M,
    )


def test_hook_gen_stamp_bump_is_required_for_shape_changes():
    body = _shim_body("/fake/coord/bin", "coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"')
    checksum = hashlib.sha256(_normalize_baked_py(body).encode("utf-8")).hexdigest()
    assert checksum == _EXPECTED_BODY_SHAPE_CHECKSUM, (
        f"_shim_body's emitted body shape changed (new checksum {checksum}) without a "
        "matching bump of _HOOK_GEN_STAMP in coordinator/bin/lib/git_hook_install.py. "
        "Fix: bump _HOOK_GEN_STAMP there, then update _EXPECTED_BODY_SHAPE_CHECKSUM in "
        "this test (coordinator/bin/lib/test_git_hook_install.py) to the new checksum."
    )
    # The stamp line itself must actually be present in what was hashed —
    # otherwise this checksum could go stale silently alongside a _shim_body
    # that stopped emitting the stamp at all.
    assert ghi._hook_gen_stamp_line() in body


def test_interpreter_rung_costs_no_unconditional_subshell():
    """The emitted hook must not spend a process resolving its interpreter.

    `prepare-commit-msg` and `post-commit` fire on every NON-ENGINE commit —
    the backstop path that survives both the staged-rollback gate's deletion
    and the engine-side commit collapse. Until 2026-08-25 both bodies opened
    with `_PY="$(_py_resolve)"`, a command substitution (a subshell, i.e. a
    process) wrapping a `$PATH` walk, paid unconditionally on every fire.
    `baked_python_lines` replaces it with an assignment plus an `[ -x ]` test.

    Asserted as a SPAWN COUNT, never a duration: the per-hook delta is about
    one scheduler quantum on this box, and DR-344 makes a process-time figure
    inside the quantum a non-result.

    The `.doe-root` rung's own `$(cat ...)` is deliberately NOT counted — it
    sits behind `[ -f "$SCRIPT" ] ||` and never runs on a box whose earlier
    SCRIPT rungs resolve. Counting it would credit this fix with removing a
    process that was already conditional, and overstating a saving is the
    recurring defect on this surface.
    """
    for script_name, invoke in (
        ("coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"'),
        ("coordinator-prepare-commit-msg", 'exec "$_PY" "$SCRIPT" "$@"'),
    ):
        body = _shim_body("/fake/coord/bin", script_name, invoke)

        # The walk MUST still be present — it is the recovery rung for a stale
        # bake, and these hooks fail OPEN, so losing it is a silent-off mode
        # rather than a loud failure. See baked_python_lines' docstring.
        assert "_py_resolve() {" in body, (
            f"{script_name}: the $PATH-walk fallback was removed — a stale "
            "baked path would now silently disable this fail-open hook"
        )
        assert '[ -x "$_PY" ] || _PY="$(_py_resolve)"' in body, (
            f"{script_name}: the bake no longer falls back to the walk"
        )

        unconditional = [
            line
            for line in body.split("\n")
            if "$(" in line
            and not line.lstrip().startswith("[ -f")
            and "||" not in line.split("$(")[0]
        ]
        assert not unconditional, (
            f"{script_name}: the interpreter rung must cost no unconditional "
            f"subshell; found {unconditional}"
        )

        # The walk's own subshell is allowed, but ONLY behind the `[ -x ]`
        # guard — i.e. paid when the bake is dead, never on the happy path.
        walk_uses = [line for line in body.split("\n") if "$(_py_resolve)" in line]
        assert len(walk_uses) == 1, f"{script_name}: expected one guarded walk use, got {walk_uses}"
        assert walk_uses[0].lstrip().startswith("[ -x "), (
            f"{script_name}: the walk is invoked unconditionally: {walk_uses[0]!r}"
        )


# ---------------------------------------------------------------------------
# _append_block — marker-absent append (existing custom hook chain preserved)
# ---------------------------------------------------------------------------

def test_append_block_missing_interpreter_message_present_in_source():
    block = _append_block(
        "/fake/coord/bin",
        "coordinator-auto-push",
        "coordinator auto-push (crash insurance)",
        '"$_PY" "$_T" "$@"',
    )
    assert "no python3/python/py interpreter found on PATH" in block
    assert "commits are NOT being auto-pushed / annotated by this hook" in block


def test_append_block_missing_interpreter_blocks_loudly_at_runtime(tmp_path):
    block = _append_block(
        "/fake/coord/bin",
        "coordinator-auto-push",
        "coordinator auto-push (crash insurance)",
        '"$_PY" "$_T" "$@"',
    )
    hook = tmp_path / "post-commit"
    hook.write_text(
        _with_unresolvable_interpreter("#!/bin/sh\n" + block + " || true\n"),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PATH"] = _no_python_path(tmp_path)
    # Hermetic, not incidental: without this the block resolves THIS box's real
    # settings-home forwarder and the exhaustion path under test never runs.
    env["COORDINATOR_SETTINGS_HOME"] = (tmp_path / "no-such-settings-home").as_posix()
    result = subprocess.run([_sh(), str(hook)], capture_output=True, text=True, env=env)

    assert result.returncode == 0  # append blocks never disturb the parent hook's exit status
    assert "WARNING" in result.stderr
    assert "no python3/python/py interpreter found on PATH" in result.stderr


def test_append_block_missing_interpreter_and_missing_script_both_warn():
    block = _append_block(
        "/fake/coord/bin",
        "coordinator-auto-push",
        "coordinator auto-push (crash insurance)",
        '"$_PY" "$_T" "$@"',
    )
    assert block.count("[coordinator] WARNING: hook installed but") == 2


# ---------------------------------------------------------------------------
# _ml_get — Windows-exec regression (extensionless `machine-local` shebang
# script is not directly invocable by CreateProcess; see this module's own
# fix and coordinator_core.launchable's module docstring for the underlying
# WinError 193 defect).
# ---------------------------------------------------------------------------

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.mark.skipif(os.name != "nt", reason="WinError 193 exec defect is Windows-only")
def test_ml_get_resolves_via_cmd_twin_on_windows(tmp_path):
    """`ml_bin` is the bareword `machine-local` (no extension) — `CreateProcess`
    cannot exec it directly. `_ml_get` must resolve through its `.cmd` twin
    (mirroring `_resolve_machine_local_bin`'s real-world layout) rather than
    silently swallowing the WinError 193 and returning None as if the key
    were simply unset."""
    ml_bin = tmp_path / "machine-local"
    ml_bin.write_text("#!/usr/bin/env python3\nraise SystemExit(1)\n", encoding="utf-8")
    ml_cmd = tmp_path / "machine-local.cmd"
    ml_cmd.write_text(
        "@echo off\r\n"
        'if "%1"=="get" if "%2"=="repos.claude_klabauter" (echo dummy-resolved-value) else (exit /b 1)\r\n',
        encoding="utf-8",
    )

    result = _ml_get(str(ml_bin), "repos.claude_klabauter")

    assert result == "dummy-resolved-value"


def test_ml_get_exec_failure_warns_to_stderr_and_returns_none(tmp_path, capsys):
    """An exec failure (resolver could not even be launched) must be
    distinguishable from a genuinely-unset key: it warns to stderr rather
    than the two cases looking identical (both `None`, zero output)."""
    unlaunchable = tmp_path  # a directory is never launchable as argv[0]

    result = _ml_get(str(unlaunchable), "some.key")

    assert result is None
    captured = capsys.readouterr()
    assert "could not execute machine-local resolver" in captured.err


def test_container_registry_keys_are_not_heal_targets(monkeypatch, tmp_path):
    """A `repos.*` container key must never reach `_classify_target`.

    `repos.fleet_root` names the directory the fleet's repos live UNDER, so it
    has no `.git` of its own and classifies as `missing` — a broken-registry
    warning, printed on a DAILY ceremony, for an entry that is correct and that
    no operator action could ever satisfy. Negative spec: this warning trains
    operators to scroll past fleet-heal output, which is the failure mode
    `_classify_target`'s three-way split exists to prevent.
    """
    fleet_root = tmp_path / "fleet"
    (fleet_root / "claude-klabauter").mkdir(parents=True)
    monkeypatch.setattr(
        ghi,
        "_merged_flat_registry",
        lambda: {
            "repos.fleet_root": str(fleet_root),
            "repos.claude_klabauter": str(fleet_root / "claude-klabauter"),
        },
    )

    roots = ghi._registry_repo_roots("")

    assert [key for key, _ in roots] == ["repos.claude_klabauter"]
    assert ghi._classify_target(str(fleet_root)) == "missing", (
        "guards the premise: fleet_root is excluded because it WOULD warn, "
        "not because it happens to classify cleanly"
    )

# ---------------------------------------------------------------------------
# The no-session gate is GENERATED from the ladder, never hand-copied.
# ---------------------------------------------------------------------------

def test_session_gate_is_generated_from_the_ladder():
    """The emitted no-session gate must name exactly SESSION_ENV_PRECEDENCE.

    This is the artifact that makes `skip_if_all_unset` safe to exist. The
    danger it guards is NOT divergence at authoring time -- the caller passes
    the constant, so the emitted line cannot disagree the day it is written.
    It is STALENESS: the ladder gains or loses a tier later, and an installed
    hook keeps testing the old set. That drift is silent and fails in the worst
    direction (a commit that should be stamped exits early with no Session-Id),
    which is exactly why a third hand-written copy of this ladder was refused.

    A gen-stamp bump does not cover it: forgetting the bump and forgetting the
    gate are the same forgetting. This test fails on the ladder change itself,
    before anything is installed anywhere.
    """
    from coordinator_core.session.core import SESSION_ENV_PRECEDENCE

    body = _shim_body(
        "/fake/coord/bin",
        "coordinator-prepare-commit-msg",
        'exec "$_PY" "$SCRIPT" "$@"',
        skip_if_all_unset=SESSION_ENV_PRECEDENCE,
    )
    gate = [ln for ln in body.splitlines() if ln.startswith('[ -z "')]
    assert len(gate) == 1, f"expected exactly one no-session gate, got {gate}"
    expected = '[ -z "' + "".join(f"${v}" for v in SESSION_ENV_PRECEDENCE) + '" ] && exit 0'
    assert gate[0] == expected, (
        f"the emitted no-session gate {gate[0]!r} no longer matches "
        f"SESSION_ENV_PRECEDENCE {tuple(SESSION_ENV_PRECEDENCE)!r}. The ladder moved. "
        "Fix: nothing in this test -- re-emit the hooks (the gate is generated), bump "
        "_HOOK_GEN_STAMP, and reinstall, or installed hooks keep testing the old tier set."
    )


def test_session_gate_resolves_before_any_interpreter_resolution():
    """Ordering invariant, owned by claude-klabauter-59 and stated
    mechanism-independently: as the emitted body executes, the sentinel guards
    resolve before ANY interpreter resolution is attempted -- including a $PATH
    walk, command substitution, or subshell. A gate that sits below the probe
    has already paid the cost it exists to avoid.
    """
    from coordinator_core.session.core import SESSION_ENV_PRECEDENCE

    body = _shim_body(
        "/fake/coord/bin",
        "coordinator-prepare-commit-msg",
        'exec "$_PY" "$SCRIPT" "$@"',
        skip_env="COORDINATOR_TRAILERS_ALREADY_APPLIED",
        skip_if_all_unset=SESSION_ENV_PRECEDENCE,
    )
    lines = body.splitlines()
    gate_at = max(i for i, ln in enumerate(lines) if ln.startswith("[ -n \"$COORDINATOR_TRAILERS") or ln.startswith('[ -z "'))
    first_interp = min(
        i for i, ln in enumerate(lines) if ln.startswith("_py_resolve()") or ln.startswith('_PY=')
    )
    assert gate_at < first_interp, (
        f"a guard at line {gate_at + 1} sits at or below the first interpreter rung at "
        f"line {first_interp + 1} -- the ordering invariant is broken"
    )


def test_post_commit_never_carries_the_no_session_gate():
    """auto-push is the SOLE PUBLISHER on a non-engine commit.

    A no-session commit is precisely when nothing else will push it, so gating
    post-commit on session presence would silently stop pushing the commits most
    in need of it -- fail-closed on publication, wearing the costume of an
    optimisation. Negative spec, pinned: only prepare-commit-msg may carry it.
    """
    body = _shim_body(
        "/fake/coord/bin",
        "coordinator-auto-push",
        'exec "$_PY" "$SCRIPT" "$@"',
        skip_env="COORDINATOR_AUTO_PUSH_SUPPRESS_FOR_SYNC_PUSH",
    )
    assert not [ln for ln in body.splitlines() if ln.startswith('[ -z "')]
    assert "CLAUDE_SESSION_ID" not in body


# ---------------------------------------------------------------------------
# _append_block — MSYS `.exe`-sibling discipline (2026-08-29). The fix that
# taught `_shim_body` to guard every rung with `_have_py` reached exactly one
# rung of `_append_block` and left the helper undefined there, so the emitted
# block called a function that does not exist: the `.doe-root` rung answered
# "no" unconditionally and every commit through a foreign hook printed a shell
# error. These tests pin BOTH halves — the helper is emitted wherever it is
# called, and no resolution rung is left on the bare `[ -f ]` that the MSYS
# `.exe` fallback makes a lie.
# ---------------------------------------------------------------------------
#
# Review: overengineering-reviewer Finding 4 residual (state/bug-backlog/
# 2026-08-30-auto-push-main-and-two-launcher-referenc-2d703797edb5.yaml) --
# the exemplar script name below was "coordinator-auto-push", a script
# `_append_block` no longer generates a shim for (`ensure_post_commit_hook`
# is a pure no-op, C7; `_append_block`'s one production caller today is
# `ensure_prepare_commit_msg_hook`). Renamed to the real current caller so
# this generic-shim test never reads as pinning a retired forwarder target
# -- `_append_block` itself is a plain string-template function with no
# behavior tied to either script name.

_APPEND_BLOCK_ARGS = (
    "/fake/coord/bin",
    "coordinator-prepare-commit-msg",
    "coordinator Session-Id trailer injection",
    '"$_PY" "$_T" "$@"',
)


def test_append_block_defines_every_helper_it_calls():
    block = _append_block(*_APPEND_BLOCK_ARGS)
    assert "_have_py " in block, "no _have_py call at all — the rungs regressed to [ -f ]"
    assert '_have_py() {' in block, (
        "_append_block calls _have_py without emitting its definition. The block is "
        "appended into a foreign hook, so nothing above it is ours to borrow from."
    )
    # The definition must precede every call, or the first rungs run against an
    # undefined function.
    assert block.index('_have_py() {') < block.index('_have_py "')


def test_append_block_resolution_rungs_never_use_bare_dash_f():
    """`[ -f "$_T" ]` is TRUE under MSYS sh when only `$_T.exe` exists."""
    block = _append_block(*_APPEND_BLOCK_ARGS)
    assert '[ -f "$_T" ]' not in block
    assert '[ ! -f "$_T" ]' not in block


def test_append_block_runs_an_installed_exe_forwarder_directly(tmp_path):
    """A `.exe` forwarder is the intended post-install artifact: run it, and
    never enter the interpreter chain that would hand it to python."""
    settings_home = tmp_path / "settings-home"
    (settings_home / "bin").mkdir(parents=True)
    forwarder = settings_home / "bin" / "coordinator-prepare-commit-msg.exe"
    marker = tmp_path / "forwarder-ran"
    forwarder.write_text(
        f'#!/bin/sh\nprintf ran > "{marker.as_posix()}"\n', encoding="utf-8"
    )
    forwarder.chmod(0o755)

    hook = tmp_path / "prepare-commit-msg"
    hook.write_text(
        _with_unresolvable_interpreter(
            "#!/bin/sh\n" + _append_block(*_APPEND_BLOCK_ARGS) + " || true\n"
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["COORDINATOR_SETTINGS_HOME"] = settings_home.as_posix()
    result = subprocess.run([_sh(), str(hook)], capture_output=True, text=True, env=env)

    assert result.returncode == 0
    assert marker.exists(), (
        f"the .exe forwarder was not run; stderr={result.stderr!r}"
    )
    assert "WARNING" not in result.stderr


def test_append_block_emits_no_shell_errors_when_nothing_resolves(tmp_path):
    """Exhaustion must be the two loud WARNINGs and nothing else — a
    `command not found` here means the block called a helper it never emitted."""
    hook = tmp_path / "post-commit"
    hook.write_text(
        _with_unresolvable_interpreter(
            "#!/bin/sh\n" + _append_block(*_APPEND_BLOCK_ARGS) + " || true\n"
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PATH"] = _no_python_path(tmp_path)
    env["COORDINATOR_SETTINGS_HOME"] = (tmp_path / "no-such-settings-home").as_posix()
    result = subprocess.run([_sh(), str(hook)], capture_output=True, text=True, env=env)

    assert result.returncode == 0
    assert "not found" in result.stderr  # the coordinator WARNING, checked below
    for line in result.stderr.splitlines():
        assert "[coordinator] WARNING" in line, f"unexpected shell error: {line!r}"


# ---------------------------------------------------------------------------
# MSYS drive-letter normalisation — BOTH emitters (2026-08-31).
#
# `_shim_body` has carried the `case "$SCRIPT" in /?/*)` expansion since the
# memo that reported the defect; `_append_block` did not, so a hook installed
# by the append leg resolved `_T` under MSYS `sh` (which reads /c/Users/...
# happily) and handed that string to a NATIVE python.exe, which has no /c
# mount and reads the leading slash as repo-relative. The rung passes its own
# existence test and execs a path rooted at the repo drive — a silent wrong
# answer, which is why both emitters' docstrings say they must change
# together. These pin that they do.
# ---------------------------------------------------------------------------


def test_both_hook_emitters_normalise_msys_drive_letters():
    """Neither emitter may hand a POSIX-absolute path to a native python.

    A TEXT-level guard on purpose, in addition to the executable one below:
    it names both emitters, so deleting the expansion from either one fails
    here with a message saying which, rather than only failing whichever
    end-to-end case happens to cover it.
    """
    shim = _shim_body("/fake/coord/bin", "coordinator-prepare-commit-msg", "hdr")
    append = _append_block(*_APPEND_BLOCK_ARGS)

    for label, body, var in (
        ("_shim_body", shim, "SCRIPT"),
        ("_append_block", append, "_T"),
    ):
        assert f'case "${var}" in /?/*)' in body, (
            f"{label} does not normalise the MSYS drive-letter form on ${var}. "
            "Under git's MSYS sh a /c/Users/... path passes every existence "
            "test and then execs against a native python.exe that has no /c "
            "mount. Both emitters carry this expansion or neither is correct "
            "-- see their own docstrings."
        )


def test_append_block_msys_normalisation_actually_transforms_the_path():
    """The expansion is executed, not merely present.

    `${_td%%/*}` / `${_td#*/}` is easy to write subtly wrong (a `%` for a `#`
    silently yields the wrong half), and a substring assertion cannot tell a
    correct expansion from a broken one. This runs the real fragment under
    the real `sh` and checks the transform.
    """
    sh = _sh()
    if not sh:
        import pytest

        pytest.skip("no POSIX sh available on this host")

    append = _append_block(*_APPEND_BLOCK_ARGS)
    match = re.search(r'case "\$_T" in /\?/\*\).*?esac', append, re.S)
    assert match, "the normalisation fragment is not in _append_block's output"

    script = f'_T="/c/Users/someone/bin/tool"\n{match.group(0)}\nprintf %s "$_T"\n'
    result = subprocess.run([sh, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    # LOWERCASE `c:`, and that is correct. The expansion is pure parameter
    # substitution -- it relocates the drive letter, it does not upcase it,
    # and Windows drive letters are case-insensitive so `c:/` resolves
    # identically to `C:/` for the native python.exe this exists to feed.
    # Asserted explicitly because BOTH emitters' comments say "-> C:/Users/..."
    # (corrected 2026-08-31): a reader who trusts that wording writes exactly
    # this test and watches it fail on a fix that is working.
    assert result.stdout == "c:/Users/someone/bin/tool", (
        f"expansion produced {result.stdout!r}, not the relocated drive form"
    )


def test_append_block_msys_normalisation_leaves_a_windows_path_alone():
    """A path that is already `C:/...` must pass through untouched -- the
    `case` arm matches a SINGLE-character first segment (`/?/`), so `/c/x`
    converts and `C:/x` does not re-enter. Pins that the guard is not merely
    absent-on-Windows but inert there."""
    sh = _sh()
    if not sh:
        import pytest

        pytest.skip("no POSIX sh available on this host")

    append = _append_block(*_APPEND_BLOCK_ARGS)
    match = re.search(r'case "\$_T" in /\?/\*\).*?esac', append, re.S)
    assert match

    script = f'_T="C:/Users/someone/bin/tool"\n{match.group(0)}\nprintf %s "$_T"\n'
    result = subprocess.run([sh, "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "C:/Users/someone/bin/tool"
