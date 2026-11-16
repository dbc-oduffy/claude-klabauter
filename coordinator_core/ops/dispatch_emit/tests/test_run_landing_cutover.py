"""The cutover switch behind `dispatch.terminal_commit`: the settings-home key named by
`run_landing.LANDING_IMPL_KEY` routes a product landing to `land_run`; off, missing, unrecognised or
unreadable keeps the old path; the non-product modes never leave it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import machine_resolver
from coordinator_core.ops.dispatch_emit import run_landing, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

ENV_KEY = machine_resolver._env_override_key(run_landing.LANDING_IMPL_KEY)
OLD_REPLY = {"routed": "old"}
NEW_REPLY = {"routed": "new"}
PRODUCT = {"script_path": "run.mjs", "inline_review": {"integration_stem": "s", "slices": []}}


@pytest.fixture
def routed(monkeypatch):
    """Both landing paths replaced by sentinels; returns the list of paths a call reached."""
    reached: list[str] = []
    monkeypatch.setattr(run_landing, "land_run", lambda p, r=None: reached.append("new") or NEW_REPLY)
    monkeypatch.setattr(terminal_commit, "_terminal_commit", lambda *a, **k: reached.append("old") or dict(OLD_REPLY))
    monkeypatch.delenv(ENV_KEY, raising=False)
    return reached


def _set_key(monkeypatch, value):
    real = machine_resolver.registry_get
    monkeypatch.setattr(
        machine_resolver, "registry_get",
        lambda key: value if key == run_landing.LANDING_IMPL_KEY else real(key),
    )


def test_key_on_routes_a_product_landing_to_land_run(routed, monkeypatch):
    _set_key(monkeypatch, "new")
    assert terminal_commit._handler_body(dict(PRODUCT), Path(".")) == NEW_REPLY
    assert routed == ["new"]


@pytest.mark.parametrize("value", ["old", None, "", "  ", "bogus", "NEWER"])
def test_key_off_missing_or_unrecognised_routes_to_the_old_path(routed, monkeypatch, value):
    _set_key(monkeypatch, value)
    out = terminal_commit._handler_body(dict(PRODUCT), Path("."))
    assert out["routed"] == "old"
    assert routed == ["old"]


def test_an_unreadable_registry_defaults_to_the_old_path(routed, monkeypatch):
    def boom(key):
        raise OSError("registry unreadable")

    monkeypatch.setattr(machine_resolver, "registry_get", boom)
    assert terminal_commit._landing_impl() == run_landing.LANDING_IMPL_DEFAULT == run_landing.IMPL_OLD
    assert terminal_commit._handler_body(dict(PRODUCT), Path("."))["routed"] == "old"


def test_the_key_value_is_case_and_space_insensitive(routed, monkeypatch):
    _set_key(monkeypatch, " New ")
    assert terminal_commit._handler_body(dict(PRODUCT), Path(".")) == NEW_REPLY


def test_env_override_selects_the_new_path_and_unsetting_rolls_back(routed, monkeypatch):
    monkeypatch.setenv(ENV_KEY, "new")
    assert terminal_commit._handler_body(dict(PRODUCT), Path(".")) == NEW_REPLY
    monkeypatch.setenv(ENV_KEY, "old")
    assert terminal_commit._handler_body(dict(PRODUCT), Path("."))["routed"] == "old"
    assert routed == ["new", "old"]


@pytest.mark.parametrize(
    "params",
    [
        {**PRODUCT, "halted_by": {"halted": "x"}},
        {**PRODUCT, "fanout": {"plans": ["p.md"]}},
        {**PRODUCT, "grind": {"rows": []}},
        {"plan_path": "p.md", "run_id": "ask-1"},
        {"task_output_path": "t.json", "inline_review": PRODUCT["inline_review"]},
        {**PRODUCT, "task_output_path": "t.json"},
    ],
    ids=["halted", "fanout", "grind", "no-script", "task-output-only", "script-and-task-output"],
)
def test_non_product_modes_stay_on_the_old_path_with_the_key_on(routed, monkeypatch, params):
    _set_key(monkeypatch, "new")
    assert terminal_commit._routes_to_land_run(params) is False
    terminal_commit._handler_body(params, Path("."))
    assert "new" not in routed


def test_the_key_is_read_per_call_so_flipping_it_needs_no_restart(routed, monkeypatch):
    _set_key(monkeypatch, "new")
    assert terminal_commit._handler_body(dict(PRODUCT), Path(".")) == NEW_REPLY
    _set_key(monkeypatch, "old")
    assert terminal_commit._handler_body(dict(PRODUCT), Path("."))["routed"] == "old"


def _git(args, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                          **no_console_creationflags()).stdout.strip()


@pytest.mark.spawns_process
@pytest.mark.parametrize("impl", ["old", "new"])
def test_the_real_handler_lands_a_commit_on_either_path(impl, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv(ENV_KEY, impl)
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                 ["config", "core.autocrlf", "false"]):
        _git(args, root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    before = _git(["rev-parse", "HEAD"], root)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="tC1", paths=("a.py",)),))
    (root / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    out = terminal_commit._handler(
        {"inline_review": reviewed(), "script_path": "run.mjs", "incomplete_chunks": []}, repo_root=root / ".git"
    )
    assert out.get("committed") is True, out
    after = _git(["rev-parse", "HEAD"], root)
    assert after != before and out["sha"] == after
    assert _git(["show", "--name-only", "--format=", "HEAD"], root) == "a.py"
