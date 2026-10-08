"""coordinator/bin/tests/test_publish_staging_skips_fleet_env.py — a provisioned
fleet environment (`.fleet-env/` and its `.prior` / `.gen-*` siblings) is never
staged, never reported as removed, and never touched.

Measured live 2026-08-20: the family totalled 9.6GB in a mirror, and every
byte used to be copied into staging, walked by the content-transform sweep and
compared, then deleted. The round now mints an EMPTY staging directory per row
and compares only what the row stages (plus the removals the sync names)
against the destination, so the family is out of every phase by construction.
What this file pins is that construction:

  * the staging directory is empty whatever the destination holds, `.git` and
    the fleet environment included;
  * the change report never lists the family as a `REMOVE:`, whatever the
    destination holds — `percolate-round.py::_extract_change_lines` builds its
    commit pathspec from those lines, and a phantom `REMOVE:` would ask git to
    record the deletion of a gitignored multi-GB environment nothing deleted;
  * a real removal the sync names is still reported;
  * a top-level FILE that happens to carry one of these names is ordinary
    payload: reported when it changes.

No git process is spawned: a plain `.git` directory is a faithful stand-in
because nothing under test reads it.

Run: python -m pytest coordinator/bin/tests/test_publish_staging_skips_fleet_env.py -x -q
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import time
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent

_FLEET_ENV_NAMES = (".fleet-env", ".fleet-env.prior", ".fleet-env.gen-72332-47c78a42")


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_staging_skips_fleet_env_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _seed_dest(root: Path, *, with_git: bool, prior_as_file: bool = False) -> Path:
    """`prior_as_file` swaps `.fleet-env.prior` from its usual directory shape
    for a top-level FILE of the same name, for the file-vs-directory test."""
    dest = root / "dest"
    (dest / "coordinator_core").mkdir(parents=True)
    (dest / "coordinator_core" / "real.py").write_text("payload\n", encoding="utf-8")
    if with_git:
        (dest / ".git").mkdir()
        (dest / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    for name in _FLEET_ENV_NAMES:
        if name == ".fleet-env.prior" and prior_as_file:
            (dest / name).write_text("prior-file-v1\n", encoding="utf-8")
            continue
        env = dest / name / "Lib" / "site-packages"
        env.mkdir(parents=True)
        (env / "vendored.py").write_text("# not ours\n", encoding="utf-8")
    return dest


def _report(staging: Path, dest: Path, removed: "frozenset[str]" = frozenset()):
    buf = io.StringIO()
    totals = publish.RunTotals()
    publish._report_staged_diff(staging, dest, removed, totals, out=buf)
    return buf.getvalue(), totals


def test_staging_is_empty_and_the_destination_is_left_alone(tmp_path):
    """Root-dest and subdir-shaped destinations alike: nothing is copied, so the
    fleet environment, the payload and `.git` are all absent from staging and
    all still present in the destination."""
    for sub, with_git in (("a", True), ("b", False)):
        dest = _seed_dest(tmp_path / sub, with_git=with_git)

        staging = publish._create_publish_staging_dir(dest)

        assert staging != dest
        assert list(staging.iterdir()) == [], f"staging was seeded from the destination: {list(staging.iterdir())}"
        for name in _FLEET_ENV_NAMES:
            assert (dest / name / "Lib" / "site-packages" / "vendored.py").is_file()
        assert (dest / "coordinator_core" / "real.py").is_file()


def test_report_does_not_list_the_fleet_env_as_removed(tmp_path):
    """Real payload removals the sync names are reported; the unstaged family is
    not, and `totals.deleted` counts only the real one."""
    dest = _seed_dest(tmp_path, with_git=True)
    (dest / "coordinator_core" / "gone.py").write_text("removed\n", encoding="utf-8")
    staging = publish._create_publish_staging_dir(dest)

    report, totals = _report(staging, dest, frozenset({"coordinator_core/gone.py"}))

    removed = [line for line in report.splitlines() if "REMOVE:" in line]
    assert [line.strip() for line in removed] == ["REMOVE: coordinator_core/gone.py"], report
    assert totals.deleted == 1 and totals.synced == 0, report


def test_report_for_a_subdir_row_is_empty_when_nothing_is_staged(tmp_path):
    dest = _seed_dest(tmp_path, with_git=False)
    staging = publish._create_publish_staging_dir(dest)

    report, totals = _report(staging, dest)

    assert not [line for line in report.splitlines() if ".fleet-env" in line], report
    assert totals.synced == 0 and totals.deleted == 0, report


def test_a_top_level_fleet_env_FILE_that_changed_is_reported(tmp_path):
    """A top-level FILE carrying a family name is ordinary payload: staged and
    changed, it reads as `UPDATE:`."""
    dest = _seed_dest(tmp_path, with_git=True, prior_as_file=True)
    staging = publish._create_publish_staging_dir(dest)
    (staging / ".fleet-env.prior").write_text("prior-file-v2\n", encoding="utf-8")

    report, totals = _report(staging, dest)

    assert any(
        line.strip().startswith("UPDATE:") and ".fleet-env.prior" in line
        for line in report.splitlines()
    ), report
    assert totals.synced == 1 and totals.deleted == 0, report


def test_a_top_level_fleet_env_FILE_that_matches_is_not_reported(tmp_path):
    dest = _seed_dest(tmp_path, with_git=True, prior_as_file=True)
    staging = publish._create_publish_staging_dir(dest)
    (staging / ".fleet-env.prior").write_text("prior-file-v1\n", encoding="utf-8")

    report, totals = _report(staging, dest)

    assert ".fleet-env.prior" not in report, report
    assert totals.synced == 0 and totals.deleted == 0, report


def test_report_ignores_a_generation_dir_created_after_staging(tmp_path):
    """A fresh `.fleet-env.gen-<pid>-<hex>` provisioned by another session after
    the staging directory is minted produces no line and no count: the report
    never walks the destination."""
    dest = _seed_dest(tmp_path, with_git=True)
    staging = publish._create_publish_staging_dir(dest)
    late_gen = dest / ".fleet-env.gen-99999-deadbeefcafe" / "Lib" / "site-packages"
    late_gen.mkdir(parents=True)
    (late_gen / "vendored.py").write_text("# arrived after staging\n", encoding="utf-8")

    report, totals = _report(staging, dest)

    assert not [line for line in report.splitlines() if ".fleet-env" in line], report
    assert totals.synced == 0 and totals.deleted == 0, report


def test_report_never_lists_the_mirrors_local_state_as_removed(tmp_path):
    """`state/` and kin are never published, so a mirror's own runtime cache
    there is absent from staging and must not read as a REMOVE."""
    dest = _seed_dest(tmp_path, with_git=True)
    staging = publish._create_publish_staging_dir(dest)
    cache = dest / "state" / "cache"
    cache.mkdir(parents=True)
    (cache / "bt-python3-invocation-cache.json").write_text("{}\n", encoding="utf-8")

    report, totals = _report(staging, dest)

    assert "state/cache" not in report, report
    assert totals.deleted == 0


def test_live_staging_dir_survives_a_sibling_rows_stale_sweep(tmp_path):
    """A second row on the same dest must not sweep the first row's live staging
    dir, even when the destination's own mtime is old."""
    dest = _seed_dest(tmp_path, with_git=True)
    old = time.time() - 7 * 86400
    os.utime(dest, (old, old))

    staging = publish._create_publish_staging_dir(dest)
    publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals(), out=io.StringIO())

    assert staging.is_dir()
