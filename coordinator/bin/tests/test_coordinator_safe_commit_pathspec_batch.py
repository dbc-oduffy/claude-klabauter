from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import types

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def test_all_valid_pathspecs_is_one_subprocess_call(monkeypatch):
    mod = _load_cli_module()
    calls: list[list[str]] = []

    def _fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    result = mod._first_invalid_pathspec(["a/b.py", "c/*.py", "d/e.py"])

    assert result is None
    assert len(calls) == 1
    assert calls[0][:3] == ["git", "ls-files", "--"]
    assert calls[0][3:] == [
        ":(literal)a/b.py",
        ":(literal)c/*.py",
        ":(literal)d/e.py",
    ]


def test_one_invalid_pathspec_falls_back_to_per_item_and_names_it(monkeypatch):
    mod = _load_cli_module()
    calls: list[list[str]] = []

    def _fake_run(cmd, **_kwargs):
        calls.append(cmd)
        pathspecs = cmd[3:]
        if len(pathspecs) > 1:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="bad pathspec")
        return types.SimpleNamespace(
            returncode=0 if pathspecs[0] != ":(literal)::bad::" else 1, stdout="", stderr=""
        )

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    result = mod._first_invalid_pathspec(["a/b.py", "::bad::", "d/e.py"])

    assert result == "::bad::"
    assert len(calls) == 3


def test_empty_pathspec_list_is_a_zero_spawn_noop(monkeypatch):
    mod = _load_cli_module()
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append(1))

    result = mod._first_invalid_pathspec([])

    assert result is None
    assert calls == []


def test_pathspec_dry_run_never_dispatches_do_pathspec(monkeypatch, tmp_path):
    mod = _load_cli_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "somefile.py").write_text("x = 1\n", encoding="utf-8")
    dispatched = []
    monkeypatch.setattr(mod, "do_pathspec", lambda args: dispatched.append(args))

    mod.main(["--dry-run", "subject", "--", "somefile.py"])

    assert dispatched == []


def test_pathspec_dry_run_preview_matches_split_paths_including_deletion(
    monkeypatch, tmp_path, capsys
):
    mod = _load_cli_module()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "present.py").write_text("x = 1\n", encoding="utf-8")
    missing_path = "deleted.py"
    monkeypatch.setattr(
        mod, "_paths_tracked_at_head", lambda root, paths: {missing_path}
    )

    expected_present, expected_deleted = mod._split_paths_for_commit_v2(
        str(tmp_path), ["present.py", missing_path]
    )
    assert expected_present == ["present.py"]
    assert expected_deleted == [missing_path]

    monkeypatch.setattr(mod, "do_pathspec", lambda args: (_ for _ in ()).throw(
        AssertionError("do_pathspec must not be dispatched on --dry-run")
    ))

    mod.main(["--dry-run", "subject", "--", "present.py", missing_path])

    captured = capsys.readouterr()
    for path in expected_present:
        assert f"  {path}" in captured.err
    for path in expected_deleted:
        assert f"  {path} [deleted]" in captured.err
