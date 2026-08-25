"""Characterization + parity tests for coordinator_core.ops.new_project_scaffold.

Spec backlink: docs/plans/2026-06-22-new-project-bootstrap-skill.md § C3
"""
from __future__ import annotations

import os
import stat
import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops import new_project_scaffold
from coordinator_core.ops.new_project_scaffold import main

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# Captured BEFORE the module-scoped _stub_repo_registration autouse fixture
# (below) ever runs, so the dedicated "Repo self-registration corpus" tests
# can exercise the REAL _register_repo even though every other test in this
# module gets it stubbed out.
_REAL_REGISTER_REPO = new_project_scaffold._register_repo


def _write_render_template_tree_sh(bin_dir: Path) -> Path:
    """A minimal, faithful-enough stand-in for render-template-tree.py's CLI contract
    (<src> <dst> [KEY=VALUE]...) -- plain-copy + naive {{KEY}} substitution, without
    depending on the real DoE clone or a real render-template.py being present on the
    test machine."""
    script = bin_dir / "render-template-tree.py"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import shutil
            import sys
            import os

            args = sys.argv[1:]
            src, dst = args[0], args[1]
            kv_pairs = args[2:]

            if os.path.isdir(dst) and os.listdir(dst):
                sys.stderr.write("render-template-tree: dst dir already exists and is non-empty\\n")
                sys.exit(1)

            shutil.copytree(src, dst, dirs_exist_ok=True)

            subs = dict(p.split("=", 1) for p in kv_pairs)
            for dirpath, _dirnames, filenames in os.walk(dst):
                for name in filenames:
                    fpath = os.path.join(dirpath, name)
                    with open(fpath, "r") as fh:
                        content = fh.read()
                    for k, v in subs.items():
                        content = content.replace("{{" + k + "}}", v)
                    if "{{" in content:
                        sys.stderr.write(f"render-template-tree: unsubstituted tokens in {fpath}\\n")
                        sys.exit(1)
                    with open(fpath, "w") as fh:
                        fh.write(content)
            sys.exit(0)
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture()
def doe_root(tmp_path: Path) -> Path:
    root = tmp_path / "doe-clone"
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    _write_render_template_tree_sh(bin_dir)

    next_app = root / "coordinator" / "skills" / "new-project" / "templates" / "next-app"
    next_app.mkdir(parents=True)
    (next_app / "package.json").write_text('{ "name": "{{PROJECT_NAME}}" }\n')
    (next_app / "README.md").write_text("# {{PROJECT_NAME}}\n")
    return root


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    monkeypatch.delenv("COORDINATOR_PROJECTS_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _stub_repo_registration(monkeypatch):
    """Stub out `_register_repo` for every test in this module by default.

    The repo-self-registration step (§ scratchpad/scout-D-makima-sizing.md
    Item 3) shells out to a real `machine-local` binary, which is not
    guaranteed to be on PATH in a test sandbox. Tests that specifically
    exercise the registration behavior (see "Repo self-registration corpus"
    below) override this stub explicitly.
    """
    monkeypatch.setattr(new_project_scaffold, "_register_repo", lambda *a, **k: 0)


# ---------------------------------------------------------------------------
# Positive corpus
# ---------------------------------------------------------------------------


def test_empty_template_no_smoke(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    rc = main(["--name", "myproj", "--parent", str(parent), "--template", "empty", "--no-smoke"])

    assert rc == 0
    target = parent / "myproj"
    assert target.is_dir()
    assert (target / ".git").is_dir()
    assert "project_type: general" in (target / "coordinator.local.md").read_text()
    assert (target / "README.md").read_text() == "# myproj\n"


def test_next_app_template_renders_and_seeds(tmp_path, monkeypatch, doe_root):
    # Force the DoE-root fallback rung: co-located resolution now wins
    # unconditionally, so this test's fixture-authored render-template-tree.py
    # (staged under doe_root) would otherwise never run.
    monkeypatch.setattr(new_project_scaffold, "_co_located_render_tree", lambda: None)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    parent = tmp_path / "parent"
    parent.mkdir()

    rc = main(["--name", "webapp", "--parent", str(parent), "--template", "next-app", "--no-smoke"])

    assert rc == 0
    target = parent / "webapp"
    assert target.is_dir()
    assert (target / ".git").is_dir()
    assert (target / "package.json").read_text() == '{ "name": "webapp" }\n'
    assert "{{" not in (target / "package.json").read_text()
    assert "project_type: web-dev" in (target / "coordinator.local.md").read_text()


def test_git_branch_is_main(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    rc = main(["--name", "branchapp", "--parent", str(parent), "--template", "empty", "--no-smoke"])
    assert rc == 0

    head = (parent / "branchapp" / ".git" / "HEAD").read_text()
    assert "refs/heads/main" in head


def test_parent_flag_wins_over_env(tmp_path, monkeypatch):
    env_root = tmp_path / "env-root"
    env_root.mkdir()
    explicit_parent = tmp_path / "explicit-parent"
    explicit_parent.mkdir()
    monkeypatch.setenv("COORDINATOR_PROJECTS_ROOT", str(env_root))

    rc = main(["--name", "flagapp", "--parent", str(explicit_parent), "--template", "empty", "--no-smoke"])

    assert rc == 0
    assert (explicit_parent / "flagapp").is_dir()
    assert not (env_root / "flagapp").exists()


def test_env_root_wins_over_default(tmp_path, monkeypatch):
    env_root = tmp_path / "env-root2"
    env_root.mkdir()
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("COORDINATOR_PROJECTS_ROOT", str(env_root))
    monkeypatch.setenv("HOME", str(fake_home))

    rc = main(["--name", "envapp", "--template", "empty", "--no-smoke"])

    assert rc == 0
    assert (env_root / "envapp").is_dir()
    assert not (fake_home / "Code_Projects" / "envapp").exists()


def test_default_parent_uses_home_code_projects(tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home2"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    rc = main(["--name", "defaultapp", "--template", "empty", "--no-smoke"])

    assert rc == 0
    assert (fake_home / "Code_Projects" / "defaultapp").is_dir()


def test_existing_empty_target_dir_is_reused(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    target = parent / "reused"
    target.mkdir(parents=True)

    rc = main(["--name", "reused", "--parent", str(parent), "--template", "empty", "--no-smoke"])

    assert rc == 0
    assert (target / ".git").is_dir()


# ---------------------------------------------------------------------------
# Negative corpus
# ---------------------------------------------------------------------------


def test_missing_name_fails(tmp_path):
    rc = main(["--parent", str(tmp_path), "--template", "empty"])
    assert rc == 1


def test_name_requires_value_fails():
    rc = main(["--name"])
    assert rc == 1


def test_unknown_arg_fails():
    rc = main(["--name", "x", "--bogus"])
    assert rc == 1


def test_bad_template_value_fails(tmp_path):
    rc = main(["--name", "x", "--parent", str(tmp_path), "--template", "nope"])
    assert rc == 1


def test_occupied_nonempty_target_dir_fails(tmp_path):
    parent = tmp_path / "parent"
    target = parent / "occupied"
    target.mkdir(parents=True)
    (target / "file.txt").write_text("existing-content\n")

    rc = main(["--name", "occupied", "--parent", str(parent), "--no-smoke"])

    assert rc == 1


def test_next_app_missing_doe_root_fails(tmp_path, monkeypatch):
    # No REPO_DOE_CLAUDE, no machine-local on PATH -> DoE root unresolvable.
    monkeypatch.setenv("PATH", "")
    parent = tmp_path / "parent"
    parent.mkdir()

    rc = main(["--name", "nodoe", "--parent", str(parent), "--template", "next-app", "--no-smoke"])

    assert rc == 1


def test_next_app_missing_template_dir_fails(tmp_path, monkeypatch):
    bare_root = tmp_path / "bare-doe"
    (bare_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(bare_root))
    parent = tmp_path / "parent"
    parent.mkdir()

    rc = main(["--name", "notemplate", "--parent", str(parent), "--template", "next-app", "--no-smoke"])

    assert rc == 1


def test_next_app_missing_render_tree_script_fails(tmp_path, monkeypatch):
    root = tmp_path / "no-render-tree-doe"
    next_app = root / "coordinator" / "skills" / "new-project" / "templates" / "next-app"
    next_app.mkdir(parents=True)
    (next_app / "a.txt").write_text("hi\n")
    (root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(root))
    parent = tmp_path / "parent"
    parent.mkdir()

    rc = main(["--name", "norender", "--parent", str(parent), "--template", "next-app", "--no-smoke"])

    assert rc == 1


# ---------------------------------------------------------------------------
# Repo self-registration corpus (_register_repo) -- these tests deliberately
# do NOT use the module-level _stub_repo_registration autouse fixture's
# effect; they either replace PATH with a fake machine-local stub, or clear
# PATH to exercise the missing-binary failure path.
# ---------------------------------------------------------------------------


def _write_fake_machine_local(bin_dir: Path, store: Path) -> Path:
    """A minimal `machine-local set|get <key> [<value>]` stand-in backed by a
    flat `key<TAB>value` line-store file, sufficient to exercise
    _register_repo's set-then-verify-via-get round-trip without depending on
    the real `machine-local` CLI being installed on the test machine."""
    script = bin_dir / "machine-local"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import sys

            store = {str(store)!r}

            def _read():
                out = {{}}
                try:
                    with open(store, "r") as fh:
                        for line in fh:
                            if "\\t" in line:
                                k, v = line.rstrip("\\n").split("\\t", 1)
                                out[k] = v
                except FileNotFoundError:
                    pass
                return out

            def _write(d):
                with open(store, "w") as fh:
                    for k, v in d.items():
                        fh.write(k + "\\t" + v + "\\n")

            args = sys.argv[1:]
            cmd = args[0]
            key = args[1]
            if cmd == "set":
                value = args[2]
                d = _read()
                d[key] = value
                _write(d)
                sys.exit(0)
            elif cmd == "get":
                d = _read()
                if key in d:
                    print(d[key])
                    sys.exit(0)
                sys.exit(1)
            sys.exit(1)
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_register_repo_success(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    store = tmp_path / "store.tsv"
    _write_fake_machine_local(bin_dir, store)
    # PREPEND (not replace) PATH -- the fake script's own `#!/usr/bin/env
    # python3` shebang needs a real python3 discoverable on PATH too.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    target = tmp_path / "target-dir" / "My-Cool-App"
    target.mkdir(parents=True)

    rc = _REAL_REGISTER_REPO("My-Cool-App", str(target))

    assert rc == 0
    assert store.read_text() == f"repos.my_cool_app\t{os.path.abspath(str(target))}\n"


def test_register_repo_missing_machine_local_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    target = tmp_path / "target-dir" / "app"
    target.mkdir(parents=True)

    rc = _REAL_REGISTER_REPO("app", str(target))

    assert rc == 1


def test_register_repo_verify_mismatch_fails(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin2"
    bin_dir.mkdir()
    store = tmp_path / "store2.tsv"
    _write_fake_machine_local(bin_dir, store)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    # Pre-seed the store with a DIFFERENT value for the slug the real `set`
    # call is about to write -- simulates a `get` that returns something
    # other than what `set` just wrote (verify-mismatch path).
    target = tmp_path / "target-dir" / "app"
    target.mkdir(parents=True)
    monkeypatch.setattr(
        new_project_scaffold,
        "_resolve_machine_local",
        lambda: str(bin_dir / "machine-local"),
    )

    def _fake_run(cmd, **kwargs):
        import subprocess as _sp

        if cmd[1] == "get":
            return _sp.CompletedProcess(cmd, 0, stdout="/some/other/path\n", stderr="")
        return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(new_project_scaffold.subprocess, "run", _fake_run)

    rc = _REAL_REGISTER_REPO("app", str(target))

    assert rc == 1
