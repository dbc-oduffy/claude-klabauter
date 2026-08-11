"""test_chunk_commits_forwarder_cwd.py — `coordinator/bin/chunk-commits`
must resolve a relative `plan_path` against the CALLER's cwd, not the op
process's.

Regression coverage for the bug fixed alongside this test: the forwarder
passed `plan_path` through to `ceremony.chunk_commits` verbatim, and the op
resolves a relative path with `Path(plan_path).resolve()` against ITS OWN
process cwd — which, through this forwarder, is the engine root, not the
caller's cwd. A caller in repo B asking about a relative plan path that only
exists in repo A got repo A's answer (or, worse, a *silent* empty list at
exit 0 when the path happened to also parse in the engine's own tree) — see
this file's own commit for the two failure shapes. This test drives the
forwarder's real `main()` in-process (never a spawned interpreter — see
CLAUDE.md's machine-load-norm note) with cwd set to repo B, and asserts a
relative path resolves in repo B, never repo A.

Negative-spec: do NOT collapse this to a single-repo test. The bug is
invisible with only one repo on disk — the forwarder's cwd and the plan's
repo are the same thing until a second repo exists to disambiguate them.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import chunk_commits as chunk_commits_op


# Declared, not excused: this file spawns real processes because the behaviour under
# test IS the spawn. _BASELINE is shrink-only pre-existing residue and is explicitly
# not the route for a new file -- test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]


_CLI_PATH = Path(__file__).resolve().parents[1] / "chunk-commits"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader("chunk_commits_cli", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:  # pragma: no cover — extensionless CLI is always loadable
        raise RuntimeError(f"could not build a module spec for {_CLI_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def _git(args, cwd) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout


def _init_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _write(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _commit(repo: Path, rel_path: str, subject: str) -> str:
    _git(["add", "--", rel_path], repo)
    _git(["commit", "-q", "-m", subject], repo)
    return _git(["rev-parse", "HEAD"], repo).strip()


@pytest.fixture()
def cli_module(monkeypatch, tmp_path):
    """Loads the real `chunk-commits` module and stubs its `route()` import to
    call the real op handler directly (no IPC/subprocess) — but, crucially,
    from a DIFFERENT process cwd than the caller's, exactly as the real op
    process's cwd differs from the caller's through this forwarder in
    production. Without this cwd swap, an in-process test cannot distinguish
    "forwarder absolutized before calling the op" from "the op happened to
    inherit the test process's own cwd" — the bug this file exists to catch
    is invisible unless the two cwds genuinely differ during resolution.

    `engine_root` is a REAL git repo — not an empty tmp dir — that itself
    contains a same-named plan file at the same relative path each test uses
    (`docs/plans/x.md`) with its OWN distinct chunk commit. This is what
    makes the fixture capable of catching the dangerous silent-collision
    shape: a relative `plan_path` that also resolves inside the engine's own
    tree must return the ENGINE's commit at exit 0 when the forwarder's
    absolutization is missing, and the CALLER's commit when it is present —
    an empty `engine_root` can only ever produce the loud
    file-not-found shape, never the silent wrong-answer one.
    """
    module = _load_cli_module()
    engine_root = _init_repo(tmp_path, "engine_root_stand_in")
    _write(engine_root, "docs/plans/x.md", "---\ntitle: x\n---\n")
    _commit(engine_root, "docs/plans/x.md", "docs: add plan x")
    _write(engine_root, "src/engine.py", "def engine(): pass\n")
    engine_chunk_sha = _commit(
        engine_root, "src/engine.py", "C1: implement engine in engine_root"
    )

    def _fake_route(op_name, params, repo_root, legacy_fallback):
        assert op_name == "ceremony.chunk_commits"
        caller_cwd = Path.cwd()
        try:
            os.chdir(engine_root)
            return chunk_commits_op._handler(params)
        except (ValueError, RuntimeError) as exc:
            # Mirrors cc_invoke.route()'s own envelope ladder, which turns any
            # handler-raised exception into a RuntimeError for main()'s catch.
            raise RuntimeError(str(exc)) from exc
        finally:
            os.chdir(caller_cwd)

    monkeypatch.setattr(module, "route", _fake_route)
    return module, engine_chunk_sha


def test_relative_plan_path_resolves_against_callers_cwd_not_repo_a(
    tmp_path, monkeypatch, cli_module, capsys
):
    module, engine_chunk_sha = cli_module

    repo_a = _init_repo(tmp_path, "repo_a")
    _write(repo_a, "docs/plans/x.md", "---\ntitle: x\n---\n")
    _commit(repo_a, "docs/plans/x.md", "docs: add plan x")
    _write(repo_a, "src/foo.py", "def foo(): pass\n")
    a_chunk_sha = _commit(repo_a, "src/foo.py", "C1: implement foo in A")

    repo_b = _init_repo(tmp_path, "repo_b")
    _write(repo_b, "docs/plans/x.md", "---\ntitle: x\n---\n")
    _commit(repo_b, "docs/plans/x.md", "docs: add plan x")
    _write(repo_b, "src/bar.py", "def bar(): pass\n")
    b_chunk_sha = _commit(repo_b, "src/bar.py", "C1: implement bar in B")

    monkeypatch.chdir(repo_b)

    exit_code = module.main(["--json", "docs/plans/x.md", "C1"])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert b_chunk_sha in captured.out
    assert a_chunk_sha not in captured.out
    # The dangerous shape this test exists to pin: `engine_root` (the
    # `cli_module` fixture's stand-in for the op process's own cwd) also
    # contains a plan at this exact relative path with its OWN distinct
    # chunk commit. If the forwarder's absolutization were dropped, the op
    # would resolve against `engine_root` instead of repo_b and answer with
    # the engine's commit at exit 0 — silently, no error raised at all. The
    # engine's sha must never appear in a correctly-absolutized answer.
    assert engine_chunk_sha not in captured.out


def test_relative_plan_path_that_only_exists_in_other_repo_fails_loud_not_silent(
    tmp_path, monkeypatch, cli_module, capsys
):
    module, _engine_chunk_sha = cli_module

    repo_a = _init_repo(tmp_path, "repo_a")
    _write(repo_a, "docs/plans/only-in-a.md", "---\ntitle: a\n---\n")
    _commit(repo_a, "docs/plans/only-in-a.md", "docs: add plan only in a")
    _write(repo_a, "src/foo.py", "def foo(): pass\n")
    _commit(repo_a, "src/foo.py", "C1: implement foo in A")

    repo_b = _init_repo(tmp_path, "repo_b")
    _write(repo_b, "README.md", "seed\n")
    _commit(repo_b, "README.md", "chore: seed repo b")

    monkeypatch.chdir(repo_b)

    exit_code = module.main(["docs/plans/only-in-a.md", "C1"])

    assert exit_code == module._EXIT_BUSINESS_FAIL
    captured = capsys.readouterr()
    assert captured.out == ""
