"""Characterization + parity tests for coordinator_core.ops.new_project_scaffold.

Spec backlink: docs/plans/2026-06-22-new-project-bootstrap-skill.md § C3
"""
from __future__ import annotations

import json
import os
import stat
import sys
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
def content_root(tmp_path: Path) -> Path:
    root = tmp_path / "content-clone"
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
    monkeypatch.setattr(new_project_scaffold, "read_content_root", lambda: "")
    monkeypatch.delenv("COORDINATOR_PROJECTS_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _stub_repo_registration(monkeypatch):
    """Stub out `_register_repo` for every test in this module by default.

    The repo-self-registration step (§ scratchpad/scout-D-claude-klabauter-sizing.md
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


def test_python_package_template_no_smoke(tmp_path, capsys):
    parent = tmp_path / "parent"
    parent.mkdir()
    rc = main(["--name", "My-Pkg", "--parent", str(parent), "--template", "python-package", "--no-smoke"])

    assert rc == 0
    target = parent / "My-Pkg"
    assert (target / ".git").is_dir()
    assert 'build-backend = "setuptools.build_meta"' in (target / "pyproject.toml").read_text()
    assert (target / "my_pkg" / "__init__.py").is_file()
    assert (target / "tests" / "test_smoke.py").is_file()
    assert (target / "scripts" / "run_fast_tests.py").is_file()
    assert "project_type: general" in (target / "coordinator.local.md").read_text()
    assert (target / "README.md").read_text() == "# My-Pkg\n"
    manifest = _manifest(capsys.readouterr().out)
    assert {"pyproject.toml", "my_pkg/__init__.py", "tests/test_smoke.py",
            "scripts/run_fast_tests.py"} <= set(manifest)


def test_python_package_seed_tests_pass_via_fast_runner(tmp_path):
    import subprocess
    import sys

    parent = tmp_path / "parent"
    parent.mkdir()
    assert main(["--name", "runme", "--parent", str(parent), "--template", "python-package",
                 "--no-smoke"]) == 0
    target = parent / "runme"
    proc = subprocess.run([sys.executable, "scripts/run_fast_tests.py"], cwd=target,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_bad_template_message_names_all_templates(tmp_path, capsys):
    assert main(["--name", "x", "--parent", str(tmp_path), "--template", "bogus"]) == 1
    err = capsys.readouterr().err
    assert "next-app" in err and "empty" in err and "python-package" in err


def test_next_app_template_renders_and_seeds(tmp_path, monkeypatch, content_root):
    # Force the content-root fallback rung: co-located resolution now wins
    # unconditionally, so this test's fixture-authored render-template-tree.py
    # (staged under content_root) would otherwise never run.
    monkeypatch.setattr(new_project_scaffold, "_co_located_render_tree", lambda: None)
    monkeypatch.setattr(new_project_scaffold, "read_content_root", lambda: str(content_root))
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


def _manifest(out: str) -> list:
    prefix = "new_project.scaffold.file_manifest: "
    lines = [ln for ln in out.splitlines() if ln.startswith(prefix)]
    assert len(lines) == 1
    return json.loads(lines[0][len(prefix):])


def test_empty_template_reports_only_seed_files_in_manifest(tmp_path, capsys):
    parent = tmp_path / "parent"
    parent.mkdir()
    rc = main(["--name", "myproj", "--parent", str(parent), "--template", "empty", "--no-smoke"])

    assert rc == 0
    assert _manifest(capsys.readouterr().out) == ["README.md", "coordinator.local.md"]


def test_next_app_manifest_lists_written_files_without_setup_entry_points(
    tmp_path, monkeypatch, content_root, capsys
):
    monkeypatch.setattr(new_project_scaffold, "_co_located_render_tree", lambda: None)
    monkeypatch.setattr(new_project_scaffold, "read_content_root", lambda: str(content_root))
    parent = tmp_path / "parent"
    parent.mkdir()

    rc = main(["--name", "webapp", "--parent", str(parent), "--template", "next-app", "--no-smoke"])

    assert rc == 0
    manifest = _manifest(capsys.readouterr().out)
    assert manifest == ["README.md", "coordinator.local.md", "package.json"]
    assert "setup.sh" not in manifest and "setup.ps1" not in manifest
    target = parent / "webapp"
    assert all((target / rel).is_file() for rel in manifest)


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
    monkeypatch.setattr(new_project_scaffold, "_is_consumer", lambda: False)
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


def test_project_type_and_subtypes_seed_config(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    rc = main(
        ["--name", "g", "--parent", str(parent), "--template", "empty", "--no-smoke",
         "--project-type", "game-dev", "--subtypes", "unreal, multiplayer"]
    )
    assert rc == 0
    text = (parent / "g" / "coordinator.local.md").read_text()
    assert text == "---\nproject_type: game-dev\nproject_subtypes: [unreal, multiplayer]\n---\n"


def test_project_type_overrides_next_app_default_without_subtypes(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    monkeypatch.setattr(new_project_scaffold, "_git_init_main", lambda t: 0)
    rc = main(["--name", "d", "--parent", str(parent), "--template", "empty",
               "--project-type", "data-science"])
    assert rc == 0
    text = (parent / "d" / "coordinator.local.md").read_text()
    assert "project_type: data-science" in text
    assert "project_subtypes" not in text


def _make_sibling(root: Path, with_local: bool = True) -> Path:
    root.mkdir(parents=True)
    if with_local:
        (root / "coordinator.local.md").write_text(
            "---\nproject_type: game-dev\nproject_subtypes: [unreal, multiplayer]\n---\n", encoding="utf-8"
        )
    (root / "CLAUDE.md").write_text(
        "# sib\n\n## What this repo is\n\nsib-specific prose\n\n## Runtime conventions\n\n"
        "- use naked Python\n\n## Build\n\nbuild prose\n",
        encoding="utf-8",
    )
    return root


def test_like_path_seeds_type_subtypes_and_conventions(tmp_path):
    sib = _make_sibling(tmp_path / "sib")
    parent = tmp_path / "parent"
    parent.mkdir()
    rc = main(["--name", "X", "--parent", str(parent), "--template", "empty", "--no-smoke", "--like", str(sib)])
    assert rc == 0
    assert (parent / "X" / "coordinator.local.md").read_text() == (
        "---\nproject_type: game-dev\nproject_subtypes: [unreal, multiplayer]\n---\n"
    )
    claude = (parent / "X" / "CLAUDE.md").read_text()
    assert "## Runtime conventions" in claude and "naked Python" in claude
    assert "sib-specific prose" not in claude and "build prose" not in claude


def test_like_registry_slug_and_explicit_flags_win(tmp_path, monkeypatch):
    sib = _make_sibling(tmp_path / "sib")
    monkeypatch.setattr(
        new_project_scaffold, "_registry_get", lambda key: str(sib) if key == "repos.my_sib" else None
    )
    parent = tmp_path / "parent"
    parent.mkdir()
    rc = main(["--name", "X", "--parent", str(parent), "--template", "empty", "--no-smoke",
               "--like", "my-sib", "--project-type", "general"])
    assert rc == 0
    text = (parent / "X" / "coordinator.local.md").read_text()
    assert "project_type: general" in text and "project_subtypes: [unreal, multiplayer]" in text


def test_like_conventions_survive_fenced_hash_lines(tmp_path):
    sib = _make_sibling(tmp_path / "sib")
    (sib / "CLAUDE.md").write_text(
        "# sib\n\n## Runtime conventions\n\n```sh\n# a comment\n## not a heading\n```\n- after fence\n\n## Build\n\nbuild prose\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent"
    parent.mkdir()
    assert main(["--name", "X", "--parent", str(parent), "--template", "empty", "--no-smoke",
                 "--like", str(sib)]) == 0
    claude = (parent / "X" / "CLAUDE.md").read_text()
    assert "- after fence" in claude and "build prose" not in claude


def test_like_manifest_lists_claude_md(tmp_path, capsys):
    sib = _make_sibling(tmp_path / "sib")
    parent = tmp_path / "parent"
    parent.mkdir()
    assert main(["--name", "X", "--parent", str(parent), "--template", "empty", "--no-smoke",
                 "--like", str(sib)]) == 0
    assert _manifest(capsys.readouterr().out) == ["CLAUDE.md", "README.md", "coordinator.local.md"]


def test_like_unresolved_or_without_local_md_fails_before_creating(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(new_project_scaffold, "_registry_get", lambda key: None)
    bare = _make_sibling(tmp_path / "bare", with_local=False)
    for like in (str(tmp_path / "missing"), str(bare)):
        rc = main(["--name", "x", "--parent", str(tmp_path), "--template", "empty", "--like", like])
        assert rc == 1
        assert not (tmp_path / "x").exists()
        assert capsys.readouterr().err.strip().count("\n") == 0
    assert main(["--name", "x", "--like"]) == 1


@pytest.mark.parametrize(
    "extra",
    [["--project-type", "bogus"], ["--project-type"], ["--subtypes"],
     ["--subtypes", "ok,Bad Name"], ["--subtypes", "a,,b"]],
)
def test_bad_project_type_or_subtypes_fail_before_creating(tmp_path, extra):
    rc = main(["--name", "x", "--parent", str(tmp_path), "--template", "empty", *extra])
    assert rc == 1
    assert not (tmp_path / "x").exists()


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


def test_next_app_missing_content_root_fails(tmp_path, monkeypatch):
    # The autouse fixture leaves the content root unresolved.
    parent = tmp_path / "parent"
    parent.mkdir()

    rc = main(["--name", "nocontent", "--parent", str(parent), "--template", "next-app", "--no-smoke"])

    assert rc == 1


def test_next_app_missing_template_dir_fails(tmp_path, monkeypatch):
    bare_root = tmp_path / "bare-content"
    (bare_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setattr(new_project_scaffold, "read_content_root", lambda: str(bare_root))
    parent = tmp_path / "parent"
    parent.mkdir()

    rc = main(["--name", "notemplate", "--parent", str(parent), "--template", "next-app", "--no-smoke"])

    assert rc == 1


def test_next_app_missing_render_tree_script_fails(tmp_path, monkeypatch):
    root = tmp_path / "no-render-tree-content"
    next_app = root / "coordinator" / "skills" / "new-project" / "templates" / "next-app"
    next_app.mkdir(parents=True)
    (next_app / "a.txt").write_text("hi\n")
    (root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setattr(new_project_scaffold, "_co_located_render_tree", lambda: None)
    monkeypatch.setattr(new_project_scaffold, "read_content_root", lambda: str(root))
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
                    pass  # no store file yet -- fresh-install cold start, not a real error
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
    if os.name == "nt":
        # An extensionless script is not launchable on Windows; a .cmd shim is
        # what shutil.which resolves through PATHEXT.
        shim = bin_dir / "machine-local.cmd"
        shim.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        return shim
    return script


@pytest.fixture
def _not_scratch(monkeypatch):
    """pytest's tmp_path is itself under the temp root; the registration-path tests need a non-scratch target."""
    monkeypatch.setattr(new_project_scaffold, "_is_scratch_target", lambda p: False)


def _run_register_with_spy(monkeypatch, target):
    import subprocess as _sp

    calls = []

    def _spy(cmd, **kwargs):
        calls.append(cmd)
        return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(new_project_scaffold, "_resolve_machine_local", lambda: "machine-local")
    monkeypatch.setattr(new_project_scaffold.subprocess, "run", _spy)
    return _REAL_REGISTER_REPO("probe", str(target)), calls


def test_register_repo_skips_target_under_temp_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(new_project_scaffold.tempfile, "gettempdir", lambda: str(tmp_path))
    target = tmp_path / "probe"
    target.mkdir()

    rc, calls = _run_register_with_spy(monkeypatch, target)

    assert rc == 0
    assert calls == []
    err = capsys.readouterr().err
    assert "not registering repos.probe" in err


def test_register_repo_skips_target_under_scratch_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(new_project_scaffold.tempfile, "gettempdir", lambda: str(tmp_path / "elsewhere"))
    target = tmp_path / "repo" / "scratch" / "tmp.abc" / "probe"
    target.mkdir(parents=True)

    rc, calls = _run_register_with_spy(monkeypatch, target)

    assert rc == 0
    assert calls == []
    assert "not registering repos.probe" in capsys.readouterr().err


def test_register_repo_still_registers_non_scratch_target(tmp_path, monkeypatch):
    monkeypatch.setattr(new_project_scaffold.tempfile, "gettempdir", lambda: str(tmp_path / "elsewhere"))
    target = tmp_path / "Code_Projects" / "realapp"
    target.mkdir(parents=True)
    monkeypatch.setattr(new_project_scaffold, "_registry_get", lambda key: str(target).replace(os.sep, "/"))

    rc, calls = _run_register_with_spy(monkeypatch, target)

    assert rc == 0
    assert [c[1:3] for c in calls] == [["set", "repos.probe"]]


def test_main_scaffold_into_temp_parent_writes_no_registry_key(tmp_path, monkeypatch, capsys):
    import subprocess as _sp

    calls = []
    monkeypatch.setattr(new_project_scaffold, "_register_repo", _REAL_REGISTER_REPO)
    monkeypatch.setattr(new_project_scaffold, "_resolve_machine_local", lambda: "machine-local")
    real_run = _sp.run

    def _spy(cmd, **kwargs):
        if cmd and cmd[0] == "machine-local":
            calls.append(cmd)
            return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(new_project_scaffold.subprocess, "run", _spy)
    monkeypatch.setattr(new_project_scaffold.tempfile, "gettempdir", lambda: str(tmp_path))
    parent = tmp_path / "p"
    parent.mkdir()

    rc = main(["--name", "zzprobe", "--parent", str(parent), "--template", "empty", "--no-smoke"])

    assert rc == 0
    assert calls == []
    assert "not registering repos.zzprobe" in capsys.readouterr().err


def test_register_repo_success(tmp_path, monkeypatch, _not_scratch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    store = tmp_path / "store.tsv"
    _write_fake_machine_local(bin_dir, store)
    # PREPEND (not replace) PATH -- the fake script's own `#!/usr/bin/env
    # python3` shebang needs a real python3 discoverable on PATH too.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # The verify leg reads the registry in-process; back it with the fake's store.
    monkeypatch.setattr(
        new_project_scaffold,
        "_registry_get",
        lambda key: dict(
            line.rstrip("\n").split("\t", 1) for line in store.read_text().splitlines(True)
        ).get(key),
    )

    target = tmp_path / "target-dir" / "My-Cool-App"
    target.mkdir(parents=True)

    rc = _REAL_REGISTER_REPO("My-Cool-App", str(target))

    assert rc == 0
    assert store.read_text() == f"repos.my_cool_app\t{os.path.abspath(str(target))}\n"


def test_register_repo_missing_machine_local_fails(tmp_path, monkeypatch, _not_scratch):
    monkeypatch.setenv("PATH", "")
    target = tmp_path / "target-dir" / "app"
    target.mkdir(parents=True)

    rc = _REAL_REGISTER_REPO("app", str(target))

    assert rc == 1


def test_register_repo_verify_mismatch_fails(tmp_path, monkeypatch, _not_scratch):
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


def test_consumer_default_parent_is_cwd_parent_and_skips_registration(tmp_path, monkeypatch, capsys):
    workdir = tmp_path / "work" / "here"
    workdir.mkdir(parents=True)
    monkeypatch.chdir(workdir)
    monkeypatch.delenv("COORDINATOR_PROJECTS_ROOT", raising=False)
    monkeypatch.setattr(new_project_scaffold, "_is_consumer", lambda: True)
    calls = []
    monkeypatch.setattr(new_project_scaffold, "_register_repo", lambda *a, **k: calls.append(a) or 0)

    rc = main(["--name", "cons", "--template", "empty", "--no-smoke"])

    assert rc == 0
    assert (tmp_path / "work" / "cons").is_dir()
    assert calls == []
