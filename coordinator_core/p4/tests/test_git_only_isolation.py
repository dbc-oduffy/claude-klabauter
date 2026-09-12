"""
test_git_only_isolation.py -- C4's isolation half.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § C4.

One runtime assertion, not four per-entry-point runtime assertions
(EM redirect, Kira overengineering, over eng-director finding 11): a
git-only repo (no `vcs_mirror: p4` key) drives `ceremony.commit_v2` then
`push.outstanding` IN-PROCESS, in a fresh subprocess so the assertion is not
at the mercy of whatever else pytest's own collection happened to import
first, and asserts `coordinator_core.p4` never lands in `sys.modules` and the
p4 CLI is never spawned (a `subprocess.Popen` spy, since the isolation
guarantee's whole point is "the p4 runner is never even reached", not merely
"one entry point does not reach it").

Static: a module-scope import scan over `write_guards/`, `bash_guards/`, and
`orientation/` asserting none imports `coordinator_core.p4` at module top
level -- strictly stronger than enumerating entry points at runtime, since it
covers any entry point the runtime enumeration does not reach.

The existing spawn pins (`ceremony.commit_v2` = 1, `push.outstanding` = 4,
`coordinator_core/tests/test_no_uncounted_spawn_on_budgeted_path.py ::
test_static_spawn_count_pins_*`) are the spawn half of the test surface and
are left unedited here -- this file adds the isolation half beside them, it
does not restate them.

Negative-spec:
  - Never asserts per-entry-point ("commit_v2 never imports p4", "push
    handler never imports p4", ...) -- the module-scope static scan already
    covers every entry point those runtime checks would enumerate, and the
    one runtime assertion covers the two named live entry points end to end.
  - Never imports `coordinator_core.p4` (or any of its submodules) directly
    in this test process -- doing so would poison the very `sys.modules`
    check the runtime leg exists to make, which is why that leg runs in a
    child interpreter instead.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Directories the static scan covers -- every module-scope entry point a
#: git-only repo can reach without ever declaring `vcs_mirror: p4` (D1 body,
#: C4 row).
_SCANNED_DIRS = ("write_guards", "bash_guards", "orientation")

_RUNTIME_PROBE = textwrap.dedent(
    r"""
    import json
    import subprocess as _subprocess
    import sys
    from pathlib import Path

    calls = []
    _OrigPopen = _subprocess.Popen

    class _SpyPopen(_OrigPopen):
        def __init__(self, cmd, *a, **kw):
            argv0 = cmd[0] if isinstance(cmd, (list, tuple)) else cmd
            if isinstance(argv0, str) and argv0.lower() in ("p4", "p4.exe"):
                calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else [cmd])
            super().__init__(cmd, *a, **kw)

    _subprocess.Popen = _SpyPopen

    from coordinator_core.ops.ceremony import commit_v2
    from coordinator_core.ops import push_outstanding as push_outstanding_mod

    repo_root = sys.argv[1]
    common_dir = Path(repo_root) / ".git"

    commit_result = commit_v2._handler(
        {"paths": ["a.txt"], "message": "isolation probe"},
        repo_root=common_dir,
    )

    push_result = push_outstanding_mod._handler(
        {"session_id": sys.argv[2]},
        repo_root=common_dir,
    )

    print(json.dumps({
        "p4_in_sys_modules": "coordinator_core.p4" in sys.modules,
        "p4_calls": calls,
        "committed": commit_result.get("committed"),
        "push_exit_code": push_result.get("exit_code"),
    }))
    """
)


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _init_git_only_repo(tmp_path: Path) -> Path:
    """A repo with NO `coordinator.local.md` at all -- the plain absence
    case, not merely a present-but-non-p4 marker (both are covered by
    `test_workspace.py`'s own `is_p4_repo` unit tests; this fixture only
    needs one of them to drive the end-to-end isolation leg)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/isolation"], repo)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    return repo


class TestGitOnlyRepoNeverTouchesP4:
    def test_commit_v2_then_push_outstanding_never_import_or_spawn_p4(self, tmp_path):
        repo = _init_git_only_repo(tmp_path)
        sid = str(uuid.uuid4())

        proc = subprocess.run(
            [sys.executable, "-c", _RUNTIME_PROBE, str(repo), sid],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
            **no_console_creationflags(),
        )

        assert proc.returncode == 0, (
            f"probe subprocess failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])

        assert payload["committed"] is True, payload
        assert payload["p4_in_sys_modules"] is False, payload
        assert payload["p4_calls"] == [], payload


#: Guard modules whose entire job IS the p4 leg (C5's checkout guard, C6/C9's
#: verb fence) -- named in this plan's own `scope:` list
#: (`coordinator_core/write_guards/p4_checkout_before_edit.py`,
#: `coordinator_core/bash_guards/p4_verb_fence.py`). These necessarily import
#: `coordinator_core.p4` at their own module scope; the isolation guarantee
#: this scan polices is that a git-only repo's runtime guard DISPATCH never
#: reaches them (`write_guards/engine.py`'s two-stage lazy import loads a
#: guard module only once its own pattern/name matches -- confirmed by
#: `discover_guard_names()`'s own docstring, "full eager discovery", being
#: reserved for the offline catalog script/diagnostic
#: (`coordinator/bin/emit-guard-enforcement-join.py`,
#: `bash_guards/_alternative_liveness.py`), never the hot dispatch path a
#: git-only session's own Edit/Write/bash actions drive), not that these two
#: declared p4 guard modules do not exist. Excluding them by name is
#: therefore the correct reading of "none imports coordinator_core.p4 at
#: module top level" -- every OTHER, non-p4-declared guard module in these
#: three directories is what that sentence is about.
_DECLARED_P4_GUARD_MODULES = {
    _REPO_ROOT / "coordinator_core" / "write_guards" / "p4_checkout_before_edit.py",
    _REPO_ROOT / "coordinator_core" / "bash_guards" / "p4_verb_fence.py",
}


class TestNoModuleScopeP4ImportInGitOnlyEntryPoints:
    """The static leg -- strictly stronger than the runtime probe above,
    since it covers every module in the three directories, not just the two
    entry points the runtime leg happens to drive."""

    def test_no_module_scope_import_of_coordinator_core_p4(self):
        offenders = []
        for dirname in _SCANNED_DIRS:
            root = _REPO_ROOT / "coordinator_core" / dirname
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.py")):
                # Test files exercise the p4 package deliberately (e.g. a
                # guard's own p4-specific test fixture) -- module-scope
                # imports IN THOSE are not the isolation guarantee this scan
                # exists to police, which is about the *production* entry
                # points a git-only repo actually loads.
                if "tests" in path.relative_to(root).parts:
                    continue
                if path in _DECLARED_P4_GUARD_MODULES:
                    continue
                try:
                    source = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                tree = ast.parse(source, filename=str(path))
                for node in tree.body:
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name == "coordinator_core.p4" or alias.name.startswith(
                                "coordinator_core.p4."
                            ):
                                offenders.append(f"{path}: import {alias.name}")
                    elif isinstance(node, ast.ImportFrom):
                        module = node.module or ""
                        if module == "coordinator_core.p4" or module.startswith(
                            "coordinator_core.p4."
                        ):
                            offenders.append(f"{path}: from {module} import ...")

        assert offenders == [], (
            "module-scope import of coordinator_core.p4 found in a git-only "
            f"entry point directory: {offenders}"
        )
