"""Cold/warm parity ratchet: the cold shell guard chain is never more permissive than the warm engine.

Two pins. The registry pin: every irreversible-class Bash dispatch guard in `deny_registry` is carried by the
cold table, `backstop.GUARD_MODULES` or `cold_chain.NO_COLD_FORM`, exactly one. The verdict pin: for each fixture
command in a real git repo, `unimatrix-host-guard.py`'s decide path answers at least as strictly cold (engine
unreachable) as warm (the engine's `warm_guard.evaluate` verdict), and a forced `git clean` over an untracked
load-bearing path denies on cold, warm, cold.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core import deny_registry
from coordinator_core.bash_guards import dispatch
from coordinator_core.host_hooks import backstop, cold_chain, shim
from coordinator_core.ops.warm_guard_evaluate import _warm_guard_evaluate

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SUBAGENT_ID = "a7889dad168f41aa5"
_LOADBEARING = "state"

_spec = importlib.util.spec_from_file_location("unimatrix_host_guard_for_ratchet", _ENGINE_ROOT / "coordinator" / "bin" / "unimatrix-host-guard.py")
_host_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_host_guard)


def _bash_dispatch_guard_names() -> set:
    chain = dispatch._build_guard_chain(
        cmd="echo cold-warm-parity-probe", session_id="parity-probe", cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}}, policy_file=None, host_is_windows=None,
    )
    return {entry.name for entry in chain}


def _irreversible_registry() -> set:
    return {g for g, cls in deny_registry.GUARD_DENY_CLASS.items() if cls in deny_registry.IRREVERSIBLE_CLASSES}


def test_registry_derived_irreversible_bash_set_equals_the_cold_carriers() -> None:
    bash = _irreversible_registry() & _bash_dispatch_guard_names()
    table, absent, pushed = set(cold_chain.SHELL_DENY_GUARDS), set(cold_chain.NO_COLD_FORM), set(backstop.GUARD_MODULES)
    assert not (table & absent) and not (table & pushed) and not (absent & pushed), (table & absent, table & pushed, absent & pushed)
    assert bash == table | absent | pushed, {"uncarried": bash - (table | absent | pushed), "not_irreversible_bash": (table | absent | pushed) - bash}
    assert set(cold_chain.COLD_PARTIAL) <= table
    assert all(reason.strip() for reason in cold_chain.NO_COLD_FORM.values())


def test_every_irreversible_guard_outside_the_bash_chain_is_a_patch_or_hook_guard() -> None:
    import coordinator_core.hooks as hooks
    import coordinator_core.write_guards as write_guards

    write_modules = {p.stem.replace("_", "-") for p in Path(write_guards.__file__).parent.glob("*.py")}
    hook_modules = {p.stem.replace("_", "-") for p in Path(hooks.__file__).parent.glob("*.py")}
    outside = _irreversible_registry() - _bash_dispatch_guard_names()
    assert outside <= write_modules | hook_modules, outside - (write_modules | hook_modules)
    assert outside & write_modules == set(cold_chain.PATCH_DENY_GUARDS)


def test_every_cold_table_guard_is_irreversible_class_and_a_registered_bash_guard() -> None:
    table = set(cold_chain.SHELL_DENY_GUARDS)
    assert table <= _irreversible_registry() & _bash_dispatch_guard_names()
    assert len(table) == len(cold_chain.SHELL_DENY_GUARDS)
    assert set(cold_chain.PATCH_DENY_GUARDS) <= _irreversible_registry()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b.c", "-c", "user.name=n", "-c", "commit.gpgsign=false", *args],
                   check=True, capture_output=True, creationflags=_NO_WINDOW)


def _repo(path: Path, *, gitignore: str = "") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    tracked = ["f.txt"]
    if gitignore:
        (path / ".gitignore").write_text(gitignore, encoding="utf-8")
        tracked.append(".gitignore")
    _git(path, "add", *tracked)
    _git(path, "commit", "-qm", "init")
    return path


@pytest.fixture(scope="module")
def repos(tmp_path_factory):
    base = tmp_path_factory.mktemp("cold-warm-ratchet")
    lo25 = _repo(base / "lo25")
    (lo25 / _LOADBEARING).mkdir()
    (lo25 / _LOADBEARING / "x.md").write_text("load-bearing\n", encoding="utf-8")
    ignored = _repo(base / "ignored", gitignore="state/\n")
    (ignored / _LOADBEARING).mkdir()
    (ignored / _LOADBEARING / "x.md").write_text("ignored\n", encoding="utf-8")
    plain = _repo(base / "plain")
    (plain / "note.txt").write_text("untracked\n", encoding="utf-8")
    (plain / "f.txt").write_text("edited, different length\n", encoding="utf-8")
    return {"lo25": lo25, "ignored": ignored, "plain": plain}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "registry.local.toml").write_text('[policy]\nnon_owned_remotes = ["github.com/some-stranger/"]\n', encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry))
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "warm"))
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    return monkeypatch


def _payload(host: str, cwd: Path, command: str, subagent: bool) -> dict:
    if host == "agy":
        return {"hookEventName": "PreToolUse", "conversationId": "ratchet-s", "workspacePaths": [str(cwd)],
                "toolCall": {"name": "run_command", "args": {"CommandLine": command}}}
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "ratchet-s", "cwd": str(cwd),
               "tool_input": {"command": command}}
    if subagent:
        payload["agent_id"] = _SUBAGENT_ID
    return payload


def _route(monkeypatch, host: str, cwd: Path, command: str, *, warm: bool, subagent: bool = False) -> tuple:
    """(denied, reason) through the host guard's decide path. Cold: the engine dial misses. Warm: the dial returns the
    engine's real `warm_guard.evaluate` verdict, and the in-process chain raises if it is reached."""
    def dial(request, budget):
        if not warm:
            return None, False
        return {"result": asyncio.run(_warm_guard_evaluate(request["params"]))}, False

    def unreachable(*args, **kwargs):
        raise AssertionError("the warm route reached the in-process cold chain")

    with monkeypatch.context() as scoped:
        # pytest points the temp dir at a per-session folder, so the fixture repos would sit outside it; a real
        # box's repo under the system temp dir (the lo-25 shape) is what the cold temp-dir exemption reads.
        scoped.setattr(tempfile, "tempdir", str(cwd.parent))
        scoped.setattr(shim, "_budgeted", dial)
        if warm:
            scoped.setattr(cold_chain, "evaluate", unreachable)
            scoped.setattr(backstop, "match", unreachable)
        answer, state = _host_guard.guard(host, json.dumps(_payload(host, cwd, command, subagent)), _ENGINE_ROOT, budget_secs=120)
    assert state != "degraded", (host, command, warm, answer)
    inner = answer.get("hookSpecificOutput") if host == "codex" else answer
    inner = inner if isinstance(inner, dict) else {}
    if host == "codex":
        return inner.get("permissionDecision") == "deny", str(inner.get("permissionDecisionReason") or "")
    return inner.get("decision") == "deny", str(inner.get("reason") or "")


