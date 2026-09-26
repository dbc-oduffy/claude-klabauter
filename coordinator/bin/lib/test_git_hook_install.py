from __future__ import annotations

import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import git_hook_install as ghi  # noqa: E402
from git_hook_install import _append_block, _ml_get, _shim_body  # noqa: E402


def _make_tool_bindir(tmp_path: Path, tools: dict) -> str:
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


def test_shim_body_missing_interpreter_message_present_in_source():
    body = _shim_body("/fake/coord/bin", "coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"')
    assert "no python3/python/py interpreter found on PATH" in body
    assert "commits are NOT being auto-pushed / annotated by this hook" in body
    assert 'exit 0; }' in body


def test_shim_body_missing_interpreter_blocks_loudly_at_runtime(tmp_path):
    body = _shim_body("/fake/coord/bin", "coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"')
    hook = tmp_path / "post-commit"
    hook.write_text(_with_unresolvable_interpreter(body), encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = _no_python_path(tmp_path)
    result = subprocess.run([_sh(), str(hook)], capture_output=True, text=True, env=env)

    assert result.returncode == 0
    assert "WARNING" in result.stderr
    assert "no python3/python/py interpreter found on PATH" in result.stderr


def test_shim_body_missing_interpreter_and_missing_script_read_the_same_shape():
    body = _shim_body("/fake/coord/bin", "coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"')
    assert body.count("[coordinator] WARNING: hook installed but") == 2
    assert body.count("commits are NOT being auto-pushed / annotated by this hook") == 2


# _HOOK_GEN_STAMP <-> emitted body shape coupling (AC3, plan
# rung (or drops one, or reorders a line) without `_HOOK_GEN_STAMP` in
# _EXPECTED_BODY_SHAPE_CHECKSUM here to match.
# THE BAKED INTERPRETER PATH IS NORMALIZED OUT BEFORE HASHING (2026-08-25, gen

_EXPECTED_BODY_SHAPE_CHECKSUM = "c9e2d335b405ad795a7cb2623addf3c2d868037a66f3cc14318f17677d84e5b4"

_BAKED_PY_PLACEHOLDER = "<BAKED-INTERPRETER>"


def _normalize_baked_py(body: str) -> str:
    return re.sub(
        r'^(_PY=")[^"]*(")$',
        r"\1" + _BAKED_PY_PLACEHOLDER + r"\2",
        body,
        flags=re.M,
    )


def test_hook_gen_stamp_bump_is_required_for_shape_changes(monkeypatch):
    monkeypatch.setattr(ghi, "_resolve_claude_klabauter_bin_sh", lambda bin_dir, script_name: None)
    monkeypatch.setattr(ghi, "_resolve_klabauter_bin_sh", lambda script_name: None)
    body = _shim_body("/fake/coord/bin", "coordinator-auto-push", 'exec "$_PY" "$SCRIPT" "$@"')
    checksum = hashlib.sha256(_normalize_baked_py(body).encode("utf-8")).hexdigest()
    assert checksum == _EXPECTED_BODY_SHAPE_CHECKSUM, (
        f"_shim_body's emitted body shape changed (new checksum {checksum}) without a "
        "matching bump of _HOOK_GEN_STAMP in coordinator/bin/lib/git_hook_install.py. "
        "Fix: bump _HOOK_GEN_STAMP there, then update _EXPECTED_BODY_SHAPE_CHECKSUM in "
        "this test (coordinator/bin/lib/test_git_hook_install.py) to the new checksum."
    )
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

        walk_uses = [line for line in body.split("\n") if "$(_py_resolve)" in line]
        assert len(walk_uses) == 1, f"{script_name}: expected one guarded walk use, got {walk_uses}"
        assert walk_uses[0].lstrip().startswith("[ -x "), (
            f"{script_name}: the walk is invoked unconditionally: {walk_uses[0]!r}"
        )


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
    env["COORDINATOR_SETTINGS_HOME"] = (tmp_path / "no-such-settings-home").as_posix()
    result = subprocess.run([_sh(), str(hook)], capture_output=True, text=True, env=env)

    assert result.returncode == 0
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


import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.mark.skipif(os.name != "nt", reason="WinError 193 exec defect is Windows-only")
def test_ml_get_resolves_via_cmd_twin_on_windows(tmp_path):
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
    unlaunchable = tmp_path

    result = _ml_get(str(unlaunchable), "some.key")

    assert result is None
    captured = capsys.readouterr()
    assert "could not execute machine-local resolver" in captured.err


def test_container_registry_keys_are_not_heal_targets(monkeypatch, tmp_path):
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


def _make_worktree(tmp_path: Path, name: str = "wt") -> Path:
    import pytest

    git = shutil.which("git")
    if not git:
        pytest.skip("no git resolvable on PATH in this environment")
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([git, "init", "-q", str(main_repo)], check=True, env=env, creationflags=no_window)
    (main_repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run([git, "-C", str(main_repo), "add", "f.txt"], check=True, env=env, creationflags=no_window)
    subprocess.run(
        [git, "-C", str(main_repo), "commit", "-q", "-m", "init"], check=True, env=env, creationflags=no_window
    )
    wt_root = tmp_path / name
    subprocess.run(
        [git, "-C", str(main_repo), "worktree", "add", str(wt_root), "-b", name],
        check=True,
        env=env,
        creationflags=no_window,
    )
    return wt_root


def test_resolve_git_hooks_dir_follows_worktree_gitfile_indirection(tmp_path):
    wt_root = _make_worktree(tmp_path)

    resolved = ghi._resolve_git_hooks_dir(str(wt_root))

    assert resolved is not None
    assert os.path.isdir(resolved), f"resolved git dir does not exist: {resolved}"
    assert os.path.normcase(os.path.normpath(resolved)) == os.path.normcase(
        os.path.normpath(str(tmp_path / "main" / ".git"))
    )


def test_classify_target_admits_a_worktree_checkout(tmp_path):
    wt_root = _make_worktree(tmp_path)
    (wt_root / "CLAUDE.md").write_text("x", encoding="utf-8")

    assert ghi._classify_target(str(wt_root)) == "worktree"


def test_ensure_hook_installs_into_worktree_common_dir(tmp_path, monkeypatch):
    wt_root = _make_worktree(tmp_path)
    coord_bin = tmp_path / "bin"
    coord_bin.mkdir()
    (coord_bin / "coordinator-prepare-commit-msg").write_text("x", encoding="utf-8")
    monkeypatch.setattr(ghi, "_resolve_coord_bin", lambda bin_dir, name: str(coord_bin))

    rc = ghi.ensure_prepare_commit_msg_hook(str(coord_bin), root=str(wt_root))

    assert rc == 0
    installed = tmp_path / "main" / ".git" / "hooks" / "prepare-commit-msg"
    assert installed.is_file(), "hook was not written into the common gitdir"
    never_written = wt_root / ".git" / "hooks" / "prepare-commit-msg"
    assert not never_written.exists(), (
        "'.git' is a FILE for a worktree, not a directory — this path can "
        "never legitimately exist"
    )


def test_ensure_hooks_fleet_one_bad_repo_does_not_abort_the_rest(tmp_path, monkeypatch, capsys):
    good_root = tmp_path / "zzz-good"
    good_root.mkdir()
    (good_root / ".git").mkdir()
    (good_root / "CLAUDE.md").write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        ghi,
        "_registry_repo_roots",
        lambda bin_dir: [
            ("repos.aaa_bad", "/definitely/not/a/real/path"),
            ("repos.zzz_good", str(good_root)),
        ],
    )
    monkeypatch.setattr(ghi, "_classify_target", lambda root: (
        "worktree" if root == str(good_root) else "worktree"
    ))

    def _fake_ensure(bin_dir, root=None, outcome=None, check_only=False):
        if root == "/definitely/not/a/real/path":
            raise OSError("simulated: unreadable .git")
        if outcome is not None:
            outcome.append("installed-absent")
        return 0

    monkeypatch.setattr(ghi, "ensure_prepare_commit_msg_hook", _fake_ensure)

    rc = ghi.ensure_hooks_fleet(str(tmp_path))

    assert rc == 0
    captured = capsys.readouterr()
    assert "repos.zzz_good prepare-commit-msg: installed-absent" in captured.err, (
        "the good repo sorted AFTER the bad one must still be healed"
    )
    assert "unexpected error" in captured.err
    assert "repos.aaa_bad" in captured.err


def test_ensure_hooks_fleet_default_never_fails_even_when_owned_hook_missing(
    tmp_path, monkeypatch
):
    owned_root = tmp_path / "owned"
    owned_root.mkdir()
    (owned_root / ".git").mkdir()
    (owned_root / "CLAUDE.md").write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        ghi, "_registry_repo_roots", lambda bin_dir: [("repos.owned", str(owned_root))]
    )
    monkeypatch.setattr(ghi, "_classify_target", lambda root: "worktree")

    def _fake_ensure_no_write(bin_dir, root=None, outcome=None, check_only=False):
        if outcome is not None:
            outcome.append("installed-absent")
        return 0

    monkeypatch.setattr(ghi, "ensure_prepare_commit_msg_hook", _fake_ensure_no_write)

    rc = ghi.ensure_hooks_fleet(str(tmp_path))

    assert rc == 0
    assert not (owned_root / ".git" / "hooks" / "prepare-commit-msg").exists()


def test_ensure_hooks_fleet_strict_fails_when_owned_hook_missing(tmp_path, monkeypatch, capsys):
    owned_root = tmp_path / "owned"
    owned_root.mkdir()
    (owned_root / ".git").mkdir()
    (owned_root / "CLAUDE.md").write_text("x", encoding="utf-8")

    monkeypatch.setattr(
        ghi, "_registry_repo_roots", lambda bin_dir: [("repos.owned", str(owned_root))]
    )
    monkeypatch.setattr(ghi, "_classify_target", lambda root: "worktree")

    def _fake_ensure_no_write(bin_dir, root=None, outcome=None, check_only=False):
        if outcome is not None:
            outcome.append("installed-absent")
        return 0

    monkeypatch.setattr(ghi, "ensure_prepare_commit_msg_hook", _fake_ensure_no_write)

    rc = ghi.ensure_hooks_fleet(str(tmp_path), strict=True)

    assert rc == 1
    captured = capsys.readouterr()
    assert "repos.owned" in captured.err
    assert "not present/executable after install" in captured.err


def test_ensure_hooks_fleet_strict_stays_clean_when_hook_actually_lands(
    tmp_path, monkeypatch
):
    owned_root = tmp_path / "owned"
    owned_root.mkdir()
    (owned_root / ".git" / "hooks").mkdir(parents=True)
    (owned_root / "CLAUDE.md").write_text("x", encoding="utf-8")
    hook_path = owned_root / ".git" / "hooks" / "prepare-commit-msg"
    hook_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook_path.chmod(0o755)

    monkeypatch.setattr(
        ghi, "_registry_repo_roots", lambda bin_dir: [("repos.owned", str(owned_root))]
    )
    monkeypatch.setattr(ghi, "_classify_target", lambda root: "worktree")

    def _fake_ensure_real_write(bin_dir, root=None, outcome=None, check_only=False):
        if outcome is not None:
            outcome.append("already-current")
        return 0

    monkeypatch.setattr(ghi, "ensure_prepare_commit_msg_hook", _fake_ensure_real_write)

    rc = ghi.ensure_hooks_fleet(str(tmp_path), strict=True)

    assert rc == 0


def test_ensure_hooks_fleet_strict_ignores_mirror_absence(tmp_path, monkeypatch):
    mirror_root = tmp_path / "mirror"
    mirror_root.mkdir()
    (mirror_root / ".git").mkdir()

    monkeypatch.setattr(
        ghi, "_registry_repo_roots", lambda bin_dir: [("repos.mirror", str(mirror_root))]
    )

    rc = ghi.ensure_hooks_fleet(str(tmp_path), strict=True)

    assert rc == 0


# The no-session gate is GENERATED from the ladder, never hand-copied.

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
    assert block.index('_have_py() {') < block.index('_have_py "')


def test_append_block_resolution_rungs_never_use_bare_dash_f():
    block = _append_block(*_APPEND_BLOCK_ARGS)
    assert '[ -f "$_T" ]' not in block
    assert '[ ! -f "$_T" ]' not in block


def test_append_block_runs_an_installed_exe_forwarder_directly(tmp_path):
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


def test_append_block_emits_no_shell_errors_when_nothing_resolves(tmp_path, monkeypatch):
    monkeypatch.setattr(ghi, "_resolve_claude_klabauter_bin_sh", lambda bin_dir, script_name: None)
    monkeypatch.setattr(ghi, "_resolve_klabauter_bin_sh", lambda script_name: None)
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
    assert "not found" in result.stderr
    for line in result.stderr.splitlines():
        assert "[coordinator] WARNING" in line, f"unexpected shell error: {line!r}"


def test_both_hook_emitters_normalise_msys_drive_letters():
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
    # A LOWERCASE drive letter, and that is correct. The expansion is pure
    # form with an UPPERCASE drive letter (corrected 2026-08-31): a reader who
    assert result.stdout == "c:/Users/someone/bin/tool", (
        f"expansion produced {result.stdout!r}, not the relocated drive form"
    )


def test_append_block_msys_normalisation_leaves_a_windows_path_alone():
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


def test_shim_body_execs_a_posix_native_forwarder_instead_of_the_interpreter():
    body = _shim_body(
        "/fake/coord/bin",
        "coordinator-prepare-commit-msg",
        'exec "$_PY" "$SCRIPT" "$@"',
    )
    assert "_native() {" in body, "no native-image probe at all — gen 12 regressed"
    assert body.index("_native() {") < body.index('_native "$_fwd"')
    assert body.index('_native "$_fwd" && exec') < body.index('SCRIPT=')


def test_append_block_defines_the_native_probe_it_calls():
    block = _append_block(*_APPEND_BLOCK_ARGS)
    assert "_native() {" in block, (
        "_append_block calls _native without emitting its definition. The block "
        "is appended into a foreign hook, so nothing above it is ours to borrow."
    )
    assert block.index("_native() {") < block.index('_native "')


def test_native_probe_costs_no_subprocess():
    body = _shim_body(
        "/fake/coord/bin",
        "coordinator-prepare-commit-msg",
        'exec "$_PY" "$SCRIPT" "$@"',
    )
    probe = re.search(r"_native\(\) \{.*?\}\n", body, re.S)
    assert probe
    for spawner in ("$(", "`", "file ", "head ", "od ", "xxd ", "grep "):
        assert spawner not in probe.group(0), f"native probe spawns via {spawner!r}"


def test_native_probe_leaves_a_genuine_python_script_to_the_interpreter(tmp_path):
    sh = _sh()
    if not sh:
        import pytest

        pytest.skip("no POSIX sh available on this host")

    body = _shim_body(
        "/fake/coord/bin",
        "coordinator-prepare-commit-msg",
        'exec "$_PY" "$SCRIPT" "$@"',
    )
    probe = re.search(r"_native\(\) \{.*?\}\n", body, re.S).group(0)

    script = tmp_path / "cli"
    script.write_text("#!/usr/bin/env python3\nprint(1)\n", encoding="utf-8")
    script.chmod(0o755)
    checks = f'{probe}_native "{script.as_posix()}" && printf native || printf script\n'
    result = subprocess.run([sh, "-c", checks], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "script"


def test_native_probe_misclassifies_an_executable_shebangless_non_native_file(tmp_path):
    """PINS THE ACCEPTED RESIDUAL (code-review finding, 2026-09-02) -- this is
    NOT the behaviour we want, it is the behaviour we have and have chosen not
    to change this pass. See the "KNOWN RESIDUAL, ACCEPTED IN WRITING" comment
    above `_NATIVE_PROBE_DEF`'s own definition for the full reasoning.

    The probe's real discriminator is "does this open with `#!`"; `[ -x ]` is
    a pre-filter resting on an invariant enforced elsewhere (the install
    chain strips the exec bit from installed `.py` sources). A file that is
    executable for any OTHER reason and carries no `#!` -- neither a genuine
    script nor a genuine Mach-O/ELF image -- is classified `_native` anyway.
    If this test ever starts asserting `"script"`, the probe grew a real
    positive discriminator and this docstring (and the module comment it
    cites) are stale and should be deleted along with it -- that would be
    fixing the residual, not breaking the test.
    """
    sh = _sh()
    if not sh:
        import pytest

        pytest.skip("no POSIX sh available on this host")

    body = _shim_body(
        "/fake/coord/bin",
        "coordinator-prepare-commit-msg",
        'exec "$_PY" "$SCRIPT" "$@"',
    )
    probe = re.search(r"_native\(\) \{.*?\}\n", body, re.S).group(0)

    script = tmp_path / "not-a-real-native-image"
    script.write_text("just some text with no shebang line\n", encoding="utf-8")
    script.chmod(0o755)
    checks = f'{probe}_native "{script.as_posix()}" && printf native || printf script\n'
    result = subprocess.run([sh, "-c", checks], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "native", (
        "the probe stopped misclassifying an executable, shebang-less, "
        "non-magic file as native -- update the accepted-residual comment "
        "above _NATIVE_PROBE_DEF and this docstring, don't just adjust the "
        "assertion"
    )


# verified by asking the PRODUCER's own question -- did I write the image, is
# test that asked a CONSUMER's question
# The fixture below is therefore derived from the PRODUCER
# (`door_install.NATIVE_IMAGE_MAGIC`) and branched on the CURRENT platform,
# `_NATIVE_PROBE_DEF` and this must FAIL. Without that control the pair test

_INCIDENT_STRINGS = ("Non-UTF-8", "SyntaxError", "can't open file")


def _door_native_magic():
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from coordinator_core.install.door_install import NATIVE_IMAGE_MAGIC

    return NATIVE_IMAGE_MAGIC


def _install_native_image_as_this_platform_does(tmp_path, script_name):
    """Write the settings-home entry the cutover produces ON THIS PLATFORM.

    POSIX gets the bare name (no extension to signal anything); Windows gets
    the `.exe` sibling. The bytes open with a real magic value from
    `door_install.NATIVE_IMAGE_MAGIC`, so the fixture cannot drift from the
    producer's own definition of a native image.
    """
    magic = _door_native_magic()
    bin_dir = tmp_path / "settings-home" / "bin"
    bin_dir.mkdir(parents=True)
    name = script_name + (".exe" if os.name == "nt" else "")
    image = bin_dir / name
    donor = Path("/usr/bin/true")
    if os.name != "nt" and donor.exists():
        shutil.copy(donor, image)
        assert image.open("rb").read(8).startswith(magic), (
            "the donor binary does not match door_install.NATIVE_IMAGE_MAGIC — "
            "the fixture is no longer derived from the producer"
        )
    else:
        image.write_bytes(magic[0] + b"\n")
    image.chmod(0o755)
    return tmp_path / "settings-home", image


def _run_both_emitted_hooks(tmp_path, settings_home):
    bodies = {
        "shim": _shim_body(
            "/fake/coord/bin",
            "coordinator-prepare-commit-msg",
            'exec "$_PY" "$SCRIPT" "$@"',
        ),
        "append": "#!/bin/sh\n" + _append_block(*_APPEND_BLOCK_ARGS) + " || true\n",
    }
    env = dict(os.environ)
    env["COORDINATOR_SETTINGS_HOME"] = settings_home.as_posix()
    env["COORDINATOR_SESSION_ID"] = "pair-test"
    results = {}
    for label, body in bodies.items():
        hook = tmp_path / f"hook-{label}"
        hook.write_text(_with_unresolvable_interpreter(body), encoding="utf-8")
        results[label] = subprocess.run(
            [_sh(), str(hook)], capture_output=True, text=True, env=env, errors="replace"
        )
    return results


def test_a_cut_over_bin_survives_both_hook_emitters(tmp_path):
    if not _sh():
        import pytest

        pytest.skip("no POSIX sh available on this host")

    settings_home, _ = _install_native_image_as_this_platform_does(
        tmp_path, "coordinator-prepare-commit-msg"
    )
    for label, result in _run_both_emitted_hooks(tmp_path, settings_home).items():
        assert result.returncode == 0, f"{label}: rc={result.returncode} {result.stderr!r}"
        combined = result.stdout + result.stderr
        for token in _INCIDENT_STRINGS:
            assert token not in combined, (
                f"{label} emitter fed the native image to an interpreter "
                f"({token!r} in output) — this is the 2026-09-02 defect"
            )
        assert "WARNING" not in result.stderr, (
            f"{label}: the interpreter chain was reached, so the image was not "
            f"run directly; stderr={result.stderr!r}"
        )


def test_pair_fixture_goes_red_without_the_native_probe(tmp_path, monkeypatch):
    """THE NEGATIVE CONTROL — the reason the pair test has teeth.

    Remove the native probe and the same fixture must reproduce the incident.
    If this test ever passes, the pair test above has stopped discriminating
    and is green on a body that never learned the POSIX half.
    """
    if not _sh() or os.name == "nt":
        import pytest

        pytest.skip("POSIX-only: the control reproduces the POSIX-half defect")

    monkeypatch.setattr(ghi, "_NATIVE_PROBE_DEF", "")
    monkeypatch.setattr(ghi, "_resolve_claude_klabauter_bin_sh", lambda bin_dir, script_name: None)
    monkeypatch.setattr(ghi, "_resolve_klabauter_bin_sh", lambda script_name: None)
    settings_home, _ = _install_native_image_as_this_platform_does(
        tmp_path, "coordinator-prepare-commit-msg"
    )
    results = _run_both_emitted_hooks(tmp_path, settings_home)

    reproduced = any(
        r.returncode != 0
        or any(t in (r.stdout + r.stderr) for t in _INCIDENT_STRINGS)
        or "WARNING" in r.stderr
        for r in results.values()
    )
    assert reproduced, (
        "stripping _NATIVE_PROBE_DEF did NOT break the hook, so the pair test "
        "above proves nothing — the probe is no longer what makes it pass"
    )


def _real_clone(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(bare), str(clone)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(clone), "config", "user.email", "t@example.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "config", "user.name", "t"],
        check=True, capture_output=True,
    )
    (clone / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(clone), "add", "f.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(clone), "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    return clone


def test_ensure_hook_skipped_no_root_is_loud_on_stderr(tmp_path, monkeypatch, capsys):
    """state/bug-backlog/2026-08-25-hook-emitters-exit-0-having-installed-no-
    *.yaml: `_ensure_hook`'s "skipped-no-root" branch used to return rc 0
    with EMPTY stderr when the checked resolver came back UNRESOLVED --
    indistinguishable from a hook that installed successfully. Reproduced
    against a REAL clone (see `_real_clone`'s own docstring for why a
    `git init` scratch repo does not exercise the same gap), with the
    checked resolver's own verdict forced to UNRESOLVED -- exactly the
    condition `_git_root()` degrades on, per its own docstring.

    Pre-fix: this test fails with an EMPTY captured.err (the exact silent
    shape the backlog entry reports). Falsified against the unfixed branch
    before this fix landed.
    """
    clone = _real_clone(tmp_path)
    monkeypatch.chdir(clone)

    import repo_identity

    monkeypatch.setattr(
        repo_identity,
        "resolve_checked_repo_root",
        lambda explicit_root=None: (
            None,
            {
                "verdict": "UNRESOLVED",
                "session_root": None,
                "resolved_root": None,
                "sid": None,
                "message": "repo-identity (checked resolver): test-forced UNRESOLVED",
            },
        ),
    )

    outcome: list = []
    rc = ghi._ensure_hook(
        bin_dir=str(clone),
        hook_name="prepare-commit-msg",
        script_name="coordinator-prepare-commit-msg",
        marker="coordinator-prepare-commit-msg",
        fresh_body="#!/bin/sh\necho fresh\n",
        append_block="\n# === test ===\necho appended\n",
        header="test header",
        outcome=outcome,
    )

    assert rc == 0, "installer must never fail loudly enough to block a commit"
    assert outcome == ["skipped-no-root"]
    assert not (clone / ".git" / "hooks" / "prepare-commit-msg").exists()

    captured = capsys.readouterr()
    assert "UNRESOLVED" in captured.err, (
        "skipped-no-root must reach stderr loudly -- an empty stderr here "
        "reproduces the exact bug this test guards against"
    )
    assert "WARNING" in captured.err
