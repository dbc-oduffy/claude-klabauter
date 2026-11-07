"""Apply-path tests for klabauter freshness over tmp_path bare-origin + clone git fixtures."""

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.install import klabauter_freshness as kf
from coordinator_core.install import klabauter_freshness_apply as apply

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
def world(tmp_path):
    origin = tmp_path / "origin.git"
    _g(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _g(tmp_path, "clone", "-q", str(origin), str(seed))
    _g(seed, "checkout", "-q", "-b", "main")
    _commit(seed, "README.md", "one\n")
    _commit(seed, "coordinator/bin/unimatrix-relay.py", "v1\n")
    _g(seed, "push", "-q", "-u", "origin", "main")
    box = tmp_path / "box"
    _g(tmp_path, "clone", "-q", str(origin), str(box))
    home = tmp_path / "home"
    home.mkdir()
    return {"seed": seed, "box": box, "home": home}


class _Spy:
    def __init__(self):
        self.calls = []

    def runner(self, name):
        def run(checkout, home, engine_root):
            self.calls.append(name)

        return run

    @property
    def table(self):
        return {n: self.runner(n) for n in (kf.INSTALLER_UNIMATRIX, kf.INSTALLER_BIN_SHIMS, kf.INSTALLER_CURRENCY_CACHE)}


def _run(world, spy, live=0, **kw):
    return apply.apply_freshness(
        world["box"],
        world["home"],
        hostname="boxy",
        live_runs=lambda: live,
        installers=spy.table,
        **kw,
    )


def _push_unimatrix_change(world):
    sha = _commit(world["seed"], "coordinator/bin/unimatrix-relay.py", "v2\n")
    _g(world["seed"], "push", "-q", "origin", "main")
    return sha


def test_ac1_behind_clean_fast_forwards_and_runs_mapped_installers(world):
    tip = _push_unimatrix_change(world)
    spy = _Spy()
    res = _run(world, spy)
    assert res.action == kf.ACTION_FAST_FORWARD
    assert res.moved and res.local_sha == tip
    assert _g(world["box"], "rev-parse", "HEAD") == tip
    assert res.changed_paths == ["coordinator/bin/unimatrix-relay.py"]
    assert spy.calls == [kf.INSTALLER_UNIMATRIX, kf.INSTALLER_CURRENCY_CACHE]


def test_ac1_unrelated_change_skips_unimatrix_installer(world):
    _commit(world["seed"], "docs/note.md", "x\n")
    _g(world["seed"], "push", "-q", "origin", "main")
    spy = _Spy()
    res = _run(world, spy)
    assert res.moved
    assert spy.calls == []


def test_ac2_live_run_defers_without_moving_ref(world):
    _push_unimatrix_change(world)
    before = _g(world["box"], "rev-parse", "HEAD")
    spy = _Spy()
    res = _run(world, spy, live=1)
    assert res.action == kf.ACTION_DEFERRED_LIVE_RUN
    assert not res.moved
    assert _g(world["box"], "rev-parse", "HEAD") == before
    assert spy.calls == []


def test_ac3_dirty_tree_surfaces_once_without_moving(world):
    tip = _push_unimatrix_change(world)
    before = _g(world["box"], "rev-parse", "HEAD")
    (world["box"] / "README.md").write_text("local edit\n", encoding="utf-8")
    spy = _Spy()
    first = _run(world, spy)
    assert first.action == kf.ACTION_SURFACE_DIRTY
    assert first.surfaced and not first.moved
    assert _g(world["box"], "rev-parse", "HEAD") == before
    assert read_key(world) == kf.SurfacedKey("boxy", before, tip, kf.STATE_DIRTY)
    second = _run(world, spy)
    assert second.action == kf.ACTION_SURFACE_DIRTY
    assert not second.surfaced


def test_ac3_diverged_tree_surfaces_once_without_moving(world):
    tip = _push_unimatrix_change(world)
    local = _commit(world["box"], "local.txt", "mine\n")
    spy = _Spy()
    first = _run(world, spy)
    assert first.action == kf.ACTION_SURFACE_DIVERGED
    assert first.surfaced and not first.moved
    assert _g(world["box"], "rev-parse", "HEAD") == local
    assert read_key(world) == kf.SurfacedKey("boxy", local, tip, kf.STATE_DIVERGED)
    assert not _run(world, spy).surfaced


def test_surface_record_cleared_when_tree_recovers(world):
    _push_unimatrix_change(world)
    readme = world["box"] / "README.md"
    readme.write_text("local edit\n", encoding="utf-8")
    spy = _Spy()
    assert _run(world, spy).surfaced
    _g(world["box"], "checkout", "--", "README.md")
    assert _run(world, spy).action == kf.ACTION_FAST_FORWARD
    assert apply.read_record(world["home"]) is None


def test_ac6_current_checkout_with_stale_installed_copy_reinstalls_without_ref_move(world):
    box, home = world["box"], world["home"]
    for name in ("unimatrix-bridge.py", "unimatrix-relay.py"):
        _commit(box, f"coordinator/bin/{name}", f"src {name}\n")
    _g(box, "push", "-q", "origin", "main")
    installer = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "unimatrix-install.py"
    (box / "coordinator" / "bin" / "unimatrix-install.py").write_text(
        installer.read_text(encoding="utf-8"), encoding="utf-8"
    )
    dest = home / apply.UNIMATRIX_DEST_DIRNAME
    dest.mkdir()
    (dest / "unimatrix-relay.py").write_text("stale\n", encoding="utf-8")
    before = _g(box, "rev-parse", "HEAD")
    calls = []
    res = apply.apply_freshness(
        box,
        home,
        hostname="boxy",
        live_runs=lambda: 0,
        installers={kf.INSTALLER_UNIMATRIX: lambda *a: calls.append("u")},
    )
    assert res.action == kf.ACTION_CURRENT and not res.moved
    assert _g(box, "rev-parse", "HEAD") == before
    assert "unimatrix-relay.py" in res.stale_installs
    assert calls == ["u"]


def test_stale_copy_not_reinstalled_during_live_run_deferral(world):
    _push_unimatrix_change(world)
    spy = _Spy()
    res = _run(world, spy, live=2)
    assert res.stale_installs == [] and spy.calls == []


def test_count_live_runs_sums_repos_and_treats_unreadable_as_live(tmp_path, monkeypatch):
    from coordinator_core.ops.workflow_fire import fire

    a, b, c = (tmp_path / n for n in "abc")
    for r in (a, b, c):
        (r / ".git").mkdir(parents=True)
    counts = {str(a): 2, str(b): 1}

    def fake(cwd=None):
        if cwd == str(c):
            raise RuntimeError("unreadable")
        return counts[cwd]

    monkeypatch.setattr(fire, "count_live_fires", fake)
    roots = {"a": a, "b": b, "gone": tmp_path / "gone"}
    assert apply.count_live_runs(roots) == 3
    assert apply.count_live_runs({**roots, "c": c}) == 4


def test_count_live_runs_unreadable_registry_defers(monkeypatch):
    from coordinator_core.ops.fleet import _memo_resolver

    def boom():
        raise _memo_resolver.RegistryReadError("bad toml")

    monkeypatch.setattr(_memo_resolver, "read_registry_repos", boom)
    assert apply.count_live_runs() >= 1


def test_fetch_failure_is_reported_and_probe_still_runs(world):
    _g(world["box"], "remote", "set-url", "origin", str(world["box"].parent / "missing.git"))
    res = _run(world, _Spy())
    assert res.fetch_error
    assert res.action == kf.ACTION_CURRENT


def test_non_checkout_reports_without_raising(tmp_path):
    res = apply.apply_freshness(tmp_path, tmp_path, hostname="boxy", live_runs=lambda: 0, installers={})
    assert res.action == kf.ACTION_CURRENT and res.reason


def read_key(world):
    return kf.key_from_record(apply.read_record(world["home"]))
