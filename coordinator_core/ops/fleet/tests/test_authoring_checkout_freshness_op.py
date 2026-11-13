"""`fleet.authoring_checkout_freshness` reports each authoring checkout's lag and ff's a safe one."""

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import _memo_resolver
from coordinator_core.ops.fleet import authoring_checkout_freshness as op
from coordinator_core.install import authoring_checkout_freshness_apply as apply

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
    _g(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _g(tmp_path, "clone", "-q", str(origin), str(seed))
    _g(seed, "checkout", "-q", "-b", "main")
    _commit(seed, "README.md", "one\n")
    _commit(seed, "other.md", "o\n")
    _g(seed, "push", "-q", "-u", "origin", "main")

    def clone(name):
        dest = tmp_path / name
        _g(tmp_path, "clone", "-q", str(origin), str(dest))
        _g(dest, "checkout", "-q", "-b", "work/day")
        return dest

    behind, dirty, mirror = clone("behind"), clone("dirty"), clone("mirror")
    _commit(seed, "README.md", "two\n")
    _g(seed, "push", "-q", "origin", "main")
    for repo in (behind, dirty, mirror):
        _g(repo, "fetch", "-q", "origin", "main")
    (dirty / "README.md").write_text("local edit\n", encoding="utf-8")

    registry = {
        "repos.claude-klabauter": str(behind),
        "repos.doe": str(dirty),
        "repos.claude_klabauter": str(mirror),
    }
    monkeypatch.setattr(_memo_resolver, "read_registry_repos", lambda: registry)
    monkeypatch.setattr(apply, "_live_run_ids", lambda key, root: ())
    return {"behind": behind, "dirty": dirty, "mirror": mirror, "seed": seed}


def test_default_call_reports_rows_ff_s_clean_and_names_dirty_overlap(world):
    out = op._handler({})
    rows = {r["key"]: r for r in out["rows"]}
    assert set(rows) == {"repos.claude-klabauter", "repos.doe"}

    clean = rows["repos.claude-klabauter"]
    assert clean["action"] == "fast_forward" and clean["moved"] is True
    assert clean["branch"] == "work/day" and clean["behind"] == 1 and clean["ahead"] == 0
    assert _g(world["behind"], "rev-parse", "HEAD") == _g(world["behind"], "rev-parse", "origin/main")

    blocked = rows["repos.doe"]
    assert blocked["action"] == "blocked_dirty_overlap" and blocked["moved"] is False
    assert _g(world["dirty"], "rev-parse", "HEAD") != _g(world["dirty"], "rev-parse", "origin/main")
    assert any("README.md" in line for line in out["advisories"])
    assert not any("repos.claude-klabauter" in line for line in out["advisories"])


def test_klabauter_mirror_is_never_probed(world, monkeypatch):
    probed = []
    real = apply.apply_checkout
    monkeypatch.setattr(apply, "apply_checkout", lambda key, root, **kw: probed.append(key) or real(key, root, **kw))
    op._handler({})
    assert "repos.claude_klabauter" not in probed


def test_unreadable_registry_is_an_advisory_not_a_raise(monkeypatch):
    def boom():
        raise _memo_resolver.RegistryReadError("bad toml")

    monkeypatch.setattr(_memo_resolver, "read_registry_repos", boom)
    out = op._handler({})
    assert out["rows"] == []
    assert "bad toml" in out["reason"] and out["advisories"]


def test_fetch_param_is_passed_and_validated(monkeypatch):
    seen = []
    monkeypatch.setattr(_memo_resolver, "read_registry_repos", lambda: {})
    monkeypatch.setattr(apply, "apply_all", lambda reg, do_fetch=False: seen.append(do_fetch) or {"rows": [], "advisories": []})
    op._handler({})
    op._handler({"fetch": True})
    assert seen == [False, True]
    with pytest.raises(ValueError):
        op._handler({"fetch": "yes"})
