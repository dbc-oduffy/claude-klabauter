"""
test_check_atlas_watch_drift.py — pytest unit tests for
coordinator_core.ops.check_atlas_watch_drift.

Port source: coordinator/tests/test_atlas_watch_drift.py (coordinator-claude, bash-oracle
subprocess tests) — reauthored here as direct in-process calls against the
ported Python module (`run(argv, cwd=...)`), same fixtures/assertions.

Spec backlink: docs/plans/2026-06-04-architecture-audit-atlas-refresh-gate.md § C2
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Isolation: every test builds a fresh fixture git repo under tmp_path with the
expected `docs/architecture/systems/` layout and calls `run()` with cwd=repo.

Negative-spec: tests do NOT touch the real ~/.claude/docs/architecture/systems/
tree or the operator's working tree.
"""

import os
import subprocess
import textwrap
from datetime import date, timedelta
from pathlib import Path

import pytest

from coordinator_core.ops import check_atlas_watch_drift
from coordinator_core.ops.check_atlas_watch_drift import run

# Declared, not excused: this file spawns real git because the ported bash-oracle
# contract (test_atlas_watch_drift.py, coordinator-claude) depends on real commit/mtime
# state in `docs/architecture/systems/` that `check_atlas_watch_drift.run()` reads
# via git plumbing -- no mock stands in for that. Each test builds its own fresh
# tmp_path repo via `_init_repo`, so mutation-heavy staleness scenarios need
# per-test isolation, not a module-scope hoist. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _init_repo(root):
    """Initialize a minimal git repo at `root` and return (root_str, systems_dir_str)."""
    root = str(root)
    subprocess.run(["git", "init", "-q", root], check=True)
    subprocess.run(["git", "-C", root, "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", root, "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", root, "config", "commit.gpgsign", "false"], check=True)
    systems = os.path.join(root, "docs", "architecture", "systems")
    os.makedirs(systems, exist_ok=True)
    return root, systems


def _write_atlas(systems_dir, name, last_mapped=None, last_attested=None):
    body = ["---"]
    if last_mapped:
        body.append(f"last_mapped: {last_mapped}")
    attested = last_attested if last_attested is not None else last_mapped
    if attested:
        body.append(f"last_attested: {attested}")
    body.append("---")
    body.append(f"# {name} atlas")
    path = os.path.join(systems_dir, f"{name}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")
    return path


def _write_watch(systems_dir, name, body):
    path = os.path.join(systems_dir, f"{name}.watch.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


# ---------------------------------------------------------------------------
# AC8 — per-system line emission, mixed .watch.sh presence
# ---------------------------------------------------------------------------


def test_emits_per_system_line(tmp_path):
    today = date.today().isoformat()
    repo, systems = _init_repo(tmp_path)

    _write_atlas(systems, "alpha", last_mapped=today)
    _write_watch(
        systems,
        "alpha",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            print("FRESH alpha skill_count=5")
            """
        ),
    )

    _write_atlas(systems, "bravo", last_mapped=today)

    _write_atlas(systems, "charlie", last_mapped=today)
    _write_watch(
        systems,
        "charlie",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            print("DRIFT charlie expected=10 got=7")
            """
        ),
    )

    lines, rc = run(["--no-stale-walk"], cwd=Path(repo))
    assert rc == 0
    lines = [ln for ln in lines if ln.strip()]

    assert any(ln == "FRESH alpha skill_count=5" for ln in lines), lines
    assert any(ln == "FRESH bravo (no watch script)" for ln in lines), lines
    assert any(ln == "DRIFT charlie expected=10 got=7" for ln in lines), lines
    assert len(lines) == 3, lines


# ---------------------------------------------------------------------------
# AC8b — non-zero exit -> ERROR, never silently FRESH
# ---------------------------------------------------------------------------


