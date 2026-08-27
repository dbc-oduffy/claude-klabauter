"""Tests for coordinator_core.install.prereq_probe.

Independently re-derives expected NDJSON output shape by mocking
subprocess.run per-probe (does NOT shell out to bash — this module is the
native port, so a subprocess-parity test would only prove "the port calls
the same binaries", not correctness of this module's own logic).

Port of: prereq_probe.sh (DoE 290997c7, 2026-07-22)
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

from coordinator_core.install import prereq_probe as pp


def _cp(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _parse(line: str) -> dict:
    assert line.endswith("\n")
    return json.loads(line)


# ---------------------------------------------------------------------------
# probe_git
# ---------------------------------------------------------------------------
def test_probe_git_pass():
    with mock.patch.object(pp, "_run", return_value=_cp(0, "git version 2.42.0\n")):
        rec = _parse(pp.probe_git())
    assert rec == {
        "name": "git", "status": "pass", "severity": "hard",
        "detail": "git version 2.42.0", "remediation": "",
    }


def test_probe_git_absent():
    with mock.patch.object(pp, "_run", return_value=None):
        rec = _parse(pp.probe_git())
    assert rec["status"] == "fail"
    assert rec["severity"] == "hard"
    assert "brew install git" in rec["remediation"]


def test_probe_git_broken_stub():
    with mock.patch.object(pp, "_run", return_value=_cp(1, "")):
        rec = _parse(pp.probe_git())
    assert rec["status"] == "fail"


# ---------------------------------------------------------------------------
# probe_python — delegates to manifest_reader.find_python (already-ported sibling)
# ---------------------------------------------------------------------------
def test_probe_python_pass():
    with mock.patch(
        "coordinator_core.install.manifest_reader.find_python", return_value="python3"
    ), mock.patch.object(pp, "_run", return_value=_cp(0, "Python 3.12.1\n")):
        rec = _parse(pp.probe_python())
    assert rec["status"] == "pass"
    assert rec["severity"] == "hard"
    assert "3.12.1" in rec["detail"]


def test_probe_python_fail():
    from coordinator_core.install.manifest_reader import NoPythonInterpreterError

    with mock.patch(
        "coordinator_core.install.manifest_reader.find_python",
        side_effect=NoPythonInterpreterError("ERROR: no functional Python 3.11+ interpreter found"),
    ):
        rec = _parse(pp.probe_python())
    assert rec["status"] == "fail"
    assert rec["severity"] == "hard"
    assert "App execution alias" in rec["remediation"] or "App Execution alias" in rec["remediation"]


# ---------------------------------------------------------------------------
# probe_uv — advisory, warn (not fail) on absence
# ---------------------------------------------------------------------------
def test_probe_uv_missing_is_warn_not_fail():
    with mock.patch.object(pp, "_run", return_value=None):
        rec = _parse(pp.probe_uv())
    assert rec["status"] == "warn"
    assert rec["severity"] == "advisory"


def test_probe_uv_pass():
    with mock.patch.object(pp, "_run", return_value=_cp(0, "uv 0.4.0\n")):
        rec = _parse(pp.probe_uv())
    assert rec["status"] == "pass"


# ---------------------------------------------------------------------------
# probe_gh — three-step hard gate + optional COORDINATOR_GH_PROBE_REPO sub-probe
# ---------------------------------------------------------------------------
def test_probe_gh_pass_no_probe_repo(monkeypatch):
    monkeypatch.delenv("COORDINATOR_GH_PROBE_REPO", raising=False)
    calls = {"n": 0}

    def fake_run(argv, **kw):
        calls["n"] += 1
        if argv == ["gh", "--version"]:
            return _cp(0, "gh version 2.40.0\n")
        if argv == ["gh", "auth", "status"]:
            return _cp(0, "")
        raise AssertionError(f"unexpected call: {argv}")

    with mock.patch.object(pp, "_run", side_effect=fake_run):
        rec = _parse(pp.probe_gh())
    assert rec["status"] == "pass"
    assert calls["n"] == 2  # no repo sub-probe when env var unset


def test_probe_gh_unauthenticated():
    def fake_run(argv, **kw):
        if argv == ["gh", "--version"]:
            return _cp(0, "gh version 2.40.0\n")
        if argv == ["gh", "auth", "status"]:
            return _cp(1, "", "not logged in")
        raise AssertionError(argv)

    with mock.patch.object(pp, "_run", side_effect=fake_run):
        rec = _parse(pp.probe_gh())
    assert rec["status"] == "fail"
    assert "gh auth login" in rec["remediation"]


def test_probe_gh_repo_subprobe_runs_when_env_set(monkeypatch):
    monkeypatch.setenv("COORDINATOR_GH_PROBE_REPO", "owner/repo")

    def fake_run(argv, **kw):
        if argv == ["gh", "--version"]:
            return _cp(0, "gh version 2.40.0\n")
        if argv == ["gh", "auth", "status"]:
            return _cp(0, "")
        if argv == ["gh", "repo", "view", "owner/repo"]:
            return _cp(0, "")
        raise AssertionError(argv)

    with mock.patch.object(pp, "_run", side_effect=fake_run):
        rec = _parse(pp.probe_gh())
    assert rec["status"] == "pass"


# ---------------------------------------------------------------------------
# probe_ue — env override, then per-OS scan
# ---------------------------------------------------------------------------
def test_probe_ue_env_override_hit(monkeypatch, tmp_path):
    engine_dir = tmp_path / "UE_5.4"
    bin_dir = engine_dir / "Engine" / "Binaries" / "Mac"
    bin_dir.mkdir(parents=True)
    (bin_dir / "UnrealEditor").write_text("stub")
    monkeypatch.setenv("EXAMPLE_GAME_REPO_UE_ROOT", str(engine_dir))

    rec = _parse(pp.probe_ue())
    assert rec["status"] == "pass"
    assert "UnrealEditor" in rec["detail"]


def test_probe_ue_env_override_miss(monkeypatch, tmp_path):
    monkeypatch.setenv("EXAMPLE_GAME_REPO_UE_ROOT", str(tmp_path / "nonexistent"))
    rec = _parse(pp.probe_ue())
    assert rec["status"] == "warn"
    assert rec["severity"] == "advisory"


def test_probe_ue_not_found_on_path(monkeypatch):
    monkeypatch.delenv("EXAMPLE_GAME_REPO_UE_ROOT", raising=False)
    with mock.patch.object(pp, "_os_name", return_value="Darwin"), mock.patch(
        "shutil.which", return_value=None
    ):
        rec = _parse(pp.probe_ue())
    assert rec["status"] == "warn"
    assert rec["severity"] == "advisory"


# ---------------------------------------------------------------------------
# probe_clone_auth — the heaviest branch; exercise the priority ladder.
# ---------------------------------------------------------------------------
def test_probe_clone_auth_gh_wins(monkeypatch):
    monkeypatch.delenv("COORDINATOR_AUTH_PROBE_URL", raising=False)
    with mock.patch.object(pp, "_run", return_value=_cp(0, "")):
        rec = _parse(pp.probe_clone_auth())
    assert rec["status"] == "pass"
    assert "GitHub CLI" in rec["detail"]


def test_probe_clone_auth_ssh_success(monkeypatch):
    monkeypatch.delenv("COORDINATOR_AUTH_PROBE_URL", raising=False)

    def fake_run(argv, **kw):
        if argv[:2] == ["gh", "auth"] or argv[:2] == ["glab", "auth"]:
            return _cp(1, "", "not authenticated")
        if argv[0] == "ssh":
            return _cp(1, "", "Hi user! You've successfully authenticated")
        raise AssertionError(argv)

    with mock.patch.object(pp, "_run", side_effect=fake_run):
        rec = _parse(pp.probe_clone_auth())
    assert rec["status"] == "pass"
    assert "SSH key authenticated" in rec["detail"]


def test_probe_clone_auth_offline_is_inconclusive(monkeypatch):
    monkeypatch.delenv("COORDINATOR_AUTH_PROBE_URL", raising=False)

    def fake_run(argv, **kw):
        if argv[:2] in (["gh", "auth"], ["glab", "auth"]):
            return None  # binaries absent
        if argv[0] == "ssh":
            return _cp(1, "", "ssh: connect to host github.com port 22: Network is unreachable")
        if argv[:2] == ["git", "credential"]:
            return _cp(0, "")  # empty fill, no password=
        if argv == ["git", "--version"]:
            return _cp(0, "git version 2.42.0")
        raise AssertionError(argv)

    with mock.patch.object(pp, "_run", side_effect=fake_run):
        rec = _parse(pp.probe_clone_auth())
    assert rec["status"] == "inconclusive"
    assert rec["severity"] == "advisory"


def test_probe_clone_auth_no_method_found_is_semi_hard(monkeypatch):
    monkeypatch.delenv("COORDINATOR_AUTH_PROBE_URL", raising=False)

    def fake_run(argv, **kw):
        if argv[:2] in (["gh", "auth"], ["glab", "auth"]):
            return None
        if argv[0] == "ssh":
            # Reachable but auth explicitly denied — NOT a network error, so
            # the network-error-only inconclusive branch must not fire.
            return _cp(1, "", "Permission denied (publickey).")
        if argv[:2] == ["git", "credential"]:
            return _cp(0, "")
        if argv == ["git", "--version"]:
            return _cp(0, "git version 2.42.0")
        raise AssertionError(argv)

    with mock.patch.object(pp, "_run", side_effect=fake_run):
        rec = _parse(pp.probe_clone_auth())
    assert rec["status"] == "warn"
    assert rec["severity"] == "semi-hard"


def test_probe_clone_auth_git_absent_is_inconclusive(monkeypatch):
    monkeypatch.delenv("COORDINATOR_AUTH_PROBE_URL", raising=False)

    def fake_run(argv, **kw):
        if argv[:2] in (["gh", "auth"], ["glab", "auth"]):
            return None
        if argv[0] == "ssh":
            return None  # ssh itself absent too
        return None

    with mock.patch.object(pp, "_run", side_effect=fake_run):
        rec = _parse(pp.probe_clone_auth())
    assert rec["status"] == "inconclusive"


# ---------------------------------------------------------------------------
# probe_longpaths — non-Windows short-circuit is the common CI case.
# ---------------------------------------------------------------------------
def test_probe_longpaths_non_windows():
    with mock.patch.object(pp, "_os_name", return_value="Darwin"):
        rec = _parse(pp.probe_longpaths())
    assert rec["status"] == "pass"
    assert rec["detail"] == "n/a (non-Windows)"


def test_probe_longpaths_windows_configured():
    with mock.patch.object(pp, "_os_name", return_value="MINGW64_NT"), mock.patch.object(
        pp, "_run", return_value=_cp(0, "true\n")
    ):
        rec = _parse(pp.probe_longpaths())
    assert rec["status"] == "pass"


def test_probe_longpaths_windows_unset():
    with mock.patch.object(pp, "_os_name", return_value="MINGW64_NT"), mock.patch.object(
        pp, "_run", return_value=_cp(1, "")
    ):
        rec = _parse(pp.probe_longpaths())
    assert rec["status"] == "warn"
    assert "<unset>" in rec["detail"]


# ---------------------------------------------------------------------------
# probe_git_lfs
# ---------------------------------------------------------------------------
def test_probe_git_lfs_pass():
    def fake_run(argv, **kw):
        if argv == ["git", "--version"]:
            return _cp(0, "git version 2.42.0")
        if argv == ["git", "lfs", "version"]:
            return _cp(0, "git-lfs/3.4.0")
        if argv == ["git", "config", "--global", "--get", "filter.lfs.clean"]:
            return _cp(0, "git-lfs clean -- %f\n")
        raise AssertionError(argv)

    with mock.patch.object(pp, "_run", side_effect=fake_run):
        rec = _parse(pp.probe_git_lfs())
    assert rec["status"] == "pass"


def test_probe_git_lfs_not_configured():
    def fake_run(argv, **kw):
        if argv == ["git", "--version"]:
            return _cp(0, "git version 2.42.0")
        if argv == ["git", "lfs", "version"]:
            return _cp(0, "git-lfs/3.4.0")
        if argv == ["git", "config", "--global", "--get", "filter.lfs.clean"]:
            return _cp(1, "")
        raise AssertionError(argv)

    with mock.patch.object(pp, "_run", side_effect=fake_run):
        rec = _parse(pp.probe_git_lfs())
    assert rec["status"] == "warn"
    assert "git lfs install" in rec["remediation"]


# ---------------------------------------------------------------------------
# probe_shell_login_env — macOS-only orphan detector.
# ---------------------------------------------------------------------------
def test_probe_shell_login_env_non_macos_short_circuits():
    with mock.patch.object(pp, "_os_name", return_value="Linux"):
        rec = _parse(pp.probe_shell_login_env())
    assert rec["status"] == "pass"
    assert "non-macOS" in rec["detail"]


def test_probe_shell_login_env_bash_orphaned(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/tester")

    def fake_run(argv, **kw):
        if argv[0] == "dscl":
            return _cp(0, "UserShell: /bin/bash\n")
        if argv[0] == "/bin/bash":
            # fresh PATH lacks ~/.local/bin, and no claude line follows.
            return _cp(0, "/usr/bin:/bin\n")
        raise AssertionError(argv)

    with mock.patch.object(pp, "_os_name", return_value="Darwin"), mock.patch.object(
        pp, "_run", side_effect=fake_run
    ):
        rec = _parse(pp.probe_shell_login_env())
    assert rec["status"] == "fail"
    assert "$HOME/.local/bin absent" in rec["detail"]


def test_probe_shell_login_env_bash_intact(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/tester")

    def fake_run(argv, **kw):
        if argv[0] == "dscl":
            return _cp(0, "UserShell: /bin/bash\n")
        if argv[0] == "/bin/bash":
            return _cp(0, "/usr/bin:/bin:/Users/tester/.local/bin\n/Users/tester/.local/bin/claude")
        raise AssertionError(argv)

    with mock.patch.object(pp, "_os_name", return_value="Darwin"), mock.patch.object(
        pp, "_run", side_effect=fake_run
    ):
        rec = _parse(pp.probe_shell_login_env())
    assert rec["status"] == "pass"
    assert "claude resolves at" in rec["detail"]


def test_probe_shell_login_env_non_bash_login_shell():
    def fake_run(argv, **kw):
        if argv[0] == "dscl":
            return _cp(0, "UserShell: /bin/zsh\n")
        if argv[0] == "/bin/zsh":
            return _cp(0, "/usr/bin:/bin\n")
        raise AssertionError(argv)

    with mock.patch.object(pp, "_os_name", return_value="Darwin"), mock.patch.object(
        pp, "_run", side_effect=fake_run
    ):
        rec = _parse(pp.probe_shell_login_env())
    assert rec["status"] == "pass"
    assert "not bash" in rec["detail"]


# ---------------------------------------------------------------------------
# probe_all / main — aggregator shape + order.
# ---------------------------------------------------------------------------
def test_probe_all_order_and_count():
    names = [
        "git", "python", "uv", "gh", "node", "pwsh", "ue", "clone_auth", "longpaths",
        "git_lfs", "shell_login_env", "skill_frontmatter_valid", "windows_terminal_presence",
    ]
    stub_lines = [f'{{"name":"{n}","status":"pass","severity":"advisory","detail":"","remediation":""}}\n' for n in names]

    with mock.patch.object(pp, "_PROBE_ORDER", tuple(lambda line=line: line for line in stub_lines)):
        lines = pp.probe_all()
    assert len(lines) == 13
    for line, name in zip(lines, names):
        assert _parse(line)["name"] == name


def test_main_writes_ndjson_and_returns_zero(capsys):
    with mock.patch.object(pp, "probe_all", return_value=['{"name":"git","status":"pass","severity":"hard","detail":"","remediation":""}\n']):
        rc = pp.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out.strip())["name"] == "git"


# ---------------------------------------------------------------------------
# subprocess hardening (A2/A4) — every _run call must carry timeout + stdin guard.
# ---------------------------------------------------------------------------
def test_run_passes_timeout_and_stdin_devnull():
    with mock.patch("subprocess.run") as mocked:
        mocked.return_value = _cp(0, "ok")
        pp._run(["git", "--version"])
    _, kwargs = mocked.call_args
    assert kwargs["timeout"] == pp._PROBE_TIMEOUT_SECS
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_run_returns_none_on_timeout():
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1)):
        assert pp._run(["git", "--version"]) is None


def test_run_returns_none_on_oserror():
    with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
        assert pp._run(["definitely-not-a-binary"]) is None


# ---------------------------------------------------------------------------
# probe_skill_frontmatter_valid / install.probe_skill_frontmatter_valid
# ---------------------------------------------------------------------------
def _write_skill_file(doe_root, *, description="a description", with_frontmatter=True):
    skill_path = doe_root.joinpath("coordinator", "skills", "setup", "SKILL.md")
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    if with_frontmatter:
        skill_path.write_text(
            f'---\nname: setup\ndescription: "{description}"\n---\n\n# body\n', encoding="utf-8"
        )
    else:
        skill_path.write_text("# no frontmatter here\n", encoding="utf-8")
    return skill_path


def test_check_skill_frontmatter_valid_ok(tmp_path):
    _write_skill_file(tmp_path)
    with mock.patch(
        "coordinator_core.ops.coordinator_doe_root.coordinator_doe_root", return_value=str(tmp_path)
    ):
        result = pp._check_skill_frontmatter_valid()
    assert result == {"ok": True, "error": None}


def test_check_skill_frontmatter_valid_doe_root_unresolvable():
    with mock.patch("coordinator_core.ops.coordinator_doe_root.coordinator_doe_root", return_value=None):
        result = pp._check_skill_frontmatter_valid()
    assert result["ok"] is False
    assert "unresolvable" in result["error"]


def test_check_skill_frontmatter_valid_file_missing(tmp_path):
    with mock.patch(
        "coordinator_core.ops.coordinator_doe_root.coordinator_doe_root", return_value=str(tmp_path)
    ):
        result = pp._check_skill_frontmatter_valid()
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_check_skill_frontmatter_valid_no_frontmatter(tmp_path):
    _write_skill_file(tmp_path, with_frontmatter=False)
    with mock.patch(
        "coordinator_core.ops.coordinator_doe_root.coordinator_doe_root", return_value=str(tmp_path)
    ):
        result = pp._check_skill_frontmatter_valid()
    assert result["ok"] is False
    assert "no parseable frontmatter" in result["error"]


def test_check_skill_frontmatter_valid_empty_description(tmp_path):
    _write_skill_file(tmp_path, description="")
    with mock.patch(
        "coordinator_core.ops.coordinator_doe_root.coordinator_doe_root", return_value=str(tmp_path)
    ):
        result = pp._check_skill_frontmatter_valid()
    assert result["ok"] is False
    assert "description" in result["error"]


def test_check_skill_frontmatter_valid_double_invocation_is_stable(tmp_path):
    """AC7: identical inputs, second invocation is a safe no-op with an identical result."""
    _write_skill_file(tmp_path)
    with mock.patch(
        "coordinator_core.ops.coordinator_doe_root.coordinator_doe_root", return_value=str(tmp_path)
    ):
        first = pp._check_skill_frontmatter_valid()
        second = pp._check_skill_frontmatter_valid()
    assert first == second == {"ok": True, "error": None}


def test_probe_skill_frontmatter_valid_pass(tmp_path):
    _write_skill_file(tmp_path)
    with mock.patch(
        "coordinator_core.ops.coordinator_doe_root.coordinator_doe_root", return_value=str(tmp_path)
    ):
        rec = _parse(pp.probe_skill_frontmatter_valid())
    assert rec["status"] == "pass"


def test_probe_skill_frontmatter_valid_warn_on_missing_file(tmp_path):
    with mock.patch(
        "coordinator_core.ops.coordinator_doe_root.coordinator_doe_root", return_value=str(tmp_path)
    ):
        rec = _parse(pp.probe_skill_frontmatter_valid())
    assert rec["status"] == "warn"
    assert "not found" in rec["detail"]


def test_op_probe_skill_frontmatter_valid_handler(tmp_path):
    _write_skill_file(tmp_path)
    with mock.patch(
        "coordinator_core.ops.coordinator_doe_root.coordinator_doe_root", return_value=str(tmp_path)
    ):
        # Review: code-reviewer — Finding 4. Handler is a plain sync `def`
        # (engine auto-offloads via asyncio.to_thread), called directly.
        result = pp._probe_skill_frontmatter_valid_op({})
    assert result == {"ok": True, "error": None}


# ---------------------------------------------------------------------------
# probe_windows_terminal_presence / install.probe_windows_terminal_presence
# ---------------------------------------------------------------------------
def test_check_windows_terminal_presence_non_windows_short_circuits():
    with mock.patch.object(pp.sys, "platform", "darwin"), mock.patch.object(pp, "_run") as run_mock:
        result = pp._check_windows_terminal_presence()
    assert result == {"present": None, "method": "not-applicable-non-windows"}
    run_mock.assert_not_called()


def test_check_windows_terminal_presence_found_on_path():
    with mock.patch.object(pp.sys, "platform", "win32"), mock.patch(
        "shutil.which", return_value="C:/Windows/wt.exe"
    ), mock.patch.object(pp, "_run") as run_mock:
        result = pp._check_windows_terminal_presence()
    assert result == {"present": True, "method": "path"}
    run_mock.assert_not_called()


def test_check_windows_terminal_presence_found_via_winget():
    with mock.patch.object(pp.sys, "platform", "win32"), mock.patch("shutil.which", return_value=None), mock.patch.object(
        pp, "_run", return_value=_cp(0, "Name    Id                            Version\nWindows Terminal    Microsoft.WindowsTerminal    1.19\n")
    ):
        result = pp._check_windows_terminal_presence()
    assert result == {"present": True, "method": "winget"}


def test_check_windows_terminal_presence_absent_via_winget():
    with mock.patch.object(pp.sys, "platform", "win32"), mock.patch("shutil.which", return_value=None), mock.patch.object(
        pp, "_run", return_value=_cp(0, "No installed package found matching input criteria.\n")
    ):
        result = pp._check_windows_terminal_presence()
    assert result == {"present": False, "method": "winget"}


def test_check_windows_terminal_presence_winget_unavailable():
    with mock.patch.object(pp.sys, "platform", "win32"), mock.patch("shutil.which", return_value=None), mock.patch.object(
        pp, "_run", return_value=None
    ):
        result = pp._check_windows_terminal_presence()
    assert result == {"present": None, "method": "winget-unavailable"}


def test_check_windows_terminal_presence_double_invocation_is_stable():
    """AC7: identical inputs, second invocation is a safe no-op with an identical result."""
    with mock.patch.object(pp.sys, "platform", "win32"), mock.patch("shutil.which", return_value="wt.exe"):
        first = pp._check_windows_terminal_presence()
        second = pp._check_windows_terminal_presence()
    assert first == second == {"present": True, "method": "path"}


def test_probe_windows_terminal_presence_non_windows_is_pass():
    with mock.patch.object(pp.sys, "platform", "darwin"):
        rec = _parse(pp.probe_windows_terminal_presence())
    assert rec["status"] == "pass"
    assert "non-Windows" in rec["detail"]


def test_probe_windows_terminal_presence_warn_when_absent():
    with mock.patch.object(pp.sys, "platform", "win32"), mock.patch("shutil.which", return_value=None), mock.patch.object(
        pp, "_run", return_value=_cp(0, "No installed package found matching input criteria.\n")
    ):
        rec = _parse(pp.probe_windows_terminal_presence())
    assert rec["status"] == "warn"
    assert "winget install Microsoft.WindowsTerminal" in rec["remediation"]


def test_probe_windows_terminal_presence_inconclusive_when_winget_unavailable():
    with mock.patch.object(pp.sys, "platform", "win32"), mock.patch("shutil.which", return_value=None), mock.patch.object(
        pp, "_run", return_value=None
    ):
        rec = _parse(pp.probe_windows_terminal_presence())
    assert rec["status"] == "inconclusive"


def test_op_probe_windows_terminal_presence_handler():
    with mock.patch.object(pp.sys, "platform", "win32"), mock.patch("shutil.which", return_value="wt.exe"):
        # Review: code-reviewer — Finding 1. Handler is a plain sync `def`
        # (engine auto-offloads via asyncio.to_thread), called directly.
        result = pp._probe_windows_terminal_presence_op({})
    assert result == {"present": True, "method": "path"}


# ---------------------------------------------------------------------------
# register_op import isolation — this module is vendored by sibling repos
# (e.g. Example-retrieval-repo-ue-addon) that carry no coordinator_core.ipc; it must
# import cleanly with a no-op register_op fallback in that environment while
# still using the real ipc.register_op (and populating ipc's registry) when
# ipc IS importable, the normal in-repo case.
# ---------------------------------------------------------------------------
def test_import_succeeds_and_ops_register_when_ipc_is_importable():
    """Positive case: normal import uses the real ipc.register_op, and both
    decorated probe ops land in ipc's registry (unaffected by this module's
    guarded import)."""
    import importlib

    from coordinator_core import ipc as real_ipc

    reloaded = importlib.reload(pp)
    assert reloaded.register_op is real_ipc.register_op
    assert callable(reloaded._probe_skill_frontmatter_valid_op)
    assert callable(reloaded._probe_windows_terminal_presence_op)
    assert real_ipc._REGISTRY.get("install.probe_skill_frontmatter_valid") is (
        reloaded._probe_skill_frontmatter_valid_op
    )
    assert real_ipc._REGISTRY.get("install.probe_windows_terminal_presence") is (
        reloaded._probe_windows_terminal_presence_op
    )


def test_import_succeeds_with_register_op_fallback_when_ipc_unimportable(monkeypatch):
    """Vendor case: coordinator_core.ipc unimportable (e.g. a sibling repo's
    vendored copy of this module, with no engine tree behind it). The module
    must still import cleanly, via the no-op register_op fallback, and its
    two decorated probe functions must still be plain callables.

    sys.modules["coordinator_core.ipc"] = None is the standard forcing idiom:
    Python treats a None entry as "this import previously failed" and raises
    ImportError immediately on the next `import coordinator_core.ipc`,
    without needing to actually delete the installed package.
    """
    import importlib
    import sys

    # monkeypatch restores both sys.modules entries on teardown (including
    # the module object this file's own `pp` reference points at, which is
    # a separate, untouched object from the one `import_module` below
    # produces) -- no manual reload needed.
    monkeypatch.setitem(sys.modules, "coordinator_core.ipc", None)
    monkeypatch.delitem(sys.modules, "coordinator_core.install.prereq_probe", raising=False)

    fresh = importlib.import_module("coordinator_core.install.prereq_probe")
    assert callable(fresh._probe_skill_frontmatter_valid_op)
    assert callable(fresh._probe_windows_terminal_presence_op)
    assert fresh.register_op.__module__ != "coordinator_core.ipc"
    # The fallback decorator returns the function unchanged.
    marker = object()
    assert fresh.register_op("some.op")(marker) is marker
