"""general_work_scope: root resolution, session-in-root, foreign checkout."""

from __future__ import annotations

from coordinator_core.session import general_work_scope as gws


def _registry(monkeypatch, values: dict, flat: dict | None = None):
    monkeypatch.setattr(gws.machine_resolver, "registry_get", lambda k: values.get(k))
    monkeypatch.setattr(gws.machine_resolver, "merged_flat_registry", lambda: flat if flat is not None else {})


def test_root_declared(monkeypatch, tmp_path):
    _registry(monkeypatch, {"coordinator.general_work_root": str(tmp_path)})
    assert gws.general_work_root() == str(tmp_path)


def test_root_fallback_key(monkeypatch, tmp_path):
    _registry(monkeypatch, {"repos.general_work": str(tmp_path)})
    assert gws.general_work_root() == str(tmp_path)


def test_root_unset_and_empty(monkeypatch):
    _registry(monkeypatch, {})
    assert gws.general_work_root() is None
    _registry(monkeypatch, {"coordinator.general_work_root": ""})
    assert gws.general_work_root() is None


def test_root_unreadable(monkeypatch, capsys):
    def boom(_k):
        raise RuntimeError("bad registry")

    monkeypatch.setattr(gws.machine_resolver, "registry_get", boom)
    assert gws.general_work_root() is None
    assert "unreadable" in capsys.readouterr().err


def test_session_under_and_outside_root(monkeypatch, tmp_path):
    root = tmp_path / "gw"
    (root / "sub").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    _registry(monkeypatch, {"coordinator.general_work_root": str(root)})
    assert gws.in_general_work_session(str(root))
    assert gws.in_general_work_session(str(root / "sub"))
    assert not gws.in_general_work_session(str(other))
    assert not gws.in_general_work_session(None)
    assert not gws.in_general_work_session("")


def test_session_false_when_root_unset(monkeypatch, tmp_path):
    _registry(monkeypatch, {})
    assert not gws.in_general_work_session(str(tmp_path))


def test_foreign_checkout_none_without_git_ancestor(monkeypatch, tmp_path):
    root = tmp_path / "gw"
    plain = tmp_path / "plain"
    root.mkdir()
    plain.mkdir()
    _registry(monkeypatch, {"coordinator.general_work_root": str(root)})
    assert gws.foreign_checkout(str(plain / "f.txt")) is None


def test_foreign_checkout_none_under_root_holding_git(monkeypatch, tmp_path):
    root = tmp_path / "gw"
    (root / ".git").mkdir(parents=True)
    _registry(monkeypatch, {"coordinator.general_work_root": str(root)}, {"repos.general_work": str(root)})
    assert gws.foreign_checkout(str(root / "a" / "f.txt")) is None


def test_foreign_checkout_returns_sibling_checkout(monkeypatch, tmp_path):
    root = tmp_path / "gw"
    sib = tmp_path / "sib"
    root.mkdir()
    (sib / ".git").mkdir(parents=True)
    _registry(monkeypatch, {"coordinator.general_work_root": str(root)})
    hit = gws.foreign_checkout(str(sib / "src" / "f.py"))
    assert hit is not None and hit.endswith("sib")


def test_foreign_checkout_registry_root_that_is_general_work_is_not_foreign(monkeypatch, tmp_path):
    root = tmp_path / "gw"
    root.mkdir()
    _registry(monkeypatch, {}, {"repos.general_work": str(root)})
    assert gws.foreign_checkout(str(root / "f.txt"), root=str(root)) is None


def test_foreign_checkout_registry_root_other_repo_is_foreign(monkeypatch, tmp_path):
    root = tmp_path / "gw"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    _registry(monkeypatch, {}, {"repos.other": str(other)})
    assert gws.foreign_checkout(str(other / "f.txt"), root=str(root)) == str(other)