def test_nonzero_exit_emits_error(tmp_path):
    today = date.today().isoformat()
    repo, systems = _init_repo(tmp_path)

    _write_atlas(systems, "delta", last_mapped=today)
    _write_watch(
        systems,
        "delta",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            print("something", file=sys.stderr)
            sys.exit(1)
            """
        ),
    )

    lines, rc = run(["--no-stale-walk"], cwd=Path(repo))
    assert rc == 0
    lines = [ln for ln in lines if ln.strip()]
    assert any(ln.startswith("ERROR delta:") and "exit=1" in ln for ln in lines), lines
    assert not any(ln.startswith("FRESH delta") for ln in lines), lines


# ---------------------------------------------------------------------------
# AC8c — malformed stdout -> MALFORMED, never silently FRESH
# ---------------------------------------------------------------------------


def test_malformed_stdout_emits_malformed(tmp_path):
    today = date.today().isoformat()
    repo, systems = _init_repo(tmp_path)

    _write_atlas(systems, "echo-sys", last_mapped=today)
    _write_watch(
        systems,
        "echo-sys",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            print("this is not a contract line")
            """
        ),
    )

    lines, rc = run(["--no-stale-walk"], cwd=Path(repo))
    assert rc == 0
    lines = [ln for ln in lines if ln.strip()]
    assert any(ln.startswith("MALFORMED echo-sys:") for ln in lines), lines
    assert not any(ln.startswith("FRESH echo-sys") for ln in lines), lines


def test_malformed_wrong_system_name_emits_malformed(tmp_path):
    today = date.today().isoformat()
    repo, systems = _init_repo(tmp_path)

    _write_atlas(systems, "foxtrot", last_mapped=today)
    _write_watch(
        systems,
        "foxtrot",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            print("FRESH wrong-name something")
            """
        ),
    )

    lines, rc = run(["--no-stale-walk"], cwd=Path(repo))
    assert rc == 0
    lines = [ln for ln in lines if ln.strip()]
    assert any(ln.startswith("MALFORMED foxtrot:") for ln in lines), lines


def test_leading_blank_line_still_malformed(tmp_path):
    """Oracle footgun: a leading blank line + exactly one content line still
    counts non_blank==1 but parses the (blank) FIRST physical line, so it
    stays MALFORMED rather than FRESH. Reproduced verbatim, not fixed."""
    today = date.today().isoformat()
    repo, systems = _init_repo(tmp_path)

    _write_atlas(systems, "golf2", last_mapped=today)
    _write_watch(
        systems,
        "golf2",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            print("")
            print("FRESH golf2")
            """
        ),
    )

    lines, rc = run(["--no-stale-walk"], cwd=Path(repo))
    assert rc == 0
    lines = [ln for ln in lines if ln.strip()]
    assert any(ln.startswith("MALFORMED golf2:") for ln in lines), lines


# ---------------------------------------------------------------------------
# STALE walk default-on (and --no-stale-walk suppression)
# ---------------------------------------------------------------------------


def test_stale_walk_default_on(tmp_path):
    repo, systems = _init_repo(tmp_path)

    old_date = (date.today() - timedelta(days=90)).isoformat()
    _write_atlas(systems, "golf", last_mapped=old_date)

    lines_default, rc_default = run([], cwd=Path(repo))
    assert rc_default == 0
    lines_default = [ln for ln in lines_default if ln.strip()]
    assert any(
        ln.startswith("STALE golf") and f"last_attested={old_date}" in ln and "age=" in ln
        for ln in lines_default
    ), lines_default

    lines_off, rc_off = run(["--no-stale-walk"], cwd=Path(repo))
    assert rc_off == 0
    lines_off = [ln for ln in lines_off if ln.strip()]
    assert not any(ln.startswith("STALE ") for ln in lines_off), lines_off


def test_stale_walk_missing_last_attested_emits_stale_not_skip(tmp_path):
    """A page with no `last_attested:` frontmatter at all must not be a
    silent skip — it is maximally unattested, and the walk must say so,
    honestly distinguishable from the dated-and-aged case (no ISO date, no
    numeric age)."""
    repo, systems = _init_repo(tmp_path)

    _write_atlas(systems, "kilo", last_mapped=None, last_attested=None)

    lines, rc = run([], cwd=Path(repo))
    assert rc == 0
    lines = [ln for ln in lines if ln.strip()]
    assert any(
        ln == "STALE kilo last_attested=none age=unattested" for ln in lines
    ), lines


