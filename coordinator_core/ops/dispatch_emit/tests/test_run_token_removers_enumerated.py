"""Guard: every non-test `run_token.consume` / `run_token.discard` call site is named, with the
ended-run condition under which it removes a record. The condition text is documentation; only the
site and its call count are enforced.

An unlisted site is a new way to strand a still-running run's token; add it to `_ALLOWED` with its
condition, or remove the call. The walk is in-process `ast`; no spawn.
"""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import coordinator_core

import pytest

from coordinator_core.ops.dispatch_emit import run_token as _rt

_REAL_KICK = _rt._kick_spool_drain

pytestmark = pytest.mark.complete

_PKG = Path(coordinator_core.__file__).resolve().parent
_REMOVERS = {"consume", "discard"}
_MODULE = "run_token"

# (repo-relative file, enclosing function) -> (call count, ended-run condition)
_ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("warp_live.py", "_record_live"): (
        1, "launch this mint submitted has terminal evidence (run ended)"),
    ("hooks/submit_engine_ask.py", "_evaluate"): (
        2, "unlaunched run refused at submission, or the emit of this call's own run failed"),
    ("hooks/submit_engine_ask.py", "_premint_deferred_envelope"): (
        1, "premint's deferred script could not be written, so the run never started"),
    ("ops/dispatch_emit/abandoned_runs.py", "_handler"): (
        1, "reaped idle run with no live holder (abandoned)"),
    ("ops/dispatch_emit/op.py", "_abandon_run"): (
        1, "recomposed refire shell nothing is driving (abandoned)"),
    ("ops/dispatch_emit/op.py", "_dispatch_emit_body"): (
        1, "this call's own fresh mint, rolled back because its run dir failed to land (never launched)"),
    ("ops/dispatch_emit/minted_run.py", "_abandon"): (
        1, "operator abandon of a minted-only run; the halt marker lands first so the run is unstartable"),
    ("ops/dispatch_emit/minted_run.py", "_clear_dirless_mint"): (
        1, "abandon of a mint whose run dir never landed (never launched)"),
    ("ops/dispatch_emit/ask_stage.py", "_refuse_live_write_overlap"): (
        1, "write-overlap refusal ending this run; reached only when a blocker is not a live engine run or the wait expired"),
    ("ops/dispatch_emit/terminal_commit.py", "_settle_token"): (
        1, "commit landed (sha present)"),
    ("ops/dispatch_emit/land_run.py", "_settle_token"): (
        1, "commit landed (sha present)"),
    ("ops/dispatch_emit/land_run_grind.py", "commit_grind_rows"): (
        1, "grind commit landed (landed set only on committed reply)"),
    ("ops/dispatch_emit/land_run_fanout.py", "land"): (
        1, "fan-out commit landed (landed set only on committed reply)"),
    ("ops/dispatch_emit/roadmap_fanout_commit.py", "commit_fanout_record"): (
        1, "fanout commit landed"),
    ("ops/dispatch_emit/land_run_grind.py", "commit_grind_rows"): (
        1, "grind commit landed"),
    ("ops/dispatch_emit/land_run_fanout.py", "land"): (
        1, "fanout commit landed"),
    ("ops/dispatch_emit/halted_run_superseded.py", "_end_parent_ask"): (
        1, "every child run of the parent ask's sizing is over and the parent is unclaimed (not mid terminal-commit)"),
    ("ops/dispatch_emit/release_run_token.py", "_handler"): (
        1, "sanctioned peer release of an unclaimed record"),
}


def _bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(names bound to the run_token module, names bound to its consume/discard functions)."""
    modules: set[str] = set()
    funcs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] == _MODULE:
                    modules.add(a.asname or a.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == _MODULE:
                    modules.add(a.asname or a.name)
                elif (node.module or "").split(".")[-1] == _MODULE and a.name in _REMOVERS:
                    funcs.add(a.asname or a.name)
    return modules, funcs


def _sites(path: Path) -> list[str]:
    """Enclosing-function name of each remover call in `path`."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    modules, funcs = _bindings(tree)
    if not modules and not funcs:
        return []
    found: list[str] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else scope
            if isinstance(child, ast.Call):
                f = child.func
                if (isinstance(f, ast.Attribute) and f.attr in _REMOVERS
                        and isinstance(f.value, ast.Name) and f.value.id in modules):
                    found.append(scope)
                elif isinstance(f, ast.Name) and f.id in funcs:
                    found.append(scope)
            visit(child, inner)

    visit(tree, "<module>")
    return found


