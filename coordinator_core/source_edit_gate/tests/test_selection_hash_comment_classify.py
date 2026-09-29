"""Tests for `selection.classify_edit`'s hash-comment-family rule (`.ps1`,
`.sh`, `.toml`, `.yaml`, etc.) -- see `_classify_hash_comment_edit`.

Spec backlink: docs/plans/2026-09-27-source-edit-test-guardrail.md.
"""

from __future__ import annotations

from coordinator_core.source_edit_gate.selection import classify_edit


def test_toml_comment_only_edit_classifies_comment_only():
    original = "# old note\nkey = 1\n"
    edited = "# new, better note\nkey = 1\n"
    assert classify_edit(original, edited, "pyproject.toml") == "comment-only"


def test_yaml_comment_edit_classifies_comment_only():
    original = "# purpose\nfoo: 1\n"
    edited = "# purpose (updated)\nfoo: 1\n"
    assert classify_edit(original, edited, "config.yaml") == "comment-only"


def test_yaml_value_change_classifies_code():
    original = "# purpose\nfoo: 1\n"
    edited = "# purpose\nfoo: 2\n"
    assert classify_edit(original, edited, "config.yml") == "code"


def test_sh_comment_only_edit_classifies_comment_only():
    original = "# does the thing\necho hi\n"
    edited = "# does the thing (rewritten)\necho hi\n"
    assert classify_edit(original, edited, "scripts/run.sh") == "comment-only"


def test_bash_code_edit_classifies_code():
    original = "# does the thing\necho hi\n"
    edited = "# does the thing\necho bye\n"
    assert classify_edit(original, edited, "scripts/run.bash") == "code"


def test_ps1_comment_only_edit_classifies_comment_only():
    original = "# note\nWrite-Host 'hi'\n"
    edited = "# note updated\nWrite-Host 'hi'\n"
    assert classify_edit(original, edited, "tools/run.ps1") == "comment-only"


def test_psm1_code_edit_classifies_code():
    original = "# note\nWrite-Host 'hi'\n"
    edited = "# note\nWrite-Host 'bye'\n"
    assert classify_edit(original, edited, "tools/module.psm1") == "code"


def test_shebang_line_edit_classifies_code():
    original = "#!/bin/bash\necho hi\n"
    edited = "#!/usr/bin/env bash\necho hi\n"
    assert classify_edit(original, edited, "scripts/run.sh") == "code"


def test_coding_cookie_edit_classifies_code():
    original = "# -*- coding: utf-8 -*-\nfoo: 1\n"
    edited = "# -*- coding: latin-1 -*-\nfoo: 1\n"
    assert classify_edit(original, edited, "config.yaml") == "code"


def test_powershell_requires_directive_classifies_code():
    original = "#Requires -Version 5.1\nWrite-Host 'hi'\n"
    edited = "#Requires -Version 7.0\nWrite-Host 'hi'\n"
    assert classify_edit(original, edited, "tools/run.ps1") == "code"


def test_shellcheck_directive_classifies_code():
    original = "# shellcheck disable=SC2086\necho $x\n"
    edited = "# shellcheck disable=SC2086,SC2046\necho $x\n"
    assert classify_edit(original, edited, "scripts/run.sh") == "code"


def test_yaml_language_server_directive_classifies_code():
    original = "# yaml-language-server: $schema=old.json\nfoo: 1\n"
    edited = "# yaml-language-server: $schema=new.json\nfoo: 1\n"
    assert classify_edit(original, edited, "config.yaml") == "code"


def test_ps1_block_comment_edit_classifies_code():
    original = "<#\nold description\n#>\nWrite-Host 'hi'\n"
    edited = "<#\nnew description that reads like code = 1\n#>\nWrite-Host 'hi'\n"
    assert classify_edit(original, edited, "tools/run.ps1") == "code"


def test_ps1_herestring_body_edit_classifies_code():
    original = "$x = @'\nold body\n'@\nWrite-Host $x\n"
    edited = "$x = @'\nnew body\n'@\nWrite-Host $x\n"
    assert classify_edit(original, edited, "tools/run.ps1") == "code"


def test_bash_heredoc_body_edit_classifies_code():
    original = "cat <<EOF\nold body\nEOF\necho done\n"
    edited = "cat <<EOF\nnew body\nEOF\necho done\n"
    assert classify_edit(original, edited, "scripts/run.sh") == "code"


def test_dockerfile_by_bare_name_comment_only_edit():
    original = "# base image\nFROM alpine\n"
    edited = "# base image (pinned)\nFROM alpine\n"
    assert classify_edit(original, edited, "docker/Dockerfile") == "comment-only"


def test_dockerfile_heredoc_body_edit_classifies_code():
    original = "RUN <<EOF\nold command\nEOF\n"
    edited = "RUN <<EOF\nnew command\nEOF\n"
    assert classify_edit(original, edited, "Dockerfile") == "code"


def test_cfg_and_ini_comment_only_edit():
    original = "# section note\n[core]\nfoo = 1\n"
    edited = "# section note (clarified)\n[core]\nfoo = 1\n"
    assert classify_edit(original, edited, "setup.cfg") == "comment-only"
    assert classify_edit(original, edited, "tox.ini") == "comment-only"


def test_r_comment_only_edit():
    original = "# helper\nx <- 1\n"
    edited = "# helper function\nx <- 1\n"
    assert classify_edit(original, edited, "scripts/analysis.r") == "comment-only"


def test_comment_inserted_above_type_ignore_is_comment_only():
    from coordinator_core.source_edit_gate.selection import classify_edit

    orig = "import os\nx = os.sep  # type: ignore[attr-defined]\n"
    edited = "import os\n# Separator for the host platform.\nx = os.sep  # type: ignore[attr-defined]\n"
    assert classify_edit(orig, edited, "m.py") == "comment-only"
