"""Pins agent_sidecar: dir derivation, clause content, and Python/JS slug parity."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import agent_sidecar as a


def test_sidecar_dir_is_repo_relative_under_root():
    d = a.sidecar_dir("emit", "2026-10-17-plan", "C1")
    assert d == ".coordinator-local/workflow-sidecars/emit/2026-10-17-plan/C1/"
    assert "\\" not in d and not d.startswith("/")


def test_slug_replaces_unsafe_chars():
    assert a.sidecar_dir("ask", "run key", "work:C1") == (
        ".coordinator-local/workflow-sidecars/ask/run-key/work-C1/"
    )


@pytest.mark.parametrize(
    "args",
    [
        ("emit", "run", ""),
        ("emit", "", "C1"),
        ("", "run", "C1"),
        ("emit", "run", "C1."),
        ("emit", "run", "."),
        ("emit", "run", ".."),
        ("emit", "run", "CON"),
        ("emit", "run", "nul.md"),
        ("emit", "com1", "C1"),
    ],
)
def test_sidecar_dir_refuses_unsafe_segments(args):
    with pytest.raises(ValueError):
        a.sidecar_dir(*args)


def test_clause_content_and_marker_is_last_line():
    d = a.sidecar_dir("emit", "p", "C1")
    c = a.sidecar_clause(d)
    assert c.startswith("\n\n")
    assert c.splitlines()[-1] == f"sidecar_dir: {d}"
    assert c.count(a.SIDECAR_DIR_MARKER_PREFIX) == 1
    for needle in ("Subagents should return findings as text", "Bash heredoc", "never skip it",
                   "guard's denial stays a stop", "mkdir -p", "sidecar_path:",
                   "only for a path the Write tool refused", "never overwrite it by heredoc"):
        assert needle in c


def test_clause_stays_under_byte_budget():
    assert len(a.sidecar_clause(a.sidecar_dir("emit", "p", "C1")).encode()) < 900


def test_js_slug_uses_same_charset_literal():
    js = a.sidecar_clause_js("ask", "run1", "id")
    assert f"[^{a._SLUG_CHARSET}]" in js
    assert a._SLUG_BAD_RE.pattern == f"[^{a._SLUG_CHARSET}]"


def test_js_clause_shares_head_and_dir_prefix():
    js = a.sidecar_clause_js("ask", "run 1", "id")
    static = a.sidecar_clause(a.sidecar_dir("ask", "run 1", "zz"))
    head = static[: static.index("zz/")]
    import json

    assert json.dumps(head) in js
    assert js.endswith("+ '/')")


def test_js_clause_validates_static_segments():
    with pytest.raises(ValueError):
        a.sidecar_clause_js("CON", "run", "id")
