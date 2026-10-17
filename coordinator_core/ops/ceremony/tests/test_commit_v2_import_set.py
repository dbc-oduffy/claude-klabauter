"""
coordinator_core.ops.ceremony.tests.test_commit_v2_import_set

Purpose: `commit_v2` does not itself pull `ops.fleet._common` (a heavy closure) at import, and a
landing without a `repo_root` param does not reach `check_repo_root`. Runs in a fresh interpreter
so `sys.modules` is the module's own closure.

A landing also leaves `baton_assemble` and `bash_guards.block_subagent_commit` unloaded: the ledger
owner resolution and the pathspec-shape guard use mirrors, pinned to the originals by the parity
tests below.

A landing with no guard module and no live peer also leaves `write_guards.guard_class_relay`,
`write_guards.engine` and `commit_companion` unloaded: `commit_v2` reaches them through lazy seams.

Negative-spec: does not assert `ops.fleet._common` is absent after a landing -- `git.commit_trailers`
reaches it through `session.claimed_plan.list_held_plan_claims`, outside `commit_v2`. Does not claim
`authoring_leaks.import_closure` (the leak gate must run) is absent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[4]

_IMPORT_DRIVER = """
import json, sys
from coordinator_core.ops.ceremony import commit_v2
print(json.dumps({"modules": sorted(sys.modules)}))
"""

_LANDING_DRIVER = """
import json, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
def g(*a):
    subprocess.run(["git", *a], cwd=str(root), check=True, capture_output=True)
g("init", "-q"); g("config", "user.email", "t@example.com"); g("config", "user.name", "t")
(root / "seed.md").write_text("seed\\n", encoding="utf-8"); g("add", "."); g("commit", "-q", "-m", "seed")
from coordinator_core.ops.ceremony import commit_v2
import coordinator_core.ops.fleet._common as common
calls = []
orig = common.check_repo_root
common.check_repo_root = lambda *a, **k: calls.append(a) or orig(*a, **k)
results = []
for name in ("a.md", "b.md"):
    (root / name).write_text(name + "\\n", encoding="utf-8")
    results.append(commit_v2._handler({"paths": [name], "message": "add " + name}, repo_root=root / ".git"))
print(json.dumps({"results": results, "check_repo_root_calls": len(calls), "modules": sorted(sys.modules)}, default=str))
"""


def _run(driver: str, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", driver, *args],
        cwd=str(_REPO_ROOT), capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_importing_commit_v2_does_not_load_fleet_common():
    assert "coordinator_core.ops.fleet._common" not in _run(_IMPORT_DRIVER)["modules"]


def test_landing_without_repo_root_param_skips_check_repo_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _run(_LANDING_DRIVER, str(repo))
    assert all(r.get("committed") for r in out["results"]), out["results"]
    assert out["check_repo_root_calls"] == 0


def test_landing_does_not_load_baton_assemble_or_block_subagent_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _run(_LANDING_DRIVER, str(repo))
    assert all(r.get("committed") for r in out["results"]), out["results"]
    for mod in (
        "coordinator_core.baton_assemble",
        "coordinator_core.bash_guards.block_subagent_commit",
    ):
        assert mod not in out["modules"], mod


def test_landing_does_not_load_guard_relay_or_companion_modules(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _run(_LANDING_DRIVER, str(repo))
    assert all(r.get("committed") for r in out["results"]), out["results"]
    for mod in (
        "coordinator_core.write_guards.guard_class_relay",
        "coordinator_core.write_guards.engine",
        "coordinator_core.ops.ceremony.commit_companion",
    ):
        assert mod not in out["modules"], mod


def test_lazy_guard_relay_seams_stay_patchable():
    from coordinator_core.ops.ceremony import commit_v2

    assert callable(commit_v2.detect_class_transition)
    assert callable(commit_v2.stage_class_transition_memo)
    assert commit_v2.detect_class_transition(None, None) is None


def _seed_claim(sessions: Path, sid: str, basename: str, claimed_at=None, stage=None, stamped=False):
    d = sessions / "handoff-claims" / basename
    d.mkdir(parents=True, exist_ok=True)
    (d / "session_id").write_text(sid, encoding="utf-8")
    if claimed_at is not None:
        (d / "claimed_at").write_text(claimed_at, encoding="utf-8")
    if stage is not None:
        (d / "stage").write_text(stage, encoding="utf-8")
    if stamped:
        from coordinator_core.session.claims import mark_claim_stamped

        mark_claim_stamped(d)


@pytest.mark.parametrize(
    "claims",
    [
        [],
        [("a.md", None, None, False)],
        [("a.md", "2026-01-01T00:00:01", "brief", False), ("b.md", "2026-01-01T00:00:00", "brief", False)],
        [("a.md", "2026-01-01T00:00:05", "apply", False), ("b.md", "2026-01-01T00:00:00", "brief", False)],
        [("a.md", "2026-01-01T00:00:05", "apply", True), ("b.md", "2026-01-01T00:00:00", "apply", False)],
        [("a.md", None, None, False), ("b.md", None, None, False)],
        [("a.md", "2026-01-01T00:00:00", "apply", False), ("b.md", "2026-01-01T00:00:00", "apply", False)],
    ],
)
def test_resolve_owner_parity(tmp_path, monkeypatch, claims):
    from coordinator_core.baton_assemble import _resolve_held_handoff_for_session
    from coordinator_core.commit_ledger import resolve_owner
    from coordinator_core.session import core as session_core

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    sessions = Path(session_core.sessions_dir(str(root)))
    for basename, claimed_at, stage, stamped in claims:
        _seed_claim(sessions, "sid-x", basename, claimed_at, stage, stamped)

    expected = _resolve_held_handoff_for_session(root, allow_standalone=True, session_id="sid-x")
    got = resolve_owner._primary_held_handoff(root, "sid-x")
    assert got == (expected[0], expected[2])


_SHAPE_CASES = [
    [],
    ["a.md"],
    ["dir/a.md", "b.md"],
    ["."],
    ["./"],
    [""],
    ["-A"],
    ["--all"],
    [":/"],
    [":(top)x"],
    [":(literal)a.md"],
    [":(literal)"],
    ["*.md"],
    ["a?.md"],
    ["[id]/page.tsx"],
    ["src/"],
    ["../x"],
    ["sub/.."],
    [{"not": "str"}],
    ["ROOT"],
    ["ROOT/a.md"],
    ["ROOT/sub/a.md"],
    ["OUTSIDE/a.md"],
    ["OUTSIDE"],
    ["a.md", "OUTSIDE/a.md"],
    ["C" + ":/elsewhere/a.md"],
    [":(literal)OUTSIDE/a.md"],
]


@pytest.mark.parametrize("raw", _SHAPE_CASES, ids=[repr(c) for c in _SHAPE_CASES])
@pytest.mark.parametrize("include_orphans", [False, True])
def test_action_guard_shape_parity(tmp_path, raw, include_orphans):
    from coordinator_core.bash_guards.block_subagent_commit import (
        _pathspec_shape_permitted as original,
    )
    from coordinator_core.git import action_guard

    root = (tmp_path / "repo").as_posix()
    outside = (tmp_path / "other").as_posix()
    (tmp_path / "repo" / "[id]").mkdir(parents=True)
    paths = [
        p.replace("ROOT", root).replace("OUTSIDE", outside) if isinstance(p, str) else p for p in raw
    ]
    assert action_guard._pathspec_shape_permitted(paths, include_orphans, root) == original(
        paths, include_orphans, root
    )
    assert action_guard._pathspec_shape_permitted(paths, include_orphans, "") == original(
        paths, include_orphans, ""
    )
