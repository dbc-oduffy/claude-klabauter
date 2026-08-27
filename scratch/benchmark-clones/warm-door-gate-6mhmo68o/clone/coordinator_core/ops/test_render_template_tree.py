"""Characterization + parity tests for coordinator_core.ops.render_template_tree.

Port of: render-template-tree.sh (DoE 290997c7, 2026-07-22).
Spec backlink: docs/plans/2026-06-22-new-project-bootstrap-skill.md § C2
"""
from __future__ import annotations

import os
import stat
import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops import render_template_tree
from coordinator_core.ops.render_template_tree import main


def _write_render_template_sh(bin_dir: Path) -> Path:
    """A minimal, faithful-enough stand-in for render-template.py used across tests.

    Implements the same CLI contract (<path> -o <path> KEY=VALUE...) and the same
    fail-loud-on-unsubstituted-token behavior as the real bash oracle, without
    depending on the DoE clone being present on the test machine.
    """
    script = bin_dir / "render-template.py"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import re
            import sys


            def render_one(path, kv_pairs):
                with open(path, "r") as fh:
                    content = fh.read()
                for key, value in kv_pairs:
                    content = content.replace("{{" + key + "}}", value)
                tokens = sorted(set(re.findall(r"\\{\\{([^}]+)\\}\\}", content)))
                if tokens:
                    return None, "unsubstituted keys: " + ",".join(tokens) + " in " + path
                return content, None


            args = sys.argv[1:]

            if args and args[0] == "--in-place":
                rest = args[1:]
                paths = []
                i = 0
                while i < len(rest) and "=" not in rest[i] and not rest[i].startswith("-"):
                    paths.append(rest[i])
                    i += 1
                kv_pairs = [tuple(a.split("=", 1)) for a in rest[i:]]
                worst_rc = 0
                for path in paths:
                    content, err = render_one(path, kv_pairs)
                    if err is not None:
                        sys.stderr.write("render-template: " + path + ": " + err + "\\n")
                        worst_rc = max(worst_rc, 1)
                        continue
                    with open(path, "w") as fh:
                        fh.write(content)
                sys.exit(worst_rc)

            template_path = args[0]
            rest = args[1:]
            output_path = None
            if rest and rest[0] == "-o":
                output_path = rest[1]
                rest = rest[2:]

            kv_pairs = [tuple(a.split("=", 1)) for a in rest]
            content, err = render_one(template_path, kv_pairs)
            if err is not None:
                sys.stderr.write("render-template: " + err + "\\n")
                sys.exit(1)

            if output_path:
                with open(output_path, "w") as fh:
                    fh.write(content)
            else:
                sys.stdout.write(content)
            sys.exit(0)
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture()
def doe_root(tmp_path: Path) -> Path:
    root = tmp_path / "doe-clone"
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    _write_render_template_sh(bin_dir)
    return root


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)


def _make_happy_src(tmp_path: Path) -> Path:
    src = tmp_path / "src1"
    (src / "sub" / "deep").mkdir(parents=True)
    (src / "pkg.json").write_text('{ "name": "{{PROJECT_NAME}}", "version": "1.0.0" }\n')
    (src / "readme.txt").write_text("This file has no template tokens.\n")
    (src / ".gitignore").write_text("node_modules/\n")
    (src / "sub" / "deep" / "config.txt").write_text("project={{PROJECT_NAME}}\n")
    return src


def test_happy_path_renders_and_copies(tmp_path, monkeypatch, doe_root):
    # Force the DoE-root fallback rung: co-located resolution now wins
    # unconditionally, so this test's fixture-authored render-template.py
    # (staged under doe_root) would otherwise never run.
    monkeypatch.setattr(render_template_tree, "_co_located_render_single", lambda: None)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    src = _make_happy_src(tmp_path)
    dst = tmp_path / "dst1"

    rc = main([str(src), str(dst), "PROJECT_NAME=testproj"])

    assert rc == 0
    assert (dst / "pkg.json").read_text() == '{ "name": "testproj", "version": "1.0.0" }\n'
    assert "{{" not in (dst / "pkg.json").read_text()
    assert (dst / "readme.txt").read_text() == "This file has no template tokens.\n"
    assert (dst / ".gitignore").is_file()
    assert (dst / "sub" / "deep" / "config.txt").read_text() == "project=testproj\n"


def test_multiple_token_files_render_in_one_spawn(tmp_path, monkeypatch, doe_root):
    """The whole token-bearing set is delegated to a single subprocess.run call.

    Verifies the amplification-gate fix: N token-bearing files no longer cost
    N spawns of render-template.py.
    """
    monkeypatch.setattr(render_template_tree, "_co_located_render_single", lambda: None)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    src = tmp_path / "src-multi"
    (src / "sub").mkdir(parents=True)
    for i in range(5):
        (src / f"f{i}.txt").write_text("v={{V}}\n")
    (src / "sub" / "g.txt").write_text("v={{V}}\n")
    (src / "plain.txt").write_text("no tokens here\n")
    dst = tmp_path / "dst-multi"

    calls = []
    real_run = render_template_tree.subprocess.run

    def _counting_run(argv, **kwargs):
        calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(render_template_tree.subprocess, "run", _counting_run)

    rc = main([str(src), str(dst), "V=hi"])

    assert rc == 0
    assert len(calls) == 1
    assert "--in-place" in calls[0]
    for i in range(5):
        assert (dst / f"f{i}.txt").read_text() == "v=hi\n"
    assert (dst / "sub" / "g.txt").read_text() == "v=hi\n"
    assert (dst / "plain.txt").read_text() == "no tokens here\n"