@pytest.mark.parametrize("host", ["codex", "agy"])
@pytest.mark.parametrize("command", ["git clean -fd", "git clean -fdx"])
def test_forced_clean_over_untracked_load_bearing_state_denies_cold_then_warm_then_cold(isolated, repos, host, command) -> None:
    cwd = repos["lo25"]
    legs = [_route(isolated, host, cwd, command, warm=warm) for warm in (False, True, False)]
    for (denied, reason), leg in zip(legs, ("cold", "warm", "cold again")):
        assert denied, (leg, command, "the route allowed a clean that discards an untracked load-bearing path")
    assert "destructive-git-clean" in legs[0][1] and _LOADBEARING in legs[0][1]
    assert "destructive-git-clean" in legs[1][1] and _LOADBEARING in legs[1][1]
    assert "destructive-git-clean" in legs[2][1] and _LOADBEARING in legs[2][1]


def test_a_dry_run_clean_over_the_same_repo_stays_an_allow_on_every_route(isolated, repos) -> None:
    for warm in (False, True, False):
        assert _route(isolated, "codex", repos["lo25"], "git clean -nd", warm=warm)[0] is False


# (id, repo kind, command, subagent). Cold strictness must be >= warm on each.
_FIXTURES = [
    ("clean-fd", "lo25", "git clean -fd", False),
    ("clean-fdx", "lo25", "git clean -fdx", False),
    ("clean-f-d", "lo25", "git clean -f -d", False),
    ("clean-force", "lo25", "git clean --force -d", False),
    ("clean-C", "lo25", "git -C {repo} clean -fd", False),
    ("clean-pathspec-state", "lo25", "git clean -fd -- state", False),
    ("clean-pathspec-other", "lo25", "git clean -fd -- other", False),
    ("clean-exclude-state", "lo25", "git clean -fd -e state", False),
    ("clean-dry", "lo25", "git clean -nd", False),
    ("clean-ignored-kept", "ignored", "git clean -fd", False),
    ("clean-ignored-x", "ignored", "git clean -fdx", False),
    ("clean-ignored-X", "ignored", "git clean -fdX", False),
    ("clean-plain", "plain", "git clean -fd", False),
    ("clean-subagent", "lo25", "git clean -fd", True),
    ("rm-untracked", "plain", "rm -rf note.txt", False),
    ("rm-untracked-subagent", "plain", "rm -rf note.txt", True),
    ("rm-state", "lo25", "rm -rf state", False),
    ("stash-drop", "plain", "git stash drop", False),
    ("stash-clear", "plain", "git stash clear", False),
    ("reset-hard", "plain", "git reset --hard", False),
    ("reset-hard-subagent", "plain", "git reset --hard", True),
    ("checkout-dot", "plain", "git checkout -- .", False),
    ("worktree-add", "plain", "git worktree add ../w", False),
    ("fs-scan", "plain", "find / -name x", False),
    ("disk-fill", "plain", "truncate -s 900000000G big.bin", False),
    ("benign", "plain", "git status", False),
]

