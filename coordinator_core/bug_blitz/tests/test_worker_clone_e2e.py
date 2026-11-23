"""End to end: a bug-blitz run edits only its worker clone until `dispatch.land_run` grind mode lands by commit.

Mint through the default `submit_bug_blitz` hook entry (real mint and emit), provision the run's clone, fix in
the clone, PAUSE before landing, read the main checkout with an independent `git ls-files -s` + byte hash,
run the targeted tests in the clone, then land with the params the composed terminal commit emits.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.bug_blitz import targeted_run, worker_clone
from coordinator_core.bug_blitz.contract import FailingTest, ParseResult, TargetSpec
from coordinator_core.hooks import submit_bug_blitz as hook
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN_ID = "20261123T120000Z"
SESSION = "sess-blitz-e2e"
MOD = "pkg_e2e/mod.py"
HELPER = "pkg_e2e/helper.py"
TEST = "pkg_e2e/test_mod.py"
FIXED_MOD = "def add(a, b):\n    return a + b\n"
FIXED_HELPER = "NAME = 'fixed'\n"
PEER_HELPER = "NAME = 'peer'\n"
_FIXTURE_PROFILE = (
    Path(__file__).resolve().parents[2] / "ops" / "dispatch_emit" / "tests" / "fixtures" / "queue-profiles" / "fixture.yaml"
)
_TESTS = (FailingTest(file=TEST, name="test_add", message="assert -1 == 5"),)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout


def _tracked_state(root: Path) -> dict:
    """`{path: (index mode+blob, sha256 of the worktree bytes or None)}` read through git plumbing, never the run."""
    out = _git(root, "ls-files", "-s", "-z")
    state = {}
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        mode, blob, _stage = meta.split(" ")
        target = root / path
        digest = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        state[path] = (f"{mode} {blob}", digest)
    return state


def _differing(before: dict, after: dict) -> set:
    return {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}


@pytest.fixture
def blitz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID", "QUEUE_APPEND_OUTPUT_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    root = tmp_path / "real"
    root.mkdir()
    _git(root, "init", "-q", "-b", "work/box/2026-11-23")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "pkg_e2e").mkdir()
    (root / "state" / "bug-backlog").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["pkg_e2e"]\n', encoding="utf-8")
    (root / MOD).write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / HELPER).write_text("NAME = 'buggy'\n", encoding="utf-8")
    (root / TEST).write_text(
        "from pathlib import Path\n\nfrom pkg_e2e import mod\n\n\n"
        "def test_add():\n    assert mod.add(2, 3) == 5\n"
        "    assert Path(mod.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    profiles = tmp_path / "queue-profiles"
    profiles.mkdir()
    (profiles / "bug.yaml").write_text(_FIXTURE_PROFILE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(hook, "_profile_dir", lambda: str(profiles))
    return root


def _mint(root: Path) -> dict:
    """The submission through the default hook entry; returns the allow envelope's `updatedInput`."""
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"name": "coordinator:bug-blitz", "args": {"targets": "red-set"}},
        "session_id": SESSION,
        "cwd": str(root),
    }
    env = hook.evaluate(
        payload,
        resolve=lambda raw, r: TargetSpec(kind="dir", path=raw),
        parse=lambda spec: ParseResult(tests=_TESTS),
        run_id=RUN_ID,
    )
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    return hso["updatedInput"]


def _land_params(updated: dict, row_id: str, clone: Path) -> dict:
    """The params the composed terminal commit sends: `_terminalCommit` in `grind_compose`."""
    args = updated["args"]
    return {
        "script_path": args["script_path"],
        "run_id": args["run_stamp"],
        "run_token": args["run_token"],
        "grind": {
            "profile": hook.PROFILE,
            "rows": [{"row_id": row_id, "outcome": "fixed", "paths": [MOD, HELPER], "deleted_paths": []}],
            "halted": None,
            "per_row": True,
            "work_root": str(clone),
        },
    }


