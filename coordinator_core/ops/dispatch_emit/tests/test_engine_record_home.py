from pathlib import Path

from coordinator_core import _settings_home
from coordinator_core.ops.dispatch_emit import engine_record_home as erh


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def test_none_when_no_common_dir(tmp_path):
    assert erh.engine_record_dir(tmp_path) is None
    assert erh.legacy_record_dir(tmp_path) is None


def test_under_machine_local_and_not_created(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: home / "machine-local")
    root = _repo(tmp_path)
    got = erh.engine_record_dir(root)
    assert got is not None
    assert got.parent == home / "machine-local" / "engine-records"
    assert len(got.name) == 16
    assert not got.exists() and not home.exists()


def test_legacy_is_git_dir_and_distinct_repos_differ(tmp_path, monkeypatch):
    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: tmp_path / "ml")
    a = _repo(tmp_path)
    b = tmp_path / "other"
    (b / ".git").mkdir(parents=True)
    assert erh.legacy_record_dir(a) == a / ".git"
    assert erh.engine_record_dir(a) != erh.engine_record_dir(b)
    assert erh.engine_record_dir(a) == erh.engine_record_dir(a)


def test_linked_worktree_shares_main_key(tmp_path, monkeypatch):
    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: tmp_path / "ml")
    main = _repo(tmp_path)
    wt_git = main / ".git" / "worktrees" / "w1"
    wt_git.mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    assert erh.engine_record_dir(wt) == erh.engine_record_dir(main)