# Fixture id -> the `NO_COLD_FORM` guard that alone makes warm stricter. Cold allowing there is the named exception.
_NAMED_COLD_GAPS = {"pytest-suite": "check-test-suite-invocation"}
_GAP_FIXTURES = [("pytest-suite", "plain", "pytest -p xdist", False)]


@pytest.mark.parametrize("fixture", _FIXTURES + _GAP_FIXTURES, ids=[f[0] for f in _FIXTURES + _GAP_FIXTURES])
def test_cold_verdict_is_never_more_permissive_than_warm(isolated, repos, fixture) -> None:
    name, kind, command, subagent = fixture
    cwd = repos[kind]
    command = command.format(repo=cwd)
    cold = _route(isolated, "codex", cwd, command, warm=False, subagent=subagent)[0]
    warm = _route(isolated, "codex", cwd, command, warm=True, subagent=subagent)[0]
    if name in _NAMED_COLD_GAPS:
        assert _NAMED_COLD_GAPS[name] in cold_chain.NO_COLD_FORM, name
        return
    assert cold or not warm, (command, "cold allows what warm denies")


def test_a_named_cold_gap_is_a_real_warm_deny_and_every_gap_is_in_no_cold_form(isolated, repos) -> None:
    for name, kind, command, subagent in _GAP_FIXTURES:
        assert _route(isolated, "codex", repos[kind], command, warm=True, subagent=subagent)[0], name
    assert set(_NAMED_COLD_GAPS.values()) <= set(cold_chain.NO_COLD_FORM)


def test_the_verdict_fixtures_exercise_a_warm_deny_on_the_clean_shapes(isolated, repos) -> None:
    denies = [
        f[0] for f in _FIXTURES
        if f[0] in {"clean-fd", "clean-fdx", "stash-drop", "fs-scan"}
        and _route(isolated, "codex", repos[f[1]], f[2].format(repo=repos[f[1]]), warm=True, subagent=f[3])[0]
    ]
    assert set(denies) == {"clean-fd", "clean-fdx", "stash-drop", "fs-scan"}, denies
