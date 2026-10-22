from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.install import runtime_ignore_apply as mod
from coordinator_core.install.runtime_ignore_apply import HEADER, apply_runtime_ignore

pytestmark = pytest.mark.spawns_process

TEMPLATE = "# managed\n.coordinator-local/\nstate/watch.json\nscratch/\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _write(root: Path, rel: str, text: str = "x\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    return root


@pytest.fixture()
def template(tmp_path: Path) -> Path:
    p = tmp_path / "tmpl"
    p.write_text(TEMPLATE, encoding="utf-8")
    return p


def _tracked(root: Path) -> set[str]:
    return set(_git(root, "ls-files").split())


def test_appends_only_missing_and_second_run_is_noop(repo, template):
    _write(repo, ".gitignore", "scratch/\n")
    res = apply_runtime_ignore(repo, template)
    assert res.appended == (".coordinator-local/", "state/watch.json")
    text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert text.count(HEADER) == 1
    assert text.count("scratch/") == 1
    again = apply_runtime_ignore(repo, template)
    assert again.appended == () and again.untracked == ()
    assert (repo / ".gitignore").read_text(encoding="utf-8") == text


def test_creates_gitignore_when_absent(repo, template):
    apply_runtime_ignore(repo, template)
    text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith(HEADER)
    assert "state/watch.json" in text


def test_crlf_preserved(repo, template):
    (repo / ".gitignore").write_bytes(b"build/\r\n")
    apply_runtime_ignore(repo, template)
    raw = (repo / ".gitignore").read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_untracks_but_keeps_on_disk_and_exempts_memo_outbox(repo, template):
    _write(repo, "state/watch.json")
    _write(repo, ".coordinator-local/dispatch-briefs/a.md")
    _write(repo, ".coordinator-local/memo-outbox/sent/m.md")
    _write(repo, "keep.txt")
    _git(repo, "add", "-f", "-A")
    res = apply_runtime_ignore(repo, template)
    assert ".coordinator-local/dispatch-briefs/a.md" in res.untracked
    assert "state/watch.json" in res.untracked
    assert not any("memo-outbox" in p for p in res.untracked)
    tracked = _tracked(repo)
    assert ".coordinator-local/memo-outbox/sent/m.md" in tracked
    assert "keep.txt" in tracked
    assert "state/watch.json" not in tracked
    assert (repo / "state/watch.json").exists()
    assert (repo / ".coordinator-local/dispatch-briefs/a.md").exists()


def test_template_none_is_skipped(repo, monkeypatch):
    monkeypatch.setattr(mod, "resolve_template_path", lambda: None)
    res = apply_runtime_ignore(repo)
    assert res.skipped and res.appended == () and res.untracked == ()
    assert not (repo / ".gitignore").exists()


def test_one_git_spawn_when_tracked(repo, template, monkeypatch):
    _write(repo, "state/watch.json")
    _write(repo, "scratch/a.txt")
    _git(repo, "add", "-f", "-A")
    calls = []
    real = mod.run_git

    def spy(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(mod, "run_git", spy)
    apply_runtime_ignore(repo, template)
    assert len(calls) == 1
    assert "rm" in calls[0] and "--pathspec-from-file=-" in calls[0]


def test_no_git_spawn_when_nothing_tracked(repo, template, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "run_git", lambda *a, **k: calls.append(a))
    apply_runtime_ignore(repo, template)
    assert calls == []


def test_nonzero_untrack_raises_with_stderr(repo, template, monkeypatch):
    _write(repo, "state/watch.json")
    _git(repo, "add", "-f", "-A")
    monkeypatch.setattr(
        mod,
        "run_git",
        lambda *a, **k: type("R", (), {"returncode": 1, "stderr": "boom\n"})(),
    )
    with pytest.raises(RuntimeError, match="boom"):
        apply_runtime_ignore(repo, template)
