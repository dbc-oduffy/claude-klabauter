"""The round runs publish's commit import-name and module-attr checks on the pinned sha before
staging, and refuses naming each violation before publish.py is ever spawned."""
from __future__ import annotations

import importlib.util
import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("percolate_round_commit_gates", _BIN_DIR / "percolate-round.py")
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path, consumer: str) -> tuple[Path, str]:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    _write(src, "coordinator_core/__init__.py", "")
    _write(src, "coordinator_core/provider.py", "def helper():\n    return 1\n")
    _write(src, "coordinator_core/consumer.py", consumer)
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "pinned")
    return src, _git(src, "rev-parse", "HEAD")


def test_a_clean_sha_prints_the_verdict_and_is_allowed(tmp_path):
    src, sha = _repo(tmp_path, "from coordinator_core import provider\n\nprovider.helper()\n")
    out = io.StringIO()

    assert _mod._commit_gates_preflight_refusal(str(src), sha, out=out) is None
    verdict = out.getvalue()
    assert f"commit gates preflight @ {sha[:12]}" in verdict
    assert "import-name 0 violation(s)" in verdict and "module-attr 0 violation(s)" in verdict


def test_a_planted_module_attr_violation_is_refused_by_name(tmp_path):
    src, sha = _repo(tmp_path, "from coordinator_core import provider\n\nprovider.missing_attr()\n")

    refusal = _mod._commit_gates_preflight_refusal(str(src), sha, out=io.StringIO())

    assert refusal and "module-attr" in refusal
    assert "coordinator_core/consumer.py" in refusal and "missing_attr" in refusal


def test_a_planted_from_import_violation_is_refused_by_name(tmp_path):
    src, sha = _repo(tmp_path, "from coordinator_core.provider import missing_name\n")

    refusal = _mod._commit_gates_preflight_refusal(str(src), sha, out=io.StringIO())

    assert refusal and "from-import" in refusal and "missing_name" in refusal


def test_a_name_only_in_the_worktree_is_still_refused(tmp_path):
    src, sha = _repo(tmp_path, "from coordinator_core.provider import helper2\n")
    _write(src, "coordinator_core/provider.py", "def helper():\n    return 1\n\ndef helper2():\n    return 2\n")

    refusal = _mod._commit_gates_preflight_refusal(str(src), sha, out=io.StringIO())

    assert refusal and "helper2" in refusal


def test_the_destination_repo_cache_is_written_and_a_repeat_run_agrees(tmp_path):
    src, sha = _repo(tmp_path, "from coordinator_core import provider\n\nprovider.missing_attr()\n")
    dest = tmp_path / "dest"
    dest.mkdir()
    _git(dest, "init", "-q")

    first = _mod._commit_gates_preflight_refusal(str(src), sha, out=io.StringIO(), dest=str(dest / "sub"))
    cache_dir = dest / ".git" / "claude-klabauter-gate-result"
    assert cache_dir.exists() and any(cache_dir.rglob("*"))
    second = _mod._commit_gates_preflight_refusal(str(src), sha, out=io.StringIO(), dest=str(dest / "sub"))

    assert first and "missing_attr" in first and second == first


def test_a_dest_without_a_git_repo_runs_uncached_and_still_refuses(tmp_path):
    src, sha = _repo(tmp_path, "from coordinator_core.provider import missing_name\n")

    refusal = _mod._commit_gates_preflight_refusal(str(src), sha, out=io.StringIO(), dest=str(tmp_path / "nogit"))

    assert refusal and "missing_name" in refusal


def test_no_pinned_sha_is_left_to_publish_gates():
    assert _mod._commit_gates_preflight_refusal("nowhere", None, out=io.StringIO()) is None


def test_an_unreadable_sha_says_so_and_allows(tmp_path):
    src, _ = _repo(tmp_path, "")
    out = io.StringIO()

    assert _mod._commit_gates_preflight_refusal(str(src), "f" * 40, out=out) is None
    assert "preflight skipped" in out.getvalue()


@pytest.fixture()
def cli(monkeypatch, tmp_path):
    """`_cmd_round` with every other preflight stubbed, recording whether staging, the round body
    or a publish.py spawn was reached."""
    calls: list = []
    withholding = SimpleNamespace(refusal=None, paths={}, walk_base=None, toplevel=None)

    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda override: str(tmp_path))
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(tmp_path / "src"))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(tmp_path / "dest"))
    monkeypatch.setattr(_mod, "_bootstrap_engine", lambda: None)
    monkeypatch.setattr(_mod, "_resolve_shipped_sha", lambda repo, ref: "a" * 40)
    monkeypatch.setattr(_mod, "_fold_bar_refusal", lambda *a, **k: None)
    monkeypatch.setattr(_mod, "_registry_integrity_refusal", lambda *a, **k: None)
    monkeypatch.setattr(_mod, "_early_engine_import_refusal", lambda *a, **k: None)
    monkeypatch.setattr(_mod, "_field7_preflight", lambda *a, **k: calls.append("field7") or None)
    monkeypatch.setattr(_mod, "_reap_dead_round_tempdirs", lambda: None)
    monkeypatch.setattr(_mod, "_cmd_round_default", lambda *a, **k: calls.append("round") or 0)
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: calls.append("spawn") or None)
    monkeypatch.setattr(_mod, "_run_step", lambda *a, **k: calls.append("spawn") or None)
    import coordinator_core.percolate.source_preflight as sp

    monkeypatch.setattr(sp, "check_source_published", lambda *a, **k: None)
    monkeypatch.setattr(sp, "source_withholding", lambda *a, **k: withholding)
    args = _mod._build_parser().parse_args(["alpha", "--percolate-root", str(tmp_path)])
    return SimpleNamespace(calls=calls, args=args, monkeypatch=monkeypatch)


def test_a_violation_refuses_before_field7_staging_and_publish_spawn(cli, capsys):
    cli.monkeypatch.setattr(
        _mod, "_commit_gates_preflight_refusal", lambda *a, **k: "consumer.py: coordinator_core.provider.missing_attr"
    )

    assert _mod._cmd_round(cli.args) == _mod._EXIT_FAIL
    assert cli.calls == []
    assert "refused, nothing synced: consumer.py: coordinator_core.provider.missing_attr" in capsys.readouterr().err


def test_a_clean_preflight_runs_the_round_after_it_for_the_default_path(cli):
    seen: list = []
    cli.monkeypatch.setattr(
        _mod, "_commit_gates_preflight_refusal", lambda *a, **k: seen.append(a[1]) or None
    )

    assert _mod._cmd_round(cli.args) == 0
    assert seen == ["a" * 40]
    assert cli.calls == ["field7", "round"]
