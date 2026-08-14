"""Characterization + parity tests for coordinator_core.ops.sync_plugin_wiki.

Port of: sync-plugin-wiki.sh (DoE b5a4192c, 2026-07-20), 162 lines.
Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from coordinator_core.ops.sync_plugin_wiki import main


def _run_main(argv, env):
    old_environ = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(argv)
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
    return rc, out.getvalue(), err.getvalue()


def _build_plugin_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugin_root"
    (root / "docs" / "wiki").mkdir(parents=True)
    for d in ("agents", "commands", "skills", "snippets", "pipelines", "hooks"):
        (root / d).mkdir()

    (root / "CLAUDE.md").write_text(
        "See docs/wiki/alpha.md and docs/wiki/beta.md and docs/wiki/foo.md (placeholder example)\n"
    )
    (root / "README.md").write_text("docs/wiki/gamma.md referenced here too.\n")
    (root / "em-operating-model.md").write_text("")
    (root / "capability-catalog.md").write_text("")

    (root / "docs" / "wiki" / "alpha.md").write_text("alpha doc\n")
    (root / "docs" / "wiki" / "gamma.md").write_text("gamma doc\n")

    for d in ("agents", "commands", "skills", "snippets", "pipelines", "hooks"):
        (root / d / "placeholder.md").write_text("")

    return root


def _env_for(tmp_path: Path, plugin_root: Path) -> dict:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    env["HOME"] = str(tmp_path / "fakehome")
    (tmp_path / "fakehome").mkdir(exist_ok=True)
    env.pop("COORDINATOR_OVERRIDE_WIKI_MIRROR", None)
    return env


class TestPositive:
    def test_clean_corpus_validates_and_warns_missing(self, tmp_path: Path):
        plugin_root = _build_plugin_root(tmp_path)
        env = _env_for(tmp_path, plugin_root)

        rc, out, err = _run_main([], env)

        assert rc == 0
        assert "beta.md referenced by plugin file but absent from bundled wiki" in out
        assert "Plugin-bundled wiki: clean (2 validated, 1 missing-bundled warnings)." in out
        # placeholder-filtered name never surfaces
        assert "foo.md" not in out

    def test_quiet_suppresses_output(self, tmp_path: Path):
        plugin_root = _build_plugin_root(tmp_path)
        env = _env_for(tmp_path, plugin_root)

        rc, out, err = _run_main(["--quiet"], env)

        assert rc == 0
        assert out == ""

    def test_no_wiki_references_nothing_to_validate(self, tmp_path: Path):
        root = tmp_path / "empty_plugin_root"
        (root / "docs" / "wiki").mkdir(parents=True)
        for d in ("agents", "commands", "skills", "snippets", "pipelines", "hooks"):
            (root / d).mkdir()
        (root / "CLAUDE.md").write_text("")
        (root / "README.md").write_text("")
        (root / "em-operating-model.md").write_text("")
        (root / "capability-catalog.md").write_text("")
        env = _env_for(tmp_path, root)

        rc, out, err = _run_main([], env)

        assert rc == 0
        assert "No docs/wiki/ references found in plugin files. Nothing to validate." in out

    def test_dev_side_mirror_exits_5(self, tmp_path: Path):
        plugin_root = _build_plugin_root(tmp_path)
        env = _env_for(tmp_path, plugin_root)
        dev_wiki = Path(env["HOME"]) / ".claude" / "docs" / "wiki"
        dev_wiki.mkdir(parents=True)
        (dev_wiki / "alpha.md").write_text("mirror\n")

        rc, out, err = _run_main([], env)

        assert rc == 5
        assert "MIRROR DETECTED: alpha.md" in out
        assert "SINGLE-TREE INVARIANT BROKEN: 1 plugin-doctrine wiki(s) have dev-side mirrors." in out
        assert "Files: alpha.md" in out

    def test_override_env_suppresses_exit_5(self, tmp_path: Path):
        plugin_root = _build_plugin_root(tmp_path)
        env = _env_for(tmp_path, plugin_root)
        dev_wiki = Path(env["HOME"]) / ".claude" / "docs" / "wiki"
        dev_wiki.mkdir(parents=True)
        (dev_wiki / "alpha.md").write_text("mirror\n")
        env["COORDINATOR_OVERRIDE_WIKI_MIRROR"] = "1"

        rc, out, err = _run_main([], env)

        assert rc == 0
        assert "WARN (override): dev-side mirror exists for alpha.md" in out

    def test_help_exits_0(self, tmp_path: Path):
        plugin_root = _build_plugin_root(tmp_path)
        env = _env_for(tmp_path, plugin_root)

        rc, out, err = _run_main(["--help"], env)

        assert rc == 0
        assert "sync-plugin-wiki.sh" in out
        assert "Exit codes:" in out


class TestNegative:
    def test_unknown_flag_exits_2(self, tmp_path: Path):
        plugin_root = _build_plugin_root(tmp_path)
        env = _env_for(tmp_path, plugin_root)

        rc, out, err = _run_main(["--bogus"], env)

        assert rc == 2
        assert "unknown flag: --bogus" in err

    def test_unresolvable_plugin_root_exits_1(self, tmp_path: Path):
        env = dict(os.environ)
        for key in ("CLAUDE_PLUGIN_ROOT", "COORDINATOR_ROOT"):
            env.pop(key, None)
        env["HOME"] = str(tmp_path / "no-doe-root-home")
        (tmp_path / "no-doe-root-home" / ".claude").mkdir(parents=True)

        rc, out, err = _run_main([], env)

        assert rc == 1
        assert "ERROR: ~/.claude/.doe-root missing/invalid" in err

    def test_unreadable_file_names_it_and_fails_closed(self, tmp_path: Path, monkeypatch):
        plugin_root = _build_plugin_root(tmp_path)
        env = _env_for(tmp_path, plugin_root)
        unreadable = plugin_root / "agents" / "locked.md"
        unreadable.write_text("docs/wiki/delta.md\n")

        # `os.chmod(0o000)` is a no-op for the owning process on Windows (POSIX
        # mode bits aren't enforced there -- the file stays readable), so it
        # never exercises the OSError-on-open path this test verifies. Simulate
        # a genuinely-unreadable file cross-platform by making `open()` raise
        # for this one path, in-process (main() runs in-process here, not a
        # subprocess -- see `_run_main`).
        import coordinator_core.ops.sync_plugin_wiki as sync_plugin_wiki_module
        real_open = open

        def _fake_open(file, *args, **kwargs):
            if os.fspath(file) == str(unreadable):
                raise PermissionError(13, "Permission denied", str(unreadable))
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(sync_plugin_wiki_module, "open", _fake_open, raising=False)

        rc, out, err = _run_main([], env)

        assert rc == 1
        assert "UNREADABLE: 1 file(s) could not be read" in out
        assert str(unreadable) in out
        assert "clean" not in out.lower()
