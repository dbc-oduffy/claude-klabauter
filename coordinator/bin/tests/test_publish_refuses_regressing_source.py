"""coordinator-publish refuses a source that drops a commit the live klabauter publish carries.
Drives the real wrapper file at default arguments against a fixture source repo and mirror."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WRAPPER = _REPO_ROOT / "coordinator" / "bin" / "coordinator-publish.py"

_STUB_DRIVER = textwrap.dedent(
    """
    import sys
    print("DRIVER_RAN argv=" + repr(sys.argv[1:]))
    sys.exit(0)
    """
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t.example",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t.example",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "maintenance.auto",
    "GIT_CONFIG_VALUE_0": "false",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
        **no_console_creationflags(),
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    """Source repo holding `base` and `live` on main plus a `sibling` branch off `base` that
    lacks `live`; a mirror whose HEAD trailer names `live`."""
    source = tmp_path / "source"
    (source / "coordinator" / "bin").mkdir(parents=True)
    (source / "coordinator" / "bin" / "publish.py").write_text(_STUB_DRIVER, encoding="utf-8")
    shutil.copy2(_WRAPPER, source / "coordinator" / "bin" / "coordinator-publish.py")
    _git(source, "init", "-q", "-b", "main")
    base = _commit(source, "base.txt")
    live = _commit(source, "live-fix.txt")
    _git(source, "checkout", "-q", "-b", "sibling", base)
    sibling = _commit(source, "sibling.txt")
    _git(source, "checkout", "-q", "main")

    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _git(mirror, "init", "-q", "-b", "main")
    (mirror / "f").write_text("f", encoding="utf-8")
    _git(mirror, "add", "--", "f")
    _git(
        mirror, "commit", "-q", "-m",
        f"publish\n\nPercolate-Round: r1\nPercolate-Source-Head: {live}\nPercolate-Signature: s",
    )
    settings = tmp_path / "settings"
    settings.mkdir()
    return {"source": source, "mirror": mirror, "base": base, "live": live, "sibling": sibling, "settings": settings}


def _publish(world, *args: str):
    env = {
        **os.environ,
        "PYTHONPATH": str(_REPO_ROOT),
        "MACHINE_LOCAL_REPOS_CLAUDE_KLABAUTER": str(world["mirror"]),
        "COORDINATOR_SETTINGS_HOME": str(world["settings"]),
    }
    return subprocess.run(
        [sys.executable, str(world["source"] / "coordinator" / "bin" / "coordinator-publish.py"), *args],
        cwd=str(world["source"]),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        **no_console_creationflags(),
    )


def test_descendant_of_the_live_head_is_admitted(world):
    _commit(world["source"], "later.txt")
    proc = _publish(world, "klabauter")
    assert proc.returncode == 0, proc.stderr
    assert "DRIVER_RAN" in proc.stdout


def test_source_at_the_live_head_is_admitted(world):
    proc = _publish(world)
    assert proc.returncode == 0, proc.stderr
    assert "DRIVER_RAN" in proc.stdout


def test_sibling_source_dropping_a_commit_is_refused_before_the_driver_runs(world):
    _git(world["source"], "checkout", "-q", "sibling")
    proc = _publish(world, "klabauter")
    assert proc.returncode != 0
    assert "DRIVER_RAN" not in proc.stdout
    assert world["live"][:7] in proc.stderr
    assert "live-fix.txt" in proc.stderr
    assert "--allow-source-rollback" in proc.stderr


def test_rollback_override_admits_the_sibling_and_never_reaches_the_driver(world):
    _git(world["source"], "checkout", "-q", "sibling")
    proc = _publish(world, "klabauter", "--allow-source-rollback")
    assert proc.returncode == 0, proc.stderr
    assert "DRIVER_RAN" in proc.stdout
    assert "--allow-source-rollback" not in proc.stdout


def test_named_source_ref_is_judged_instead_of_head(world):
    refused = _publish(world, "klabauter", "--source-ref", world["sibling"])
    assert refused.returncode != 0
    assert "DRIVER_RAN" not in refused.stdout
    admitted = _publish(world, "klabauter", "--source-ref", world["live"])
    assert admitted.returncode == 0, admitted.stderr


def test_dry_run_and_other_targets_are_not_judged(world):
    _git(world["source"], "checkout", "-q", "sibling")
    assert _publish(world, "klabauter", "--dry-run").returncode == 0
    assert _publish(world, "coordinator-claude").returncode == 0


def test_mirror_without_a_trailer_leaves_the_guard_silent(world):
    _git(world["source"], "checkout", "-q", "sibling")
    _git(world["mirror"], "commit", "-q", "--amend", "-m", "publish without trailers")
    proc = _publish(world, "klabauter")
    assert proc.returncode == 0, proc.stderr


def test_provenance_record_stands_in_when_the_mirror_has_no_trailer(world):
    _git(world["source"], "checkout", "-q", "sibling")
    _git(world["mirror"], "commit", "-q", "--amend", "-m", "publish without trailers")
    record = world["settings"] / "machine-local" / "publish-provenance.json"
    record.parent.mkdir()
    record.write_text(
        json.dumps(
            {
                "rows": {
                    "claude-klabauter": {
                        "published": True,
                        "published_at": "2026-11-14T00:00:00+00:00",
                        "toplevels": {str(world["source"]): world["live"]},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    proc = _publish(world, "klabauter")
    assert proc.returncode != 0
    assert world["live"][:7] in proc.stderr


def test_refusal_over_the_listing_cap_says_over_and_truncates(world):
    for i in range(11):
        world["live"] = _commit(world["source"], f"more{i}.txt")
    _git(world["mirror"], "commit", "-q", "--amend", "-m",
         f"publish\n\nPercolate-Source-Head: {world['live']}")
    _git(world["source"], "checkout", "-q", "sibling")
    proc = _publish(world, "klabauter")
    assert proc.returncode != 0
    assert "over 10 commit(s)" in proc.stderr
    assert "and more" in proc.stderr


def test_dest_root_flag_value_is_not_mistaken_for_the_target(world):
    _git(world["source"], "checkout", "-q", "sibling")
    proc = _publish(world, "--dest-root-override", "elsewhere", "klabauter")
    assert proc.returncode != 0
    assert "DRIVER_RAN" not in proc.stdout
    assert "live-fix.txt" in proc.stderr
