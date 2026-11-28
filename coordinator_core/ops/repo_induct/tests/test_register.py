import pytest

from coordinator_core import machine_resolver
from coordinator_core.ops.fleet import _memo_resolver
from coordinator_core.ops.repo_induct import _register

KEY = "repos.fifa_main"
EM_ID = "fifa-main-em"


@pytest.fixture
def box(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    (settings / "machine-local").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    for var in ("MACHINE_LOCAL_REGISTRY_DIR", "MACHINE_LOCAL_IMPL", "MACHINE_LOCAL_REPOS_FIFA_MAIN"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "work" / "fifa_main"
    (root / ".git").mkdir(parents=True)
    return tmp_path, settings, root


def _statuses(steps):
    return [(s["step"], s["status"]) for s in steps]


def test_set_readback_and_inbox_resolves(box):
    _, _, root = box
    steps = _register.register_and_route(str(root), KEY)
    assert _statuses(steps) == [("register_repo_key", "done"), ("receiver_inbox", "done")]
    assert machine_resolver.registry_get(KEY) == str(root)
    inbox, repo, _ = _memo_resolver.resolve_receiver_inbox(machine_resolver.repo_key_to_em_id(KEY))
    assert inbox == root / "state" / "cross-repo" / "inbox"
    assert inbox.is_dir() and repo == root


def test_second_run_performs_nothing(box):
    _, _, root = box
    _register.register_and_route(str(root), KEY)
    again = _register.register_and_route(str(root), KEY)
    assert _statuses(again) == [("register_repo_key", "skipped"), ("receiver_inbox", "skipped")]
    assert all(s["changed"] == [] for s in again)


def test_key_holding_other_root_is_not_overwritten(box):
    tmp_path, _, root = box
    other = tmp_path / "work" / "other"
    (other / ".git").mkdir(parents=True)
    machine_resolver.registry_set(KEY, str(other))
    steps = _register.register_and_route(str(root), KEY)
    assert _statuses(steps) == [("register_repo_key", "failed"), ("receiver_inbox", "failed")]
    assert machine_resolver.registry_get(KEY) == str(other)
    assert not (root / "state").exists()


def test_root_inside_external_workspace_fails_without_writing(box):
    _, settings, root = box
    (settings / "machine-local" / "registry.toml").write_text(
        f'[external_workspaces.p4_x]\npath = "{root.parent.as_posix()}"\n', encoding="utf-8"
    )
    steps = _register.register_and_route(str(root), KEY)
    assert steps[0]["status"] == "failed" and "external workspace" in steps[0]["detail"]
    assert machine_resolver.registry_get(KEY) is None


def test_external_workspace_after_registration_fails_receiver_step(box):
    _, settings, root = box
    machine_resolver.registry_set(KEY, str(root))
    with open(settings / "machine-local" / "registry.toml", "a", encoding="utf-8") as fh:
        fh.write(f'\n[external_workspaces.p4_x]\npath = "{root.parent.as_posix()}"\n')
    step = _register.ensure_receiver_inbox(str(root), KEY)
    assert step["status"] == "failed" and "ExternalWorkspaceReceiverError" in step["detail"]


def test_root_without_git_fails_receiver_step(box):
    tmp_path, _, _ = box
    bare = tmp_path / "work" / "no_git"
    bare.mkdir(parents=True)
    steps = _register.register_and_route(str(bare), "repos.no_git")
    assert steps[0]["status"] == "done"
    assert steps[1]["status"] == "failed" and "NonGitReceiverError" in steps[1]["detail"]


def test_missing_root_fails_without_writing(box):
    tmp_path, _, _ = box
    step = _register.register_repo_key(str(tmp_path / "nope"), KEY)
    assert step["status"] == "failed"
    assert machine_resolver.registry_get(KEY) is None


def test_readback_mismatch_fails_loud(box, monkeypatch):
    _, _, root = box
    written = []
    monkeypatch.setattr(machine_resolver, "registry_set", lambda key, value: written.append(value))
    reads = iter([None, "elsewhere"])
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: next(reads))
    step = _register.register_repo_key(str(root), KEY)
    assert written and step["status"] == "failed" and "read-back" in step["detail"]