def _scan() -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for path in sorted(_PKG.rglob("*.py")):
        rel = path.relative_to(_PKG).as_posix()
        parts = rel.split("/")
        if "tests" in parts or parts[-1].startswith("test_") or rel == "ops/dispatch_emit/run_token.py":
            continue
        for scope in _sites(path):
            counts[(rel, scope)] = counts.get((rel, scope), 0) + 1
    return counts


def test_every_run_token_remover_is_allowlisted_with_its_condition():
    found = _scan()
    unlisted = {k: n for k, n in found.items() if k not in _ALLOWED}
    assert not unlisted, f"run_token remover not in _ALLOWED (name its ended-run condition): {unlisted}"
    drift = {k: (found.get(k, 0), n) for k, (n, _) in _ALLOWED.items() if found.get(k, 0) != n}
    assert not drift, f"allowlist count drift (found, expected): {drift}"
    assert all(cond.strip() for _, cond in _ALLOWED.values())


def test_run_token_own_unlinks_are_the_protocol_functions_only():
    """`run_token.py` is outside the caller walk; its own `.unlink` calls, the spent-receipt
    removal in `mint` and the escrow TTL reaper `sweep_expired_escrows` included (escrow
    removal is protocol too), must stay in the protocol functions. `redeem_claim` is `redeem`'s body
    (`redeem` delegates); `consume` and `discard` remove through `_unlink_both`, which with
    `_drop_legacy` is the single record-plus-legacy-copy remover."""
    tree = ast.parse((_PKG / "ops/dispatch_emit/run_token.py").read_text(encoding="utf-8"))
    scopes: set[str] = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "unlink"):
                    scopes.add(fn.name)
    # `_bounded_glob` (its nested `work`) reaps TTL-expired legacy unclaimed files in its listing pass.
    assert scopes == {"mint", "redeem_claim", "_unlink_both", "_drop_legacy", "sweep_expired_escrows",
                      "_bounded_glob", "work"}


def test_only_consume_and_discard_wake_parked_stages():
    """`release` restores a claimed record to live after a commit that landed nothing: not an end."""
    tree = ast.parse((_PKG / "ops/dispatch_emit/run_token.py").read_text(encoding="utf-8"))
    callers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_wake_stage_waiters"
            for n in ast.walk(fn)
        )
    }
    assert callers == {"consume", "discard"}


SCRIPT = "scratch/warp/x.mjs"
SIZING = "state/sizings/2026-11/x.yaml"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def kicks(monkeypatch):
    """Stub the background drain so spooled entries stay pending for assertion; records each kick."""
    from coordinator_core.ops.dispatch_emit import run_token

    seen = []
    monkeypatch.setattr(run_token, "_kick_spool_drain", lambda r: seen.append(str(r)))
    return seen


def _park_behind(root, blocker, waiter="waiter", *, sizing=SIZING, blockers=None):
    from coordinator_core.ops.dispatch_emit import run_token, stage_wait

    run_token.mint(root, blocker, SCRIPT, session_id="peer-sess")
    run_token.mint(root, waiter, SCRIPT, session_id="s1", sizing_rel=sizing)
    stage_wait.park(root, waiter, blockers or [blocker], ["a.py"], session_id="s1")


def _spooled(root):
    from coordinator_core.ask_spool import record

    out = []
    for spool_id in record.list_pending(str(root)):
        header, body = record.read_entry(record.entry_path(str(root), spool_id))
        out.append((header, json.loads(body)))
    return out


@pytest.mark.parametrize("ender", ["consume", "discard"])
def test_blocker_end_wakes_the_waiter_once_and_spools_its_refire(repo, ender):
    from coordinator_core.ask_spool import notice
    from coordinator_core.ops.dispatch_emit import run_token, stage_wait

    _park_behind(repo, "blocker")
    getattr(run_token, ender)(repo, "blocker")
    assert [n["status"] for n in notice.consume_notices(str(repo), "s1")] == ["started"]
    assert stage_wait.read(repo, "waiter")["started"] is True
    assert not run_token.is_live(repo, "waiter")
    entries = _spooled(repo)
    assert len(entries) == 1
    _header, payload = entries[0]
    assert payload["tool_input"]["name"] == "coordinator:engine-ask"
    assert payload["tool_input"]["args"] == {"sizing": SIZING, "resume_run_id": "waiter"}
    getattr(run_token, ender)(repo, "blocker")
    assert len(_spooled(repo)) == 1
    assert notice.consume_notices(str(repo), "s1") == []