def test_no_token_bearing_files_skips_spawn(tmp_path, monkeypatch, doe_root):
    """No {{ }} anywhere in the tree -- zero spawns, not one wasted call."""
    monkeypatch.setattr(render_template_tree, "_co_located_render_single", lambda: None)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    src = tmp_path / "src-notoken"
    src.mkdir()
    (src / "plain.txt").write_text("no tokens here\n")
    dst = tmp_path / "dst-notoken"

    calls = []
    real_run = render_template_tree.subprocess.run

    def _counting_run(argv, **kwargs):
        calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(render_template_tree.subprocess, "run", _counting_run)

    rc = main([str(src), str(dst)])

    assert rc == 0
    assert calls == []


def test_one_bad_file_does_not_block_the_rest(tmp_path, monkeypatch, doe_root):
    """A single failing file in the batch still fails the tree-walk (matches the old
    short-circuit's observable rc), but the render-template.py --in-place layer it
    delegates to renders every OTHER file rather than stopping at the first failure.
    """
    monkeypatch.setattr(render_template_tree, "_co_located_render_single", lambda: None)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    src = tmp_path / "src-onebad"
    src.mkdir()
    (src / "good.txt").write_text("v={{V}}\n")
    (src / "bad.txt").write_text("v={{MISSING}}\n")
    dst = tmp_path / "dst-onebad"

    rc = main([str(src), str(dst), "V=hi"])

    assert rc != 0
    assert (dst / "good.txt").read_text() == "v=hi\n"
    assert "{{" in (dst / "bad.txt").read_text()


def test_dst_may_be_pre_existing_empty_dir(tmp_path, monkeypatch, doe_root):
    # Force the DoE-root fallback rung — see test_happy_path_renders_and_copies.
    monkeypatch.setattr(render_template_tree, "_co_located_render_single", lambda: None)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    src = _make_happy_src(tmp_path)
    dst = tmp_path / "dst-empty"
    dst.mkdir()

    rc = main([str(src), str(dst), "PROJECT_NAME=testproj"])

    assert rc == 0
    assert (dst / "readme.txt").is_file()


# ---------------------------------------------------------------------------
# Negative corpus
# ---------------------------------------------------------------------------


def test_usage_error_on_too_few_args(monkeypatch, doe_root):
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    rc = main(["only-one-arg"])
    assert rc == 1


def test_missing_src_dir_fails(tmp_path, monkeypatch, doe_root):
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    rc = main([str(tmp_path / "nope"), str(tmp_path / "dst")])
    assert rc == 1


def test_non_empty_dst_dir_fails(tmp_path, monkeypatch, doe_root):
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    src = tmp_path / "src3"
    src.mkdir()
    (src / "a.txt").write_text("hi\n")
    dst = tmp_path / "dst3"
    dst.mkdir()
    (dst / "existing.txt").write_text("occupied\n")

    rc = main([str(src), str(dst)])

    assert rc == 1


def test_unsubstituted_token_fails_loud(tmp_path, monkeypatch, doe_root, capsys):
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(doe_root))
    src = tmp_path / "src4"
    src.mkdir()
    (src / "broken.txt").write_text("hello={{UNDEFINED}}\n")
    dst = tmp_path / "dst4"

    rc = main([str(src), str(dst)])

    assert rc == 1


def test_missing_render_template_sh_fails(tmp_path, monkeypatch):
    # Force the DoE-root fallback rung so the empty DoE clone (no
    # render-template.py sibling) is actually consulted, rather than the
    # real co-located script this repo ships winning unconditionally.
    monkeypatch.setattr(render_template_tree, "_co_located_render_single", lambda: None)
    empty_root = tmp_path / "empty-doe"
    (empty_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(empty_root))
    src = tmp_path / "src5"
    src.mkdir()
    (src / "a.txt").write_text("hi\n")

    rc = main([str(src), str(tmp_path / "dst5")])

    assert rc == 1


def test_doe_root_unresolvable_fails(tmp_path, monkeypatch):
    # Force the DoE-root fallback rung so DoE-root unresolvability is
    # actually reached, rather than short-circuited by the real
    # co-located script this repo ships.
    monkeypatch.setattr(render_template_tree, "_co_located_render_single", lambda: None)
    # No REPO_DOE_CLAUDE, no machine-local on PATH.
    monkeypatch.setenv("PATH", "")
    src = tmp_path / "src6"
    src.mkdir()
    (src / "a.txt").write_text("hi\n")

    rc = main([str(src), str(tmp_path / "dst6")])

    assert rc == 1
