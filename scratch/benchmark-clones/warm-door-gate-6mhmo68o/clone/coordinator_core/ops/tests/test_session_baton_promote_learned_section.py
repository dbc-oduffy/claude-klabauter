"""
coordinator_core.ops.tests.test_session_baton_promote_learned_section — the
"## What I Learned" section: the coupling between coordinator-doc-new's
scaffolded placeholder and session_baton_promote's fill logic (AC2), plus
promote's fill/no-fill behaviour (AC3/AC4).

Spec backlink: docs/plans/2026-08-20-the-handoff-carries-the-jottings.md.

AC2 is the load-bearing criterion here: promote's documented behaviour on a
placeholder mismatch is a SILENT no-op (see session_baton_promote.py's own
comments on _ACCOMPLISHED_PLACEHOLDER/_LEARNED_PLACEHOLDER) — nothing at
runtime ever surfaces a drift between the string the scaffolder emits and
the constant promote searches for. A test that hardcodes the same literal
in two places would pass even as both copies drift away from the source, so
this module reads the placeholder the scaffolder ACTUALLY emits out of
coordinator-doc-new.py's own source text, rather than retyping it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from coordinator_core.ops import session_baton_promote as promote_mod
from coordinator_core.session_baton import store

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SCAFFOLDER_PATH = (
    Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "coordinator-doc-new.py"
)

# Matches the three emitted lines for the "## What I Learned" section inside
# _scaffold_handoff's `lines.extend([...])` call: the heading, a blank
# string, and the placeholder HTML comment. Read straight out of the
# scaffolder's source text — not a hand-typed copy of the literal — so a
# rename upstream fails THIS test instead of degrading silently at runtime.
_LEARNED_SECTION_RE = re.compile(
    r'"## What I Learned",\s*"",\s*"(?P<placeholder><!--.*?-->)",'
)


def _extract_scaffolder_learned_placeholder() -> str:
    text = _SCAFFOLDER_PATH.read_text(encoding="utf-8")
    match = _LEARNED_SECTION_RE.search(text)
    assert match, (
        "coordinator-doc-new.py's _scaffold_handoff no longer emits the "
        "'## What I Learned' section in the expected shape (heading, blank "
        "line, single-line placeholder comment) - update this regex or the "
        "scaffolder"
    )
    return match.group("placeholder")


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _ensure_session_dir(repo: Path, sid: str) -> Path:
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def _promote(**params):
    return promote_mod._handler(dict(params))


def _fake_scaffold(dest_rel: str, learned_placeholder: str):
    """Same fake-scaffold pattern as test_session_baton_promote.py, extended
    with a "## What I Learned" section carrying the placeholder read off
    the real scaffolder source (never a hardcoded second copy)."""

    def _fake(title, branch, cwd, category=None, summary=None, gated_predicate=None):
        target = Path(cwd) / dest_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\n"
            f"title: \"{title}\"\n"
            "kind: session-handoff\n"
            "---\n\n"
            "## What Was Accomplished\n\n"
            "<!-- Replace with what was built, fixed, or shipped this session. -->\n\n"
            "## Current State\n\n"
            "<!-- Replace with where things stand now. -->\n\n"
            "## Next Steps\n\n"
            "<!-- Replace with what the next session should do first. -->\n\n"
            "## What I Learned\n\n"
            f"{learned_placeholder}\n",
            encoding="utf-8",
        )
        return dest_rel

    return _fake


# ---------------------------------------------------------------------------
# AC2 — the coupling itself
# ---------------------------------------------------------------------------


def test_scaffolder_placeholder_matches_promote_constant():
    scaffolder_placeholder = _extract_scaffolder_learned_placeholder()
    assert scaffolder_placeholder == promote_mod._LEARNED_PLACEHOLDER


# ---------------------------------------------------------------------------
# AC3 — intent present -> filled
# ---------------------------------------------------------------------------


def test_promote_with_intent_fills_learned_section(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-learned")
    store.merge_baton("sid-learned", cwd=str(repo), intent="watch out for the mutex")

    placeholder = _extract_scaffolder_learned_placeholder()
    monkeypatch.setattr(
        promote_mod,
        "_scaffold_via_doc_new",
        _fake_scaffold("state/handoffs/2026-08-20-learned.md", placeholder),
    )

    result = _promote(session_id="sid-learned", cwd=str(repo))
    assert result["exit_code"] == 0

    handoff_text = (repo / "state/handoffs/2026-08-20-learned.md").read_text(
        encoding="utf-8"
    )
    assert "watch out for the mutex" in handoff_text
    assert placeholder not in handoff_text
    assert "## What I Learned" in handoff_text


# ---------------------------------------------------------------------------
# AC4 — no intent -> placeholder survives, section still present
# ---------------------------------------------------------------------------


def test_promote_without_intent_leaves_learned_placeholder(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-nolearned")

    placeholder = _extract_scaffolder_learned_placeholder()
    monkeypatch.setattr(
        promote_mod,
        "_scaffold_via_doc_new",
        _fake_scaffold("state/handoffs/2026-08-20-nolearned.md", placeholder),
    )

    result = _promote(session_id="sid-nolearned", cwd=str(repo))
    assert result["exit_code"] == 0

    handoff_text = (repo / "state/handoffs/2026-08-20-nolearned.md").read_text(
        encoding="utf-8"
    )
    assert placeholder in handoff_text
    assert "## What I Learned" in handoff_text


def test_promote_with_whitespace_intent_leaves_learned_placeholder(tmp_path, monkeypatch):
    """AC4, whitespace arm (slice-A review finding 2, integrated 2026-08-20).

    A whitespace-only ``intent`` is truthy, so a bare ``if not intent`` guard admits it
    and then ``.strip()`` replaces the placeholder with an empty string — leaving the
    "## What I Learned" heading standing over silence. That defeats the very AC it
    appears to satisfy: AC4 exists so the unfilled PROMPT survives as the nudge.
    """
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-wslearned")

    placeholder = _extract_scaffolder_learned_placeholder()
    store.merge_baton("sid-wslearned", cwd=str(repo), intent="   \n\t  ")
    monkeypatch.setattr(
        promote_mod,
        "_scaffold_via_doc_new",
        _fake_scaffold("state/handoffs/2026-08-20-wslearned.md", placeholder),
    )

    result = _promote(session_id="sid-wslearned", cwd=str(repo))
    assert result["exit_code"] == 0

    handoff_text = (repo / "state/handoffs/2026-08-20-wslearned.md").read_text(
        encoding="utf-8"
    )
    assert placeholder in handoff_text, (
        "a whitespace-only intent must leave the placeholder intact, not strip it to empty"
    )
    assert "## What I Learned" in handoff_text
