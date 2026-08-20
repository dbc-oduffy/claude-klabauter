"""coordinator/bin/tests/test_publish_staging_skips_fleet_env.py — regression
guard for `_create_publish_staging_dir` copying a provisioned fleet environment
into the publish staging tree.

Measured live 2026-08-20 on a real `claude-klabauter-publish-repo-toplevel`
run: `.fleet-env/` and `.fleet-env.gen-<pid>-<hex>/` totalled 9.6GB in the
mirror, and every byte was copied into staging, walked by
`run_content_transform_sweep`, compared by `_dir_trees_equal`, then deleted —
four full passes over content no phase the staging tree feeds ever reads. The
sweep leg additionally reproduced verbatim the non-utf-8 joblib read failure
that `surface.STRUCTURAL_NEVER_PUBLISHED_PREFIXES`' `.fleet-env` entry was
itself added to stop, because `.fleet-env.gen-*` is a sibling name that
segment-exact matching never covered.

The two constraints that make the exclusion safe rather than destructive are
what this file actually pins, because getting either wrong silently deletes a
multi-GB directory:

  * top-level only — a NESTED path carrying one of these basenames is still
    copied (`shutil.ignore_patterns` could not express this, which is why the
    implementation is a callable);
  * root-dest rows only — gated on `(dest_dir / ".git").exists()`, the same
    predicate `_swap_publish_staging_into_dest` uses to choose the branch that
    never removes a top-level directory from `dest_dir`. A `dest_subdir` row
    takes the whole-tree branch, which renames staging ONTO the destination,
    where the same exclusion would destroy the environment instead of
    preserving it.

No git process is spawned: `_create_publish_staging_dir` tests only for the
EXISTENCE of `.git`, so a plain directory is a faithful stand-in and this file
stays off the fast tier's spawn ratchet.

Run: python -m pytest coordinator/bin/tests/test_publish_staging_skips_fleet_env.py -x -q
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


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
    for a top-level FILE of the same, regex-matching name. It exists as an
    opt-in rather than a blanket change to every caller because the
    directory shape is what most of this file's tests need to pin the
    top-level/nested and root-dest/subdir gates; only the FILE-vs-directory
    tests below need the file shape, and on the same basename so the regex
    match is not itself in question."""
    dest = root / "dest"
    (dest / "coordinator_core").mkdir(parents=True)
    (dest / "coordinator_core" / "real.py").write_text("payload\n", encoding="utf-8")
    if with_git:
        (dest / ".git").mkdir()
        (dest / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    for name in (".fleet-env", ".fleet-env.prior", ".fleet-env.gen-72332-47c78a42"):
        if name == ".fleet-env.prior" and prior_as_file:
            (dest / name).write_text("prior-file-v1\n", encoding="utf-8")
            continue
        env = dest / name / "Lib" / "site-packages"
        env.mkdir(parents=True)
        (env / "vendored.py").write_text("# not ours\n", encoding="utf-8")
    return dest


def test_root_dest_staging_copy_skips_the_whole_fleet_env_family(tmp_path):
    """The three top-level fleet-env directories are absent from staging, the
    real payload is present, and — critically — the destination still HAS
    them: the root-dest swap branch leaves a directory it never staged alone,
    so skipping the copy must never be observable as a removal."""
    dest = _seed_dest(tmp_path, with_git=True)

    staging = publish._create_publish_staging_dir(dest)

    for name in (".fleet-env", ".fleet-env.prior", ".fleet-env.gen-72332-47c78a42"):
        assert not (staging / name).exists(), f"{name} was copied into staging"
        assert (dest / name / "Lib" / "site-packages" / "vendored.py").is_file(), (
            f"{name} was removed from the destination"
        )
    assert (staging / "coordinator_core" / "real.py").read_text(encoding="utf-8") == "payload\n"
    assert not (staging / ".git").exists()


def test_subdir_row_staging_copy_keeps_fleet_env(tmp_path):
    """A `dest_dir` with no `.git` of its own is a `dest_subdir` row, whose
    swap renames staging ONTO the destination. Excluding anything there would
    delete it, so the gate must keep the copy — even for these basenames."""
    dest = _seed_dest(tmp_path, with_git=False)

    staging = publish._create_publish_staging_dir(dest)

    for name in (".fleet-env", ".fleet-env.prior", ".fleet-env.gen-72332-47c78a42"):
        assert (staging / name / "Lib" / "site-packages" / "vendored.py").is_file(), (
            f"{name} must survive into staging on a non-root dest"
        )


def test_nested_fleet_env_name_is_still_copied(tmp_path):
    """Top-level only. A nested directory that happens to carry the basename is
    ordinary content — `shutil.ignore_patterns` would have matched it at every
    depth, which is exactly the over-reach the callable exists to avoid."""
    dest = _seed_dest(tmp_path, with_git=True)
    nested = dest / "coordinator_core" / ".fleet-env"
    nested.mkdir()
    (nested / "fixture.py").write_text("fixture\n", encoding="utf-8")

    staging = publish._create_publish_staging_dir(dest)

    assert (staging / "coordinator_core" / ".fleet-env" / "fixture.py").is_file()


def test_published_diff_does_not_report_unstaged_fleet_env_as_removed(tmp_path):
    """The other half of the staging skip, and the reason it is not merely a
    performance change.

    `_create_publish_staging_dir` leaves the fleet-env family out of staging,
    so it is present in `dest_dir` and absent from `staging_dir` BY
    CONSTRUCTION. `_report_published_diff` reports `set(dest_files) -
    set(staged_files)` as REMOVE lines, so without a matching exclusion it
    reports every file of a multi-GB environment as deleted — measured live at
    95,256 phantom lines before this was fixed.

    That matters beyond the log: `percolate-round.py::_extract_change_lines`
    builds the pathspec it hands `scoped-git-commit` from these exact lines, so
    the phantom REMOVEs would ask git to record the deletion of a gitignored
    environment nothing deleted. Real payload removals must still be reported.
    """
    dest = _seed_dest(tmp_path, with_git=True)
    (dest / "coordinator_core" / "gone.py").write_text("removed\n", encoding="utf-8")

    staging = publish._create_publish_staging_dir(dest)
    # A genuine removal: the sync would have deleted this from staging.
    (staging / "coordinator_core" / "gone.py").unlink()

    buf = io.StringIO()
    totals = publish.RunTotals()
    publish._report_published_diff(staging, dest, totals, out=buf)
    report = buf.getvalue()

    removed = [line for line in report.splitlines() if "REMOVE:" in line]
    assert any("coordinator_core/gone.py" in line for line in removed), report
    assert not [line for line in removed if ".fleet-env" in line], (
        "unstaged fleet-env reported as removed — this feeds percolate-round's "
        "commit pathspec:\n" + "\n".join(removed[:10])
    )
    assert totals.deleted == 1, f"totals.deleted inflated by the fleet-env tree: {totals.deleted}"


def test_published_diff_reports_a_subdir_row_in_full(tmp_path):
    """The report-side exclusion must carry the SAME root-dest gate the
    staging skip carries. Review (code-reviewer on c5e5bcf81): without it, a
    `dest_subdir` row — which stages the family in full, because its swap
    renames staging ONTO the destination — would stage those files and then
    drop them from the dest walk, emitting a phantom `NEW:` for byte-identical
    untouched content on every run. Same class of defect as the phantom
    `REMOVE:` this pairing was introduced to fix, opposite sign."""
    dest = _seed_dest(tmp_path, with_git=False)

    staging = publish._create_publish_staging_dir(dest)

    buf = io.StringIO()
    totals = publish.RunTotals()
    publish._report_published_diff(staging, dest, totals, out=buf)
    report = buf.getvalue()

    assert not [line for line in report.splitlines() if ".fleet-env" in line], report
    assert totals.synced == 0 and totals.deleted == 0, report


def test_published_diff_reports_a_top_level_fleet_env_FILE_that_changed(tmp_path):
    """Directory-only, on both sides — proven by a FILE that positively
    matches `_FLEET_ENV_STAGING_SKIP_RE` (`.fleet-env.prior`, via
    `prior_as_file=True`), not one that merely happens to start with the same
    prefix. A top-level FILE carrying this name IS staged (the root-dest swap
    unlinks top-level files absent from staging), so it must also be
    reported — otherwise it reads as a phantom `UPDATE:` forever, or worse,
    silently drops a real change.

    This is the byte-differs half: staging is mutated after copy so the two
    sides genuinely disagree, and the assertions require the name to be
    present under BOTH `staging_dir` and `dest_dir` (not merely absent from
    both diff lists, which would pass just as well if the name were dropped
    from staging entirely — the vacuity `.fleet-env.lock` used to hide,
    since that name never matches the regex in the first place). If
    `entry.is_dir()` were dropped from `_fleet_env_unstaged_names`, this file
    would be swept into `unstaged` (it matches the regex) and never staged at
    all, so `(staging / ...).is_file()` below would fail first."""
    dest = _seed_dest(tmp_path, with_git=True, prior_as_file=True)

    staging = publish._create_publish_staging_dir(dest)

    assert (staging / ".fleet-env.prior").is_file()
    assert (dest / ".fleet-env.prior").is_file()
    (staging / ".fleet-env.prior").write_text("prior-file-v2\n", encoding="utf-8")

    buf = io.StringIO()
    totals = publish.RunTotals()
    publish._report_published_diff(staging, dest, totals, out=buf)
    report = buf.getvalue()

    assert any(
        line.strip().startswith("UPDATE:") and ".fleet-env.prior" in line
        for line in report.splitlines()
    ), report
    assert totals.synced == 1 and totals.deleted == 0, report


def test_published_diff_reports_a_top_level_fleet_env_FILE_that_matches(tmp_path):
    """The other half of the pairing above: same FILE, unchanged between
    staging and dest, must appear in NEITHER the `NEW:`/`UPDATE:` list nor the
    `REMOVE:` list — the only way to prove `totals.synced == 0 and
    totals.deleted == 0` reflects "present and identical on both sides"
    rather than "absent from both", which the single-assertion version of
    this test could not distinguish."""
    dest = _seed_dest(tmp_path, with_git=True, prior_as_file=True)

    staging = publish._create_publish_staging_dir(dest)

    assert (staging / ".fleet-env.prior").is_file()
    assert (dest / ".fleet-env.prior").is_file()
    assert (staging / ".fleet-env.prior").read_text(encoding="utf-8") == (
        dest / ".fleet-env.prior"
    ).read_text(encoding="utf-8")

    buf = io.StringIO()
    totals = publish.RunTotals()
    publish._report_published_diff(staging, dest, totals, out=buf)
    report = buf.getvalue()

    assert ".fleet-env.prior" not in report, report
    assert totals.synced == 0 and totals.deleted == 0, report


def test_report_excludes_a_generation_dir_created_after_the_staging_copy(tmp_path):
    """Pins the TOCTOU closure commit `08f4dc693` introduced: `_went_unstaged`
    (§ `_report_published_diff`) observes what `staging_dir` actually holds
    instead of re-scanning `dest_dir` a second time.

    This machine runs 50-70 concurrent sessions (`docs/wiki/machine-load-norm.md`);
    a fresh `.fleet-env.gen-<pid>-<hex>` can be provisioned by another one in
    the window between `_create_publish_staging_dir`'s copy and
    `_report_published_diff`'s walk. This test pins the observable behaviour:
    a `.fleet-env.gen-*` directory appearing in `dest_dir` after the staging
    copy must produce no `REMOVE:`/`NEW:` line and must not inflate `totals`,
    because those lines feed `percolate-round.py::_extract_change_lines`'s
    commit pathspec.

    It does NOT discriminate `_went_unstaged` from the prior `dest_dir`-re-
    deriving shape — that shape happens to reach the same answer for this
    input too. The reason to prefer `_went_unstaged` is the absence of a
    second `dest_dir` scan, and therefore of a window at all, which is a
    structural property no black-box test over this function can observe."""
    dest = _seed_dest(tmp_path, with_git=True)

    staging = publish._create_publish_staging_dir(dest)

    late_gen = dest / ".fleet-env.gen-99999-deadbeefcafe" / "Lib" / "site-packages"
    late_gen.mkdir(parents=True)
    (late_gen / "vendored.py").write_text("# arrived after the copy\n", encoding="utf-8")

    buf = io.StringIO()
    totals = publish.RunTotals()
    publish._report_published_diff(staging, dest, totals, out=buf)
    report = buf.getvalue()

    assert not [line for line in report.splitlines() if ".fleet-env" in line], report
    assert totals.deleted == 0, report
    assert totals.synced == 0, report


def test_fleet_env_unstaged_names_root_dest_directories_only(tmp_path):
    """`_fleet_env_unstaged_names` has exactly one caller —
    `_create_publish_staging_dir` — after commit `08f4dc693` split the
    report side onto its own `_went_unstaged` closure over `staging_dir`
    (§ `_report_published_diff`'s docstring, "two earlier attempts to share a
    derivation here did both [fail]"). This test pins this helper's own two
    gates directly, not as a stand-in for the report side's behaviour."""
    root_dest = _seed_dest(tmp_path / "a", with_git=True, prior_as_file=True)
    subdir_dest = _seed_dest(tmp_path / "b", with_git=False)

    assert publish._fleet_env_unstaged_names(root_dest) == frozenset(
        {".fleet-env", ".fleet-env.gen-72332-47c78a42"}
    )
    # A file, not a directory — never unstaged, even though its name matches.
    assert ".fleet-env.prior" not in publish._fleet_env_unstaged_names(root_dest)
    # Not a root-dest row — nothing is unstaged at all.
    assert publish._fleet_env_unstaged_names(subdir_dest) == frozenset()
    assert publish._fleet_env_unstaged_names(tmp_path / "absent") == frozenset()


@pytest.mark.parametrize(
    "name, skipped",
    [
        (".fleet-env", True),
        (".fleet-env.prior", True),
        (".fleet-env.gen-1-a", True),
        (".fleet-env.gen-72332-47c78a42", True),
        (".fleet-envy", False),
        ("fleet-env", False),
        (".fleet-env.md", False),
    ],
)
def test_skip_pattern_is_anchored_to_the_family(name, skipped):
    """The pattern must not reach a neighbouring name. `.fleet-env.md` in
    particular is the shape a doc file would take."""
    assert bool(publish._FLEET_ENV_STAGING_SKIP_RE.match(name)) is skipped


def test_a_fleet_env_FILE_at_top_level_is_still_copied(tmp_path):
    """`.fleet-env.prior` here is seeded as a FILE (`prior_as_file=True`), not
    a directory — it therefore DOES match `_FLEET_ENV_STAGING_SKIP_RE`, unlike
    `.fleet-env.lock`/`.fleet-env.prior-notes` used previously, which never
    matched the pattern at all and so could not distinguish the `is_dir()`
    gate from "doesn't match the regex in the first place": mutating
    `entry.is_dir()` out of `_fleet_env_unstaged_names` would have left both
    of those tests green.

    With the regex genuinely matching, only `entry.is_dir()` keeps this name
    out of `unstaged` and therefore out of `_ignore`'s exclusion set. Delete
    that condition and this file's basename joins `unstaged`, `_ignore`
    starts returning it from `_create_publish_staging_dir`'s `copytree`
    callback, and the assertion below goes red — proving the condition is
    load-bearing rather than merely present.

    The root-dest swap DOES delete a top-level file that is absent from
    staging (only directories are exempt), so skipping the copy would delete
    a file another session may be holding — the reason the skip is
    directory-gated at all."""
    dest = _seed_dest(tmp_path, with_git=True, prior_as_file=True)

    staging = publish._create_publish_staging_dir(dest)

    assert (staging / ".fleet-env.prior").is_file()
