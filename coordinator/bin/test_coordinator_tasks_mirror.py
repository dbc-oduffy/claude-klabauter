"""test_coordinator_tasks_mirror.py — self-contained test suite for coordinator-tasks-mirror.py.

Exercises the module's write/update
functions directly (no subprocess, no git-repo fixture required — cmd_init/cmd_update
take repo_root as an explicit parameter, decoupled from git rev-parse resolution) plus
the CLI's usage/error paths via subprocess for the argument-parsing surface.

Contract under test:
    CLI: coordinator/bin/coordinator-tasks-mirror.py
    Spec backlink: DoE-claude:pln-ceremony-as-pipeline-2-land-th-aa5ace § C1.2

Run with: python3 -m pytest coordinator/bin/test_coordinator_tasks_mirror.py

Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (Plan C, Wave E3-d)
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys

import pytest
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT = os.path.join(SCRIPT_DIR, "coordinator-tasks-mirror.py")


PASS = 0
FAIL = 0


def _pass(label: str) -> None:
    global PASS
    print(f"  PASS: {label}")
    PASS += 1


def _fail(label: str, detail: str = "") -> None:
    """Fail the enclosing test.

    Negative-spec: this MUST raise. It previously only printed and bumped a
    module-global counter that nothing ever asserted on, which made every
    check in this file decorative -- 40 checks that could not fail a run.
    Do not "restore" the counting-only shape.
    """
    global FAIL
    print(f"  FAIL: {label}")
    if detail:
        print(f"    {detail}")
    FAIL += 1
    pytest.fail(f"{label}: {detail}" if detail else label, pytrace=False)


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("coordinator_tasks_mirror_under_test", SUBJECT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_mirror_write(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- mirror-write: yaml lands at state/tasks/<sid>/ with correct items")
    repo = os.path.join(tmp_root, "write")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-abc123"
    titles = [
        "[re-validate after restart] MCP server starts",
        "[completeness] Claude Code plugin loads",
        "[completeness] doctor probe passes",
    ]
    rc = mod.cmd_init(repo, sid, "completeness-checklist", titles)
    mirror_file = os.path.join(repo, "state", "tasks", sid, "completeness-checklist.yaml")

    if rc == 0:
        _pass("init returns 0")
    else:
        _fail("init returns 0", f"rc={rc}")

    if os.path.isfile(mirror_file):
        _pass("mirror file created at expected path")
    else:
        _fail("mirror file created", mirror_file)
        return

    content = open(mirror_file, encoding="utf-8").read()
    for t in titles:
        if f"title: '{t}'" in content:
            _pass(f"title present: {t}")
        else:
            _fail(f"title present: {t}")

    if content.count("state: open") == 3:
        _pass("all 3 items start open")
    else:
        _fail("all 3 items start open", f"got {content.count('state: open')}")

    if "schema: completeness-checklist-mirror-v1" in content:
        _pass("schema field present")
    else:
        _fail("schema field present")

    if f"sid: '{sid}'" in content:
        _pass("sid field matches")
    else:
        _fail("sid field matches")


def test_resumption_survival(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- resumption-survival: mirror readable across a fresh module load")
    repo = os.path.join(tmp_root, "resume")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-resume99"
    mod.cmd_init(repo, sid, "completeness-checklist", [
        "[completeness] plugin installed",
        "[completeness] hooks wired",
    ])
    mirror_file = os.path.join(repo, "state", "tasks", sid, "completeness-checklist.yaml")
    if not os.path.isfile(mirror_file):
        _fail("mirror written", mirror_file)
        return
    _pass("mirror written")

    # Simulate a session boundary: read the file from a plain open(), no
    # module state involved — proves the data is purely on disk.
    with open(mirror_file, encoding="utf-8") as f:
        fresh_read = f.read()

    if "schema: completeness-checklist-mirror-v1" in fresh_read:
        _pass("schema field present in fresh-read")
    else:
        _fail("schema field present in fresh-read")
    if "title: '[completeness] plugin installed'" in fresh_read:
        _pass("item1 present in fresh-read")
    else:
        _fail("item1 present in fresh-read")
    if "title: '[completeness] hooks wired'" in fresh_read:
        _pass("item2 present in fresh-read")
    else:
        _fail("item2 present in fresh-read")


def test_update_state(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- update: flips item state open->done without touching sibling item")
    repo = os.path.join(tmp_root, "update")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-update55"
    mod.cmd_init(repo, sid, "completeness-checklist", [
        "[completeness] surface A",
        "[completeness] surface B",
    ])
    rc = mod.cmd_update(repo, sid, "completeness-checklist", "[completeness] surface A", "done")
    if rc == 0:
        _pass("update returns 0")
    else:
        _fail("update returns 0", f"rc={rc}")

    mirror_file = os.path.join(repo, "state", "tasks", sid, "completeness-checklist.yaml")
    content = open(mirror_file, encoding="utf-8").read()
    done_count = content.count("state: done")
    open_count = content.count("state: open")
    if done_count == 1:
        _pass("1 done item")
    else:
        _fail("1 done item", f"got {done_count}")
    if open_count == 1:
        _pass("1 open item (sibling untouched)")
    else:
        _fail("1 open item (sibling untouched)", f"got {open_count}")


def test_init_required_before_update(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- init required before update: missing mirror returns non-zero")
    repo = os.path.join(tmp_root, "gate")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-gate77"
    rc = mod.cmd_update(repo, sid, "completeness-checklist", "[completeness] some item", "done")
    if rc != 0:
        _pass("update on missing mirror returns non-zero")
    else:
        _fail("update on missing mirror returns non-zero", f"got rc={rc}")


def test_update_missing_title(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- update on unknown title returns non-zero (F2 parity)")
    repo = os.path.join(tmp_root, "missing-title")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-missing1"
    mod.cmd_init(repo, sid, "completeness-checklist", ["[completeness] real item"])
    rc = mod.cmd_update(repo, sid, "completeness-checklist", "no such title", "done")
    if rc != 0:
        _pass("update on unknown title returns non-zero")
    else:
        _fail("update on unknown title returns non-zero", f"got rc={rc}")


def test_slug_sanitize(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- slug sanitize: special-char name produces safe filename")
    repo = os.path.join(tmp_root, "slug")
    os.makedirs(repo, exist_ok=True)
    sid = "test-sid-slug88"
    mod.cmd_init(repo, sid, "my checklist / 2026!", ["[completeness] item one"])
    mirror_dir = os.path.join(repo, "state", "tasks", sid)
    yamls = [f for f in os.listdir(mirror_dir) if f.endswith(".yaml")] if os.path.isdir(mirror_dir) else []
    if yamls:
        _pass(f"yaml found under state/tasks/{sid}/")
    else:
        _fail(f"yaml found under state/tasks/{sid}/")
        return
    basename = yamls[0]
    if " " not in basename and "/" not in basename and "\\" not in basename:
        _pass(f"filename has no unsafe chars: {basename}")
    else:
        _fail("filename has no unsafe chars", basename)


def test_cli_usage_error() -> None:
    print("--- CLI: too few args -> usage error, exit 1")
    result = subprocess.run(
        [sys.executable, SUBJECT],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    if result.returncode == 1:
        _pass("CLI with no args exits 1")
    else:
        _fail("CLI with no args exits 1", f"rc={result.returncode}")
    if "Usage:" in result.stderr:
        _pass("CLI usage message on stderr")
    else:
        _fail("CLI usage message on stderr", result.stderr)


def test_repo_root_flag_reaches_explicit_resolver(mod, monkeypatch) -> None:
    print("--- --repo-root threads through to resolve_checked_repo_root as explicit_root")
    seen = {}

    def _fake_resolve(explicit_root=None):
        seen["explicit_root"] = explicit_root
        return explicit_root, {"verdict": "EXPLICIT"}

    monkeypatch.setattr(mod, "resolve_checked_repo_root", _fake_resolve, raising=False)
    import types
    fake_lib = types.ModuleType("repo_identity")
    fake_lib.resolve_checked_repo_root = _fake_resolve
    monkeypatch.setitem(sys.modules, "repo_identity", fake_lib)

    fake_root = "not-a-real-path/sibling-repo"  # abs-path-ok: arbitrary opaque fixture string, never resolved to disk
    root, mismatch = mod._resolve_repo_root(fake_root)
    if root == fake_root:
        _pass("explicit_root threaded through and returned as root")
    else:
        _fail("explicit_root threaded through and returned as root", f"got {root!r}")
    if mismatch is None:
        _pass("EXPLICIT verdict is not refused (no mismatch message)")
    else:
        _fail("EXPLICIT verdict is not refused", mismatch)
    if seen.get("explicit_root") == fake_root:
        _pass("resolve_checked_repo_root called with explicit_root=PATH")
    else:
        _fail("resolve_checked_repo_root called with explicit_root=PATH", seen)


def test_positional_only_invocation_unchanged(mod) -> None:
    print("--- positional-only args (no --repo-root) behave exactly as before")
    args, repo_root, err = mod._extract_repo_root_flag(["init", "my-name", "a title"])
    if args == ["init", "my-name", "a title"]:
        _pass("positionals returned unchanged")
    else:
        _fail("positionals returned unchanged", args)
    if repo_root is None:
        _pass("repo_root is None when flag absent")
    else:
        _fail("repo_root is None when flag absent", repo_root)
    if err is None:
        _pass("no error when flag absent")
    else:
        _fail("no error when flag absent", err)


def test_title_beginning_with_dash_not_misparsed(mod) -> None:
    print("--- a title beginning with '-' is not eaten by flag parsing")
    args, repo_root, err = mod._extract_repo_root_flag(
        ["update", "my-name", "-1 point deduction", "done"]
    )
    if args == ["update", "my-name", "-1 point deduction", "done"]:
        _pass("dash-leading title preserved as positional text")
    else:
        _fail("dash-leading title preserved as positional text", args)
    if err is None:
        _pass("no spurious error")
    else:
        _fail("no spurious error", err)

    fake_root = "not-a-real-path/other-repo"  # abs-path-ok: arbitrary opaque fixture string, never resolved to disk
    args2, repo_root2, err2 = mod._extract_repo_root_flag(
        ["--repo-root", fake_root, "init", "my-name", "--not-a-flag title"]
    )
    if args2 == ["init", "my-name", "--not-a-flag title"]:
        _pass("--repo-root stripped, other dash-leading token kept")
    else:
        _fail("--repo-root stripped, other dash-leading token kept", args2)
    if repo_root2 == fake_root:
        _pass("repo_root extracted alongside a dash-leading title")
    else:
        _fail("repo_root extracted alongside a dash-leading title", repo_root2)


def test_repo_root_missing_value_errors(mod) -> None:
    print("--- --repo-root with no following value errors")
    args, repo_root, err = mod._extract_repo_root_flag(["init", "my-name", "--repo-root"])
    if err is not None:
        _pass("missing-value --repo-root returns an error string")
    else:
        _fail("missing-value --repo-root returns an error string", (args, repo_root, err))

    result = subprocess.run(
        [sys.executable, SUBJECT, "init", "my-name", "--repo-root"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    if result.returncode == 1:
        _pass("CLI: --repo-root with no value exits 1")
    else:
        _fail("CLI: --repo-root with no value exits 1", f"rc={result.returncode}")
    if "--repo-root requires a value" in result.stderr:
        _pass("CLI: usage error names --repo-root")
    else:
        _fail("CLI: usage error names --repo-root", result.stderr)


def test_cli_end_to_end(mod, tmp_path) -> None:
    tmp_root = str(tmp_path)
    print("--- CLI: end-to-end init via subprocess in a real git repo")
    if not shutil.which("git"):
        _pass("skipped (no git on PATH)")
        return
    repo = os.path.join(tmp_root, "cli-e2e")
    os.makedirs(repo, exist_ok=True)
    init = subprocess.run(
        ["git", "init", "-q", repo], capture_output=True, text=True, **no_console_creationflags()
    )
    if init.returncode != 0:
        _pass("skipped (git init unavailable in this sandbox)")
        return
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "test@example.com"],
        capture_output=True,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "-C", repo, "config", "user.name", "Test"],
        capture_output=True,
        **no_console_creationflags(),
    )

    env = dict(os.environ)
    env["COORDINATOR_SESSION_ID"] = "cli-e2e-sid"
    env.pop("CLAUDE_SESSION_ID", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)

    result = subprocess.run(
        [sys.executable, SUBJECT, "init", "completeness-checklist", "[completeness] cli item"],
        capture_output=True,
        text=True,
        cwd=repo,
        env=env,
        **no_console_creationflags(),
    )
    if result.returncode == 0:
        _pass("CLI init exits 0 in a real git repo")
    else:
        _fail("CLI init exits 0 in a real git repo", f"rc={result.returncode} stderr={result.stderr}")
    mirror_file = os.path.join(repo, "state", "tasks", "cli-e2e-sid", "completeness-checklist.yaml")
    if os.path.isfile(mirror_file):
        _pass("CLI init wrote the mirror file")
    else:
        _fail("CLI init wrote the mirror file", mirror_file)




def test_sid_resolution_ignores_cwd(tmp_path) -> None:
    """Pins the invariant the module docstring's cross-repo arm rests on.

    resolve_session_id reads the env ladder ONLY — the `.current-session-id`
    sentinel tier was removed (KS-4, 2026-08-07) and the `cwd` parameter is
    vestigial. If a cwd-derived tier were ever reintroduced, a --repo-root
    invocation whose cwd is a sibling repo would silently file its journal
    under the SIBLING's session id, with no error. This test fails the moment
    that becomes possible again.
    """
    from coordinator_core.session.core import resolve_session_id

    print("--- sid-resolution: cwd is never a resolution input")
    sentinel_dir = tmp_path / ".git" / "coordinator-sessions"
    sentinel_dir.mkdir(parents=True)
    (sentinel_dir / ".current-session-id").write_text("sibling-repo-sid\n", encoding="utf-8")

    saved = {var: os.environ.pop(var, None)
             for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")}
    try:
        resolved = resolve_session_id(str(tmp_path))
        if resolved == "":
            _pass("cwd sentinel is not consulted; unresolvable sid returns empty")
        else:
            _fail("cwd sentinel is not consulted", f"resolve_session_id returned {resolved!r}")

        os.environ["COORDINATOR_SESSION_ID"] = "dispatching-session-sid"
        resolved = resolve_session_id(str(tmp_path))
        if resolved == "dispatching-session-sid":
            _pass("env ladder wins over a sibling repo's cwd sentinel")
        else:
            _fail("env ladder wins over a sibling repo's cwd sentinel", f"got {resolved!r}")
    finally:
        os.environ.pop("COORDINATOR_SESSION_ID", None)
        for var, val in saved.items():
            if val is not None:
                os.environ[var] = val


def test_empty_sid_refuses_rather_than_guessing(tmp_path) -> None:
    """An unresolvable sid must fail loud, never write to state/tasks//."""
    print("--- empty-sid: CLI refuses when the whole env ladder is empty")
    repo = tmp_path / "no-sid"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], capture_output=True,
                   **no_console_creationflags())

    env = dict(os.environ)
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        env.pop(var, None)

    result = subprocess.run(
        [sys.executable, SUBJECT, "init", "checklist", "an item"],
        capture_output=True, text=True, cwd=str(repo), env=env,
        **no_console_creationflags(),
    )
    if result.returncode == 1 and "could not resolve session id" in result.stderr:
        _pass("CLI exits 1 with an actionable message on an unresolvable sid")
    else:
        _fail("CLI exits 1 on an unresolvable sid",
              f"rc={result.returncode} stderr={result.stderr!r}")
    if not (repo / "state" / "tasks").exists():
        _pass("no state/tasks/ tree was created on the refusal path")
    else:
        _fail("no state/tasks/ tree was created on the refusal path",
              str(list((repo / "state" / "tasks").iterdir())))
