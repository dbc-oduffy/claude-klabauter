"""
test_app_session.py — coverage for chunk C4 of
docs/plans/2026-08-15-app-session-launch-census-teardown-ops.md.

Covers AC3 through AC9. AC1/AC2 (the mapping reader and the zero-spawn root
resolver) already have their own coverage in
coordinator_core/tests/test_read_local_md_mapping.py — not duplicated here.

Fixture discipline (Anti-scope): every fixture that stands up a "consuming
repo" uses pytest's `tmp_path`, which pytest places under the OS temp root —
a directory tree with no relationship whatsoever to this package's own
on-disk location (`coordinator_core/ops/`). A fixture whose consuming-repo
root and code root are the same directory cannot see the anchoring bug this
whole plan exists to prevent; `tmp_path` structurally rules that out for
every test below. Electron fixtures are fabricated `node_modules/electron/`
trees under `tmp_path`, never a real install.

Git-common-dir handling: `app_session._handle_dir` resolves the handle store
via `coordinator_core.lifecycle.git_common_dir`, which resolves by a
pure-Python `.git` walk and does NOT spawn (guarded below by
`test_handle_dir_resolution_spawns_no_subprocess`). Tests here monkeypatch
that function directly (see `_patch_git_common_dir`) rather than standing up real git
repos/worktrees — this keeps the suite spawn-free and fast while still
proving `_handle_dir` COMPOSES whatever `git_common_dir` returns rather than
naively joining `<root>/.git` (the trap a `.git`-file worktree falls into).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import psutil
import pytest

from coordinator_core.ops import _app_session_runtime as runtime_mod
from coordinator_core.ops import _git_root_util
from coordinator_core.ops import _registry_map
from coordinator_core.ops import app_session
from coordinator_core.ops._app_session_runtime import (
    RUNTIME_RESOLVERS,
    resolve_runtime,
)
from coordinator_core.resolve_validation_cmd import (
    metachar_warn as canonical_metachar_warn,
    redact_for_diag as canonical_redact_for_diag,
)


# --- shared fixtures -------------------------------------------------------


@pytest.fixture()
def consuming_repo(tmp_path, monkeypatch):
    """A fabricated 'consuming repo' tree, unrelated to this package's own
    location on disk, with a fake git-common-dir wired via monkeypatch so
    handle storage works without shelling out to git.
    """
    repo = tmp_path / "consuming-repo"
    repo.mkdir()
    common_dir = tmp_path / "consuming-repo" / ".git"
    common_dir.mkdir()

    def _fake_git_common_dir(root):
        assert Path(root) == repo, "git_common_dir must be called with the CONSUMING repo root"
        return common_dir

    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", _fake_git_common_dir)
    return repo


def _write_local_md(repo: Path, body: str) -> None:
    (repo / "coordinator.local.md").write_text(f"---\n{body}\n---\n", encoding="utf-8")


def _fail_if_called(*_a, **_kw):
    raise AssertionError("no spawn/download seam should have been invoked")


# =====================================================================
# AC3 — unrecognised / absent runtime kind degrades to plain argv, with
# NO resolution step attempted.
# =====================================================================


def test_ac3_unrecognised_kind_degrades_to_plain_argv(consuming_repo):
    config = {"runtime": "bogus-kind-nobody-registered", "command": "pnpm dev"}
    result = resolve_runtime(config["runtime"], config, str(consuming_repo))
    assert result.ok is True
    assert result.argv == ["pnpm", "dev"]


def test_ac3_absent_kind_degrades_to_plain_argv(consuming_repo):
    config = {"command": "pnpm dev"}
    result = resolve_runtime(None, config, str(consuming_repo))
    assert result.ok is True
    assert result.argv == ["pnpm", "dev"]


def test_ac3_unrecognised_kind_never_reaches_electron_resolver(consuming_repo, monkeypatch):
    """'no resolution step attempted' — assert directly, not by inference:
    monkeypatch the electron resolver to blow up if it is ever called, then
    resolve an unrecognised kind and confirm it never fires.
    """
    monkeypatch.setitem(RUNTIME_RESOLVERS, "electron", _fail_if_called)
    config = {"runtime": "server", "command": "pnpm dev"}
    result = resolve_runtime("server", config, str(consuming_repo))
    assert result.ok is True
    assert result.argv == ["pnpm", "dev"]


# =====================================================================
# AC4 — electron resolver: ELECTRON_OVERRIDE_DIST_PATH wins over path.txt;
# a missing binary reports-and-stops with NO download attempted.
# =====================================================================


def test_ac4_env_override_wins_over_path_txt(consuming_repo, monkeypatch, tmp_path):
    # Fabricate a node_modules/electron tree whose path.txt points at ONE
    # binary, plus a completely separate override binary — if the override
    # doesn't win, resolution would pick the path.txt binary instead.
    electron_pkg = consuming_repo / "node_modules" / "electron"
    electron_pkg.mkdir(parents=True)
    (electron_pkg / "path.txt").write_text("dist/electron.exe", encoding="utf-8")
    (electron_pkg / "dist").mkdir()
    (electron_pkg / "dist" / "electron.exe").write_bytes(b"not-a-real-binary")

    override_binary = tmp_path / "override" / "electron-override.exe"
    override_binary.parent.mkdir()
    override_binary.write_bytes(b"override-binary")

    monkeypatch.setenv("ELECTRON_OVERRIDE_DIST_PATH", str(override_binary))
    result = resolve_runtime("electron", {"runtime": "electron"}, str(consuming_repo))
    assert result.ok is True
    assert result.binary == str(override_binary)
    assert result.argv[0] == str(override_binary)


def test_ac4_missing_binary_reports_and_stops_no_download(consuming_repo, monkeypatch):
    # No node_modules/electron tree at all — the missing-binary path.
    monkeypatch.delenv("ELECTRON_OVERRIDE_DIST_PATH", raising=False)

    # If resolution ever attempted a download, it would have to spawn a
    # process or open a network connection. Fail the test hard if either
    # seam is touched.
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    monkeypatch.setattr(subprocess, "Popen", _fail_if_called)
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _fail_if_called)

    result = resolve_runtime("electron", {"runtime": "electron"}, str(consuming_repo))
    assert result.ok is False
    assert "electron not installed" in result.error


def test_ac4_missing_binary_file_reports_and_stops_no_download(consuming_repo, monkeypatch):
    """path.txt present but the binary it points at does not exist."""
    electron_pkg = consuming_repo / "node_modules" / "electron"
    electron_pkg.mkdir(parents=True)
    (electron_pkg / "path.txt").write_text("dist/does-not-exist.exe", encoding="utf-8")
    monkeypatch.delenv("ELECTRON_OVERRIDE_DIST_PATH", raising=False)
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    monkeypatch.setattr(subprocess, "Popen", _fail_if_called)

    result = resolve_runtime("electron", {"runtime": "electron"}, str(consuming_repo))
    assert result.ok is False
    assert "electron not installed" in result.error


# =====================================================================
# AC5 — census: recycled-PID stranger is NOT reported live; an orphan
# handle (no in-memory session, handle file only) IS found.
# =====================================================================


def _self_start_epoch() -> int:
    return int(psutil.Process(os.getpid()).create_time())


def test_ac5_recycled_pid_stranger_not_reported_live(consuming_repo):
    """A stranger process occupies the recorded PID (we use THIS test
    process's own real, live PID) but the persisted start_epoch does not
    match its actual birth instant — the recycled-PID scenario. Census
    must not report it live: this is the whole reason the second identity
    signal exists, and the assertion must not be weakened to pass trivially.
    """
    handle = {
        "key": "desktop",
        "pid": os.getpid(),
        "start_epoch": 1,  # nowhere near this process's real create_time()
        "argv": ["pnpm", "dev"],
        "cwd": str(consuming_repo),
        "launched_at": "2020-01-01T00:00:00Z",
    }
    app_session._write_handle(str(consuming_repo), "desktop", handle)

    result = app_session._census({"key": "desktop", "repo_root": str(consuming_repo)})

    assert result["ok"] is True
    assert result["sessions"][0]["pid"] == os.getpid()
    assert result["sessions"][0]["live"] is False, (
        "a stranger occupying a recycled PID must never be reported live"
    )


def test_ac5_census_finds_orphan_handle_with_no_tracking_session(consuming_repo):
    """No 'launch' was ever called through this op family for this handle —
    it is written directly, simulating a handle left by a session that has
    already exited. Census enumerates from PERSISTED state only, so it must
    still find and correctly re-validate it.
    """
    handle = {
        "key": "orphan",
        "pid": os.getpid(),
        "start_epoch": _self_start_epoch(),
        "argv": ["pnpm", "dev"],
        "cwd": str(consuming_repo),
        "launched_at": "2020-01-01T00:00:00Z",
    }
    app_session._write_handle(str(consuming_repo), "orphan", handle)

    result = app_session._census({"repo_root": str(consuming_repo)})
    assert result["ok"] is True
    keys = {s["key"] for s in result["sessions"]}
    assert "orphan" in keys
    orphan_session = next(s for s in result["sessions"] if s["key"] == "orphan")
    assert orphan_session["live"] is True


# =====================================================================
# AC6 — teardown releases locks even on an already-dead process, and
# never kills by process-name match.
# =====================================================================


def _unused_pid() -> int:
    live_pids = set(psutil.pids())
    candidate = 999_999
    while candidate in live_pids:
        candidate -= 1
    return candidate


def test_ac6_teardown_on_dead_process_still_releases_handle(consuming_repo):
    dead_pid = _unused_pid()
    handle = {
        "key": "desktop",
        "pid": dead_pid,
        "start_epoch": 12345,
        "argv": ["pnpm", "dev"],
        "cwd": str(consuming_repo),
        "launched_at": "2020-01-01T00:00:00Z",
    }
    app_session._write_handle(str(consuming_repo), "desktop", handle)
    assert app_session._handle_path(str(consuming_repo), "desktop").is_file()

    result = app_session._teardown({"key": "desktop", "repo_root": str(consuming_repo)})

    assert result["ok"] is True
    assert result["was_live"] is False
    assert result["reaped"] is False
    assert not app_session._handle_path(str(consuming_repo), "desktop").is_file(), (
        "the handle (lock) must be released even when the process was already gone"
    )


def test_ac6_teardown_never_kills_by_process_name(consuming_repo, monkeypatch):
    """Assert the negative-spec directly: teardown must never enumerate
    processes by name. psutil.process_iter is the only name-matching seam
    available — fail the test hard if teardown ever touches it.
    """
    monkeypatch.setattr(psutil, "process_iter", _fail_if_called)

    dead_pid = _unused_pid()
    handle = {
        "key": "desktop",
        "pid": dead_pid,
        "start_epoch": 12345,
        "argv": ["pnpm", "dev"],
        "cwd": str(consuming_repo),
        "launched_at": "2020-01-01T00:00:00Z",
    }
    app_session._write_handle(str(consuming_repo), "desktop", handle)

    result = app_session._teardown({"key": "desktop", "repo_root": str(consuming_repo)})
    assert result["ok"] is True


# =====================================================================
# AC7 — no second copy of the shared spawn substrate primitives.
# =====================================================================

_TOUCHED_MODULE_FILES = [
    Path(app_session.__file__),
    Path(runtime_mod.__file__),
    Path(_git_root_util.__file__),
]


@pytest.mark.parametrize(
    "primitive_name",
    ["win_safe_shlex_split", "redact_for_diag", "metachar_warn"],
)
def test_ac7_no_second_definition_of_shared_primitive(primitive_name):
    for path in _TOUCHED_MODULE_FILES:
        text = path.read_text(encoding="utf-8")
        assert f"def {primitive_name}(" not in text, (
            f"{path} redefines {primitive_name}() instead of importing the shared one"
        )


def test_ac7_no_hand_rolled_create_no_window():
    """Docstring prose is allowed to name CREATE_NO_WINDOW as the thing NOT
    to hand-roll (see app_session.py's own negative-spec) — what must never
    appear is actual CODE referencing it (an attribute access off
    `subprocess.` or a bare assignment), which would mean a hand-rolled flag
    slipped past no_console_creationflags().
    """
    for path in _TOUCHED_MODULE_FILES:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "CREATE_NO_WINDOW" not in line:
                continue
            # Prose naming it as the thing NOT to hand-roll is allowed
            # (app_session.py's own negative-spec docstring does this);
            # actual code referencing it (e.g. `subprocess.CREATE_NO_WINDOW`,
            # a bare assignment) is not.
            assert "hand-rolled" in line or "hand rolled" in line, (
                f"{path}:{lineno} references CREATE_NO_WINDOW outside of "
                "negative-spec prose — use no_console_creationflags() instead"
            )


def test_ac7_primitives_are_actually_imported_from_canonical_source():
    app_session_text = Path(app_session.__file__).read_text(encoding="utf-8")
    assert "from coordinator_core.resolve_validation_cmd import" in app_session_text
    assert "metachar_warn" in app_session_text
    assert "redact_for_diag" in app_session_text
    assert app_session.metachar_warn is canonical_metachar_warn
    assert app_session.redact_for_diag is canonical_redact_for_diag


# =====================================================================
# AC8 — DP-1 (env rung) and DP-2 (absent-key branch) as single named
# predicates, each documenting its default as a deviation from
# fast_test_cmd, superseded by the peer's forthcoming contract.
# =====================================================================


def test_ac8_dp1_env_rung_default_is_no_env_rung():
    assert app_session._app_session_env_rung("desktop") is None


def test_ac8_dp1_docstring_states_default_and_deviation():
    doc = app_session._app_session_env_rung.__doc__ or ""
    assert "DEFAULT" in doc
    assert "DEVIATION" in doc
    assert "fast_test_cmd" in doc


def test_ac8_dp2_absent_key_returns_structured_not_configured_result():
    result = app_session._absent_key_result("app_session.launch", "desktop", "no entry")
    assert result["ok"] is True
    assert result["configured"] is False
    assert result["op"] == "app_session.launch"
    assert result["key"] == "desktop"
    # Never raises and never a silent SUCCESS in the "did the thing" sense —
    # the structured configured:False distinguishes "nothing to do" from
    # both "it broke" and "it launched".


def test_ac8_dp2_docstring_states_default_and_deviation():
    doc = app_session._absent_key_result.__doc__ or ""
    assert "DEFAULT" in doc
    assert "DEVIATION" in doc
    assert "fast_test_cmd" in doc


def test_ac8_dp2_absent_key_never_raises_via_launch(consuming_repo):
    # No coordinator.local.md at all — an unopted-in repo.
    result = app_session._launch({"key": "desktop", "repo_root": str(consuming_repo)})
    assert result["ok"] is True
    assert result["configured"] is False


# =====================================================================
# AC9 — the three ops are reachable via BOTH registration paths.
# =====================================================================


@pytest.mark.parametrize(
    "op_name",
    ["app_session.launch", "app_session.census", "app_session.teardown"],
)
def test_ac9_registered_in_registry_map(op_name):
    assert _registry_map.OP_MODULE_MAP.get(op_name) == "coordinator_core.ops.app_session"


def test_ac9_registered_in_eager_import_list():
    from coordinator_core import ops as ops_pkg

    module_paths = {m for m, _note in ops_pkg._EAGER_OP_MODULES}
    assert "coordinator_core.ops.app_session" in module_paths


def test_ac9_reachable_via_ipc_registry_after_import():
    import coordinator_core.ops  # noqa: F401 — trigger eager registration
    from coordinator_core.ipc import _REGISTRY

    for op_name in ("app_session.launch", "app_session.census", "app_session.teardown"):
        assert op_name in _REGISTRY, f"{op_name} not reachable via the eager import path"


# =====================================================================
# Worth-it-if-cheap: handle dir resolves via git COMMON dir (composes
# whatever git_common_dir returns), not a naive <root>/.git join — this is
# exactly the trap a `.git`-FILE worktree falls into.
# =====================================================================


def test_handle_dir_composes_git_common_dir_not_naive_join(tmp_path, monkeypatch):
    repo_root = tmp_path / "worktree-consuming-repo"
    repo_root.mkdir()
    # Simulate a linked worktree: the MAIN worktree's real .git directory
    # lives somewhere else entirely, and repo_root/.git would be a FILE in
    # reality — git_common_dir is the seam that already resolves this
    # correctly; here we assert app_session composes ITS return value
    # rather than re-deriving <repo_root>/.git itself.
    main_worktree_git_dir = tmp_path / "main-worktree" / ".git"
    main_worktree_git_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir",
        lambda root: main_worktree_git_dir,
    )

    handle_dir = app_session._handle_dir(str(repo_root))
    assert handle_dir == main_worktree_git_dir / "coordinator-app-sessions"
    assert handle_dir != repo_root / ".git" / "coordinator-app-sessions"


# =====================================================================
# Review-integration fixes (2026-08-15) — the P1/P2/P3 findings applied
# from state/subagent-share/9d00b97b-1373-4aeb-8648-e0875801500c/
# coordinatorcode-reviewer-1b40a1bc.md.
# =====================================================================


def test_metachar_tripwire_fires_on_dp1_env_override_branch(consuming_repo, monkeypatch):
    """DP-1 is dead by default (always returns None) so this branch is only
    exercised by monkeypatching the predicate directly. Regression coverage
    for the finding that `_resolve_argv`'s path called `metachar_warn` but
    the DP-1 `env_override` branch in `_launch` did not — the fix moves the
    tripwire to a single post-convergence call site both branches pass
    through, so it must now fire even when DP-1 supplies the argv.
    """
    _write_local_md(consuming_repo, 'app_session:\n  desktop:\n    command: "pnpm dev"')
    monkeypatch.setattr(app_session, "_app_session_env_rung", lambda key: "sh -c 'a; b'")

    calls = []
    monkeypatch.setattr(
        app_session,
        "metachar_warn",
        lambda s, step, caller="app_session.launch": calls.append(s),
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("P", (), {"pid": 4242})())

    result = app_session._launch({"key": "desktop", "repo_root": str(consuming_repo)})
    assert result["ok"] is True
    assert len(calls) == 1, "metachar_warn must fire exactly once, on the DP-1-supplied argv"
    assert "; " in calls[0]


def test_metachar_tripwire_fires_on_normal_resolve_argv_branch(consuming_repo, monkeypatch):
    """Same tripwire, exercised via the normal (non-DP-1) path — the
    post-convergence call site must still fire when DP-1 is absent (the
    default, everyday case).
    """
    _write_local_md(consuming_repo, "app_session:\n  desktop:\n    command: \"pnpm dev; echo hi\"")

    calls = []
    monkeypatch.setattr(
        app_session,
        "metachar_warn",
        lambda s, step, caller="app_session.launch": calls.append(s),
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("P", (), {"pid": 4243})())

    result = app_session._launch({"key": "desktop", "repo_root": str(consuming_repo)})
    assert result["ok"] is True
    assert len(calls) == 1
    assert "; " in calls[0]


def test_argv_redacted_is_whole_command_not_per_token(consuming_repo, monkeypatch):
    """A secret embedded as a single argv token must not survive per-token
    redaction unredacted — `redact_for_diag` only truncates past 60 chars,
    and individual tokens are almost never that long. Whole-command
    redaction is the fix; assert the secret token does not appear verbatim
    in the returned `argv_redacted` field.
    """
    secret_token = "--token=" + ("s3cr3t" * 12)  # long, would truncate whole-command
    _write_local_md(consuming_repo, f'app_session:\n  desktop:\n    command: "pnpm dev {secret_token}"')

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: type("P", (), {"pid": 4244})())

    result = app_session._launch({"key": "desktop", "repo_root": str(consuming_repo)})
    assert result["ok"] is True
    assert secret_token not in result["argv_redacted"], (
        "per-token redaction would leave a short-lived secret token unredacted; "
        "whole-command redaction must truncate across the joined argv"
    )


def test_electron_binary_path_invalid_utf8_never_raises(consuming_repo, monkeypatch):
    """A path.txt containing invalid UTF-8 raises UnicodeDecodeError (a
    ValueError, not an OSError) from Path.read_text — resolve_runtime's own
    contract is 'never raises'. Regression for the bare `except OSError`
    that let this escape uncaught.
    """
    electron_pkg = consuming_repo / "node_modules" / "electron"
    electron_pkg.mkdir(parents=True)
    (electron_pkg / "path.txt").write_bytes(b"\xff\xfe\x00invalid-utf8")
    monkeypatch.delenv("ELECTRON_OVERRIDE_DIST_PATH", raising=False)

    result = resolve_runtime("electron", {"runtime": "electron"}, str(consuming_repo))
    assert result.ok is False
    assert "electron not installed" in result.error


def test_teardown_toctou_recheck_skips_terminate_on_recycled_pid(consuming_repo, monkeypatch):
    """Regression for the TOCTOU window between `_handle_is_live`'s
    validation and `.terminate()`: even when the handle passes the initial
    liveness check, a recycled PID whose create_time no longer matches the
    persisted start_epoch at the re-check immediately before `.terminate()`
    must not be signalled.
    """
    handle = {
        "key": "desktop",
        "pid": os.getpid(),
        "start_epoch": _self_start_epoch(),
        "argv": ["pnpm", "dev"],
        "cwd": str(consuming_repo),
        "launched_at": "2020-01-01T00:00:00Z",
    }
    app_session._write_handle(str(consuming_repo), "desktop", handle)

    monkeypatch.setattr(app_session, "_handle_is_live", lambda h: True)

    terminate_calls = []

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 999999999  # does not match the persisted start_epoch

        def terminate(self):
            terminate_calls.append(self.pid)

    monkeypatch.setattr(psutil, "Process", lambda pid: _FakeProc(pid))

    result = app_session._teardown({"key": "desktop", "repo_root": str(consuming_repo)})
    assert result["ok"] is True
    assert terminate_calls == [], (
        "a recycled PID whose create_time no longer matches the handle's "
        "start_epoch at the pre-terminate re-check must never be signalled"
    )


def test_handle_dir_resolution_spawns_no_subprocess(tmp_path, monkeypatch):
    """Hard constraint 7 (zero-spawn) must hold THROUGH the handle-store
    resolution, not merely up to it.

    `git_root_zero_spawn` being spawn-free buys nothing if `_handle_dir`'s
    `lifecycle.git_common_dir` call shells out one level deeper — every
    launch/census/teardown would pay a `git rev-parse` on a box whose spawn
    cost is a standing P0. A code review read exactly that from the (then
    stale) docstrings and filed it as debt; this test is the artifact that
    keeps the claim honest instead of the prose.

    Deliberately does NOT monkeypatch `git_common_dir` — the point is to
    exercise the real resolver against a real `.git` directory.
    """
    from coordinator_core.git import repo_root as repo_root_seam
    from coordinator_core.lifecycle import git_common_dir

    consuming_repo = tmp_path / "consuming-repo"
    (consuming_repo / ".git").mkdir(parents=True)

    git_common_dir.cache_clear()
    repo_root_seam._memo.clear()

    spawns = []

    def _forbidden(*args, **kwargs):
        spawns.append(args[0] if args else kwargs.get("args"))
        raise AssertionError(f"unexpected subprocess spawn: {spawns[-1]}")

    monkeypatch.setattr(repo_root_seam.subprocess, "run", _forbidden)

    resolved = app_session._handle_dir(str(consuming_repo))

    assert resolved == consuming_repo / ".git" / app_session._HANDLE_DIRNAME
    assert spawns == []
