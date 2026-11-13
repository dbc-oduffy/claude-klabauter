"""`fleet.klabauter_freshness` handler resolves the box's klabauter clone, never the claude-klabauter tree."""

import functools
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.install import engine_root_for_install as eri
from coordinator_core.install import klabauter_freshness as kf
from coordinator_core.install import klabauter_freshness_apply as apply
from coordinator_core.ops.fleet import klabauter_freshness as op

pytestmark = pytest.mark.complete

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _g(cwd, *args):
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env={**os.environ, **_ENV}
    )
    return out.stdout.strip()


def _commit(repo: Path, rel: str, text: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _g(repo, "add", rel)
    _g(repo, "commit", "-q", "-m", f"edit {rel}")
    return _g(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    _g(tmp_path, "init", "-q", "--bare", "-b", "candidate", str(origin))
    seed = tmp_path / "seed"
    _g(tmp_path, "clone", "-q", str(origin), str(seed))
    _g(seed, "checkout", "-q", "-b", "candidate")
    _commit(seed, "README.md", "one\n")
    _g(seed, "push", "-q", "-u", "origin", "candidate")
    box = tmp_path / "box"
    _g(tmp_path, "clone", "-q", str(origin), str(box))
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setattr("coordinator_core._settings_home.settings_home", lambda: home)
    monkeypatch.setattr(
        "coordinator_core.engine_root.coordinator_engine_root",
        lambda: (_ for _ in ()).throw(RuntimeError("no engine root")),
    )
    monkeypatch.setattr(
        apply,
        "apply_freshness",
        functools.partial(apply.apply_freshness, live_runs=lambda: 0, installers={}, hostname="boxy"),
    )
    return {"seed": seed, "box": box, "home": home}


def _resolve_to(monkeypatch, kind, root):
    result = eri.InstallEngineRoot(
        kind=kind, root=root, remediation=None if root else "machine-local set repos.claude_klabauter <path>"
    )
    monkeypatch.setattr(eri, "resolve_engine_root_for_install", lambda: result)


def _handler(params):
    return op._handler(params)


def test_default_params_fast_forward_the_resolved_clone_not_the_claude_klabauter_tree(world, monkeypatch):
    tip = _commit(world["seed"], "docs/note.md", "x\n")
    _g(world["seed"], "push", "-q", "origin", "candidate")
    _resolve_to(monkeypatch, "published", world["box"])

    res = _handler({})

    assert res["action"] == kf.ACTION_FAST_FORWARD
    assert res["moved"] is True
    assert res["origin_sha"] == _g(world["box"], "rev-parse", "@{u}") == tip
    assert _g(world["box"], "rev-parse", "HEAD") == tip


def test_fetch_false_compares_against_the_already_fetched_upstream(world, monkeypatch):
    tip = _commit(world["seed"], "docs/note.md", "x\n")
    _g(world["seed"], "push", "-q", "origin", "candidate")
    _g(world["box"], "fetch", "-q")
    _resolve_to(monkeypatch, "self-rooted", world["box"])

    res = _handler({"fetch": False})

    assert res["action"] == kf.ACTION_FAST_FORWARD
    assert res["moved"] is True
    assert res["origin_sha"] == tip


def test_unfetched_clone_with_fetch_false_stays_current(world, monkeypatch):
    before = _g(world["box"], "rev-parse", "HEAD")
    _commit(world["seed"], "docs/note.md", "x\n")
    _g(world["seed"], "push", "-q", "origin", "candidate")
    _resolve_to(monkeypatch, "published", world["box"])

    res = _handler({"fetch": False})

    assert res["action"] == kf.ACTION_CURRENT
    assert not res["moved"]
    assert _g(world["box"], "rev-parse", "HEAD") == before


def test_kind_none_refuses_naming_the_registry_key_and_never_probes(world, monkeypatch):
    _resolve_to(monkeypatch, "none", None)
    probed = []
    monkeypatch.setattr(apply, "probe", lambda checkout: probed.append(checkout))
    monkeypatch.setattr(apply, "apply_freshness", lambda *a, **k: probed.append("apply"))

    res = _handler({})

    assert "repos.claude_klabauter" in res["reason"]
    assert "machine-local set repos.claude_klabauter" in res["reason"]
    assert res["action"] == kf.ACTION_CURRENT
    assert res["moved"] is False
    assert res["hostname"]
    assert probed == []


def test_explicit_checkout_bypasses_resolution(world, monkeypatch):
    _commit(world["seed"], "docs/note.md", "x\n")
    _g(world["seed"], "push", "-q", "origin", "candidate")
    _resolve_to(monkeypatch, "none", None)

    res = op.run_freshness(checkout=world["box"])

    assert res["action"] == kf.ACTION_FAST_FORWARD
