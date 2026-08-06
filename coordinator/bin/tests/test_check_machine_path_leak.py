#!/usr/bin/env python3
"""tests/test_check_machine_path_leak.py — Tests for bin/check-machine-path-leak.py.

Purpose: Verifies that the machine-path-leak guard correctly blocks settings.json
with machine-specific absolute paths (hard/exit-1) and only warns on working-repos.yaml
with current-machine home-rooted paths (soft/exit-0).

Native-Python successor to the former check-machine-path-leak.bats plain-bash harness
(2026-07-19 Windows de-bash campaign). Exercises the guard by spawning it under the same
interpreter running this test — no shell on the critical path.

Covers:
    AC7 regression — /Users/thislaptop/... directory-source marketplace entry → exit 1
    AC6 pass case  — clean settings.json with only a git-URL marketplace source → exit 0
    Soft warn      — working-repos.yaml with $HOME path → WARN on stderr, exit 0
    Hard blocks    — /home/<name>/, C:/Users/, X:\\ backslash → exit 1
    Non-target     — non-sentinel filename ignored → exit 0 + "nothing to check"

Spec backlink: docs/plans/2026-06-23-machine-path-leak-guard.md
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md

Run: python3 -m pytest <settings-home>/coordinator/bin/tests/test_check_machine_path_leak.py
"""

import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.normpath(os.path.join(_HERE, "..", "check-machine-path-leak.py"))


def _run_guard(*args, env=None):
    proc = subprocess.run(
        [sys.executable, GUARD, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write(tmpdir, name, content):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


# ---------------------------------------------------------------------------
# Test 1 (AC7): settings.json with /Users/thislaptop/... → hard block (exit != 0)
# ---------------------------------------------------------------------------

def test_settings_json_with_machine_path_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        f = _write(tmp, "settings.json", """{
  "enabledPlugins": { "my-plugin@my-marketplace": true },
  "extraKnownMarketplaces": {
    "my-marketplace": {
      "source": { "source": "directory", "path": "/Users/thislaptop/dev/my-plugin-repo" }
    }
  },
  "theme": "dark"
}
""")
        rc, _, _ = _run_guard(f)
        assert rc != 0, "guard exited 0 (should block /Users/thislaptop path)"


# ---------------------------------------------------------------------------
# Test 2 (AC6): clean git-URL-only settings.json → exit 0
# ---------------------------------------------------------------------------

def test_clean_settings_json_exits_zero():
    with tempfile.TemporaryDirectory() as tmp:
        f = _write(tmp, "settings.json", """{
  "enabledPlugins": { "coordinator@coordinator-claude": true },
  "extraKnownMarketplaces": {
    "coordinator-claude": {
      "source": { "source": "git", "url": "https://github.com/example/coordinator-claude.git" }
    }
  },
  "theme": "dark",
  "effortLevel": "medium"
}
""")
        rc, _, _ = _run_guard(f)
        assert rc == 0, "guard exited {} (should pass)".format(rc)


# ---------------------------------------------------------------------------
# Test 3: working-repos.yaml with a current-$HOME path → WARN stderr, exit 0
# ---------------------------------------------------------------------------

def test_working_repos_yaml_home_path_warns_but_exits_zero():
    current_home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or "/tmp"
    with tempfile.TemporaryDirectory() as tmp:
        f = _write(tmp, "working-repos.yaml", """repos:
  my-project:
    path: "{home}/dev/my-project"
    description: "local project path"
  machine-a-catalog:
    path: "X:\\\\Projects\\\\machine-a-project"
    description: "intentional Machine-a catalog entry"
""".format(home=current_home))
        env = dict(os.environ)
        env["HOME"] = current_home
        rc, _, err = _run_guard(f, env=env)
        assert rc == 0, "guard exited {} (should be 0 for soft warn)".format(rc)
        if "WARN" in err:
            return
        # No WARN is only acceptable if PyYAML is genuinely absent.
        try:
            import yaml  # noqa: F401
            raise AssertionError(
                "PyYAML present but no WARN for $HOME-rooted path; stderr was: {}".format(err)
            )
        except ImportError:
            pass  # guard exited 0 with no WARN — PyYAML absent, fallback mode


# ---------------------------------------------------------------------------
# Test 4: settings.json with /home/<name>/ Linux path → exit != 0
# ---------------------------------------------------------------------------

def test_settings_json_linux_home_path_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        f = _write(tmp, "settings.json", """{
  "mcpServers": { "my-mcp": { "command": "/home/devuser/bin/my-mcp-server" } }
}
""")
        rc, _, _ = _run_guard(f)
        assert rc != 0, "guard exited 0 (should block /home/devuser path)"


# ---------------------------------------------------------------------------
# Test 5: non-target filename ignored → exit 0 + "nothing to check"
# ---------------------------------------------------------------------------

def test_non_target_filename_is_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        f = _write(tmp, "config.json", '{ "path": "/Users/someuser/local/thing" }\n')
        rc, out, _ = _run_guard(f)
        assert rc == 0 and "nothing to check" in out, (
            "expected exit 0 + 'nothing to check', got rc={} stdout={}".format(rc, out)
        )


# ---------------------------------------------------------------------------
# Test 6: settings.json with Windows C:/Users/ path → exit != 0
# ---------------------------------------------------------------------------

def test_settings_json_windows_users_path_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        f = _write(tmp, "settings.json", """{
  "extraKnownMarketplaces": {
    "local-dev": {
      "source": { "source": "directory", "path": "C:/Users/devbox/projects/my-plugin" }
    }
  }
}
""")
        rc, _, _ = _run_guard(f)
        assert rc != 0, "guard exited 0 (should block C:/Users/ path)"


# ---------------------------------------------------------------------------
# Test 7: settings.json with backslash Windows path X:\... → exit != 0
# ---------------------------------------------------------------------------

def test_settings_json_backslash_windows_path_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        # JSON \\ is a single literal backslash → X:\projects\my-plugin
        f = _write(tmp, "settings.json", """{
  "extraKnownMarketplaces": {
    "local-machine-a": {
      "source": { "source": "directory", "path": "X:\\\\projects\\\\my-plugin" }
    }
  }
}
""")
        rc, _, _ = _run_guard(f)
        assert rc != 0, "guard exited 0 (should block X:\\projects\\ path)"