def test_a_wake_kicks_one_drain_and_a_non_wake_kicks_none(repo, kicks):
    from coordinator_core.ops.dispatch_emit import run_token, stage_wait

    _park_behind(repo, "blocker", "w1")
    run_token.mint(repo, "w2", SCRIPT, session_id="s2", sizing_rel=SIZING)
    stage_wait.park(repo, "w2", ["blocker"], [], session_id="s2")
    run_token.consume(repo, "blocker")
    assert kicks == [str(repo)]
    run_token.mint(repo, "other", SCRIPT, session_id="p")
    run_token.consume(repo, "other")
    assert kicks == [str(repo)]


def test_kick_runs_drain_run_on_the_repo(repo, monkeypatch):
    from coordinator_core.ask_spool import drain
    from coordinator_core.ops.dispatch_emit import run_token

    done = threading.Event()
    calls = []

    def fake_run(root, **k):
        calls.append(root)
        done.set()
        return {}

    monkeypatch.setattr(drain, "run", fake_run)
    _REAL_KICK(repo)
    assert done.wait(5) and calls == [str(repo)]


def test_waiter_with_two_blockers_resumes_only_after_the_last(repo):
    from coordinator_core.ops.dispatch_emit import run_token

    _park_behind(repo, "b1", blockers=["b1", "b2"])
    run_token.consume(repo, "b1")
    assert _spooled(repo) == [] and run_token.is_live(repo, "waiter")
    run_token.mint(repo, "b2", SCRIPT, session_id="peer-sess")
    run_token.discard(repo, "b2")
    assert len(_spooled(repo)) == 1


def test_release_wakes_nothing(repo):
    from coordinator_core.ops.dispatch_emit import run_token, stage_wait

    run_token.mint(repo, "blocker", SCRIPT, session_id="peer-sess")
    token = run_token.mint(repo, "blocker", SCRIPT, session_id="peer-sess")
    stage_wait.park(repo, "waiter", ["blocker"], [], session_id="s1")
    assert run_token.redeem(repo, "blocker", token, SCRIPT)
    assert run_token.release(repo, "blocker", token, SCRIPT)
    assert stage_wait.read(repo, "waiter")["started"] is False
    assert _spooled(repo) == []


def test_a_failing_wake_never_fails_the_removal(repo, monkeypatch, caplog):
    from coordinator_core.ops.dispatch_emit import run_token, stage_wait

    run_token.mint(repo, "blocker", SCRIPT, session_id="peer-sess")

    def boom(*a, **k):
        raise RuntimeError("wake exploded")

    monkeypatch.setattr(stage_wait, "wake_on_end", boom)
    with caplog.at_level("WARNING"):
        run_token.consume(repo, "blocker")
    assert not run_token.is_live(repo, "blocker")
    assert "stage wake after blocker ended failed" in caplog.text


def test_a_failing_spool_never_fails_the_removal_or_the_other_waiters(repo, monkeypatch, caplog):
    from coordinator_core.ask_spool import record
    from coordinator_core.ops.dispatch_emit import run_token, stage_wait

    _park_behind(repo, "blocker", "w1")
    run_token.mint(repo, "w2", SCRIPT, session_id="s2", sizing_rel=SIZING)
    stage_wait.park(repo, "w2", ["blocker"], [], session_id="s2")
    real = record.spool_ask
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("spool unwritable")
        return real(*a, **k)

    monkeypatch.setattr(record, "spool_ask", flaky)
    with caplog.at_level("WARNING"):
        run_token.consume(repo, "blocker")
    assert len(calls) == 2 and len(_spooled(repo)) == 1
    assert "stage resume of w1 after blocker ended failed" in caplog.text


def test_waiter_without_a_sizing_retires_its_token_and_spools_nothing(repo, caplog):
    from coordinator_core.ops.dispatch_emit import run_token

    _park_behind(repo, "blocker", sizing=None)
    with caplog.at_level("WARNING"):
        run_token.consume(repo, "blocker")
    assert not run_token.is_live(repo, "waiter")
    assert _spooled(repo) == []
    assert "no sizing to refire" in caplog.text


def test_walk_resolves_aliased_and_from_imports(tmp_path):
    src = tmp_path / "m.py"
    src.write_text(
        "from x import run_token as _rt\n"
        "from x.run_token import discard as d\n"
        "def a(r):\n    _rt.consume(r, 'i')\n"
        "def b(r):\n    d(r, 'i')\n"
        "def c(r):\n    other.consume(r)\n",
        encoding="utf-8",
    )
    assert sorted(_sites(src)) == ["a", "b"]
