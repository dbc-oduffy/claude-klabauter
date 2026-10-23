"""`_cmd_round` refuses before staging anything when the source HEAD is not
pushed. Real gate (REAL_SOURCE_PREFLIGHT opts out of conftest's
stub), real git source, local bare origin; tmp_path only."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REAL_SOURCE_PREFLIGHT = True

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("percolate_round_source_preflight", _BIN_DIR / "percolate-round.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags()
    )


def _source_with_origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    repo = tmp_path / "source"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    _git(["add", "--", "a.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/wiring"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", "work/wiring"], repo)
    return repo


def _run(monkeypatch, tmp_path, source: Path, *extra: str):
    reached = []
    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda override: str(tmp_path / "root"))
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(tmp_path / "dest"))
    monkeypatch.setattr(_mod, "_cmd_round_default", lambda *a, **k: reached.append(a) or 0)
    args = _mod._build_parser().parse_args(["alpha", "--percolate-root", str(tmp_path / "root"), *extra])
    return _mod._cmd_round(args), reached


def test_unpushable_source_refuses_before_the_round(monkeypatch, tmp_path, capsys):
    source = _source_with_origin(tmp_path)
    _git(["checkout", "-q", "-b", "work/no-upstream"], source)

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert "has no upstream" in capsys.readouterr().err


def test_pushed_source_reaches_the_round(monkeypatch, tmp_path):
    source = _source_with_origin(tmp_path)

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == 0
    assert len(reached) == 1


def _stamp_mirror_with_foreign_head(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    _git(["init", "-q"], dest)
    _git(["config", "user.email", "t@t.example"], dest)
    _git(["config", "user.name", "t"], dest)
    (dest / "f.txt").write_text("x", encoding="utf-8")
    _git(["add", "--", "f.txt"], dest)
    _git(["commit", "-q", "-m", "publish [source-head deadbeefcafe]"], dest)


def test_source_lacking_the_mirror_stamp_refuses_before_the_round(monkeypatch, tmp_path, capsys):
    source = _source_with_origin(tmp_path)
    _stamp_mirror_with_foreign_head(tmp_path)

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert "deadbeefcafe" in capsys.readouterr().err


def test_allow_source_rollback_flag_reaches_the_round(monkeypatch, tmp_path):
    source = _source_with_origin(tmp_path)
    _stamp_mirror_with_foreign_head(tmp_path)

    rc, reached = _run(monkeypatch, tmp_path, source, "--allow-source-rollback")

    assert rc == 0
    assert len(reached) == 1


def _out(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


def _shared_and_clone(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    _git(["init", "-q"], shared)
    _git(["config", "user.email", "t@t.example"], shared)
    _git(["config", "user.name", "t"], shared)
    (shared / "a.py").write_text("a = 1\n", encoding="utf-8")
    _git(["add", "--", "a.py"], shared)
    _git(["commit", "-q", "-m", "seed"], shared)
    _git(["branch", "-m", "work/shared"], shared)
    clone = tmp_path / "clone"
    _git(["clone", "-q", str(shared), str(clone)], tmp_path)
    _git(["config", "user.email", "t@t.example"], clone)
    _git(["config", "user.name", "t"], clone)
    _git(["checkout", "-q", "-b", "round1-merge"], clone)
    (clone / "b.py").write_text("b = 1\n", encoding="utf-8")
    _git(["add", "--", "b.py"], clone)
    _git(["commit", "-q", "-m", "merge"], clone)
    return shared, clone, _out(["rev-parse", "HEAD"], clone)


def test_private_clone_commit_on_candidate_source_ref_reaches_the_round(monkeypatch, tmp_path):
    shared, clone, sha = _shared_and_clone(tmp_path)
    _git(["config", "receive.denyCurrentBranch", "ignore"], shared)
    _git(["push", "-q", "origin", f"{sha}:refs/heads/candidate-source"], clone)
    before = _out(["rev-parse", "work/shared"], shared)

    rc, reached = _run(monkeypatch, tmp_path, clone, "--source-ref", sha)

    assert rc == 0
    assert len(reached) == 1
    assert _out(["rev-parse", "work/shared"], shared) == before
    assert _out(["symbolic-ref", "HEAD"], shared) == "refs/heads/work/shared"
    assert subprocess.run(
        ["git", "config", "branch.round1-merge.remote"], cwd=str(clone), capture_output=True, text=True,
        **no_console_creationflags(),
    ).returncode != 0
    _git(["cat-file", "-e", f"{sha}^{{commit}}"], shared)


def test_unpublished_source_ref_refuses_before_the_round(monkeypatch, tmp_path, capsys):
    _shared, clone, sha = _shared_and_clone(tmp_path)

    rc, reached = _run(monkeypatch, tmp_path, clone, "--source-ref", sha)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert "candidate-source" in capsys.readouterr().err


def test_source_ref_lacking_the_mirror_stamp_refuses(monkeypatch, tmp_path, capsys):
    shared, clone, sha = _shared_and_clone(tmp_path)
    _git(["config", "receive.denyCurrentBranch", "ignore"], shared)
    _git(["push", "-q", "origin", f"{sha}:refs/heads/candidate-source"], clone)
    _stamp_mirror_with_foreign_head(tmp_path)

    rc, reached = _run(monkeypatch, tmp_path, clone, "--source-ref", sha)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert "deadbeefcafe" in capsys.readouterr().err
