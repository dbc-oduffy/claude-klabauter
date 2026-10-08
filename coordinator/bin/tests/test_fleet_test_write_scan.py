"""Tests for fleet-test-write-scan.py using synthetic repos under tmp_path."""
import importlib.util
import json
import textwrap
from pathlib import Path

import pytest


_SRC = Path(__file__).resolve().parents[1] / "fleet-test-write-scan.py"
_spec = importlib.util.spec_from_file_location("fleet_test_write_scan", _SRC)
scan = importlib.util.module_from_spec(_spec)
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(scan)


_WRITABLE = Path.home().as_posix() + "/zz"
# On Windows the home path is drive-lettered, which the scanner deliberately treats as relative.
_posix_home = pytest.mark.skipif(not _WRITABLE.startswith("/"), reason="drive-letter home is relative to the scanner")


def run(tmp_path, body, name="tests/test_x.py", extra=None):
    repo = tmp_path / "repo"
    f = repo / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(body))
    for rel, text in (extra or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    findings, _ = scan.scan_repo("r", repo)
    return findings


def rules(findings):
    return sorted((f.severity, f.rule) for f in findings)


# ---- home ------------------------------------------------------------------
def test_home_mkdir_flagged(tmp_path):
    assert ("high", "home-write") in rules(run(tmp_path, """
        from pathlib import Path
        def test_a():
            (Path.home() / "x").mkdir()
    """))


def test_home_via_variable_and_expanduser_and_environ(tmp_path):
    f = run(tmp_path, """
        import os
        def test_a():
            h = os.path.expanduser("~/foo")
            open(h, "w")
        def test_b():
            os.makedirs(os.environ["HOME"] + "/z")
        def test_c():
            p = os.environ.get("USERPROFILE")
            os.makedirs(os.path.join(p, "q"))
    """)
    assert len([x for x in f if x.rule == "home-write"]) == 3


def test_home_sandboxed_by_monkeypatch_not_flagged(tmp_path):
    assert not run(tmp_path, """
        from pathlib import Path
        def test_a(monkeypatch, tmp_path):
            monkeypatch.setenv("HOME", str(tmp_path))
            (Path.home() / "x").mkdir()
    """)


def test_home_sandboxed_by_autouse_conftest_not_flagged(tmp_path):
    assert not run(tmp_path, """
        from pathlib import Path
        def test_a():
            (Path.home() / "x").mkdir()
    """, extra={"tests/conftest.py": """
        import pytest
        @pytest.fixture(autouse=True)
        def _h(monkeypatch, tmp_path):
            monkeypatch.setenv("HOME", str(tmp_path))
    """})


def test_tmp_path_not_flagged(tmp_path):
    assert not run(tmp_path, """
        def test_a(tmp_path):
            (tmp_path / "a").mkdir()
            (tmp_path / "a" / "b.txt").write_text("x")
            open(tmp_path / "c", "w").close()
            (tmp_path / ".." / "sib").mkdir()
    """)


# ---- repo parent -------------------------------------------------------------
def test_parents_beyond_repo_root_flagged(tmp_path):
    f = run(tmp_path, """
        from pathlib import Path
        def test_a():
            (Path(__file__).resolve().parents[2] / "out").mkdir()
    """)
    assert ("high", "parent-write") in rules(f)


def test_parents_at_repo_root_not_flagged(tmp_path):
    # tests/test_x.py: parents[1] is the repo root
    assert not run(tmp_path, """
        from pathlib import Path
        def test_a():
            (Path(__file__).resolve().parents[1] / "out").mkdir()
    """)


def test_repo_root_parent_and_dotdot_flagged(tmp_path):
    f = run(tmp_path, """
        import os
        def test_a():
            (repo_root.parent / "sib").mkdir()
        def test_b():
            os.makedirs("../escape")
    """)
    assert [x.rule for x in f].count("parent-write") == 2


# ---- relative ------------------------------------------------------------------
def test_relative_write_medium_and_chdir_exempt(tmp_path):
    f = run(tmp_path, """
        def test_a():
            open("out.txt", "w").write("x")
        def test_b(monkeypatch, tmp_path):
            monkeypatch.chdir(tmp_path)
            open("out.txt", "w").write("x")
    """)
    assert rules(f) == [("medium", "relative-write")]
    assert f[0].line == 3


def test_read_open_not_flagged(tmp_path):
    assert not run(tmp_path, """
        def test_a():
            open("in.txt").read()
            open("in.txt", "r").read()
    """)


# ---- absolute / fake ----------------------------------------------------------
@_posix_home
def test_absolute_literal_write_flagged_tmp_exempt(tmp_path):
    f = run(tmp_path, """
        import os
        def test_a():
            os.makedirs("/definitely/a/fake/directory")
        def test_b():
            os.makedirs("/tmp/ok")
        def test_c():
            d = "WRITABLE/x"
            os.makedirs(d)
    """.replace("WRITABLE", _WRITABLE))
    assert [x.rule for x in f if x.severity == "high"] == ["drive-root-write", "absolute-write"]
    assert not [x for x in f if x.severity == "info"]
    assert not [x for x in f if "/tmp/ok" in x.snippet]


@_posix_home
def test_root_kwarg_and_flag_absolute(tmp_path):
    f = run(tmp_path, """
        import subprocess
        def test_a():
            run_it(root="WRITABLE/y")
        def test_b():
            subprocess.run(["tool", "--root", "WRITABLE/y"])
        def test_c():
            subprocess.run(["tool", "--root=WRITABLE/y"])
        def test_d(tmp_path):
            subprocess.run(["tool", "--root", str(tmp_path)], cwd=tmp_path)
        def test_e():
            run_it(root="/x/y")
    """.replace("WRITABLE", _WRITABLE))
    assert [(x.severity, x.rule) for x in f] == [("high", "root-arg")] * 4


def test_fake_relative_literal_into_code_under_test(tmp_path):
    f = run(tmp_path, """
        def test_a():
            main(root="definitely/a/fake/directory")
        def test_b():
            build("fake/dir")
        def test_c():
            x = Path("fake/dir")
    """)
    assert rules(f) == [("high", "decoy-path"), ("high", "fake-path-arg")]


# ---- tempfile -----------------------------------------------------------------
def test_mkdtemp_leak_low_and_cleanup_exempt(tmp_path):
    f = run(tmp_path, """
        import tempfile, shutil
        def test_a():
            d = tempfile.mkdtemp()
        def test_b():
            d = tempfile.mkdtemp()
            shutil.rmtree(d)
        def test_c():
            with tempfile.TemporaryDirectory() as d:
                open(d + "/f", "w")
    """)
    assert rules(f) == [("low", "temp-leak")]
    assert f[0].line == 4


def test_mkdtemp_dir_home_is_high(tmp_path):
    f = run(tmp_path, """
        import tempfile
        from pathlib import Path
        def test_a():
            tempfile.mkdtemp(dir=Path.home())
    """)
    assert ("high", "home-write") in rules(f)


# ---- discovery / CLI ----------------------------------------------------------
def test_non_test_files_ignored_and_helpers_scanned(tmp_path):
    f = run(tmp_path, """
        def helper():
            open("x", "w")
    """, name="src/mod.py", extra={"tests/helpers.py": """
        def helper():
            open("x", "w")
    """})
    assert [x.file for x in f] == ["tests/helpers.py"]


@_posix_home
def test_cli_exit_code_and_json(tmp_path, capsys):
    repo = tmp_path / "myrepo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("import os\ndef test_a():\n    os.makedirs('" + _WRITABLE + "/y')\n")
    rc = scan.main(["--path", str(repo), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["repos"]["myrepo"]["counts"]["high"] == 1

    (repo / "tests" / "test_a.py").write_text("def test_a(tmp_path):\n    (tmp_path / 'a').mkdir()\n")
    assert scan.main(["--path", str(repo), "--json"]) == 0
    capsys.readouterr()


def test_syntax_error_counted_not_fatal(tmp_path):
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_bad.py").write_text("def (:\n")
    findings, perr = scan.scan_repo("r", repo)
    assert findings == [] and perr == 1


def test_decoy_names(tmp_path):
    f = run(tmp_path, """
        def test_a(tmp_path):
            main(root="/nonexistent/x")
        def test_b():
            main(root="definitely/a/fake/directory")
        def test_c(tmp_path):
            main(root=tmp_path / "fake" / "dir")
        def test_d():
            main(root="templates/x")
    """)
    assert rules(f) == [("high", "decoy-path"), ("high", "root-arg")]


def test_info_hidden_by_default_and_shown_on_request(tmp_path, capsys):
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("import subprocess\ndef test_a():\n    subprocess.run(['x'], cwd='../..')\n")
    scan.main(["--path", str(repo), "--json"])
    assert json.loads(capsys.readouterr().out)["repos"]["r"]["findings"] == []
    rc = scan.main(["--path", str(repo), "--json", "--min-severity", "info"])
    out = json.loads(capsys.readouterr().out)["repos"]["r"]
    assert rc == 0 and out["counts"]["info"] == 1


def test_posix_absolute_write_sinks_are_high(tmp_path):
    f = run(tmp_path, """
        from pathlib import Path
        import sqlite3
        def test_a():
            Path("/repo").mkdir()
        def test_b():
            Path("/fake/root").joinpath("x.txt").write_text("hi")
        def test_c():
            open("/repo/f.txt", "w")
        def test_d():
            sqlite3.connect("/repo/db.sqlite")
        def test_e():
            root = "/fake/root"
            create_project(root)
        def test_f():
            run_it(root="/repo")
    """)
    assert len(f) == 6 and {x.severity for x in f} == {"high"}


def test_posix_absolute_predicate_and_assert_only_not_flagged(tmp_path):
    f = run(tmp_path, """
        import os
        from pathlib import Path
        def test_a():
            assert Path("/repo").is_absolute()
            assert str(Path("/fake/root")) == "/fake/root"
            assert not os.path.exists("/repo")
            assert os.path.isabs("/repo")
            open("/repo/f.txt")
    """)
    assert f == []


def test_abs_literal_handed_to_a_pure_reader_is_not_a_leak(tmp_path):
    f = run(tmp_path, """
        import os
        def same_repo(cwd, repo_root):
            return os.path.normcase(os.path.normpath(cwd)) == os.path.normcase(repo_root)
        def describe(baton_path, git_root):
            try:
                shown = os.path.relpath(baton_path, git_root)
            except Exception:
                shown = baton_path
            return "JOURNAL " + shown.replace(os.sep, "/")
        def test_a():
            assert same_repo("/x", repo_root="/repo")
        def test_b():
            root = "/repo"
            assert describe("/repo/a", root)
    """)
    assert f == []


def test_abs_literal_reaching_a_write_through_the_callee_is_still_flagged(tmp_path):
    f = run(tmp_path, """
        import os
        from pathlib import Path
        def make_direct(root):
            Path(root).joinpath("x").mkdir()
        def make_aliased(root):
            target = os.path.join(root, "sub")
            os.makedirs(target)
        def make_inner(d):
            open(d + "/f", "w")
        def make_outer(root):
            make_inner(root)
        def test_a():
            make_direct("/repo")
        def test_b():
            make_aliased(root="/repo")
        def test_c():
            make_outer("/repo")
    """)
    assert [(x.severity, x.line) for x in f] == [("high", 14), ("high", 16), ("high", 18)]


def test_abs_literal_to_unresolvable_or_attribute_stored_callee_is_flagged(tmp_path):
    f = run(tmp_path, """
        class Keeper:
            def create_hold(self, root):
                self.root = root
        def test_a():
            create_elsewhere("/repo")
        def test_b():
            Keeper().create_hold("/repo")
    """)
    assert [x.line for x in f if x.severity == "high"] == [6, 8]


def test_callee_resolution_prefers_the_same_file_def(tmp_path):
    f = run(tmp_path, """
        def run(root):
            return root == "/repo"
        def test_a():
            run("/repo")
    """, extra={"tests/test_other.py": """
        import os
        def run(root):
            os.makedirs(root)
    """})
    assert [x for x in f if x.file.endswith("test_x.py")] == []


def test_cwd_routed_into_a_hook_stdin_payload_is_still_a_leak(tmp_path):
    """A hook fed `cwd` may write under it (the C:/repo incident); stdin is not an exemption."""  # abs-path-ok: drive path is the subject under test
    f = run(tmp_path, """
        import io, json, sys
        def drive(main_fn, cwd):
            payload = {"cwd": cwd}
            sys.stdin = io.StringIO(json.dumps(payload))
            return main_fn()
        def test_a():
            drive(lambda: 0, cwd="/some/cwd")
    """)
    assert [(x.severity, x.line) for x in f] == [("high", 8)]


def test_http_route_argument_is_not_a_path(tmp_path):
    f = run(tmp_path, """
        def post(conn, path):
            conn.request("POST", path, body=b"{}")
        def test_b(conn):
            post(conn, path="/hook")
    """)
    assert f == []


def test_hook_payload_posix_absolute_paths_are_flagged_when_a_hook_runs(tmp_path):
    f = run(tmp_path, """
        def test_a(hook):
            hook.main({"cwd": "/repo", "transcript_path": "/t.jsonl", "tool_input": {"file_path": "/x"}})
        def test_b(hook, tmp_path):
            hook.main({"cwd": str(tmp_path), "hook_path": "/hook", "session_id": "/s", "relative_dir": "d"})
    """, name="tests/test_x_hook.py")
    assert [(x.rule, x.severity) for x in f] == [("hook-payload-path", "high")]
    assert "payload 'cwd'" in f[0].reason


def test_hook_payload_paths_ignored_when_no_hook_runs(tmp_path):
    assert run(tmp_path, """
        def test_a(roster):
            roster.add({"cwd": "/fake/coordinator-content-repo", "project_dir": "/repo"})
    """) == []


def test_subprocess_input_and_env_are_values_but_argv_and_cwd_are_targets(tmp_path):
    f = run(tmp_path, """
        import subprocess
        def make_feed(root):
            subprocess.run(["tool"], input=root, env={"R": root})
        def make_in(root):
            subprocess.run(["tool"], cwd=root)
        def make_with(root):
            subprocess.run(["tool", "--out", root])
        def test_a():
            make_feed("/repo")
        def test_b():
            make_in("/repo")
        def test_c():
            make_with("/repo")
    """)
    assert [x.line for x in f if x.severity == "high"] == [12, 14]


def test_written_content_flag_value_and_path_list_are_not_paths(tmp_path):
    f = run(tmp_path, """
        def test_a(tmp_path):
            (tmp_path / ".gitignore").write_text("/foo\\n")
            (tmp_path / "p").write_text("/gone/old-clone\\n")
            set_path("/usr/bin:/bin")
        def test_b():
            run_it("--tool-cli", "/x/tool")
        def test_c():
            run_it("--root", "/x/dir")
    """)
    assert [x.line for x in f if x.severity == "high"] == [9]
    assert not scan._is_abs_literal("/foo\n")
    assert not scan._is_abs_literal("/usr/bin:/bin")


def test_subprocess_relative_decoy_in_argv(tmp_path):
    f = run(tmp_path, """
        import subprocess
        def test_a():
            subprocess.run(["tool", "definitely/a/fake/directory"])
        def test_b():
            subprocess.run(["tool", "definitely/a/fake/directory"], cwd=tmp_path)
        def test_c():
            subprocess.run(["tool", "plain"])
        def test_d():
            subprocess.run(["tool", "/definitely/absolute"])
    """)
    assert [(x.severity, x.rule, x.line) for x in f if x.rule == "decoy-path"] == [("high", "decoy-path", 4)]


def test_repo_anchored_parent_and_fstring_not_flagged(tmp_path):
    assert not run(tmp_path, """
        from pathlib import Path
        ROOT = Path(__file__).parent.parent
        GOLDEN = ROOT / "tests" / "fixtures" / "g.json"
        def test_a(repo, n):
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            (repo / f"{n}/file_{n}.txt").write_text("x")
            (ROOT / n).parent.mkdir(parents=True, exist_ok=True)
    """)


def test_file_suffix_and_flag_decoys_not_flagged_cwd_parent_is_info(tmp_path):
    f = run(tmp_path, """
        import subprocess
        def test_a():
            load("docs/does-not-exist.md")
        def test_b():
            subprocess.run(["tool", "--not-a-real-flag"])
        def test_c():
            subprocess.run(["tool"], cwd="../sib")
        def test_d():
            make("fake/dir")
    """)
    assert [(x.severity, x.line) for x in f] == [("info", 8), ("high", 10)]


def test_windows_drive_literal_is_relative_on_posix():
    # `C:/x` creates a `C:` directory in cwd on macOS/Linux, so it is never a harmless absolute.  # abs-path-ok: drive path is the subject under test
    assert not scan._is_abs_literal("C:" + "/fake/root")  # pure predicate; split so the scan stays clean
    assert not scan._is_abs_literal("\\\\host\\share")
    assert scan._is_abs_literal("/fake/root")


def test_windows_pipe_path_is_not_a_directory():
    assert scan._is_abs_literal(r"\\.\pipe\fake")
    assert scan._abs_value(r"\\.\pipe\fake")[0] == scan.SAFE
