"""Behavioral tests for the drive-root / home new-top-level-entry Bash and PowerShell guard."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import _write_bump_sink_shapes as sinks
from coordinator_core.bash_guards import block_drive_root_new_dir_bash_write as guard
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "root"
    home = tmp_path / "home"
    work = tmp_path / "work"
    for d in (root, home, work, root / "existing", root / "placed" / ".git"):
        d.mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_DRIVE_ROOTS", str(root))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    monkeypatch.delenv("COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR", raising=False)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
    machine_profile.reset_cache()
    yield root, home, work
    machine_profile.reset_cache()


def _payload(cmd, tool="Bash", cwd=None, env=None):
    p = {"tool_name": tool, "tool_input": {"command": cmd}}
    if cwd is not None:
        p["cwd"] = str(cwd)
    if env is not None:
        p["env"] = env
    return p


def _denied(result):
    if result is None:
        return False
    out = result["hookSpecificOutput"]
    # Ruled advisory (deny_registry.ADVISORY_BY_RULING, DR-475, landed b17ec35251): the would-be
    # deny surfaces as an allow carrying the deny text, never a refusal.
    assert out["permissionDecision"] == "allow"
    assert result["guardLevel"] == "log"
    text = out["additionalContext"]
    assert "DENY:" in text
    assert "<repo>/scratch/" in text
    assert "COORDINATOR_" not in text
    return True


@pytest.mark.parametrize(
    "tool,tmpl",
    [
        ("Bash", "mkdir {r}/new"),
        ("Bash", "mkdir -p {r}/new/a"),
        ("Bash", "echo x > {r}/new/f"),
        ("Bash", "cp a {r}/new/"),
        ("Bash", "curl -o {r}/new/f http://u"),
        ("Bash", "curl --out={r}/new/f http://u"),
        ("PowerShell", "New-Item {r}/new -ItemType Directory"),
        ("PowerShell", "New-Item -Path {r} -Name new"),
        ("PowerShell", "mkdir {r}/new"),
        ("PowerShell", "md {r}/new"),
        ("PowerShell", '"x" | Out-File {r}/new/f'),
        ("PowerShell", "curl.exe -o {r}/new/f http://u"),
    ],
)
def test_new_root_entry_is_denied(world, tool, tmpl):
    root, _, work = world
    assert _denied(_enveloped(guard.check)(_payload(tmpl.format(r=root.as_posix()), tool, work)))


@pytest.mark.parametrize(
    "tmpl",
    [
        "mkdir {r}/existing",
        "mkdir -p {r}/existing/a/b",
        "echo x > {r}/placed/f",
        "grep -o foo f",
        "ls {r}/new",
    ],
)
def test_existing_or_nonwriting_is_allowed(world, tmpl):
    root, _, work = world
    assert _enveloped(guard.check)(_payload(tmpl.format(r=root.as_posix()), "Bash", work)) is None


@pytest.mark.parametrize(
    "tmpl",
    [
        "git grep -o {r}/new f",
        "git -C x grep -o {r}/new f",
        "grep -o {r}/new f",
        "grep -rno {r}/new f",
        "rg -o {r}/new f",
        "rg.exe -o {r}/new f",
    ],
)
def test_only_matching_search_with_absolute_pattern_is_allowed(world, tmpl):
    root, _, work = world
    assert _enveloped(guard.check)(_payload(tmpl.format(r=root.as_posix()), "Bash", work)) is None


def test_curl_output_still_denied_beside_only_matching_search(world):
    root, _, work = world
    r = root.as_posix()
    assert _denied(_enveloped(guard.check)(_payload(f"curl -o {r}/new/f http://u", "Bash", work)))
    assert _denied(_enveloped(guard.check)(_payload(f"git grep -o x f && curl -o {r}/new/f http://u", "Bash", work)))


def test_cwd_at_root(world):
    root, _, _ = world
    assert _denied(_enveloped(guard.check)(_payload("mkdir new", "Bash", root)))
    assert _enveloped(guard.check)(_payload("grep -o foo f", "Bash", root)) is None
    assert _enveloped(guard.check)(_payload("mkdir existing", "Bash", root)) is None


def test_home_rules(world):
    _, home, work = world
    assert _denied(_enveloped(guard.check)(_payload("mkdir -p ~/newdir/a", "Bash", work)))
    assert _denied(_enveloped(guard.check)(_payload("mkdir $HOME/newdir/a", "Bash", work)))
    assert _enveloped(guard.check)(_payload("mkdir ~/newdir", "Bash", work)) is None  # depth 1: block-home-root-bash-write owns it
    assert _enveloped(guard.check)(_payload("echo x > ~/f.txt", "Bash", work)) is None


def test_override_in_payload_env_allows(world):
    root, _, work = world
    cmd = f"mkdir {root.as_posix()}/new"
    assert _enveloped(guard.check)(_payload(cmd, "Bash", work, env={"COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR": "1"})) is None
    assert _denied(_enveloped(guard.check)(_payload(cmd, "Bash", work, env={"COORDINATOR_DRIVE_ROOTS": str(root)})))


def test_bare_slash_on_windows(world, monkeypatch):
    monkeypatch.setattr(sinks, "_host_is_windows", lambda: True)
    monkeypatch.setattr(guard, "_host_is_windows", lambda: True)
    monkeypatch.delenv("COORDINATOR_DRIVE_ROOTS", raising=False)
    cwd = "Z:/work"  # abs-path-ok: fake drive letter, never touched on disk
    for tool in ("PowerShell", "Bash"):
        assert _denied(_enveloped(guard.check)(_payload("echo x > /zz-new-top/f", tool, cwd)))
    assert _enveloped(guard.check)(_payload("echo x > /tmp/f", "Bash", cwd)) is None


@pytest.mark.parametrize(
    "cmd",
    ["pytest --basetemp=../root/new", "pytest --basetemp ../root/new/a", "curl --out ../root/new/f http://u"],
)
def test_relative_climbing_out_flag_is_judged(world, cmd):
    _, _, work = world
    assert _denied(_enveloped(guard.check)(_payload(cmd, "Bash", work)))


@pytest.mark.parametrize(
    "cmd",
    ["pytest --basetemp=../root/existing/t", "pytest --basetemp=tmp/x", "pytest --basetemp=../work/t"],
)
def test_relative_out_flag_inside_or_existing_is_allowed(world, cmd):
    _, _, work = world
    assert _enveloped(guard.check)(_payload(cmd, "Bash", work)) is None


@pytest.mark.parametrize(
    "tool,tmpl",
    [
        ("Bash", "PYTEST_DEBUG_TEMPROOT={r}/new python -m pytest x"),
        ("Bash", "TMPDIR={r}/new python -m pytest x"),
        ("Bash", "TEMP={r}/new TMP={r}/new python -m pytest x"),
        ("Bash", "export TMPDIR={r}/new && python -m pytest x"),
        ("Bash", "env TMPDIR={r}/new python -m pytest x"),
        ("Bash", "TMPDIR='{r}/new' pytest x"),
        ("PowerShell", "$env:TEMP='{r}/new'; python -m pytest x"),
        ("PowerShell", "$env:PYTEST_DEBUG_TEMPROOT = '{r}/new'; pytest x"),
        ("PowerShell", "python -m pytest --basetemp={rw}/new x"),
        ("PowerShell", "python -m pytest --basetemp {rw}/new x"),
    ],
)
def test_pytest_temp_root_route_into_a_new_root_entry_is_denied(world, tool, tmpl):
    root, _, work = world
    cmd = tmpl.format(r=root.as_posix(), rw=str(root))
    assert _denied(_enveloped(guard.check)(_payload(cmd, tool, work)))


@pytest.mark.parametrize(
    "tool,tmpl",
    [
        ("Bash", "TMPDIR={r}/existing/t python -m pytest x"),
        ("Bash", "TMPDIR=scratch/tmp python -m pytest x"),
        ("Bash", "TMP=$HOME_NOT_SET/x python -m pytest x"),
        ("Bash", "echo TMPDIR_NOTE=1"),
        ("PowerShell", "$env:TEMP='{r}/existing/t'; pytest x"),
        ("PowerShell", "python -m pytest --basetemp=scratch/x"),
    ],
)
def test_pytest_temp_root_route_inside_or_existing_is_allowed(world, tool, tmpl):
    root, _, work = world
    assert _enveloped(guard.check)(_payload(tmpl.format(r=root.as_posix()), tool, work)) is None


def test_delete_of_top_level_entry_is_not_a_creation(world, monkeypatch):
    monkeypatch.setattr(sinks, "_host_is_windows", lambda: True)
    monkeypatch.setattr(guard, "_host_is_windows", lambda: True)
    monkeypatch.delenv("COORDINATOR_DRIVE_ROOTS", raising=False)
    cwd = "Z:/work"  # abs-path-ok: fake drive letter, never touched on disk
    check = _enveloped(guard.check)
    for cmd in (
        "python -c \"import shutil; shutil.rmtree('/e/zz-probe-gone')\"",
        "python -c \"import os; os.unlink('/e/zz-probe-gone')\"",
        "python -c \"import os; os.system('rm -rf /e/zz-probe-gone*')\"",
    ):
        assert check(_payload(cmd, "Bash", cwd)) is None, cmd
    for cmd in (
        "python -c \"import os; os.makedirs('/e/zz-probe-gone')\"",
        "python -c \"import shutil; shutil.rmtree('/e/old'); os.makedirs('/e/zz-probe-gone')\"",
    ):
        assert _warned(guard.check(_payload(cmd, "Bash", cwd))), cmd


def _warned(result):
    out = envelope_of(result)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert "<repo>/scratch/" in out["additionalContext"]
    return True


def test_heredoc_prose_and_interpreter_body_warn_never_deny(world, monkeypatch):
    monkeypatch.setattr(sinks, "_host_is_windows", lambda: True)
    monkeypatch.setattr(guard, "_host_is_windows", lambda: True)
    monkeypatch.delenv("COORDINATOR_DRIVE_ROOTS", raising=False)
    cwd = "Z:/work"  # abs-path-ok: fake drive letter, never touched on disk
    py = "python - <<'EOF'\nprint('see /x/ for details')\nEOF"
    assert _warned(guard.check(_payload(py, "Bash", cwd)))
    prose = "cat <<'EOF'\nmkdir /zz-prose-dir\nEOF"
    assert guard.check(_payload(prose, "Bash", cwd)) is None
    assert _denied(_enveloped(guard.check)(_payload("mkdir /zz-new-top", "Bash", cwd)))


def test_heredoc_strip_keeps_redirect_target(world):
    root, _, work = world
    cmd = f"cat > {root.as_posix()}/new/f <<'EOF'\nbody\nEOF"
    assert _denied(_enveloped(guard.check)(_payload(cmd, "Bash", work)))


def test_shell_variable_assignment_to_msys_path_is_not_a_write(world, monkeypatch):
    monkeypatch.setattr(sinks, "_host_is_windows", lambda: True)
    monkeypatch.setattr(guard, "_host_is_windows", lambda: True)
    monkeypatch.delenv("COORDINATOR_DRIVE_ROOTS", raising=False)
    cwd = "Z:/work"  # abs-path-ok: fake drive letter, never touched on disk
    c = chr(47) + "c" + chr(47)
    check = _enveloped(guard.check)
    for cmd in (
        f"B={c}Users/x/bin; ls $B | wc -l",
        f"B={c}Users/x",
    ):
        assert check(_payload(cmd, "Bash", cwd)) is None, cmd
    assert _denied(check(_payload(f"mkdir {c}zz-c2-newdir", "Bash", cwd)))


class _Counter:
    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self.fn(*a, **k)


@pytest.fixture
def counters(monkeypatch):
    c1 = _Counter(guard.resolve_command_positions)
    c2 = _Counter(guard.resolve_segments_for_dialect)
    monkeypatch.setattr(guard, "resolve_command_positions", c1)
    monkeypatch.setattr(guard, "resolve_segments_for_dialect", c2)
    return c1, c2


def test_prepass_skips_tokenising_when_all_tops_exist(world, counters):
    root, _, work = world
    cmd = f"cat {root.as_posix()}/existing/a > {root.as_posix()}/existing/b"
    assert _enveloped(guard.check)(_payload(cmd, "Bash", work)) is None
    assert sum(c.calls for c in counters) == 0


def test_prepass_admits_new_and_root_and_cwd_root(world, counters):
    root, _, work = world
    r = root.as_posix()
    assert _denied(_enveloped(guard.check)(_payload(f"mkdir {r}/new", "Bash", work)))
    assert counters[0].calls >= 1
    assert _denied(_enveloped(guard.check)(_payload(f"New-Item -Path {r} -Name new", "PowerShell", work)))
    assert counters[1].calls >= 1
    before = counters[0].calls
    assert _denied(_enveloped(guard.check)(_payload("mkdir new", "Bash", root)))
    assert counters[0].calls > before


_PM_DIRS = ("ptbase-c1", "tmp-c2", "ps7-c5-tmp", "pytest-reverify-warp-2026-11-12-x-0035cb", "_agent-work")


@pytest.mark.parametrize("name", _PM_DIRS)
@pytest.mark.parametrize(
    "tool,tmpl",
    [
        ("Bash", "pytest --basetemp {r}/{n}"),
        ("Bash", "pytest --basetemp={r}/{n} -q"),
        ("Bash", "TMP={r}/{n} pytest"),
        ("Bash", "FOO=1 TEMP={r}/{n} pytest"),
        ("Bash", "export TMPDIR={r}/{n}"),
        ("Bash", 'export TMP="{r}/{n}" && pytest'),
        ("Bash", "env TEMP={r}/{n} pytest"),
        ("PowerShell", "pytest --basetemp {r}/{n}"),
        ("PowerShell", "TMP={r}/{n} pytest"),
        ("PowerShell", "$env:TEMP='{r}/{n}'"),
        ("PowerShell", "$env:TMP = '{r}/{n}'; pytest"),
        ("PowerShell", "Set-Item env:TMPDIR {r}/{n}"),
        ("PowerShell", "Set-Item -Path env:TEMP -Value {r}/{n}"),
        ("PowerShell", "[Environment]::SetEnvironmentVariable('TMP','{r}/{n}')"),
    ],
)
def test_basetemp_and_temp_env_to_new_root_entry_is_denied(world, tool, tmpl, name):
    root, _, work = world
    assert _denied(_enveloped(guard.check)(_payload(tmpl.format(r=root.as_posix(), n=name), tool, work)))


@pytest.mark.parametrize(
    "tool,tmpl",
    [
        ("Bash", "pytest --basetemp {r}/existing/t"),
        ("Bash", "TMP={r}/existing/t pytest"),
        ("Bash", "export TMPDIR={r}/existing"),
        ("Bash", "TMP=tmp/x pytest"),
        ("Bash", "TMP=/tmp/x pytest"),
        ("PowerShell", "$env:TEMP='{r}/existing/t'"),
        ("PowerShell", "Set-Item env:TMP {r}/existing"),
        ("PowerShell", "$env:TMP = 'relative/x'; pytest"),
    ],
)
def test_basetemp_and_temp_env_to_existing_or_nonroot_is_allowed(world, tool, tmpl):
    root, _, work = world
    assert _enveloped(guard.check)(_payload(tmpl.format(r=root.as_posix()), tool, work)) is None


@pytest.mark.parametrize(
    "tool,cmd,denied",
    [
        ("Bash", 'echo "TMP={r}/newroot"', False),
        ("Bash", "printf 'TEMP={r}/newroot\n'", False),
        ("PowerShell", 'Write-Host "$env:TMP={r}/newroot"', False),
        ("Bash", 'echo hi; TMP={r}/newroot pytest', True),
        ("Bash", 'echo "x" && TMP={r}/newroot pytest', True),
        ("Bash", 'bash -c "TMP={r}/newroot pytest"', True),
    ],
)
def test_quoted_print_text_is_not_a_temp_route(world, tool, cmd, denied):
    root, _, work = world
    result = _enveloped(guard.check)(_payload(cmd.format(r=root.as_posix()), tool, work))
    assert bool(_denied(result)) is denied


def test_managed_temp_layout_basetemp_and_env_pass(world):
    import tempfile
    from pathlib import Path

    _, _, work = world
    managed = (Path(tempfile.gettempdir()) / "coordinator" / "repo" / "pytest").as_posix()
    check = _enveloped(guard.check)
    for tool, cmd in (
        ("Bash", f"pytest --basetemp {managed}"),
        ("Bash", f"TMP={managed} pytest"),
        ("PowerShell", f"$env:TEMP='{managed}'"),
    ):
        assert check(_payload(cmd, tool, work)) is None, cmd


def test_msys_and_native_spellings_of_drive_root_basetemp_and_temp_env(world, monkeypatch):
    monkeypatch.setattr(sinks, "_host_is_windows", lambda: True)
    monkeypatch.setattr(guard, "_host_is_windows", lambda: True)
    monkeypatch.delenv("COORDINATOR_DRIVE_ROOTS", raising=False)
    cwd = "Z:/work"  # abs-path-ok: fake drive letter, never touched on disk
    check = _enveloped(guard.check)
    for tool, cmd in (
        ("Bash", "pytest --basetemp /zz-ptbase-c1"),
        ("Bash", "export TMPDIR=/zz-x"),
        ("Bash", "TMP=/zz-tmp-c2 pytest"),
        ("PowerShell", r"$env:TEMP='Z:\zz-ps7-c5-tmp'"),
        ("PowerShell", "pytest --basetemp Z:/zz-ptbase-c1"),
    ):
        assert _denied(check(_payload(cmd, tool, cwd))), cmd
    assert check(_payload("TMP=/tmp/x pytest", "Bash", cwd)) is None


def test_malformed_payload_is_allowed(world):
    assert _enveloped(guard.check)({"tool_name": "Bash", "tool_input": None}) is None
    assert _enveloped(guard.check)({"tool_name": "Read"}) is None
