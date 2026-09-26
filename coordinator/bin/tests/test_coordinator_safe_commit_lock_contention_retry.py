from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import types

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


class TestIsLockContention:
    def test_raw_git_cli_refusal_text_is_recognized(self):
        mod = _load_cli_module()
        exc = RuntimeError("fatal: Unable to create '/repo/.git/index.lock': File exists.")
        assert mod._is_lock_contention(exc) is True

    def test_index_write_lock_busy_message_is_recognized(self):
        mod = _load_cli_module()
        exc = RuntimeError(".git/index.lock exists -- a peer holds the index")
        assert mod._is_lock_contention(exc) is True

    def test_an_unrelated_runtime_error_is_not_lock_contention(self):
        mod = _load_cli_module()
        assert mod._is_lock_contention(RuntimeError("some other refusal")) is False


def _args_for_pathspec(mod, tmp_path):
    (tmp_path / "touched.py").write_text("x = 1\n", encoding="utf-8")
    return mod.parse_args(["fix the frobnicator", "--", "touched.py"])


def _stub_cc_invoke_module(monkeypatch, mod, calls, outcomes):
    import sys

    def _fake_cc_invoke(op, params, worktree_root):
        calls.append((op, params))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    fake_cc_invoke_mod = types.ModuleType("cc_invoke")
    fake_cc_invoke_mod.cc_invoke = _fake_cc_invoke
    fake_cc_invoke_mod.require_engine_on_path = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "cc_invoke", fake_cc_invoke_mod)
    monkeypatch.setattr(mod, "_bootstrap_engine", lambda: None)
    mod.require_engine_on_path = lambda *_a, **_k: None

    fake_lock_preflight_mod = types.ModuleType("coordinator_core.lock_preflight")
    reap_calls = []
    fake_lock_preflight_mod.preflight_reap_stale_lock = lambda root: reap_calls.append(root)
    monkeypatch.setitem(sys.modules, "coordinator_core.lock_preflight", fake_lock_preflight_mod)
    return reap_calls


class TestDoPathspecRetriesOnLockContention:
    def test_lock_contention_is_retried_and_eventually_succeeds(self, monkeypatch, tmp_path, capsys):
        mod = _load_cli_module()
        args = _args_for_pathspec(mod, tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_refuse_contested_pathspec", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_warn_undeclared_untracked_siblings", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_resolve_pre_sha_for_reconcile", lambda root: None)
        monkeypatch.setattr(mod.time, "sleep", lambda *_a: None)

        calls = []
        outcomes = [
            RuntimeError(".git/index.lock exists -- a peer holds the index"),
            {"committed": True, "sha": "deadbeef", "warnings": []},
        ]
        reap_calls = _stub_cc_invoke_module(monkeypatch, mod, calls, outcomes)

        mod.do_pathspec(args)

        assert len(calls) == 2, "expected exactly one retry"
        assert len(reap_calls) == 1, "lock preflight must run before the retry"
        err = capsys.readouterr().err
        assert "index-lock contention" in err
        assert "committed sha=deadbeef" in err

    def test_a_non_lock_runtime_error_is_never_retried(self, monkeypatch, tmp_path, capsys):
        mod = _load_cli_module()
        args = _args_for_pathspec(mod, tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_refuse_contested_pathspec", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_warn_undeclared_untracked_siblings", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_resolve_pre_sha_for_reconcile", lambda root: None)
        monkeypatch.setattr(mod, "_is_indeterminate_outcome", lambda exc: False)
        monkeypatch.setattr(mod.time, "sleep", lambda *_a: None)

        calls = []
        outcomes = [RuntimeError("params.paths must be a list of strings")]
        _stub_cc_invoke_module(monkeypatch, mod, calls, outcomes)

        with pytest.raises(SystemExit) as exc:
            mod.do_pathspec(args)
        assert exc.value.code == 1
        assert len(calls) == 1, "a non-lock error must not be retried"

    def test_persistent_lock_contention_exhausts_retries_and_reports_the_error(
        self, monkeypatch, tmp_path, capsys
    ):
        mod = _load_cli_module()
        args = _args_for_pathspec(mod, tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod, "_refuse_contested_pathspec", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_warn_undeclared_untracked_siblings", lambda *a, **k: None)
        monkeypatch.setattr(mod, "_resolve_pre_sha_for_reconcile", lambda root: None)
        monkeypatch.setattr(mod, "_is_indeterminate_outcome", lambda exc: False)
        monkeypatch.setattr(mod.time, "sleep", lambda *_a: None)

        lock_exc = RuntimeError(".git/index.lock exists -- a peer holds the index")
        calls = []
        outcomes = [lock_exc, lock_exc, lock_exc]
        _stub_cc_invoke_module(monkeypatch, mod, calls, outcomes)

        with pytest.raises(SystemExit) as exc:
            mod.do_pathspec(args)
        assert exc.value.code == 1
        assert len(calls) == 3
