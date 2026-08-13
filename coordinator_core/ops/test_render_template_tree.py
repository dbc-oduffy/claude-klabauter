"""Characterization + parity tests for coordinator_core.ops.render_template_tree.

Port of: render-template-tree.sh (coordinator-claude 290997c7, 2026-07-22).
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
    depending on the coordinator-claude clone being present on the test machine.
    """
    script = bin_dir / "render-template.py"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys

            args = sys.argv[1:]
            template_path = args[0]
            rest = args[1:]
            output_path = None
            if rest and rest[0] == "-o":
                output_path = rest[1]
                rest = rest[2:]

            with open(template_path, "r") as fh:
                content = fh.read()

            for pair in rest:
                key, _, value = pair.partition("=")
                content = content.replace("{{" + key + "}}", value)

            if "{{" in content:
                import re
                tokens = sorted(set(re.findall(r"\\{\\{([^}]+)\\}\\}", content)))
                sys.stderr.write(
                    "render-template: unsubstituted keys: "
                    + ",".join(tokens)
                    + " in " + template_path + "\\n"
                )
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
    monkeypatch.delenv("REPO_EXAMPLE_DOCTRINE_REPO", raising=False)


def _make_happy_src(tmp_path: Path) -> Path:
    src = tmp_path / "src1"
    (src / "sub" / "deep").mkdir(parents=True)
    (src / "pkg.json").write_text('{ "name": "{{PROJECT_NAME}}", "version": "1.0.0" }\n')
    (src / "readme.txt").write_text("This file has no template tokens.\n")
    (src / ".gitignore").write_text("node_modules/\n")
    (src / "sub" / "deep" / "config.txt").write_text("project={{PROJECT_NAME}}\n")
    return src


def test_happy_path_renders_and_copies(tmp_path, monkeypatch, doe_root):
    # Force the coordinator-claude-root fallback rung: co-located resolution now wins
    # unconditionally, so this test's fixture-authored render-template.py
    # (staged under doe_root) would otherwise never run.
    monkeypatch.setattr(render_template_tree, "_co_located_render_single", lambda: None)
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(doe_root))
    src = _make_happy_src(tmp_path)
    dst = tmp_path / "dst1"

    rc = main([str(src), str(dst), "PROJECT_NAME=testproj"])

    assert rc == 0
    assert (dst / "pkg.json").read_text() == '{ "name": "testproj", "version": "1.0.0" }\n'
    assert "{{" not in (dst / "pkg.json").read_text()
    assert (dst / "readme.txt").read_text() == "This file has no template tokens.\n"
    assert (dst / ".gitignore").is_file()
    assert (dst / "sub" / "deep" / "config.txt").read_text() == "project=testproj\n"


def test_dst_may_be_pre_existing_empty_dir(tmp_path, monkeypatch, doe_root):
    # Force the coordinator-claude-root fallback rung — see test_happy_path_renders_and_copies.
    monkeypatch.setattr(render_template_tree, "_co_located_render_single", lambda: None)
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(doe_root))
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
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(doe_root))
    rc = main(["only-one-arg"])
    assert rc == 1


def test_missing_src_dir_fails(tmp_path, monkeypatch, doe_root):
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(doe_root))
    rc = main([str(tmp_path / "nope"), str(tmp_path / "dst")])
    assert rc == 1


def test_non_empty_dst_dir_fails(tmp_path, monkeypatch, doe_root):
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(doe_root))
    src = tmp_path / "src3"
    src.mkdir()
    (src / "a.txt").write_text("hi\n")
    dst = tmp_path / "dst3"
    dst.mkdir()
    (dst / "existing.txt").write_text("occupied\n")

    rc = main([str(src), str(dst)])

    assert rc == 1


def test_unsubstituted_token_fails_loud(tmp_path, monkeypatch, doe_root, capsys):
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(doe_root))
    src = tmp_path / "src4"
    src.mkdir()
    (src / "broken.txt").write_text("hello={{UNDEFINED}}\n")
    dst = tmp_path / "dst4"

    rc = main([str(src), str(dst)])

    assert rc == 1


def test_missing_render_template_sh_fails(tmp_path, monkeypatch):
    # Force the coordinator-claude-root fallback rung so the empty coordinator-claude clone (no
    # render-template.py sibling) is actually consulted, rather than the
    # real co-located script this repo ships winning unconditionally.
    monkeypatch.setattr(render_template_tree, "_co_located_render_single", lambda: None)
    empty_root = tmp_path / "empty-doe"
    (empty_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", str(empty_root))
    src = tmp_path / "src5"
    src.mkdir()
    (src / "a.txt").write_text("hi\n")

    rc = main([str(src), str(tmp_path / "dst5")])

    assert rc == 1


def test_doe_root_unresolvable_fails(tmp_path, monkeypatch):
    # Force the coordinator-claude-root fallback rung so coordinator-claude-root unresolvability is
    # actually reached, rather than short-circuited by the real
    # co-located script this repo ships.
    monkeypatch.setattr(render_template_tree, "_co_located_render_single", lambda: None)
    # No REPO_EXAMPLE_DOCTRINE_REPO, no machine-local on PATH.
    monkeypatch.setenv("PATH", "")
    src = tmp_path / "src6"
    src.mkdir()
    (src / "a.txt").write_text("hi\n")

    rc = main([str(src), str(tmp_path / "dst6")])

    assert rc == 1
