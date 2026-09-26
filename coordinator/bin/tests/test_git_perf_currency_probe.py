from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "workday_start_health_probes_under_test_gpc", _BIN_DIR / "workday-start-health-probes.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _write_config(repo: Path, *, untracked_cache: bool | None) -> None:
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    lines = ["[core]"]
    if untracked_cache is not None:
        lines.append(f"\tuntrackedCache = {'true' if untracked_cache else 'false'}")
    (repo / ".git" / "config").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fake_helpers(roots, classify):
    def registry_repo_roots(_bin_dir):
        return list(roots)

    def classify_target(root):
        return classify(root)

    return registry_repo_roots, classify_target


def _patch_helpers(monkeypatch, roots, classify):
    from coordinator_core.install import git_perf_config as gpc

    monkeypatch.setattr(
        gpc,
        "_git_hook_install_registry_helpers",
        lambda: _fake_helpers(roots, classify),
    )


def test_key_present_everywhere_is_exit_zero(tmp_path, monkeypatch):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    _write_config(repo_a, untracked_cache=True)
    _write_config(repo_b, untracked_cache=True)
    _patch_helpers(
        monkeypatch,
        [("repos.a", str(repo_a)), ("repos.b", str(repo_b))],
        lambda _root: "worktree",
    )

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_git_perf_currency([])

    assert rc == 0
    assert err.getvalue() == ""


def test_key_absent_in_one_repo_is_exit_one_naming_it(tmp_path, monkeypatch):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    _write_config(repo_a, untracked_cache=True)
    _write_config(repo_b, untracked_cache=None)
    _patch_helpers(
        monkeypatch,
        [("repos.a", str(repo_a)), ("repos.b", str(repo_b))],
        lambda _root: "worktree",
    )

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_git_perf_currency([])

    assert rc == 1
    output = err.getvalue()
    assert "repos.b" in output


def test_mirror_targets_are_skipped_silently(tmp_path, monkeypatch):
    repo_mirror = tmp_path / "mirror"
    _write_config(repo_mirror, untracked_cache=None)
    _patch_helpers(
        monkeypatch,
        [("repos.mirror", str(repo_mirror))],
        lambda _root: "mirror",
    )

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_git_perf_currency([])

    assert rc == 0
    assert err.getvalue() == ""


def test_missing_targets_are_reported(tmp_path, monkeypatch):
    missing_path = str(tmp_path / "does-not-exist")
    _patch_helpers(
        monkeypatch,
        [("repos.gone", missing_path)],
        lambda _root: "missing",
    )

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_git_perf_currency([])

    assert rc == 1
    assert "repos.gone" in err.getvalue()
    assert missing_path in err.getvalue()


