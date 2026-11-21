"""`publish.dispatch_end_of_run_repo_key_literal_gate`: fails when a row's
content transform changes a registry repo-key literal in source.

Run: python -m pytest coordinator/bin/tests/test_publish_repo_key_literal_gate.py -q
"""

from __future__ import annotations

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = pytest.mark.complete

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("publish_repo_key_gate_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_SOURCE = 'KEY = "repos.x"\n'


_KROW = "claude-klabauter-row"


def _run(tmp_path: Path, section: dict) -> bool:
    (tmp_path / "mod.py").write_text(_SOURCE, encoding="utf-8")
    target = SimpleNamespace(name="row", source_dir=tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    return publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, section)])


def test_renaming_row_fails_and_prints_literal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    section = {"hooks": ["substitute"], "substitute": [{"key": "repos.x", "value": "repos.y"}]}
    assert _run(tmp_path, section) is False
    err = capsys.readouterr().err
    assert "repos.x" in err
    assert "FAILED" in err


def test_declared_rename_passes_gate(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text('KEY = "repos.claude_klabauter"\n', encoding="utf-8")
    section = {
        "hooks": ["substitute"],
        "substitute": [{"key": "repos.claude_klabauter", "value": "repos.claude_klabauter", "match_mode": "plain"}],
    }
    target = SimpleNamespace(name="row", source_dir=tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, section)]) is True


def test_other_dotted_key_rename_still_fails_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "mod.py").write_text('KEY = "repos.doe_claude"\n', encoding="utf-8")
    section = {
        "hooks": ["substitute"],
        "substitute": [{"key": "repos.doe_claude", "value": "repos.claude_klabauter", "match_mode": "plain"}],
    }
    target = SimpleNamespace(name="row", source_dir=tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, section)]) is False
    assert "repos.doe_claude" in capsys.readouterr().err


def test_bare_quoted_codename_in_export_fails_and_prints_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "mod.py").write_text('X = "claude_klabauter"\n', encoding="utf-8")
    target = SimpleNamespace(name=_KROW, source_dir=tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, {"hooks": []})]) is False
    captured = capsys.readouterr()
    assert "codename residuals for claude-klabauter-row: 1 (baseline 0 at 268caaf9c2)" in captured.out
    assert "codename residual gate FAILED" in captured.err
    assert "mod.py: \"claude_klabauter\"" in captured.err


def test_a_non_klabauter_row_carries_codenames_by_design(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "mod.py").write_text('X = "claude-klabauter"\n', encoding="utf-8")
    target = SimpleNamespace(name="coordinator-claude", source_dir=tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, {"hooks": []})]) is True
    assert "codename" not in capsys.readouterr().out


def test_clean_export_reports_zero_residuals_and_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "mod.py").write_text(_SOURCE, encoding="utf-8")
    target = SimpleNamespace(name=_KROW, source_dir=tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, {"hooks": []})]) is True
    assert "codename residuals for claude-klabauter-row: 0 (baseline 0 at 268caaf9c2)" in capsys.readouterr().out


def _forbid_transform(monkeypatch) -> None:
    """Make any content transform raise while `_transform_identity` still hashes the real
    transform body (patching the function would otherwise change the identity and miss)."""
    from coordinator_core.percolate import engine as pct_engine

    real_apply, real_identity = pct_engine._apply_content_transforms, pct_engine._transform_identity

    def boom(*args, **kwargs):
        raise AssertionError("content transform ran on a cached, unchanged tree")

    def identity(params) -> str:
        pct_engine._apply_content_transforms = real_apply
        try:
            return real_identity(params)
        finally:
            pct_engine._apply_content_transforms = boom

    monkeypatch.setattr(pct_engine, "_apply_content_transforms", boom)
    monkeypatch.setattr(pct_engine, "_transform_identity", identity)


