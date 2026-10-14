"""Pins the workweek-complete-doc-staleness trampoline: --doc narrows the registry list before
the history walk, an unregistered --doc spawns no git, --ack is rejected, --root is honoured."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.spawns_process

_SCRIPT = Path(__file__).resolve().parent.parent / "workweek-complete-doc-staleness.py"


def _load_trampoline():
    spec = importlib.util.spec_from_file_location("workweek_complete_doc_staleness", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "coordinator.local.md").write_text(
        "---\nhuman_facing_docs: [README.md, GUIDE.md]\n"
        "doc_staleness_commits: 1\ndoc_staleness_days: 0\n---\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# readme\n\nfirst\n", encoding="utf-8")
    (root / "GUIDE.md").write_text("# guide\n\nfirst\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "docs")
    (root / "code.txt").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "code")
    return root


@pytest.fixture
def recorded(monkeypatch):
    import coordinator_core.ops.doc_staleness as op

    calls: list[list[str]] = []

    def fake(root, doc_paths, threshold_commits, threshold_days, **kw):
        calls.append(list(doc_paths))
        return {"docs": []}

    monkeypatch.setattr(op, "build_doc_staleness_report", fake)
    return calls


def _run(mod, argv, capsys) -> dict:
    assert mod.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_doc_filter_passes_only_the_named_doc_to_the_walk(repo, recorded, capsys):
    mod = _load_trampoline()
    _run(mod, ["--root", str(repo), "--doc", "README.md"], capsys)
    assert recorded == [["README.md"]]


def test_no_doc_flag_passes_the_whole_registry(repo, recorded, capsys):
    mod = _load_trampoline()
    _run(mod, ["--root", str(repo)], capsys)
    assert recorded == [["README.md", "GUIDE.md"]]


def test_doc_filter_prints_one_entry_on_a_real_repo(repo, capsys):
    mod = _load_trampoline()
    out = _run(mod, ["--root", str(repo), "--doc", "README.md"], capsys)
    assert [e["path"] for e in out["docs"]] == ["README.md"]
    assert out["docs"][0]["status"] == "ok"


def test_unregistered_doc_prints_empty_and_spawns_no_git(repo, monkeypatch, capsys):
    mod = _load_trampoline()

    def boom(*a, **k):
        raise AssertionError("git spawned for an unregistered doc")

    import coordinator_core.ops.doc_staleness as op

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(op, "_newest_touches", boom)
    monkeypatch.setattr(op, "_commits_since", boom)
    assert mod.main(["--root", str(repo), "--doc", "NOPE.md"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"docs": []}
    assert "not a registered doc" in captured.err


def test_doc_path_spelling_is_normalised(repo, recorded, capsys):
    mod = _load_trampoline()
    _run(mod, ["--root", str(repo), "--doc", ".\\README.md"], capsys)
    assert recorded == [["README.md"]]


def test_ack_flag_is_rejected_by_the_parser():
    mod = _load_trampoline()
    with pytest.raises(SystemExit) as exc:
        mod._build_parser().parse_args(["--ack"])
    assert exc.value.code == 2


def test_root_is_honoured_from_a_different_cwd(repo, tmp_path, monkeypatch, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    mod = _load_trampoline()
    out = _run(mod, ["--root", str(repo)], capsys)
    assert sorted(e["path"] for e in out["docs"]) == ["GUIDE.md", "README.md"]