def test_gitlink_file_is_resolved_via_commondir(tmp_path, monkeypatch):
    common = tmp_path / "common" / ".git"
    common.mkdir(parents=True)
    (common / "config").write_text("[core]\n\tuntrackedCache = true\n", encoding="utf-8")

    worktree = tmp_path / "linked-worktree"
    worktree.mkdir()
    private_gitdir = tmp_path / "common" / ".git" / "worktrees" / "linked-worktree"
    private_gitdir.mkdir(parents=True)
    (private_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {private_gitdir}\n", encoding="utf-8")

    _patch_helpers(
        monkeypatch,
        [("repos.linked", str(worktree))],
        lambda _root: "worktree",
    )

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_git_perf_currency([])

    assert rc == 0
    assert err.getvalue() == ""


def test_unreadable_config_is_reported_not_silently_passed(tmp_path, monkeypatch):
    repo = tmp_path / "unreadable"
    (repo / ".git").mkdir(parents=True)
    broken = tmp_path / "broken-worktree"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: " + str(tmp_path / "nonexistent-gitdir") + "\n", encoding="utf-8")

    _patch_helpers(
        monkeypatch,
        [("repos.broken", str(broken))],
        lambda _root: "worktree",
    )

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_git_perf_currency([])

    assert rc == 1
    assert "repos.broken" in err.getvalue()
    assert "unreadable" in err.getvalue()


def test_bare_detector_is_zero_spawn(tmp_path, monkeypatch):
    repo_a = tmp_path / "a"
    _write_config(repo_a, untracked_cache=True)
    _patch_helpers(
        monkeypatch,
        [("repos.a", str(repo_a))],
        lambda _root: "worktree",
    )

    def _forbidden(*args, **kwargs):
        raise AssertionError("must not spawn a subprocess")

    for _name in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(_mod.subprocess, _name, _forbidden, raising=False)

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_git_perf_currency([])

    assert rc == 0


def test_fix_form_calls_apply_fleet_in_process(monkeypatch):
    from coordinator_core.install import git_perf_config as gpc

    calls = []

    def _fake_apply_fleet(bin_dir, **kwargs):
        calls.append(bin_dir)
        return ["ok      core.untrackedCache = true (already set)", "fleet summary: swept 1 registered repo(s), applied to 1 worktree(s)."]

    monkeypatch.setattr(gpc, "apply_fleet", _fake_apply_fleet)

    rc = _mod.cmd_git_perf_currency(["--fix"])

    assert rc == 0
    assert len(calls) == 1


def test_fix_form_reports_failed_lines_as_exit_one(monkeypatch):
    from coordinator_core.install import git_perf_config as gpc

    def _fake_apply_fleet(bin_dir, **kwargs):
        return ["FAILED  repos.a: boom"]

    monkeypatch.setattr(gpc, "apply_fleet", _fake_apply_fleet)

    rc = _mod.cmd_git_perf_currency(["--fix"])

    assert rc == 1


def test_unrecognized_argument_is_usage_error():
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = _mod.cmd_git_perf_currency(["--bogus"])

    assert rc == 2


def _run_bare() -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = _mod.cmd_git_perf_currency([])
    return rc, buf.getvalue()


def test_unavailable_registry_helpers_is_not_reported_as_a_current_fleet(monkeypatch):
    from coordinator_core.install import git_perf_config as gpc

    monkeypatch.setattr(gpc, "_git_hook_install_registry_helpers", lambda: None)
    rc, err = _run_bare()
    assert rc == 1
    assert "could not establish fleet currency" in err
    assert "registry helpers unavailable" in err


def test_registry_read_raising_is_not_reported_as_a_current_fleet(monkeypatch):
    from coordinator_core.install import git_perf_config as gpc

    def _raising_helpers():
        def registry_repo_roots(_bin_dir):
            raise OSError("registry unreadable")

        return registry_repo_roots, (lambda _root: "worktree")

    monkeypatch.setattr(gpc, "_git_hook_install_registry_helpers", _raising_helpers)
    rc, err = _run_bare()
    assert rc == 1
    assert "could not read the repo registry" in err


def test_empty_registry_is_explicit_not_success(monkeypatch):
    _patch_helpers(monkeypatch, [], lambda _root: "worktree")
    rc, err = _run_bare()
    assert rc == 1
    assert "not the same fact as" in err


def test_bare_key_with_no_equals_is_implicit_true():
    text = "[core]\n\tuntrackedCache\n"
    assert _mod._config_has_untracked_cache(text) is True


def test_yes_on_1_spellings_are_recognized_as_true():
    for spelling in ("yes", "on", "1"):
        text = f"[core]\n\tuntrackedCache = {spelling}\n"
        assert _mod._config_has_untracked_cache(text) is True, spelling


def test_trailing_inline_comment_on_value_is_stripped():
    text = "[core]\n\tuntrackedCache = true ; comment\n"
    assert _mod._config_has_untracked_cache(text) is True


def test_duplicate_core_blocks_last_one_wins():
    text = "[core]\n\tuntrackedCache = true\n[core]\n\tuntrackedCache = false\n"
    assert _mod._config_has_untracked_cache(text) is False
