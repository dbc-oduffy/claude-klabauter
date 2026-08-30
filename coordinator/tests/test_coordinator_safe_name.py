"""
coordinator/tests/test_coordinator_safe_name.py

Exercises the coordinator-safe-name CLI black-box (subcommand -> stdout/exit
code), matching how every real caller (check-no-illegal-paths.py, agent
prompts) invokes it.

Spec backlink: docs/plans/2026-06-30-cross-platform-file-naming-helper.md § AC1, AC2
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (Wave E3-c)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

BIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/bin"
CLI = os.path.join(BIN_DIR, "coordinator-safe-name.py")
LIB = os.path.join(BIN_DIR, "lib", "coordinator_safe_name.py")

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}Z$")


def _run(args, cwd=None, stdin=None):
    return subprocess.run(
        [sys.executable, CLI, *args], cwd=cwd, input=stdin, capture_output=True, text=True, timeout=30, **_NO_CONSOLE
    )


def test_cli_and_lib_present():
    assert os.path.isfile(CLI), f"CLI not found at {CLI}"
    assert os.path.isfile(LIB), f"lib not found at {LIB}"


def test_timestamp_now_format_and_no_colon():
    ts = _run(["timestamp", "--now"]).stdout.strip()
    assert _TS_RE.match(ts), f"format mismatch: {ts!r}"
    assert ":" not in ts


def test_timestamp_mtime_format_and_no_colon():
    ts = _run(["timestamp", "--mtime", LIB]).stdout.strip()
    assert _TS_RE.match(ts), f"format mismatch: {ts!r}"
    assert ":" not in ts


def test_slug_lowercases():
    assert _run(["slug", "UPPERCASE"]).stdout.strip() == "uppercase"


def test_slug_output_is_alnum_and_hyphen_only():
    slug = _run(["slug", "hello world! foo"]).stdout.strip()
    assert re.match(r"^[a-z0-9-]+$", slug), slug


def test_slug_trims_leading_trailing_hyphen():
    slug = _run(["slug", "  leading and trailing  "]).stdout.strip()
    assert not slug.startswith("-") and not slug.endswith("-")


def test_slug_caps_at_40_chars_no_trailing_hyphen():
    slug = _run(
        ["slug", "this is a very long title that definitely exceeds the forty character limit for slugs"]
    ).stdout.strip()
    assert len(slug) <= 40
    assert not slug.endswith("-")


@pytest.mark.parametrize("name", ["safe-filename-2026", "hello", "2026abc"])
def test_check_accepts_safe_names(name):
    assert _run(["check", name]).returncode == 0


@pytest.mark.parametrize(
    "name",
    [
        "file:name",
        "file?name",
        "file*name",
        "file<name",
        "file>name",
        "file|name",
        'file"name',
        "file\\name",
        "file/name",
        "filename.",
        "filename ",
    ],
)
def test_check_rejects_illegal_names(name):
    assert _run(["check", name]).returncode != 0


def test_cli_runs_from_unrelated_cwd():
    result = _run(["check", "ok"], cwd="/")
    assert result.returncode == 0


def _ts_for_filename(iso_ts: str) -> str:
    return re.sub(r"[:+]", "-", iso_ts)


@pytest.mark.parametrize(
    "iso_ts",
    [
        "2026-06-15T10:30:00+00:00",
        "2099-12-31T23:59:59Z",
        "2000-01-01T00:00:00+05:30",
        "2026-06-15T10:30:00-08:00",
        "2026-01-01T00:00:00+00:00",
    ],
)
def test_cross_impl_ts_for_filename_passes_check(iso_ts):
    out = _ts_for_filename(iso_ts)
    assert _run(["check", out]).returncode == 0, out


def _slug_from_title(title: str) -> str:
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:40]


@pytest.mark.parametrize(
    "title",
    [
        "Hello World: The Final Episode",
        "Plan/Spec?? For <File> Naming | System",
        'UPPER CASE title with "quotes"',
        "trailing spaces   ",
        "Multiple---hyphens and CAPS",
        "file.name.with.dots",
        "   leading spaces too",
    ],
)
def test_cross_impl_slug_from_title_passes_check(title):
    out = _slug_from_title(title)
    if not out:
        pytest.skip("slug produced empty output for this input")
    assert _run(["check", out]).returncode == 0, out


def test_check_paths_batch_mixed():
    stdin = "clean/path\nbad:name/file\n"
    result = _run(["check-paths"], stdin=stdin)
    assert result.returncode == 1
    assert "bad:name/file" in result.stdout + result.stderr
    assert "clean/path" not in (result.stdout + result.stderr)


def test_check_paths_batch_all_clean():
    stdin = "clean/path\nother/clean\n"
    result = _run(["check-paths"], stdin=stdin)
    assert result.returncode == 0
