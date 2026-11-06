"""`publish.dispatch_end_of_run_repo_key_literal_gate`: fails when a row's
content transform changes a registry repo-key literal in source.

Run: python -m pytest coordinator/bin/tests/test_publish_repo_key_literal_gate.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
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


def test_renaming_row_fails_and_prints_literal(tmp_path, capsys):
    section = {"hooks": ["substitute"], "substitute": [{"key": "repos.x", "value": "repos.y"}]}
    assert _run(tmp_path, section) is False
    err = capsys.readouterr().err
    assert "repos.x" in err
    assert "FAILED" in err


def test_declared_rename_passes_gate(tmp_path):
    (tmp_path / "mod.py").write_text('KEY = "repos.claude_klabauter"\n', encoding="utf-8")
    section = {
        "hooks": ["substitute"],
        "substitute": [{"key": "repos.claude_klabauter", "value": "repos.claude_klabauter", "match_mode": "plain"}],
    }
    target = SimpleNamespace(name="row", source_dir=tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, section)]) is True


def test_other_dotted_key_rename_still_fails_gate(tmp_path, capsys):
    (tmp_path / "mod.py").write_text('KEY = "repos.doe_claude"\n', encoding="utf-8")
    section = {
        "hooks": ["substitute"],
        "substitute": [{"key": "repos.doe_claude", "value": "repos.claude_klabauter", "match_mode": "plain"}],
    }
    target = SimpleNamespace(name="row", source_dir=tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, section)]) is False
    assert "repos.doe_claude" in capsys.readouterr().err


def test_bare_quoted_codename_in_export_fails_and_prints_count(tmp_path, capsys):
    (tmp_path / "mod.py").write_text('X = "claude_klabauter"\n', encoding="utf-8")
    target = SimpleNamespace(name=_KROW, source_dir=tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, {"hooks": []})]) is False
    captured = capsys.readouterr()
    assert "codename residuals for claude-klabauter-row: 1 (baseline 0 at 268caaf9c2)" in captured.out
    assert "codename residual gate FAILED" in captured.err
    assert "mod.py: \"claude_klabauter\"" in captured.err


def test_a_non_klabauter_row_carries_codenames_by_design(tmp_path, capsys):
    (tmp_path / "mod.py").write_text('X = "claude-klabauter"\n', encoding="utf-8")
    target = SimpleNamespace(name="coordinator-claude", source_dir=tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, {"hooks": []})]) is True
    assert "codename" not in capsys.readouterr().out


def test_clean_export_reports_zero_residuals_and_passes(tmp_path, capsys):
    (tmp_path / "mod.py").write_text(_SOURCE, encoding="utf-8")
    target = SimpleNamespace(name=_KROW, source_dir=tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, {"hooks": []})]) is True
    assert "codename residuals for claude-klabauter-row: 0 (baseline 0 at 268caaf9c2)" in capsys.readouterr().out


def test_gate_second_run_is_served_from_the_destination_git_dir_and_still_fails_a_rename(tmp_path, monkeypatch):
    src, dest = tmp_path / "src", tmp_path / "dest"
    (dest / ".git").mkdir(parents=True)
    src.mkdir()
    (src / "mod.py").write_text(_SOURCE, encoding="utf-8")
    target = SimpleNamespace(name="row", source_dir=src, dest_dir=dest)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    section = {"hooks": []}
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, section)]) is True
    assert list((dest / ".git" / "claude-klabauter-gate-result").glob("gate-result-repo-key-literal-*.json"))

    from coordinator_core.percolate import engine as pct_engine

    def boom(*args, **kwargs):
        raise AssertionError("content transform ran on a cached, unchanged tree")

    monkeypatch.setattr(pct_engine, "_apply_content_transforms", boom)
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, section)]) is True
    monkeypatch.undo()
    renaming = {"hooks": ["substitute"], "substitute": [{"key": "repos.x", "value": "repos.y"}]}
    assert publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, renaming)]) is False


def test_identity_row_passes(tmp_path):
    assert _run(tmp_path, {"hooks": []}) is True


def test_unloadable_transform_fails_closed(tmp_path, capsys):
    section = {"hooks": ["substitute"], "substitute": 5}
    assert _run(tmp_path, section) is False
    assert "raised" in capsys.readouterr().err


def test_engine_pin_without_census_module_still_judged(tmp_path, capsys, monkeypatch):
    """A `--commit` pin predating repo_key_literals must not disable the gate."""
    import builtins

    real_import = builtins.__import__

    def no_census(name, globals=None, locals=None, fromlist=(), level=0):
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


def test_exact_waiver_passes_and_is_printed(tmp_path, capsys, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(publish._load_repo_key_waivers, "__defaults__", (_waiver_file(tmp_path, 0),))
    section = {"hooks": ["substitute"], "substitute": [{"key": "repos.x", "value": "repos.y"}]}
    assert _run(src, section) is True
    assert "WAIVED" in capsys.readouterr().out


def test_waiver_with_other_count_still_fails(tmp_path, monkeypatch):
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


def test_codename_in_a_file_the_allowlist_withholds_is_not_counted(tmp_path, capsys):
    assert _allowlisted_run(tmp_path, "pub.py") is True
    assert "codename residuals for claude-klabauter-row: 0" in capsys.readouterr().out


def test_codename_in_a_published_file_still_fails_under_an_allowlist(tmp_path, capsys):
    assert _allowlisted_run(tmp_path, "pub.py,held.py") is False
    assert "codename residuals for claude-klabauter-row: 1" in capsys.readouterr().out


def _git_source(tmp_path: Path, tracked: "dict[str, str]") -> Path:
    import subprocess

    src = tmp_path / "src"
    src.mkdir()
    for rel, text in tracked.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "add", "--", *tracked], check=True)
    return src


def _install_md_run(src: Path, allowlist: str = "INSTALL.md") -> bool:
    target = SimpleNamespace(name="row", source_dir=src, allowlist=allowlist)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    section = {"hooks": ["substitute"], "substitute": [{"key": "repos.x", "value": "repos.y"}]}
    return publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, section)])


def test_gitignored_scratch_copy_is_not_scanned_on_a_dot_source_install_md_row(tmp_path):
    src = _git_source(tmp_path, {"INSTALL.md": "hello\n", ".gitignore": "scratch/\n"})
    (src / "scratch").mkdir()
    (src / "scratch" / "_engine_root.py").write_text(_SOURCE, encoding="utf-8")
    assert _install_md_run(src) is True


def test_untracked_drifting_file_is_not_scanned(tmp_path):
    src = _git_source(tmp_path, {"INSTALL.md": "hello\n"})
    (src / "other.py").write_text(_SOURCE, encoding="utf-8")
    assert _install_md_run(src) is True


def test_tracked_allowlisted_drift_still_fails(tmp_path, capsys):
    src = _git_source(tmp_path, {"INSTALL.md": "hello\n", "mod.py": _SOURCE})
    assert _install_md_run(src, "INSTALL.md,mod.py") is False
    err = capsys.readouterr().err
    assert "repos.x" in err and "mod.py" in err


def test_tracked_file_the_allowlist_withholds_is_not_scanned(tmp_path):
    src = _git_source(tmp_path, {"INSTALL.md": "hello\n", "held.py": _SOURCE})
    assert _install_md_run(src) is True
