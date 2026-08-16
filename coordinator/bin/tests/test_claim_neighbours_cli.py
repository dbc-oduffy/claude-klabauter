"""Tests for coordinator/bin/claim-neighbours.

Plan: docs/plans/2026-08-16-trace-a-claim-back-to-its-session.md, chunk C4.

Loads the extensionless CLI via SourceFileLoader (same convention as
test_chunk_commits_forwarder_cwd.py) and drives its real `main()` in
process — never a spawned interpreter (CLAUDE.md's machine-load-norm
note). Each test builds a REAL git repo under `tmp_path` and writes
`touched.txt`/claim-marker files under that repo's own
`.git/coordinator-sessions/` (what `core.sessions_dir()` resolves to for a
non-worktree repo) rather than passing a synthetic `sessions_dir=` override
— this CLI's `main()` takes no such override (bare paths + the caller's
real cwd is its whole contract), so the fixture must be a real repo for
`core.sessions_dir(cwd)` to resolve at all.

Liveness and (for the raising-lookup case) `claim_index.lookup` are
monkeypatched on the REAL `coordinator_core.session.liveness` /
`coordinator_core.session.claim_index` module objects — the CLI re-imports
them by name on every `main()` call, from the same `sys.modules` cache, so
patching the real module object is what the CLI's own local import picks
up.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.session import claim_index as real_claim_index
from coordinator_core.session import liveness as real_liveness

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_CLI_PATH = Path(__file__).resolve().parents[1] / "claim-neighbours"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader("claim_neighbours_cli", str(_CLI_PATH))
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
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "chore: seed"], repo)
    return repo


def _sessions_dir(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _touch_line(verb: str, path: str, when: str = "2026-08-16T10:00:00.000000Z") -> str:
    return f"{verb} {when} {path}"


def _write_touched(repo: Path, sid: str, lines: list) -> None:
    _write(_sessions_dir(repo) / sid / "touched.txt", "\n".join(lines) + "\n")


def _write_plan_claim(repo: Path, sid: str, slug: str) -> None:
    _write(_sessions_dir(repo) / "plan-claims" / slug / "session_id", sid + "\n")


@pytest.fixture()
def cli_module():
    return _load_cli_module()


def test_path_with_live_peer_claimant_is_named(cli_module, tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path, "repo")
    _write_touched(repo, "peer-sid", [_touch_line("T", "coordinator_core/session/claims.py")])
    monkeypatch.setattr(real_liveness, "session_live", lambda sid, cwd=None: sid == "peer-sid")
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.chdir(repo)

    exit_code = cli_module.main(["coordinator_core/session/claims.py"])

    assert exit_code == cli_module._EXIT_OK
    out = capsys.readouterr().out
    assert "peer-sid" in out
    assert "coordinator_core/session/claims.py" in out


def test_path_with_no_claimant_is_printed_distinctly(cli_module, tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path, "repo")
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.chdir(repo)

    exit_code = cli_module.main(["some/untouched/path.py"])

    assert exit_code == cli_module._EXIT_OK
    out = capsys.readouterr().out
    assert "no claimant" in out
    assert "some/untouched/path.py" in out
    # "checked, nobody" must never render identically to "could not answer".
    assert "cannot determine" not in out


def test_dead_claimant_excluded(cli_module, tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path, "repo")
    _write_touched(repo, "dead-sid", [_touch_line("T", "some/file.py")])
    monkeypatch.setattr(real_liveness, "session_live", lambda sid, cwd=None: False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.chdir(repo)

    exit_code = cli_module.main(["some/file.py"])

    assert exit_code == cli_module._EXIT_OK
    out = capsys.readouterr().out
    assert "dead-sid" not in out
    assert "no claimant" in out
    assert "some/file.py" in out


def test_callers_own_claim_excluded(cli_module, tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path, "repo")
    _write_touched(repo, "my-own-sid", [_touch_line("T", "some/file.py")])
    monkeypatch.setattr(real_liveness, "session_live", lambda sid, cwd=None: True)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "my-own-sid")
    monkeypatch.chdir(repo)

    exit_code = cli_module.main(["some/file.py"])

    assert exit_code == cli_module._EXIT_OK
    out = capsys.readouterr().out
    assert "my-own-sid" not in out
    assert "no claimant" in out


def test_neighbour_carries_peers_claimed_artifact(cli_module, tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path, "repo")
    _write_touched(repo, "peer-sid", [_touch_line("T", "some/file.py")])
    _write_plan_claim(repo, "peer-sid", "peers-own-plan")
    monkeypatch.setattr(real_liveness, "session_live", lambda sid, cwd=None: sid == "peer-sid")
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.chdir(repo)

    exit_code = cli_module.main(["some/file.py"])

    assert exit_code == cli_module._EXIT_OK
    out = capsys.readouterr().out
    assert "docs/plans/peers-own-plan.md" in out


def test_neighbour_with_no_claimed_artifact_says_so(cli_module, tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path, "repo")
    _write_touched(repo, "peer-sid", [_touch_line("T", "some/file.py")])
    monkeypatch.setattr(real_liveness, "session_live", lambda sid, cwd=None: sid == "peer-sid")
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.chdir(repo)

    exit_code = cli_module.main(["some/file.py"])

    assert exit_code == cli_module._EXIT_OK
    out = capsys.readouterr().out
    assert "no claimed artifact" in out


def test_raising_claim_index_lookup_is_unanswerable_not_no_claimant(
    cli_module, tmp_path, monkeypatch, capsys
):
    repo = _init_repo(tmp_path, "repo")
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.chdir(repo)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated claim_index failure")

    monkeypatch.setattr(real_claim_index, "lookup", _boom)

    exit_code = cli_module.main(["some/file.py"])

    assert exit_code == cli_module._EXIT_OK
    out = capsys.readouterr().out
    assert "cannot determine" in out
    assert "no claimant" not in out


def test_no_paths_is_a_usage_error(cli_module, capsys):
    exit_code = cli_module.main([])

    assert exit_code == cli_module._EXIT_USAGE
    err = capsys.readouterr().err
    assert "usage" in err.lower()


def test_multiple_paths_dedupe_across_separator_dialects(cli_module, tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path, "repo")
    _write_touched(repo, "peer-sid", [_touch_line("T", "some/file.py")])
    monkeypatch.setattr(real_liveness, "session_live", lambda sid, cwd=None: sid == "peer-sid")
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.chdir(repo)

    exit_code = cli_module.main(["some/file.py", "some\\file.py"])

    assert exit_code == cli_module._EXIT_OK
    out = capsys.readouterr().out
    # The overlap for peer-sid must be reported once, not twice.
    assert out.count("peer-sid") == 1