def test_stale_walk_unparseable_last_attested_groups_with_missing(tmp_path):
    """A present-but-unparseable `last_attested:` value groups with the
    absent case rather than getting a third line shape."""
    repo, systems = _init_repo(tmp_path)

    path = os.path.join(systems, "lima.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\nlast_attested: not-a-date\n---\n# lima atlas\n")

    lines, rc = run([], cwd=Path(repo))
    assert rc == 0
    lines = [ln for ln in lines if ln.strip()]
    assert any(
        ln == "STALE lima last_attested=none age=unattested" for ln in lines
    ), lines


def test_stale_walk_threshold_configurable(tmp_path):
    repo, systems = _init_repo(tmp_path)

    age_days = 45
    aged = (date.today() - timedelta(days=age_days)).isoformat()
    _write_atlas(systems, "hotel", last_mapped=aged)

    lines_default, _ = run([], cwd=Path(repo))
    lines_default = [ln for ln in lines_default if ln.strip()]
    assert any(ln.startswith("STALE hotel") for ln in lines_default), lines_default

    lines_high, _ = run(["--stale-days", "60"], cwd=Path(repo))
    lines_high = [ln for ln in lines_high if ln.strip()]
    assert not any(ln.startswith("STALE hotel") for ln in lines_high), lines_high

    lines_legacy, _ = run(["--last-mapped-stale-days", "60"], cwd=Path(repo))
    lines_legacy = [ln for ln in lines_legacy if ln.strip()]
    assert not any(ln.startswith("STALE hotel") for ln in lines_legacy), lines_legacy


# ---------------------------------------------------------------------------
# Always returns rc 0 (informational contract)
# ---------------------------------------------------------------------------


# Review: code-reviewer — Finding 1 named a blind spot: no test covered
# _watch_line's subprocess.run timeout bound (module docstring's "never
# blocks a caller pipeline" contract). Covers the fix, not just the fixture.
def test_watch_script_timeout_emits_error_not_hang(tmp_path, monkeypatch):
    monkeypatch.setattr(check_atlas_watch_drift, "_SUBPROCESS_TIMEOUT_SECS", 1)
    today = date.today().isoformat()
    repo, systems = _init_repo(tmp_path)

    _write_atlas(systems, "juliet", last_mapped=today)
    _write_watch(
        systems,
        "juliet",
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import time
            time.sleep(5)
            print("FRESH juliet")
            """
        ),
    )

    lines, rc = run(["--no-stale-walk"], cwd=Path(repo))
    assert rc == 0
    lines = [ln for ln in lines if ln.strip()]
    assert any(ln == "ERROR juliet: juliet.watch.py timeout" for ln in lines), lines


def test_always_exits_zero_even_with_failures(tmp_path):
    repo, systems = _init_repo(tmp_path)
    old_date = (date.today() - timedelta(days=100)).isoformat()

    _write_atlas(systems, "india", last_mapped=old_date)
    _write_watch(
        systems,
        "india",
        "#!/usr/bin/env python3\nimport sys\nsys.exit(2)\n",
    )

    _, rc = run([], cwd=Path(repo))
    assert rc == 0


# ---------------------------------------------------------------------------
# CLI arg parsing errors (informational — still rc 0)
# ---------------------------------------------------------------------------


def test_missing_stale_days_value_emits_error(tmp_path):
    repo, _ = _init_repo(tmp_path)
    lines, rc = run(["--stale-days"], cwd=Path(repo))
    assert rc == 0
    assert any(ln.startswith("ERROR check-atlas-watch-drift:") and "requires a value" in ln for ln in lines), lines


def test_unknown_arg_emits_error(tmp_path):
    repo, _ = _init_repo(tmp_path)
    lines, rc = run(["--bogus"], cwd=Path(repo))
    assert rc == 0
    assert any(ln.startswith("ERROR check-atlas-watch-drift:") and "unknown arg" in ln for ln in lines), lines


def test_no_atlas_dir_returns_empty(tmp_path):
    repo = str(tmp_path)
    subprocess.run(["git", "init", "-q", repo], check=True)
    lines, rc = run([], cwd=Path(repo))
    assert rc == 0
    assert lines == []


def test_no_git_repo_returns_empty(tmp_path):
    lines, rc = run([], cwd=Path(tmp_path))
    assert rc == 0
    assert lines == []