def test_gate_second_run_is_served_from_the_destination_git_dir_and_still_fails_a_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src, dest = tmp_path / "src", tmp_path / "dest"
    (dest / ".git").mkdir(parents=True)
    src.mkdir()
    (src / "mod.py").write_text(_SOURCE, encoding="utf-8")
    target = SimpleNamespace(name="row", source_dir=src, dest_dir=dest)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    section = {"hooks": []}
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, section)]) is True
    assert list((dest / ".git" / "claude-klabauter-gate-result").glob("gate-result-repo-key-literal-*.json"))

    _forbid_transform(monkeypatch)
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, section)]) is True
    monkeypatch.undo()
    renaming = {"hooks": ["substitute"], "substitute": [{"key": "repos.x", "value": "repos.y"}]}
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, renaming)]) is False


def test_commit_module_attr_gate_second_run_is_served_from_the_destination_git_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "dest"
    (dest / ".git").mkdir(parents=True)
    target = SimpleNamespace(name="row", source_dir=tmp_path / "src", dest_dir=dest)
    judged: list = []
    progress_seen: list = []

    def fake_gate(root, sha, *, cache=None, progress=None):
        progress_seen.append(callable(progress))
        key = cache.key("a.py", b"x", "P")
        if cache.get(key) is None:
            judged.append(key)
            cache.put(key, [])
        return 1, []

    class _ReadError(Exception):
        pass

    monkeypatch.setattr(publish, "_bootstrap_engine", lambda: None)
    monkeypatch.setattr(publish, "find_commit_module_attr_violations", fake_gate, raising=False)
    monkeypatch.setattr(publish, "CommitReadError", _ReadError, raising=False)
    monkeypatch.setattr(publish, "_COMMIT_GATE_RESULTS", {})
    monkeypatch.setattr(publish, "commit_blob_reads", None, raising=False)
    assert publish._commit_module_attr_gate(target, "abc123") is True
    assert list((dest / ".git" / "claude-klabauter-gate-result").glob("gate-result-commit-module-attr-*.json"))
    assert publish._commit_module_attr_gate(target, "abc123") is True
    assert progress_seen == [True]  # a second row of the same round reuses the verdict
    publish._COMMIT_GATE_RESULTS.clear()  # a later round: served from the destination's cache
    assert publish._commit_module_attr_gate(target, "abc123") is True
    assert len(judged) == 1
    assert progress_seen == [True, True]

    publish._COMMIT_GATE_RESULTS.clear()
    bare = SimpleNamespace(name="row", source_dir=tmp_path / "src")
    monkeypatch.setattr(publish, "_gate_cache_for", lambda *a: None)
    monkeypatch.setattr(publish, "find_commit_module_attr_violations", lambda *a, cache=None, progress=None: (1, [("f.py", "m.x")]))
    assert publish._commit_module_attr_gate(bare, "abc123") is False


def test_identity_row_passes(tmp_path: Path) -> None:
    assert _run(tmp_path, {"hooks": []}) is True


def test_unloadable_transform_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    section = {"hooks": ["substitute"], "substitute": 5}
    assert _run(tmp_path, section) is False
    assert "raised" in capsys.readouterr().err


