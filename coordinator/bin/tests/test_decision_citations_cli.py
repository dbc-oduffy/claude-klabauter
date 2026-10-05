"""Fixture tests for `coordinator/bin/check-decision-citations.py` on a tmp_path git corpus.

Fixture ids are built at run time so this file carries no dangling DR literal. Sibling roots are
injected, never read from the live machine-local registry.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN = Path(__file__).resolve().parents[1] / "check-decision-citations.py"
_spec = importlib.util.spec_from_file_location("check_decision_citations", _BIN)
cdc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdc)  # type: ignore[union-attr]


def _id(digit: str) -> str:
    return "DR-" + digit * 3


def _sc(digit: str) -> str:
    return "SC-DR-" + digit * 3


LOCAL_FILE = _id("1")
LOCAL_FM = _id("2")
SIBLING = _id("3")
SC_WIKI = _sc("4")
FENCED = _id("5")
ARCHIVED = _id("6")
BASELINED = _id("7")
NEW = _id("8")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, f"docs/decisions/{LOCAL_FILE}-slug.md", "# a\n")
    _write(root, "docs/decisions/2026-01-01-dated.md", f'---\nid: "{LOCAL_FM}"\n---\nbody\n')
    _write(root, cdc.SC_WIKI_REL, f"# wiki\n{SC_WIKI}\n")
    _write(
        root,
        "docs/notes.md",
        f"cites {LOCAL_FILE} {LOCAL_FM} {SIBLING} {SC_WIKI} {_id('0')} {_id('9')}\n"
        f"```\n{FENCED}\n```\n",
    )
    _write(root, "archive/old.md", f"{ARCHIVED}\n")
    _write(root, "docs/baselined.md", f"{BASELINED}\n")
    _write(root, "docs/namespaced.md", "DR-PV1-123 and DR-INSTALL-123\n")
    return root


def _add(root: Path) -> None:
    _git(root, "add", "-A")


SIBLINGS = lambda: {"sib": {SIBLING}}  # noqa: E731


def _seed_baseline(root: Path) -> None:
    _add(root)
    assert cdc.emit_baseline(root, SIBLINGS()) == 0


def test_resolution_classes_and_non_live_tokens(corpus):
    _add(corpus)
    cited = cdc.live_tokens(corpus)
    assert FENCED not in cited
    assert ARCHIVED not in cited
    assert _id("0") not in cited and _id("9") not in cited
    assert not any(i.startswith("DR-PV1") or "INSTALL" in i for i in cited)
    dangling, bare = cdc.classify(corpus, cited, SIBLINGS())
    assert dangling == [BASELINED]
    assert bare == {SIBLING: ["sib"]}


def test_absent_sibling_is_skipped_not_fatal(corpus, monkeypatch):
    monkeypatch.setattr(cdc, "_registry_repo_paths", lambda: {"gone": corpus / "no-such-repo"})
    assert cdc.sibling_decision_ids(corpus) == {}


def test_absent_baseline_exits_zero(corpus, capsys):
    _add(corpus)
    assert cdc.check(corpus, SIBLINGS()) == 0
    assert "no baseline" in capsys.readouterr().err


def test_baselined_dangling_does_not_fire(corpus, capsys):
    _seed_baseline(corpus)
    assert cdc.check(corpus, SIBLINGS()) == 0
    assert "decision citations: clean" in capsys.readouterr().out


def test_new_dangling_id_fires(corpus, capsys):
    _seed_baseline(corpus)
    _write(corpus, "docs/fresh.md", f"{NEW}\n")
    _add(corpus)
    assert cdc.check(corpus, SIBLINGS()) == 1
    out = capsys.readouterr().out
    assert NEW in out and "docs/fresh.md" in out and BASELINED not in out


def test_emit_is_idempotent_and_header_names_keys_only(corpus):
    _seed_baseline(corpus)
    path = corpus / cdc.BASELINE_REL
    first = path.read_bytes()
    assert cdc.emit_baseline(corpus, SIBLINGS()) == 0
    assert path.read_bytes() == first
    text = first.decode()
    assert "Resolved sibling registry keys: sib" in text
    assert str(corpus) not in text


def test_emit_refuses_when_a_named_sibling_key_is_lost(corpus, capsys):
    _seed_baseline(corpus)
    path = corpus / cdc.BASELINE_REL
    before = path.read_bytes()
    assert cdc.emit_baseline(corpus, {}) == 2
    assert path.read_bytes() == before


def test_git_grep_failure_exits_two(tmp_path, capsys):
    assert cdc.main(["--root", str(tmp_path)]) == 0  # no baseline: skipped before any grep
    (tmp_path / cdc.BASELINE_REL).parent.mkdir(parents=True)
    (tmp_path / cdc.BASELINE_REL).write_text("## Dangling\n", encoding="utf-8")
    assert cdc.main(["--root", str(tmp_path)]) == 2
    assert "could not check" in capsys.readouterr().err


def test_repo_root_is_the_callers_repo_not_the_script_location(corpus, tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SUBJECT_REPO_ROOT", raising=False)
    monkeypatch.chdir(corpus)
    assert cdc.repo_root() == corpus.resolve()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("COORDINATOR_SUBJECT_REPO_ROOT", str(corpus))
    assert cdc.repo_root() == corpus.resolve()
