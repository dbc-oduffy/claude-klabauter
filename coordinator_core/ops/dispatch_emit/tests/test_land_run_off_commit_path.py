"""The landing path of ``dispatch.land_run`` never runs a corpus scan: the stopped-run scan, the
sizing-cascade scan and ``ceremony.commit_v2``'s handler stay off it in every mode, and the cascade
runs only in the deferred close job, scoped to the run's own records."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import deliverable_cascade, review_stamp
from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.ceremony.tests import test_close_background_land_run_legs as legs
from coordinator_core.ops.dispatch_emit import ask_fanout, land_run, land_run_contract
from coordinator_core.ops.dispatch_emit.ask_contract import (
    FANOUT_RECORD_PARAM,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests import test_land_run_fanout as fanout_tests
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261021T000000-aaaaaa"
DIR = Path(__file__).resolve().parents[1]
MODE_FILES = ("land_run.py", "land_run_halt.py", "land_run_grind.py", "land_run_fanout.py")
FORBIDDEN_IMPORTS = ("stopped_runs", "warp_live", "commit_v2")
HALT_PEER_CLAIM_IMPORT = {"land_run_halt.py": {"commit_v2"}}


def _forbidden_functions() -> dict:
    from coordinator_core import warp_live

    return {
        "warp_live.run_ended_on_evidence": warp_live.run_ended_on_evidence,
        "deliverable_cascade.cascade_terminal_sync": deliverable_cascade.cascade_terminal_sync,
        "deliverable_cascade._collect_live_candidates_for_kind": deliverable_cascade._collect_live_candidates_for_kind,
        "commit_v2._handler": commit_v2._handler,
    }


class _CallWatch:
    """Records every call of the named functions by code object, so a from-import binding is caught too."""

    def __init__(self, named: dict):
        self._codes = {fn.__code__: name for name, fn in named.items()}
        self.calls: list = []

    def _profile(self, frame, event, _arg):
        if event == "call":
            name = self._codes.get(frame.f_code)
            if name is not None:
                self.calls.append(name)

    def __enter__(self):
        sys.setprofile(self._profile)
        return self

    def __exit__(self, *_exc):
        sys.setprofile(None)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    for name in ("a.py", "b.py"):
        _write(root, name, "x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _record(root: Path) -> None:
    for name in ("ask.md", "run.json"):
        _write(root, f"{RECORD_DIR_ROOT}/{RUN}/{name}", f"{name}\n")


def _declare(root: Path, *paths: str) -> None:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=tuple(paths)),), plan_path=None)
    _write(root, "run.mjs", "// emitted\n" + render_marker(request) + "\n")


def _drive_product(root: Path) -> dict:
    _record(root)
    _write(root, "a.py", "x = 2\n")
    _declare(root, "a.py")
    return {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": {"integration_stem": "s", "slices": 1, "fixes": 0}}


def _drive_halted(root: Path) -> dict:
    _record(root)
    _write(root, "b.py", "x = 2\n")
    _declare(root, "b.py")
    return {"script_path": "run.mjs", HALTED_RECORD_PARAM: {"halted": "refusal", "reason": "needs a PM touchpoint"}}


def _drive_grind(root: Path) -> dict:
    _write(root, "a.py", "x = 2\n")
    row = {"row_id": "r1", "outcome": "fixed", "paths": ["a.py"], "deleted_paths": []}
    return {"script_path": "run.mjs", "grind": {"profile": "bug", "rows": [row]}}


def _drive_fanout(root: Path) -> dict:
    fanout_tests._make_plan(root, "a")
    _record(root)
    _write(root, "run.mjs", "// script\n")
    return {"script_path": "run.mjs", FANOUT_RECORD_PARAM: {"plans": [fanout_tests.PLANS["a"]]}}


DRIVERS = {
    "product": _drive_product,
    "halted": _drive_halted,
    "grind": _drive_grind,
    "fanout": _drive_fanout,
}


@pytest.mark.parametrize("mode", sorted(DRIVERS))
def test_no_corpus_scan_or_commit_v2_handler_runs_during_a_landing(repo, monkeypatch, mode):
    monkeypatch.setattr(ask_fanout, "fanout", lambda p, r: {"exit_code": 0, "ready": [], "runs": []})
    params = {"run_id": RUN, "session_id": None, **DRIVERS[mode](repo)}
    assert land_run_contract.mode_of(params) == mode
    named = _forbidden_functions()

    with _CallWatch(named) as watch:
        out = land_run._handler(params, repo_root=repo / ".git")

    assert watch.calls == [], f"{mode}: forbidden calls on the landing path: {watch.calls}"
    assert out.get("committed") is True, f"{mode}: the landing did not land, so the pin proves nothing: {out}"


def _imports_of(path: Path) -> set:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(part for alias in node.names for part in alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            found.update((node.module or "").split("."))
            found.update(alias.name for alias in node.names)
    return found


@pytest.mark.parametrize("name", MODE_FILES)
def test_land_run_and_its_mode_modules_import_neither_scan_nor_commit_v2(name):
    bad = (_imports_of(DIR / name) & set(FORBIDDEN_IMPORTS)) - HALT_PEER_CLAIM_IMPORT.get(name, set())
    assert not bad, f"{name} imports {sorted(bad)}"


def test_the_one_commit_v2_import_is_the_halt_peer_claim_helper_and_nothing_else():
    """The exemption is narrow: land_run_halt may touch commit_v2 only for ``_peer_claim_warnings``."""
    tree = ast.parse((DIR / "land_run_halt.py").read_text(encoding="utf-8"))
    used = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "commit_v2"
    }
    assert used == {"_peer_claim_warnings"}, used
    for name in MODE_FILES:
        if name != "land_run_halt.py":
            assert "commit_v2" not in _imports_of(DIR / name), name


def test_a_fresh_import_of_land_run_loads_none_of_the_scan_modules():
    probe = (
        "import sys, coordinator_core.ops.dispatch_emit.land_run; "
        f"print('LOADED=' + ','.join(m for m in sys.modules if m.rsplit('.', 1)[-1] in {FORBIDDEN_IMPORTS!r}))"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
        cwd=DIR.parents[2], **no_console_creationflags(),
    )
    assert done.returncode == 0, done.stderr[-500:]
    assert done.stdout.split("LOADED=", 1)[1].strip() == ""


def test_the_close_job_runs_the_cascade_over_the_runs_own_records_only(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = legs._build(root)
    monkeypatch.setattr(review_stamp, "mint", legs._fake_mint)
    legs._job(root, sha, ("review-stamp", "plan-reconciliation", "deliverable-cascade", "close-commit"), claimed_batons=[legs.BATON])
    run_calls: list = []
    real_run = deliverable_cascade.cascade_run

    def spy(*args, **kwargs):
        run_calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(deliverable_cascade, "cascade_run", spy)
    named = _forbidden_functions()
    named.pop("commit_v2._handler")
    scoped = {"deliverable_cascade._read_sizing_meta": deliverable_cascade._read_sizing_meta, **named}

    with _CallWatch(scoped) as watch:
        outcomes = cb.run_background(legs._ctx(root, batons=[legs.BATON]))

    assert outcomes["deliverable-cascade"].status == "ran", outcomes["deliverable-cascade"]
    assert len(run_calls) == 1
    records = list(dict.fromkeys([legs.SIZING, legs.BATON]))
    assert set(run_calls[0][2]) <= set(records)
    corpus_calls = [c for c in watch.calls if c != "deliverable_cascade._read_sizing_meta"]
    assert corpus_calls == [], corpus_calls
    assert watch.calls.count("deliverable_cascade._read_sizing_meta") <= len(records)
