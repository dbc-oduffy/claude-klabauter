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
    assert out["permissionDecision"] == "deny"
    assert "<repo>/scratch/" in out["permissionDecisionReason"]
    assert "COORDINATOR_" not in out["permissionDecisionReason"]
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
        assert _denied(check(_payload(cmd, "Bash", cwd))), cmd


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


def test_malformed_payload_is_allowed(world):
    assert _enveloped(guard.check)({"tool_name": "Bash", "tool_input": None}) is None
    assert _enveloped(guard.check)({"tool_name": "Read"}) is None
