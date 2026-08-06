"""Tests for coordinator_core.ops.list_week_changelog.

Golden oracle snapshotted 2026-07-16
(Port of: list-week-changelog.sh, example-doctrine-repo b5a4192c, 2026-07-20):

    real state/week-changelog/ (this repo, on-disk, no commit-shaped lines)
        -> per-file "lines=N  commit-lines=0" line, THEN a bare "0" line
           (the doubled-zero grep-bug artifact), then "---", then the
           HEADER's Week-starting/Last-workweek-start marker lines. Exit 0.
    cwd outside any git repo
        -> single stderr "not a git repo" line, exit 0, no further output.
"""

from __future__ import annotations

import io
import contextlib
from pathlib import Path

from coordinator_core.ops import list_week_changelog as lwc


# ---------------------------------------------------------------------------
# _grep_commit_count_bugcompat — parity-critical doubled-zero bug reproduction
# ---------------------------------------------------------------------------


def test_bugcompat_zero_matches_doubles(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("not a commit line\nnor this one\n")
    assert lwc._grep_commit_count_bugcompat(f) == "0\n0"


def test_bugcompat_nonzero_matches_single(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("abc1234 a commit\nnot a commit\ndef5678 another\n")
    assert lwc._grep_commit_count_bugcompat(f) == "2"


def test_wc_l_counts_newlines(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("one\ntwo\nthree\n")
    assert lwc._wc_l(f) == "3"


# ---------------------------------------------------------------------------
# main() — full CLI behavior via LWC_TEST_STATE_ROOT seam
# ---------------------------------------------------------------------------


def _run_main(monkeypatch, tmp_path, argv):
    monkeypatch.setenv("LWC_TEST_STATE_ROOT", str(tmp_path / "state"))
    # pytest's tmp_path is never itself inside a git repo — stub the git-repo
    # guard so these tests exercise the state-root/listing logic, not git.
    monkeypatch.setattr(lwc, "_git_root", lambda cwd=None: str(tmp_path))
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = lwc.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_no_changelog_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rc, out, err = _run_main(monkeypatch, tmp_path, [str(tmp_path)])
    assert rc == 0
    assert f"(no state/week-changelog/ directory at {tmp_path})" in out


def test_no_daily_files_only_header(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "state" / "week-changelog"
    d.mkdir(parents=True)
    (d / "HEADER.md").write_text(
        "**Week starting:** 2026-07-13\n**Last /workweek-start:** 2026-07-13\n"
    )
    rc, out, err = _run_main(monkeypatch, tmp_path, [str(tmp_path)])
    assert rc == 0
    lines = out.splitlines()
    assert "(no daily files)" in lines
    assert "---" in lines
    assert "**Week starting:** 2026-07-13" in lines
    assert "**Last /workweek-start:** 2026-07-13" in lines


def test_daily_file_zero_commit_lines_reproduces_doubled_zero(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "state" / "week-changelog"
    d.mkdir(parents=True)
    (d / "2026-07-15-foo.md").write_text("no shas here\njust prose\n")
    rc, out, err = _run_main(monkeypatch, tmp_path, [str(tmp_path)])
    assert rc == 0
    assert "2026-07-15-foo.md  lines=2  commit-lines=0\n0" in out


def test_daily_file_with_commit_lines(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "state" / "week-changelog"
    d.mkdir(parents=True)
    (d / "2026-07-15-foo.md").write_text(
        "abc1234 first commit\ndef5678 second commit\nprose line\n"
    )
    rc, out, err = _run_main(monkeypatch, tmp_path, [str(tmp_path)])
    assert rc == 0
    assert "2026-07-15-foo.md  lines=3  commit-lines=2" in out


def test_header_md_excluded_from_daily_listing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "state" / "week-changelog"
    d.mkdir(parents=True)
    (d / "HEADER.md").write_text("**Week starting:** 2026-07-13\n")
    (d / "2026-07-15-foo.md").write_text("x\n")
    rc, out, err = _run_main(monkeypatch, tmp_path, [str(tmp_path)])
    assert "HEADER.md" not in out.split("---")[0].replace("HEADER has no", "")
    assert "2026-07-15-foo.md" in out


def test_no_header_md(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "state" / "week-changelog"
    d.mkdir(parents=True)
    (d / "2026-07-15-foo.md").write_text("x\n")
    rc, out, err = _run_main(monkeypatch, tmp_path, [str(tmp_path)])
    assert "(no HEADER.md)" in out


def test_header_without_markers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "state" / "week-changelog"
    d.mkdir(parents=True)
    (d / "HEADER.md").write_text("# nothing relevant\n")
    rc, out, err = _run_main(monkeypatch, tmp_path, [str(tmp_path)])
    assert "(HEADER has no Week-starting / Last-workweek-start markers)" in out


def test_not_a_git_repo(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    rc, out, err = lwc.main([str(tmp_path)]), None, None
    # Re-run via direct call (no state-root seam needed since git guard fires first).
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = lwc.main([str(tmp_path)])
    assert rc == 0
    assert "is not a git repo" in err_buf.getvalue()
    assert out_buf.getvalue() == ""


def test_main_never_raises_on_unexpected_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def _boom(argv):
        raise RuntimeError("boom")

    monkeypatch.setattr(lwc, "_main", _boom)
    assert lwc.main([]) == 0
