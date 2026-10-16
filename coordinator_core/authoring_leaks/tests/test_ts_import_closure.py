"""Unit tests for the ts_import_closure detector against real throwaway git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.authoring_leaks import HeadView, leak_gate, ts_import_closure
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TSCONFIG = '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}\n'


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "tsconfig.json", TSCONFIG)
    _write(tmp_path, "src/app.tsx", "export const a = 1;\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _gate(root, paths):
    return leak_gate(root, paths, detectors=[ts_import_closure])


def _alias_importer(root):
    _write(root, "src/app.tsx", 'import { X } from "@/components/x";\n')


def test_alias_import_of_untracked_file_is_one_finding(repo):
    _alias_importer(repo)
    _write(repo, "src/components/x.tsx", "export const X = 1;\n")
    out = _gate(repo, ["src/app.tsx"])
    assert not out.passed
    assert "src/app.tsx:1: ts_import_closure" in out.diagnostics[0]
    assert "src/components/x.tsx is uncommitted in the worktree" in out.diagnostics[0]


def test_alias_import_of_tracked_file_passes(repo):
    _write(repo, "src/components/x.tsx", "export const X = 1;\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    _alias_importer(repo)
    assert _gate(repo, ["src/app.tsx"]).passed


def test_target_co_landing_in_gate_passes(repo):
    _alias_importer(repo)
    _write(repo, "src/components/x.tsx", "export const X = 1;\n")
    assert _gate(repo, ["src/app.tsx", "src/components/x.tsx"]).passed


def test_relative_import_of_untracked_file_is_a_finding(repo):
    _write(repo, "src/app.tsx", 'import x from "./x";\n')
    _write(repo, "src/x.ts", "export default 1;\n")
    out = _gate(repo, ["src/app.tsx"])
    assert not out.passed
    assert "src/x.ts is uncommitted" in out.diagnostics[0]


def test_js_specifier_resolves_to_ts_source(repo):
    _write(repo, "src/x.ts", "export const x = 1;\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    _write(repo, "src/app.tsx", 'import { x } from "./x.js";\n')
    assert _gate(repo, ["src/app.tsx"]).passed


def test_index_file_resolves(repo):
    _write(repo, "src/lib/index.ts", "export const l = 1;\n")
    _write(repo, "src/app.tsx", 'import { l } from "./lib";\n')
    assert not _gate(repo, ["src/app.tsx"]).passed
    assert _gate(repo, ["src/app.tsx", "src/lib/index.ts"]).passed


def test_gate_deleting_a_head_target_is_a_finding(repo):
    _write(repo, "src/x.ts", "export const x = 1;\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    _write(repo, "src/app.tsx", 'import { x } from "./x";\n')
    (repo / "src/x.ts").unlink()
    out = _gate(repo, ["src/app.tsx", "src/x.ts"])
    assert not out.passed
    assert "src/x.ts is deleted by this commit" in out.diagnostics[0]


def test_bare_package_specifier_is_external(repo):
    _write(repo, "src/app.tsx", 'import React from "react";\nconst fs = require("node:fs");\n')
    assert _gate(repo, ["src/app.tsx"]).passed


def test_wholly_absent_target_is_not_a_finding(repo):
    _write(repo, "src/app.tsx", 'import "./generated";\n')
    assert _gate(repo, ["src/app.tsx"]).passed


def test_tsconfig_with_comments_and_trailing_comma_parses(repo):
    _write(
        repo, "tsconfig.json",
        '{\n  // note\n  "compilerOptions": {\n    /* block */\n'
        '    "paths": {"@/*": ["./src/*"],},\n  },\n}\n',
    )
    _alias_importer(repo)
    _write(repo, "src/components/x.tsx", "export const X = 1;\n")
    assert not _gate(repo, ["src/app.tsx"]).passed


def test_unparsable_tsconfig_means_no_aliases(repo):
    _write(repo, "tsconfig.json", "{ not json")
    _alias_importer(repo)
    _write(repo, "src/components/x.tsx", "export const X = 1;\n")
    assert _gate(repo, ["src/app.tsx"]).passed


def test_extends_is_not_followed_so_inherited_aliases_resolve_nothing(repo):
    _write(repo, "tsconfig.json", '{"extends": "./base.json"}\n')
    _write(repo, "base.json", TSCONFIG)
    _alias_importer(repo)
    _write(repo, "src/components/x.tsx", "export const X = 1;\n")
    assert _gate(repo, ["src/app.tsx"]).passed


def test_base_url_is_honoured(repo):
    _write(repo, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"~/*": ["*"]}}}\n')
    _write(repo, "src/app.tsx", 'import y from "~/y";\n')
    _write(repo, "src/y.ts", "export default 1;\n")
    assert not _gate(repo, ["src/app.tsx"]).passed


def test_nested_tsconfig_wins(repo):
    _write(repo, "pkg/tsconfig.json", '{"compilerOptions": {"paths": {"@/*": ["./lib/*"]}}}\n')
    _write(repo, "pkg/app.ts", 'import z from "@/z";\n')
    _write(repo, "pkg/lib/z.ts", "export default 1;\n")
    _write(repo, "src/z.ts", "export default 1;\n")
    _git(repo, "add", "src/z.ts")
    _git(repo, "commit", "-q", "-m", "root z")
    out = _gate(repo, ["pkg/app.ts"])
    assert not out.passed
    assert "pkg/lib/z.ts is uncommitted" in out.diagnostics[0]


def test_multi_line_import_matches_on_from_line(repo):
    _write(repo, "src/app.tsx", 'import {\n  a,\n  b,\n} from "./m";\n')
    _write(repo, "src/m.ts", "export const a = 1, b = 2;\n")
    out = _gate(repo, ["src/app.tsx"])
    assert "src/app.tsx:4:" in out.diagnostics[0]


def test_unchanged_committed_file_is_never_parsed(repo, monkeypatch):
    _write(repo, "src/app.tsx", 'import { X } from "@/components/x";\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "importer")
    _write(repo, "src/app.tsx", 'import { X } from "@/components/x";\nconst y = 2;\n')
    called = []
    monkeypatch.setattr(ts_import_closure, "_bases", lambda *a: called.append(a) or [])
    head = HeadView(repo, ["src/app.tsx"])
    assert ts_import_closure.detect(repo, ["src/app.tsx"], head) == []
    assert called == []


def test_candidates_are_ts_js_paths_only(repo):
    got = list(ts_import_closure.candidates(["a.ts", "b.py", "c.cjs", "d.md", "e.tsx"]))
    assert got == ["a.ts", "c.cjs", "e.tsx"]


def test_not_wired_by_default():
    from coordinator_core.authoring_leaks import WIRED_DETECTORS

    assert "ts_import_closure" not in WIRED_DETECTORS