def test_main_checkout_is_byte_identical_through_the_fix_leg_and_the_fix_lands_from_the_clone(blitz: Path) -> None:
    root = blitz
    updated = _mint(root)
    decisions = json.loads((Path(updated["scriptPath"]).parent / hook.DECISIONS_FILE).read_text(encoding="utf-8"))
    row_id = Path(decisions["minted"][0]).stem
    row_path = root / decisions["minted"][0]
    before = _tracked_state(root)
    head_before = _git(root, "rev-parse", "HEAD").strip()

    clone = worker_clone.provision(root, RUN_ID)
    assert clone.resolve() != root.resolve() and Path(decisions["work_root"]).resolve() == clone.resolve()
    (clone / MOD).write_text(FIXED_MOD, encoding="utf-8")
    (clone / HELPER).write_text(FIXED_HELPER, encoding="utf-8")

    # PAUSE: the fix leg is done, land has not been called.
    assert _tracked_state(root) == before
    assert _git(root, "status", "--porcelain", "-uno") == ""
    assert _git(root, "rev-parse", "HEAD").strip() == head_before

    rec = targeted_run.run(clone, row_path, [MOD])
    assert rec.outcome == "passed", rec
    assert rec.selected == (TEST,)
    assert rec.base_sha == head_before
    assert _tracked_state(root) == before  # the targeted run did not touch main either

    unfixed = targeted_run.run(root, row_path, [MOD])
    assert unfixed.outcome == "failed", unfixed  # the same test at main is still red: main holds the bug

    (root / HELPER).write_text(PEER_HELPER, encoding="utf-8")  # a peer dirties a declared path in main
    reply = land_run._handler(_land_params(updated, row_id, clone), repo_root=root / ".git")

    assert reply.get("committed") is True, reply
    assert _git(root, "rev-list", "--count", f"{head_before}..HEAD").strip() == "1"
    assert set(_git(root, "show", "--name-only", "--no-renames", "--format=", "HEAD").split()) == {MOD, HELPER}
    assert _git(root, "show", f"HEAD:{MOD}") == FIXED_MOD
    assert _git(root, "show", f"HEAD:{HELPER}") == FIXED_HELPER
    assert (root / MOD).read_text(encoding="utf-8") == FIXED_MOD  # main worktree updated
    assert (root / HELPER).read_text(encoding="utf-8") == PEER_HELPER  # the peer's dirt is stranded, not overwritten
    assert [(s["path"], s["reason"]) for s in reply["stranded"]] == [(HELPER, "peer-dirty-in-main")]
    assert not clone.exists() and worker_clone.clone_root(root, RUN_ID) is None
    assert _differing(before, _tracked_state(root)) == {MOD, HELPER}


def test_the_fire_args_hand_the_composed_script_the_clone_the_hook_decided(blitz: Path) -> None:
    updated = _mint(blitz)
    decisions = json.loads((Path(updated["scriptPath"]).parent / hook.DECISIONS_FILE).read_text(encoding="utf-8"))
    assert decisions["work_root"]
    assert updated["args"].get("work_root") == decisions["work_root"]


def test_the_runner_provisions_exactly_the_clone_the_fire_args_name(blitz: Path, capsys: pytest.CaptureFixture) -> None:
    updated = _mint(blitz)
    work_root = updated["args"]["work_root"]
    assert worker_clone.clone_root(blitz, RUN_ID) is None

    wrong = targeted_run.main(["provision", "--repo", str(blitz), "--run-id", RUN_ID, "--work-root", str(blitz / "elsewhere")])
    assert wrong == 1 and "not the fire args'" in capsys.readouterr().err
    worker_clone.reap(blitz, RUN_ID)

    code = targeted_run.main(["provision", "--repo", str(blitz), "--run-id", RUN_ID, "--work-root", work_root])
    seen = capsys.readouterr()
    assert code == 0, seen.err
    assert json.loads(seen.out.splitlines()[-1])["work_root"] == Path(work_root).as_posix()
    clone = worker_clone.clone_root(blitz, RUN_ID)
    assert clone is not None and clone.resolve() == Path(work_root).resolve()
    assert (clone / MOD).read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"


def test_the_runner_refuses_an_unsafe_run_id_without_touching_disk(blitz: Path, capsys: pytest.CaptureFixture) -> None:
    code = targeted_run.main(["provision", "--repo", str(blitz), "--run-id", "../x", "--work-root", str(blitz / "x")])
    assert code == 1 and "not a safe path component" in capsys.readouterr().err
