"""MSYS `/x/` mount operands: the in-process answerer declines on a Windows host."""

from coordinator_core.search import answer as answer_mod

_MSYS = "/" + "e" + "/" + "somewhere/file.txt"


def _cmd(path: str) -> str:
    return 'grep -rn "needle" ' + path


def test_windows_host_declines_msys_operand(monkeypatch):
    monkeypatch.setattr(answer_mod, "_is_windows_host", lambda: True)
    assert answer_mod.plan_for(_cmd(_MSYS)) is None
    assert answer_mod.answer(_cmd(_MSYS)) is None


def test_windows_host_declines_bare_mount_root(monkeypatch):
    monkeypatch.setattr(answer_mod, "_is_windows_host", lambda: True)
    assert answer_mod.plan_for(_cmd("/" + "e")) is None


def test_windows_host_keeps_ordinary_path(monkeypatch, tmp_path):
    monkeypatch.setattr(answer_mod, "_is_windows_host", lambda: True)
    (tmp_path / "f.txt").write_text("needle\n", encoding="utf-8")
    cmd = _cmd(str(tmp_path).replace("\\", "/") + "/f.txt")
    assert answer_mod.plan_for(cmd) is not None


def test_non_windows_host_does_not_decline(monkeypatch):
    monkeypatch.setattr(answer_mod, "_is_windows_host", lambda: False)
    assert answer_mod.plan_for(_cmd(_MSYS)) is not None
