"""coordinator/bin/test_spawn_census.py — fixture-based tests for the
`spawn-census` CLI (report and assert modes over one shared detector).

Purpose: pin the two-mode contract against small, hand-built fixture repos
(never the live checkout, which drifts) — report emits both a human table
and `--json` with identical fields; assert shares the same detector and
exits non-zero exactly when an unsanctioned site exists; exclusions
(scratch/, scratchpad/) and the unpinned-digest count are surfaced in both
output shapes; and the negative-spec (no "hot path"/"cost"/"latency"
wording — this tool reports call-sites, never live costs) holds.

`coordinator/bin/spawn-census` has no `.py` suffix (deliberate — see its own
module docstring), so it is loaded here via `importlib.util.spec_from_file_
location` rather than a normal import statement.

Spec backlink: pln-shell-spawn-regrowth-gate-cens-097e21 § C4
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import textwrap
from importlib.machinery import SourceFileLoader
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_SCRIPT = Path(__file__).resolve().parent / "spawn-census"


def _load_module():
    loader = SourceFileLoader("spawn_census_under_test", str(_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


spawn_census = _load_module()


_ONE_SANCTIONED_SITE = textwrap.dedent(
    """\
    import subprocess

    def _install():
        subprocess.run(["bash", "installer.sh"])
    """
)

_ONE_UNSANCTIONED_SITE = textwrap.dedent(
    """\
    import subprocess

    def _other():
        subprocess.run(["bash", "other.sh"])
    """
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _digest_for(root: Path, path: str, enclosing: str, argv0: str, ordinal: int) -> str:
    """Compute the real argv_digest for a fixture site via the pinned
    detector, so the register block seeds a genuinely pinned entry rather
    than a guessed digest string.
    """
    from coordinator_core.spawn_policy import sites_in_source

    text = (root / path).read_text(encoding="utf-8")
    for site in sites_in_source(text, path):
        if site.enclosing == enclosing and site.argv0 == argv0 and site.ordinal == ordinal:
            return site.argv_digest
    raise AssertionError(f"no site found for {path}:{enclosing}:{argv0}:{ordinal}")


def _make_repo(tmp_path: Path, *, unpinned: bool = False, with_unsanctioned: bool = False) -> Path:
    root = tmp_path / "repo"
    _write(root / "coordinator_core" / "installer.py", _ONE_SANCTIONED_SITE)
    if with_unsanctioned:
        _write(root / "coordinator_core" / "other.py", _ONE_UNSANCTIONED_SITE)
    # Fixture in scratch/ — must be excluded and counted as suppressed.
    _write(root / "scratch" / "snapshot.py", _ONE_SANCTIONED_SITE)

    digest = None if unpinned else _digest_for(
        root, "coordinator_core/installer.py", "_install", "bash", 0
    )
    digest_yaml = "null" if digest is None else f'"{digest}"'
    carve_outs = textwrap.dedent(
        f"""\
        # Shell-out carve-outs (fixture)

        ```yaml shell-out-allowlist
        - cls: a
          path: coordinator_core/installer.py
          enclosing: _install
          argv0: bash
          ordinal: 0
          argv_digest: {digest_yaml}
          reason: "fixture carve-out"
          ruled_on: "2026-08-06"
        ```
        """
    )
    _write(root / "docs" / "reference" / "shell-out-carve-outs.md", carve_outs)
    return root


def _make_repo_over_parallel_threshold(tmp_path: Path, n_files: int = 30) -> Path:
    """A repo above spawn-census's own _PARALLEL_FILE_THRESHOLD, so the
    ProcessPoolExecutor path (rather than the small-repo serial fallback)
    actually runs.
    """
    root = tmp_path / "repo"
    for i in range(n_files):
        text = _ONE_SANCTIONED_SITE if i % 2 == 0 else _ONE_UNSANCTIONED_SITE
        _write(root / "coordinator_core" / f"mod_{i}.py", text)
    # Fixture in scratch/ — must be excluded and counted as suppressed.
    _write(root / "scratch" / "snapshot.py", _ONE_SANCTIONED_SITE)

    digest = _digest_for(root, "coordinator_core/mod_0.py", "_install", "bash", 0)
    carve_outs = textwrap.dedent(
        f"""\
        # Shell-out carve-outs (fixture)

        ```yaml shell-out-allowlist
        - cls: a
          path: coordinator_core/mod_0.py
          enclosing: _install
          argv0: bash
          ordinal: 0
          argv_digest: "{digest}"
          reason: "fixture carve-out"
          ruled_on: "2026-08-06"
        ```
        """
    )
    _write(root / "docs" / "reference" / "shell-out-carve-outs.md", carve_outs)
    return root


def _run_subprocess(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        **no_console_creationflags(),
    )


def test_parallel_and_serial_report_json_are_byte_identical(tmp_path):
    """The whole point of parallelising the walk: a faster census that
    reports differently is not the same census. Spawns the real script via
    subprocess (not the in-process module load used elsewhere in this file)
    so ProcessPoolExecutor's 'spawn' start method actually forks workers,
    over a fixture repo sized above _PARALLEL_FILE_THRESHOLD.
    """
    root = _make_repo_over_parallel_threshold(tmp_path)

    serial = _run_subprocess(["report", "--json", "--root", str(root), "--workers", "1"])
    parallel = _run_subprocess(["report", "--json", "--root", str(root), "--workers", "4"])

    assert serial.returncode == 0, serial.stderr
    assert parallel.returncode == 0, parallel.stderr

    serial_census = json.loads(serial.stdout)
    parallel_census = json.loads(parallel.stdout)
    # elapsed_seconds legitimately differs between the two modes -- it is
    # the one field this test does not pin.
    serial_census.pop("elapsed_seconds")
    parallel_census.pop("elapsed_seconds")
    assert serial_census == parallel_census


def test_parallel_and_serial_report_human_table_identical_modulo_timing_line(tmp_path):
    root = _make_repo_over_parallel_threshold(tmp_path)

    serial = _run_subprocess(["report", "--root", str(root), "--workers", "1"])
    parallel = _run_subprocess(["report", "--root", str(root), "--workers", "4"])

    assert serial.returncode == 0, serial.stderr
    assert parallel.returncode == 0, parallel.stderr

    # First line carries the elapsed-time figure; every other line (site
    # ordering, By-kind/By-file breakdown, exclusions, unpinned/unsanctioned
    # counts) must match exactly.
    serial_lines = serial.stdout.splitlines()[1:]
    parallel_lines = parallel.stdout.splitlines()[1:]
    assert serial_lines == parallel_lines


def test_default_worker_cap_is_bounded_never_bare_cpu_count(tmp_path):
    cap = spawn_census.default_worker_cap()
    assert cap >= 1
    # Never a bare os.cpu_count() fan-out (the plan's hard constraint).
    assert cap <= 8
    assert cap <= max(1, (os.cpu_count() or 1))


def test_small_repo_stays_serial_regardless_of_workers_flag(tmp_path):
    """Below _PARALLEL_FILE_THRESHOLD, --workers is accepted but the process
    pool never spins up -- avoids fork/pickle overhead on tiny repos (and
    keeps every other in-process fixture test in this file fast).
    """
    root = _make_repo(tmp_path)
    code, out, _err = _run(["report", "--json", "--root", str(root), "--workers", "8"])
    assert code == 0
    census = json.loads(out)
    assert census["total_sites"] == 1


def test_comment_only_mention_of_subprocess_is_still_fully_parsed(tmp_path):
    """If a textual pre-filter is ever added (plan step 3, not needed here --
    parallelising the walk alone cleared the AC3 budget), it must be a
    strict over-approximation: a file merely mentioning 'subprocess' in a
    comment, with no real call, must still be fully AST-parsed and still
    yield zero sites -- never skipped on a textual guess.
    """
    root = tmp_path / "repo"
    _write(
        root / "coordinator_core" / "commentary.py",
        "# this module does not use subprocess, os.system, or os.popen\n"
        "def _noop():\n"
        "    return 1\n",
    )
    _write(
        root / "docs" / "reference" / "shell-out-carve-outs.md",
        "# Shell-out carve-outs (fixture)\n\n```yaml shell-out-allowlist\n[]\n```\n",
    )
    code, out, _err = _run(["report", "--json", "--root", str(root)])
    assert code == 0
    census = json.loads(out)
    assert census["total_sites"] == 0


def _run(args: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = spawn_census.main(args)
    return code, out.getvalue(), err.getvalue()


def test_report_json_shape_and_counts(tmp_path):
    root = _make_repo(tmp_path)
    code, out, _err = _run(["report", "--json", "--root", str(root)])
    assert code == 0
    census = json.loads(out)

    assert census["total_sites"] == 1  # scratch/ site is excluded, not counted here
    assert census["counts_by_kind"] == {"shell-binary": 1}
    assert census["unsanctioned_count"] == 0
    assert census["unpinned_count"] == 0

    excl = census["excluded"]
    assert "scratch" in excl["paths"]
    assert excl["suppressed_site_count"] == 1


def test_report_human_table_has_exclusions_and_unpinned_lines(tmp_path):
    root = _make_repo(tmp_path)
    code, out, _err = _run(["report", "--root", str(root)])
    assert code == 0
    assert "Excluded paths:" in out
    assert "suppressed_site_count=1" in out
    assert "Unpinned register entries (argv_digest: null): 0" in out
    assert "Unsanctioned sites: 0" in out


def test_negative_spec_no_cost_or_hot_path_language(tmp_path):
    root = _make_repo(tmp_path, with_unsanctioned=True)
    _code, out_json, _err = _run(["report", "--json", "--root", str(root)])
    _code2, out_table, _err2 = _run(["report", "--root", str(root)])
    for blob in (out_json, out_table):
        lowered = blob.lower()
        assert "hot path" not in lowered
        assert "hot-path" not in lowered
        assert "latency" not in lowered
        # "cost" is intentionally not checked bare -- it is not used at all
        # in this tool's vocabulary, so a stricter substring check is safe.
        assert "cost" not in lowered


def test_assert_mode_passes_when_all_sanctioned(tmp_path):
    root = _make_repo(tmp_path)
    code, _out, _err = _run(["assert", "--root", str(root)])
    assert code == 0


def test_assert_mode_fails_on_unsanctioned_site(tmp_path):
    root = _make_repo(tmp_path, with_unsanctioned=True)
    code, _out, err = _run(["assert", "--root", str(root)])
    assert code == 1
    assert "other.py" in err


def test_unpinned_count_surfaced_in_both_shapes(tmp_path):
    root = _make_repo(tmp_path, unpinned=True)
    code, out, _err = _run(["report", "--json", "--root", str(root)])
    assert code == 0
    census = json.loads(out)
    assert census["unpinned_count"] == 1
    # An unpinned entry still matches on site_key() alone (bootstrap state),
    # so the sanctioned site remains sanctioned even while unpinned.
    assert census["unsanctioned_count"] == 0

    code2, out2, _err2 = _run(["report", "--root", str(root)])
    assert code2 == 0
    assert "Unpinned register entries (argv_digest: null): 1" in out2


def test_report_mode_json_and_table_agree_on_totals(tmp_path):
    root = _make_repo(tmp_path, with_unsanctioned=True)
    _code, out_json, _err = _run(["report", "--json", "--root", str(root)])
    census = json.loads(out_json)
    _code2, out_table, _err2 = _run(["report", "--root", str(root)])
    assert f"Unsanctioned sites: {census['unsanctioned_count']}" in out_table
    assert census["unsanctioned_count"] == 1


def test_unknown_mode_is_a_usage_error():
    code, _out, _err = _run(["bogus-mode"])
    assert code == 2
