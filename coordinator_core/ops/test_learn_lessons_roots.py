"""Characterization + parity tests for coordinator_core.ops.learn_lessons_roots.

Spec backlink: docs/plans/2026-06-19-portability-tracked-per-machine-config.md § C1
"""

from __future__ import annotations

import io
import os
import stat
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from coordinator_core.ops.learn_lessons_roots import main
from coordinator_core.testing.fake_machine_local import write_fake_machine_local


def _make_machine_local(bin_dir: Path, repos: dict, publish_targets=(), extra=None) -> Path:
    """Write a fake cross-platform `machine-local` CLI supporting `keys` / `get <key>`.

    repos: {short_key: path} -> registers as `repos.<short_key>`.
    publish_targets: iterable of (name, type, source, dest) 4-tuples for
      `get publish.targets` (pipe-joined, one per line).
    extra: additional {key: value} pairs beyond repos.* (e.g. denylist keys).

    Delegates the platform-correct on-disk shape (bare shim on POSIX,
    `.cmd` launcher + co-located `.py` body on Windows) to
    `coordinator_core.testing.fake_machine_local.write_fake_machine_local` --
    see that module's docstring for why a raw `#!/bin/sh`/`#!/usr/bin/env
    bash` stub cannot be exec'd directly by Windows `CreateProcess`.
    """
    extra_dict = extra or {}
    python_body = f"""
import sys

_REPOS = {repos!r}
_PUBLISH_TARGETS = {list(publish_targets)!r}
_EXTRA = {extra_dict!r}


def _main():
    args = sys.argv[1:]
    if not args:
        return 0
    if args[0] == "keys":
        for short_key in _REPOS:
            print(f"repos.{{short_key}}")
        return 0
    if args[0] == "get" and len(args) > 1:
        key = args[1]
        if key.startswith("repos.") and key[len("repos."):] in _REPOS:
            print(_REPOS[key[len("repos."):]])
            return 0
        if key == "publish.targets" and _PUBLISH_TARGETS:
            for t in _PUBLISH_TARGETS:
                print("|".join(t))
            return 0
        if key in _EXTRA:
            print(_EXTRA[key])
            return 0
        return 0
    return 0


sys.exit(_main())
"""
    return write_fake_machine_local(bin_dir, python_body)


def _env_for(tmp_path: Path) -> dict:
    env = dict(os.environ)
    env["CLAUDE_HOME"] = str(tmp_path)  # CLAUDE_HOME overrides $HOME, per oracle
    # No coordinator-claude content root in these tests -- _coordinator_state_root_central()
    # degrades to "" (script not found), config_path becomes bogus and is
    # correctly skipped (isfile() False) -- exercises the negative-spec path.
    env.pop("COORDINATOR_ROOT", None)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return env


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


