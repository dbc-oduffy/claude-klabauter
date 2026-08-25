"""Characterization + parity tests for coordinator_core.ops.render_template.

Port of: render-template.sh (DoE 290997c7, 2026-07-22).
Spec backlink: docs/plans/2026-05-19-coordinator-installer-redesign-implementation.md § C1 (D3.b)
               docs/plans/2026-06-26-coordinator-install-update-friction-fix-slate.md § C-R3a

Positive/negative cases mirror the golden oracle captured from the bash
original (exercised on a temp corpus) prior to porting — see plan chunk
notes; not re-derived here.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.render_template import main


def _run(capsys, argv):
    rc = main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_basic_substitution_stdout(tmp_path, capsys):
    tpl = tmp_path / "t1.tpl"
    tpl.write_text("Hello {{NAME}},\nYour project is {{PROJECT}}.\nMulti:\n{{MULTI}}\nEnd.\n")
    rc, out, err = _run(capsys, [str(tpl), "NAME=Donal", "PROJECT=DoE", "MULTI=line1\nline2"])
    assert rc == 0
    assert err == ""
    assert out == "Hello Donal,\nYour project is DoE.\nMulti:\nline1\nline2\nEnd.\n"


def test_output_path_atomic_write(tmp_path, capsys):
    tpl = tmp_path / "t1.tpl"
    tpl.write_text("Hello {{NAME}},\nYour project is {{PROJECT}}.\nMulti:\n{{MULTI}}\nEnd.\n")
    out_path = tmp_path / "out2.txt"
    rc, out, err = _run(
        capsys,
        [str(tpl), "-o", str(out_path), "NAME=Donal", "PROJECT=DoE", "MULTI=line1\nline2"],
    )
    assert rc == 0
    assert err == ""
    assert out == ""
    assert out_path.read_text() == "Hello Donal,\nYour project is DoE.\nMulti:\nline1\nline2\nEnd.\n"


def test_no_trailing_newline_preserved(tmp_path, capsys):
    tpl = tmp_path / "t3.tpl"
    tpl.write_text("X={{X}}")
    rc, out, err = _run(capsys, [str(tpl), "X=5"])
    assert rc == 0
    assert out == "X=5"


def test_no_args_usage_message(capsys):
    rc, out, err = _run(capsys, [])
    assert rc == 1
    assert err.strip() == (
        "usage: render-template.sh <template-path> [-o <output-path>] [KEY=VALUE]..."
    )


def test_unreadable_template(tmp_path, capsys):
    missing = tmp_path / "no" / "such" / "file.tpl"
    rc, out, err = _run(capsys, [str(missing)])
    assert rc == 1
    assert err.strip() == f"render-template: cannot read template: {missing}"


def test_unsubstituted_key(tmp_path, capsys):
    tpl = tmp_path / "t4.tpl"
    tpl.write_text("Hi {{NAME}} {{MISSING}}\n")
    rc, out, err = _run(capsys, [str(tpl), "NAME=Bob"])
    assert rc == 1
    assert err.strip() == f"render-template: unsubstituted keys: MISSING in {tpl}"


def test_whitespace_inside_braces_rejected(tmp_path, capsys):
    tpl = tmp_path / "t5.tpl"
    tpl.write_text("Hi {{ SPACE }}\n")
    rc, out, err = _run(capsys, [str(tpl)])
    assert rc == 1
    assert err.strip() == f"render-template: unsubstituted keys: SPACE in {tpl}"


def test_invalid_key_not_bare_identifier(tmp_path, capsys):
    tpl = tmp_path / "t6.tpl"
    tpl.write_text("Hi {{1BAD}}\n")
    rc, out, err = _run(capsys, [str(tpl), "1BAD=x"])
    assert rc == 1
    assert err.strip() == "render-template: invalid key: 1BAD (must be a bare identifier)"


def test_dash_o_missing_argument(tmp_path, capsys):
    tpl = tmp_path / "t1.tpl"
    tpl.write_text("x")
    rc, out, err = _run(capsys, [str(tpl), "-o"])
    assert rc == 1
    assert err.strip() == "render-template: -o requires an argument"


def test_unexpected_argument(tmp_path, capsys):
    tpl = tmp_path / "t1.tpl"
    tpl.write_text("x")
    rc, out, err = _run(capsys, [str(tpl), "weird"])
    assert rc == 1
    assert err.strip() == "render-template: unexpected argument: weird"


def test_output_dir_does_not_exist(tmp_path, capsys):
    tpl = tmp_path / "t1.tpl"
    tpl.write_text("{{NAME}}")
    out_path = tmp_path / "no" / "such" / "dir" / "out.txt"
    rc, out, err = _run(capsys, [str(tpl), "-o", str(out_path), "NAME=x"])
    assert rc == 1
    assert err.strip() == f"render-template: output directory does not exist: {out_path.parent}"


def test_literal_replace_no_regex_metacharacters(tmp_path, capsys):
    """Values containing regex-special characters are inserted verbatim (str.replace, not re.sub)."""
    tpl = tmp_path / "t7.tpl"
    tpl.write_text("{{VAL}}")
    rc, out, err = _run(capsys, [str(tpl), r"VAL=a.*b\1$"])
    assert rc == 0
    assert out == r"a.*b\1$"


def test_guard_sentinel_nonexistent_output(tmp_path, capsys):
    tpl = tmp_path / "t8.tpl"
    tpl.write_text("{{NAME}}")
    out_path = tmp_path / "out8.txt"
    rc, out, err = _run(
        capsys, [str(tpl), "-o", str(out_path), "--guard-sentinel", "TOKEN", "NAME=x"]
    )
    assert rc == 0
    assert err == ""
    assert out_path.read_text() == "x"


def test_guard_sentinel_existing_empty_file(tmp_path, capsys):
    tpl = tmp_path / "t9.tpl"
    tpl.write_text("{{NAME}}")
    out_path = tmp_path / "out9.txt"
    out_path.write_text("")
    rc, out, err = _run(
        capsys, [str(tpl), "-o", str(out_path), "--guard-sentinel", "TOKEN", "NAME=x"]
    )
    assert rc == 0
    assert err == ""
    assert out_path.read_text() == "x"


def test_guard_sentinel_existing_file_with_sentinel(tmp_path, capsys):
    tpl = tmp_path / "t10.tpl"
    tpl.write_text("{{NAME}}")
    out_path = tmp_path / "out10.txt"
    out_path.write_text("<!-- coordinator:claude-md-seed:v1 -->\nold content\n")
    rc, out, err = _run(
        capsys,
        [
            str(tpl),
            "-o",
            str(out_path),
            "--guard-sentinel",
            "<!-- coordinator:claude-md-seed:v1 -->",
            "NAME=x",
        ],
    )
    assert rc == 0
    assert err == ""
    assert out_path.read_text() == "x"


def test_guard_sentinel_existing_file_without_sentinel_refused(tmp_path, capsys):
    tpl = tmp_path / "t11.tpl"
    tpl.write_text("{{NAME}}")
    out_path = tmp_path / "out11.txt"
    original = "hand-authored content, no token here\n"
    out_path.write_text(original)
    rc, out, err = _run(
        capsys, [str(tpl), "-o", str(out_path), "--guard-sentinel", "TOKEN", "NAME=x"]
    )
    assert rc == 3
    assert out_path.read_text() == original
    assert err.strip() == (
        f"render-template: refusing to overwrite {out_path}: existing non-empty file "
        "lacks guard sentinel 'TOKEN' (never-clobber guard)"
    )
    residue = list(tmp_path.glob("out11.txt.render-template.tmp.*"))
    assert residue == []


def test_guard_sentinel_without_output_path(tmp_path, capsys):
    tpl = tmp_path / "t12.tpl"
    tpl.write_text("{{NAME}}")
    rc, out, err = _run(capsys, [str(tpl), "--guard-sentinel", "TOKEN", "NAME=x"])
    assert rc == 1
    assert err.strip() == "render-template: --guard-sentinel requires -o <output-path>"


def test_guard_sentinel_missing_argument(tmp_path, capsys):
    tpl = tmp_path / "t13.tpl"
    tpl.write_text("x")
    rc, out, err = _run(capsys, [str(tpl), "--guard-sentinel"])
    assert rc == 1
    assert err.strip() == "render-template: --guard-sentinel requires an argument"


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="unreadable-file simulation via chmod is unreliable as root or on Windows",
)
def test_guard_sentinel_existing_unreadable_file_fails_closed(tmp_path, capsys):
    """An existing output file that cannot be read is refuse-worthy (fail-closed).

    Spec backlink: coordinator_core/ops/render_template.py:189-203 — an OSError
    on the pre-write read of the existing file must refuse (exit 3), never
    fail-open and clobber.
    """
    tpl = tmp_path / "t15.tpl"
    tpl.write_text("{{NAME}}")
    out_path = tmp_path / "out15.txt"
    original = "hand-authored content, unreadable\n"
    out_path.write_text(original)
    out_path.chmod(0o000)
    try:
        rc, out, err = _run(
            capsys, [str(tpl), "-o", str(out_path), "--guard-sentinel", "TOKEN", "NAME=x"]
        )
        assert rc == 3
        assert "refusing to overwrite" in err
        assert "cannot read existing file to verify guard sentinel" in err
    finally:
        out_path.chmod(0o644)
    assert out_path.read_text() == original
    residue = list(tmp_path.glob("out15.txt.render-template.tmp.*"))
    assert residue == []


def test_in_place_batch_renders_every_file_one_call(tmp_path, capsys):
    """--in-place renders N files in a single main() invocation (one spawn at the caller)."""
    f1 = tmp_path / "a.tpl"
    f2 = tmp_path / "b.tpl"
    f1.write_text("Hello {{NAME}}\n")
    f2.write_text("Bye {{NAME}}\n")
    rc, out, err = _run(capsys, ["--in-place", str(f1), str(f2), "NAME=Donal"])
    assert rc == 0
    assert err == ""
    assert f1.read_text() == "Hello Donal\n"
    assert f2.read_text() == "Bye Donal\n"


def test_in_place_batch_atomic_write_no_tmp_residue(tmp_path, capsys):
    f1 = tmp_path / "a.tpl"
    f1.write_text("X={{X}}")
    rc, out, err = _run(capsys, ["--in-place", str(f1), "X=5"])
    assert rc == 0
    assert f1.read_text() == "X=5"
    assert list(tmp_path.glob("a.tpl.render-template.tmp.*")) == []


def test_in_place_batch_per_file_failure_attribution(tmp_path, capsys):
    """One bad file among several fails loud with ITS OWN path, and does not skip the rest."""
    good1 = tmp_path / "good1.tpl"
    bad = tmp_path / "bad.tpl"
    good2 = tmp_path / "good2.tpl"
    good1.write_text("{{NAME}}")
    bad.write_text("{{MISSING}}")
    good2.write_text("{{NAME}}")

    rc, out, err = _run(capsys, ["--in-place", str(good1), str(bad), str(good2), "NAME=x"])

    assert rc == 1
    assert good1.read_text() == "x"
    assert good2.read_text() == "x"
    assert bad.read_text() == "{{MISSING}}"  # untouched -- the failing render never wrote
    assert err.strip() == f"render-template: {bad}: unsubstituted keys: MISSING in {bad}"


def test_in_place_batch_worst_severity_wins(tmp_path, capsys):
    """rc 3 (guard-style severity) is never applicable here, but 1 beats 0 across the batch."""
    good = tmp_path / "good.tpl"
    bad = tmp_path / "bad.tpl"
    good.write_text("{{NAME}}")
    bad.write_text("{{MISSING}}")

    rc, out, err = _run(capsys, ["--in-place", str(bad), str(good), "NAME=x"])

    assert rc == 1
    assert good.read_text() == "x"


def test_in_place_requires_at_least_one_path(capsys):
    rc, out, err = _run(capsys, ["--in-place", "NAME=x"])
    assert rc == 1
    assert err.strip() == "render-template: --in-place requires at least one path"


def test_in_place_unexpected_flag_argument(tmp_path, capsys):
    f1 = tmp_path / "a.tpl"
    f1.write_text("{{NAME}}")
    rc, out, err = _run(capsys, ["--in-place", str(f1), "--bogus"])
    assert rc == 1
    assert err.strip() == "render-template: unexpected argument: --bogus"


def test_no_guard_legacy_path_still_clobbers(tmp_path, capsys):
    """Regression pin: default behaviour (no --guard-sentinel) is unchanged."""
    tpl = tmp_path / "t14.tpl"
    tpl.write_text("{{NAME}}")
    out_path = tmp_path / "out14.txt"
    out_path.write_text("hand-authored content, no guard requested\n")
    rc, out, err = _run(capsys, [str(tpl), "-o", str(out_path), "NAME=x"])
    assert rc == 0
    assert err == ""
    assert out_path.read_text() == "x"