def test_engine_pin_without_census_module_still_judged(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A `--commit` pin predating repo_key_literals must not disable the gate."""
    import builtins

    real_import = builtins.__import__

    def no_census(name, globals=None, locals=None, fromlist=(), level: int = 0) -> ModuleType:
        if name == "coordinator_core.percolate" and fromlist and "repo_key_literals" in fromlist:
            raise ImportError("cannot import name 'repo_key_literals'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_census)
    section = {"hooks": ["substitute"], "substitute": [{"key": "repos.x", "value": "repos.y"}]}
    assert _run(tmp_path, section) is False
    assert "FAILED" in capsys.readouterr().err


def _waiver_file(tmp_path: Path, published: int) -> Path:
    import json

    p = tmp_path / "waivers.json"
    p.write_text(json.dumps({"ruling": "r", "waivers": [
        {"row": "row", "path": "mod.py", "literal": "repos.x", "source": 1, "published": published},
    ]}), encoding="utf-8")
    return p


def test_exact_waiver_passes_and_is_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(publish._load_repo_key_waivers, "__defaults__", (_waiver_file(tmp_path, 0),))
    section = {"hooks": ["substitute"], "substitute": [{"key": "repos.x", "value": "repos.y"}]}
    assert _run(src, section) is True
    assert "WAIVED" in capsys.readouterr().out


def test_waiver_with_other_count_still_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(publish._load_repo_key_waivers, "__defaults__", (_waiver_file(tmp_path, 3),))
    section = {"hooks": ["substitute"], "substitute": [{"key": "repos.x", "value": "repos.y"}]}
    assert _run(src, section) is False


def _allowlisted_run(tmp_path: Path, allowlist: str) -> bool:
    (tmp_path / "pub.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "held.py").write_text('X = "claude_klabauter"\n', encoding="utf-8")
    target = SimpleNamespace(name=_KROW, source_dir=tmp_path, allowlist=allowlist)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    return publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, {"hooks": []})])


def test_codename_in_a_file_the_allowlist_withholds_is_not_counted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _allowlisted_run(tmp_path, "pub.py") is True
    assert "codename residuals for claude-klabauter-row: 0" in capsys.readouterr().out


def test_codename_in_a_published_file_still_fails_under_an_allowlist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _allowlisted_run(tmp_path, "pub.py,held.py") is False
    assert "codename residuals for claude-klabauter-row: 1" in capsys.readouterr().out


def _many_rows(tmp_path: Path, count: int) -> "tuple[list, SimpleNamespace]":
    """`count` rows sharing one destination repo, each under its own transform identity;
    row 0 holds a repo-key literal its transform renames, the rest hold one it keeps."""
    dest = tmp_path / "dest"
    (dest / ".git").mkdir(parents=True)
    rows = []
    for i in range(count):
        src = tmp_path / f"src{i}"
        src.mkdir()
        (src / "mod.py").write_text(_SOURCE if i == 0 else f'KEY = "repos.k{i}"\n', encoding="utf-8")
        section = {"hooks": ["substitute"], "substitute": [{"key": f"unused{i}", "value": f"other{i}"}]}
        if i == 0:
            section["substitute"].append({"key": "repos.x", "value": "repos.y"})
        rows.append((SimpleNamespace(name=f"row{i}", source_dir=src, dest_dir=dest), section))
    return rows, SimpleNamespace(engine_claude_klabauter=object())


def test_a_round_of_many_rows_is_served_from_cache_with_identical_verdicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows, ctx = _many_rows(tmp_path, 14)
    clean = rows[1:]
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, clean) is True
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, rows) is False

    _forbid_transform(monkeypatch)
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, clean) is True
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, rows) is False


def test_allowlist_denial_matches_path_covers_for_every_shape(tmp_path: Path) -> None:
    allowlist = "docs/,pub.py,lib/keep,!docs/secret,!lib/keep/old.py"
    target = SimpleNamespace(allowlist=allowlist)
    rels = [
        "pub.py", "pub.py.bak", "docs/a.md", "docs/secret/k.md", "docs/secretive.md", "docs",
        "lib/keep/x.py", "lib/keep/old.py", "lib/keepers.py", "lib/other.py", "top.py",
    ]
    entries, exclusions = publish.split_inclusion_exclusion(publish.parse_allowlist_csv(allowlist))
    want = {
        r
        for r in rels
        if any(publish._path_covers(x, r) for x in exclusions)
        or not any(publish._path_covers(e, r) for e in entries)
    }
    assert publish._paths_the_row_allowlist_denies(target, rels) == want
    assert {"pub.py", "docs/a.md", "lib/keep/x.py"}.isdisjoint(want)
    assert {"pub.py.bak", "docs/secret/k.md", "lib/keep/old.py", "lib/other.py", "top.py"} <= want