class TestPositive:
    def test_claude_home_always_first_line(self, tmp_path: Path):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)
        env = _env_for(tmp_path)
        rc, out, err = _run_main([], env)

        assert rc == 0
        lines = out.splitlines()
        assert lines[0] == str(claude_home)

    def test_registry_repo_included_when_dir_exists(self, tmp_path: Path):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)
        repo_a = tmp_path / "repo-a"
        repo_a.mkdir()
        _make_machine_local(claude_home / "bin", repos={"repo_a": str(repo_a)})

        env = _env_for(tmp_path)
        rc, out, err = _run_main([], env)

        assert rc == 0
        lines = out.splitlines()
        assert str(claude_home) in lines
        assert str(repo_a) in lines
        assert lines[0] == str(claude_home)

    def test_registry_repo_skipped_when_dir_missing(self, tmp_path: Path):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)
        missing = tmp_path / "does-not-exist"
        _make_machine_local(claude_home / "bin", repos={"ghost": str(missing)})

        env = _env_for(tmp_path)
        rc, out, err = _run_main([], env)

        assert rc == 0
        assert str(missing) not in out.splitlines()

    def test_publish_target_excluded(self, tmp_path: Path):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)
        mirror = tmp_path / "mirror-repo"
        mirror.mkdir()
        _make_machine_local(
            claude_home / "bin",
            repos={"mirror": str(mirror)},
            publish_targets=[("mirror", "type", "src", str(mirror))],
        )

        env = _env_for(tmp_path)
        rc, out, err = _run_main([], env)

        assert rc == 0
        assert str(mirror) not in out.splitlines()

    def test_denylist_key_excludes_coordinator_claude(self, tmp_path: Path):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)
        cc_repo = tmp_path / "cc-repo"
        cc_repo.mkdir()
        _make_machine_local(
            claude_home / "bin",
            repos={"cc": str(cc_repo)},
            extra={"repos.coordinator_claude": str(cc_repo)},
        )

        env = _env_for(tmp_path)
        rc, out, err = _run_main([], env)

        assert rc == 0
        assert str(cc_repo) not in out.splitlines()

    def test_dedup_preserves_first_occurrence_order(self, tmp_path: Path):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)
        dup = tmp_path / "dup-repo"
        dup.mkdir()
        # Two registry keys resolving to the SAME dir -- must dedup to one line.
        _make_machine_local(
            claude_home / "bin",
            repos={"a_dup": str(dup), "b_dup": str(dup)},
        )

        env = _env_for(tmp_path)
        rc, out, err = _run_main([], env)

        assert rc == 0
        lines = out.splitlines()
        assert lines.count(str(dup)) == 1

    def test_supplemental_sentinel_roots_appended(self, tmp_path: Path):
        """C11 (2026-07-21): `_coordinator_state_root_central` is now a native
        in-process call to `coordinator_state_root(central=True)` rather than a
        shell-out to a fake `coordinator-state-root.sh` -- monkeypatch the
        call-site directly rather than driving the full native resolution chain
        (CLAUDE_KLABAUTER_ROOT / machine-local registry) through env, matching the
        equivalent stand-in pattern in test_central_run_due.py."""
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)
        central_state = tmp_path / "central-state"
        central_state.mkdir(parents=True)
        supp = tmp_path / "supplemental-repo"
        supp.mkdir()

        cfg = central_state / "learn-lessons-config.md"
        cfg.write_text(
            "some preamble\n"
            "<!-- BEGIN learn-lessons-roots -->\n"
            f"- {supp}\n"
            "<!-- END learn-lessons-roots -->\n"
            "trailer\n"
        )

        env = _env_for(tmp_path)

        import coordinator_core.ops.learn_lessons_roots as _mod

        old_fn = _mod._coordinator_state_root_central
        _mod._coordinator_state_root_central = lambda: str(central_state)
        try:
            rc, out, err = _run_main([], env)
        finally:
            _mod._coordinator_state_root_central = old_fn

        assert rc == 0
        assert str(supp) in out.splitlines()


class TestNegative:
    def test_no_machine_local_emits_only_claude_home(self, tmp_path: Path):
        """OSS fresh-install: no machine-local at all -- graceful degradation."""
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)

        env = _env_for(tmp_path)
        rc, out, err = _run_main([], env)

        assert rc == 0
        assert out.splitlines() == [str(claude_home)]

    def test_unresolvable_claude_home_still_exits_zero(self, tmp_path: Path):
        """CLAUDE_HOME pointed at a nonexistent directory -- still exits 0, still
        emits the (nonexistent) CLAUDE_HOME as the first line, matching the bash
        oracle's `[nonexistent-negative-test]/.claude` observed behavior."""
        nonexistent = tmp_path / "nonexistent-negative-test"
        env = dict(os.environ)
        env["CLAUDE_HOME"] = str(nonexistent)
        env.pop("COORDINATOR_ROOT", None)
        env.pop("CLAUDE_PLUGIN_ROOT", None)

        rc, out, err = _run_main([], env)

        assert rc == 0
        assert out.splitlines() == [str(nonexistent / ".claude")]

    def test_empty_registry_keys_still_exits_zero(self, tmp_path: Path):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir(parents=True)
        _make_machine_local(claude_home / "bin", repos={})

        env = _env_for(tmp_path)
        rc, out, err = _run_main([], env)

        assert rc == 0
        assert out.splitlines() == [str(claude_home)]
