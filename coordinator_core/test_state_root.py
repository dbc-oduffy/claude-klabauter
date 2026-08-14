"""
Tests for coordinator_core.state_root — the 5-rule state-root seam resolver.

Port of: coordinator-state-root.sh (DoE 6fb5fb37, 2026-07-22) (de-bash W2)

The four sibling resolver ladders (doe_root, claude_klabauter_root, artifact classify,
is_meta_repo) are each covered by their own test modules; here we monkeypatch
them at the state_root module boundary so these tests exercise ONLY the 5-rule
dispatch logic — including the Rule 5 meta-repo -> claude-klabauter central-state redirect.

Spec backlinks:
  docs/plans/2026-07-03-stop-the-rot-claude-klabauter-state-home-placement.md § C2 / AC2
  docs/plans/2026-07-04-doe-authoring-repo-build-subject-matter-.md § W2.3
"""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core import state_root as sr
from coordinator_core.artifact_subject import Subject
from coordinator_core.meta_repo_identity import MetaRepoResolutionError

_DOE = "/repos/DoE-claude"
_CLAUDE_KLABAUTER = "/repos/claude-klabauter"
_SIBLING = "/repos/some-sibling"


def _state(root: str) -> str:
    return os.path.join(root, "state")


@pytest.fixture
def stub_peers(monkeypatch):
    """Default happy-path stubs for all four composed peers; each test overrides
    only what it needs.

    ``coordinator_claude_klabauter_root_with_class`` is stubbed to the
    RESOLUTION_LIVE_WORKING_TREE class by default — the overwhelming common
    case, and the class this module must remain byte-identical for.
    ``print_map`` now also routes through ``coordinator_claude_klabauter_root_with_class``
    (Gap 1, review-integrator 2026-08-12) rather than the class-less
    ``coordinator_claude_klabauter_root`` — the latter is stubbed too since it is
    still used elsewhere in this module (``_doe_state``/``_claude_klabauter_state``
    peers), even though ``print_map`` no longer calls it.
    """
    monkeypatch.setattr(sr, "coordinator_doe_root", lambda: _DOE)
    monkeypatch.setattr(sr, "coordinator_claude_klabauter_root", lambda: _CLAUDE_KLABAUTER)
    monkeypatch.setattr(
        sr,
        "coordinator_claude_klabauter_root_with_class",
        lambda: (_CLAUDE_KLABAUTER, "live-working-tree"),
    )
    monkeypatch.setattr(sr, "classify", lambda _p: Subject.DOCTRINE)
    monkeypatch.setattr(sr, "is_meta_repo", lambda _g: False)
    monkeypatch.setattr(sr, "_resolve_git_root", lambda: _SIBLING)
    monkeypatch.setattr(sr, "published_engine_mirror_path", lambda: None)
    return monkeypatch


# --- Rule 1: central + subject=doctrine -> DoE state -----------------------


def test_rule1_doctrine_routes_to_doe_state(stub_peers):
    assert sr.coordinator_state_root(central=True, subject="doctrine") == _state(_DOE)


def test_rule1_doctrine_fail_loud_no_claude_klabauter_fallback(stub_peers):
    stub_peers.setattr(sr, "coordinator_doe_root", lambda: None)
    with pytest.raises(sr.StateRootError) as exc:
        sr.coordinator_state_root(central=True, subject="doctrine")
    assert "DoE" in str(exc.value) or "doe_claude" in str(exc.value)
    # Must NOT silently fall through to claude-klabauter state.
    assert _CLAUDE_KLABAUTER not in str(exc.value)


# --- Rule 2: central + subject=engine -> claude-klabauter state ----------------------


def test_rule2_engine_routes_to_claude_klabauter_state(stub_peers):
    assert sr.coordinator_state_root(central=True, subject="engine") == _state(_CLAUDE_KLABAUTER)


def test_rule2_engine_fail_loud_when_claude_klabauter_unresolvable(stub_peers):
    def _boom():
        raise RuntimeError("cannot resolve CLAUDE_KLABAUTER_ROOT")

    stub_peers.setattr(sr, "coordinator_claude_klabauter_root_with_class", _boom)
    with pytest.raises(sr.StateRootError):
        sr.coordinator_state_root(central=True, subject="engine")


# --- Published-mirror guard: RESOLUTION_RESOLVED_ENGINE must never become
# --- a state parent (the defect under test in this dispatch) ---------------


def test_rule2_engine_fail_loud_when_resolved_engine_is_published_mirror(stub_peers):
    _PUBLISHED_MIRROR = "/repos/claude-klabauter"
    stub_peers.setattr(
        sr,
        "coordinator_claude_klabauter_root_with_class",
        lambda: (_PUBLISHED_MIRROR, "resolved-engine"),
    )
    with pytest.raises(sr.StateRootError) as exc:
        sr.coordinator_state_root(central=True, subject="engine")
    assert _PUBLISHED_MIRROR in str(exc.value)
    # Must NOT silently return a path under the published mirror.
    assert not str(exc.value).startswith(_state(_PUBLISHED_MIRROR))


def test_rule4_fail_loud_when_resolved_engine_is_published_mirror(stub_peers):
    stub_peers.setattr(
        sr,
        "coordinator_claude_klabauter_root_with_class",
        lambda: ("/repos/claude-klabauter", "resolved-engine"),
    )
    with pytest.raises(sr.StateRootError):
        sr.coordinator_state_root(central=True)


def test_rule5_meta_repo_fail_loud_when_resolved_engine_is_published_mirror(
    stub_peers,
):
    stub_peers.setattr(sr, "_resolve_git_root", lambda: "/home/user/.claude")
    stub_peers.setattr(sr, "is_meta_repo", lambda _g: True)
    stub_peers.setattr(
        sr,
        "coordinator_claude_klabauter_root_with_class",
        lambda: ("/repos/claude-klabauter", "resolved-engine"),
    )
    with pytest.raises(sr.StateRootError):
        sr.coordinator_state_root()


def test_rule2_engine_live_working_tree_unchanged(stub_peers):
    # RESOLUTION_LIVE_WORKING_TREE (the default stub_peers class) resolves
    # exactly as before -- no regression for the common case.
    stub_peers.setattr(
        sr,
        "coordinator_claude_klabauter_root_with_class",
        lambda: (_CLAUDE_KLABAUTER, "live-working-tree"),
    )
    assert sr.coordinator_state_root(central=True, subject="engine") == _state(_CLAUDE_KLABAUTER)


# --- Rule 3: central + artifact -> classifier-routed -----------------------


def test_rule3_artifact_doctrine_routes_to_doe(stub_peers):
    stub_peers.setattr(sr, "classify", lambda _p: Subject.DOCTRINE)
    assert (
        sr.coordinator_state_root(central=True, artifact="docs/wiki/foo.md")
        == _state(_DOE)
    )


def test_rule3_artifact_engine_routes_to_claude_klabauter(stub_peers):
    stub_peers.setattr(sr, "classify", lambda _p: Subject.ENGINE)
    assert (
        sr.coordinator_state_root(central=True, artifact="coordinator_core/x.py")
        == _state(_CLAUDE_KLABAUTER)
    )


def test_rule3_artifact_cross_cutting_fail_loud(stub_peers):
    stub_peers.setattr(sr, "classify", lambda _p: Subject.CROSS_CUTTING)
    with pytest.raises(sr.CrossCuttingStateRoot) as exc:
        sr.coordinator_state_root(central=True, artifact="docs/plans/DR-207-x.md")
    assert exc.value.artifact == "docs/plans/DR-207-x.md"
    assert "cross-cutting" in exc.value.message


def test_rule3_uses_real_classifier_end_to_end(stub_peers):
    # No classify stub override -> exercises the real artifact_subject.classify.
    stub_peers.undo()
    stub_peers.setattr(sr, "coordinator_doe_root", lambda: _DOE)
    stub_peers.setattr(sr, "coordinator_claude_klabauter_root", lambda: _CLAUDE_KLABAUTER)
    stub_peers.setattr(
        sr,
        "coordinator_claude_klabauter_root_with_class",
        lambda: (_CLAUDE_KLABAUTER, "live-working-tree"),
    )
    # A coordinator_core path classifies engine -> claude-klabauter state.
    assert (
        sr.coordinator_state_root(central=True, artifact="coordinator_core/ipc.py")
        == _state(_CLAUDE_KLABAUTER)
    )
    # A DR-207 path classifies cross-cutting -> fail-loud.
    with pytest.raises(sr.CrossCuttingStateRoot):
        sr.coordinator_state_root(central=True, artifact="docs/plans/DR-207-foo.md")


# --- Rule 4: central only (no subject/artifact) -> claude-klabauter [BACKWARD-COMPAT] -


def test_rule4_central_default_routes_to_claude_klabauter(stub_peers):
    assert sr.coordinator_state_root(central=True) == _state(_CLAUDE_KLABAUTER)


# --- Rule 5: no central -> git-root routing (the central-state redirect) ----


def test_rule5_meta_repo_redirects_to_claude_klabauter_central_state(stub_peers):
    # cwd git root IS the meta-repo -> central state redirects to claude-klabauter.
    stub_peers.setattr(sr, "_resolve_git_root", lambda: "/home/user/.claude")
    stub_peers.setattr(sr, "is_meta_repo", lambda _g: True)
    assert sr.coordinator_state_root() == _state(_CLAUDE_KLABAUTER)


def test_rule5_sibling_repo_uses_own_state(stub_peers):
    # cwd git root is a sibling repo -> per-repo state stays in the repo.
    stub_peers.setattr(sr, "_resolve_git_root", lambda: _SIBLING)
    stub_peers.setattr(sr, "is_meta_repo", lambda _g: False)
    assert sr.coordinator_state_root() == _state(_SIBLING)


def test_rule5_sibling_repo_fail_loud_when_it_is_the_published_mirror(stub_peers):
    # cwd git root IS the registered published-engine mirror clone (e.g. a
    # claude-klabauter checkout) -- must refuse, not silently treat it as
    # an ordinary sibling repo's own state root. Bug backlog:
    # state/bug-backlog/2026-08-13-state-root-rule-5-cannot-tell-a-publishe-fd79452138b2.yaml
    _MIRROR = "/repos/claude-klabauter"
    stub_peers.setattr(sr, "_resolve_git_root", lambda: _MIRROR)
    stub_peers.setattr(sr, "is_meta_repo", lambda _g: False)
    stub_peers.setattr(sr, "published_engine_mirror_path", lambda: _MIRROR)
    with pytest.raises(sr.StateRootError) as exc:
        sr.coordinator_state_root()
    assert _MIRROR in str(exc.value)
    # Must NOT silently return a path under the mirror.
    assert not str(exc.value).startswith(_state(_MIRROR))


def test_rule5_sibling_repo_unaffected_when_mirror_registered_elsewhere(stub_peers):
    # A legitimate sibling repo (e.g. Example-retrieval-repo, DoE-claude) still resolves
    # normally even when SOME OTHER path is the registered published mirror
    # -- the guard must not over-fire against every sibling repo, only the
    # one that actually IS the mirror clone.
    stub_peers.setattr(sr, "_resolve_git_root", lambda: _SIBLING)
    stub_peers.setattr(sr, "is_meta_repo", lambda _g: False)
    stub_peers.setattr(
        sr, "published_engine_mirror_path", lambda: "/repos/claude-klabauter"
    )
    assert sr.coordinator_state_root() == _state(_SIBLING)


def test_rule5_sibling_repo_fail_loud_mirror_trailing_separator_real_realpath(
    stub_peers, tmp_path
):
    # Review: coordinatorcode-reviewer-e4a7d6a8 P3 -- the existing mirror-guard
    # tests both stub `published_engine_mirror_path` with bare monkeypatch
    # values, so realpath()'s actual normalization is never exercised by the
    # committed suite. Use a real tmp_path directory and a trailing separator
    # on the registry-value side to exercise realpath() for real.
    mirror = tmp_path / "claude-klabauter"
    mirror.mkdir()
    mirror_with_trailing_sep = str(mirror) + os.sep
    stub_peers.setattr(sr, "_resolve_git_root", lambda: str(mirror))
    stub_peers.setattr(sr, "is_meta_repo", lambda _g: False)
    stub_peers.setattr(
        sr, "published_engine_mirror_path", lambda: mirror_with_trailing_sep
    )
    with pytest.raises(sr.StateRootError) as exc:
        sr.coordinator_state_root()
    assert str(mirror) in str(exc.value)


def test_rule5_fail_loud_on_unresolvable_git_root(stub_peers):
    def _boom():
        raise sr.StateRootError("not a git repo")

    stub_peers.setattr(sr, "_resolve_git_root", _boom)
    with pytest.raises(sr.StateRootError):
        sr.coordinator_state_root()


def test_rule5_meta_resolution_error_surfaces_as_state_root_error(stub_peers):
    stub_peers.setattr(sr, "_resolve_git_root", lambda: _SIBLING)

    def _boom(_g):
        raise MetaRepoResolutionError("home dir unresolvable")

    stub_peers.setattr(sr, "is_meta_repo", _boom)
    with pytest.raises(sr.StateRootError):
        sr.coordinator_state_root()


# --- Argument validation ----------------------------------------------------


def test_subject_and_artifact_mutually_exclusive(stub_peers):
    with pytest.raises(sr.StateRootError):
        sr.coordinator_state_root(central=True, subject="engine", artifact="x")


def test_unknown_subject_value_fail_loud(stub_peers):
    with pytest.raises(sr.StateRootError):
        sr.coordinator_state_root(central=True, subject="nonsense")


# --- _resolve_git_root real behavior ---------------------------------------


def test_resolve_git_root_raises_outside_git_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # pytest tmp_path is not a git repo
    with pytest.raises(sr.StateRootError):
        sr._resolve_git_root()


# --- print_map --------------------------------------------------------------


def test_print_map_both_resolvable(stub_peers):
    out = sr.print_map()
    parsed = json.loads(out)
    assert parsed["schema"] == "coordinator-state-root-map/v1"
    assert parsed["subjects"]["doctrine"] == _state(_DOE)
    assert parsed["subjects"]["engine"] == _state(_CLAUDE_KLABAUTER)


def test_print_map_doctrine_null_when_doe_unresolvable(stub_peers, capsys):
    stub_peers.setattr(sr, "coordinator_doe_root", lambda: None)
    parsed = json.loads(sr.print_map())
    assert parsed["subjects"]["doctrine"] is None
    assert parsed["subjects"]["engine"] == _state(_CLAUDE_KLABAUTER)
    assert "doctrine root unresolvable" in capsys.readouterr().err


def test_print_map_engine_null_when_claude_klabauter_unresolvable(stub_peers, capsys):
    def _boom():
        raise RuntimeError("no claude-klabauter")

    stub_peers.setattr(sr, "coordinator_claude_klabauter_root_with_class", _boom)
    parsed = json.loads(sr.print_map())
    assert parsed["subjects"]["engine"] is None
    assert parsed["subjects"]["doctrine"] == _state(_DOE)
    assert "engine root unresolvable" in capsys.readouterr().err


def test_print_map_engine_null_when_resolved_engine_is_published_mirror(stub_peers, capsys):
    """Gap 1 (review-integrator, 2026-08-12): ``print_map`` previously called
    the class-LESS ``coordinator_claude_klabauter_root()``, bypassing the published-
    mirror guard ``_claude_klabauter_state()`` applies (Rule 2/4/5) — the diagnostic
    surface would report a path the resolver itself refuses to hand out for
    writing. Now routed through ``coordinator_claude_klabauter_root_with_class()`` so
    a ``RESOLUTION_RESOLVED_ENGINE`` class nulls the engine subject too."""
    _PUBLISHED_MIRROR = "/repos/claude-klabauter"
    stub_peers.setattr(
        sr,
        "coordinator_claude_klabauter_root_with_class",
        lambda: (_PUBLISHED_MIRROR, "resolved-engine"),
    )
    parsed = json.loads(sr.print_map())
    assert parsed["subjects"]["engine"] is None
    assert parsed["subjects"]["doctrine"] == _state(_DOE)
    err = capsys.readouterr().err
    assert "published" in err.lower()


# --- main() CLI parity: exit codes 0 / 1 / 2 -------------------------------


def test_main_success_prints_path_rc0(stub_peers, capsys):
    rc = sr.main(["--central", "--subject", "engine"])
    assert rc == 0
    assert capsys.readouterr().out == _state(_CLAUDE_KLABAUTER)


def test_main_cross_cutting_rc2(stub_peers, capsys):
    stub_peers.setattr(sr, "classify", lambda _p: Subject.CROSS_CUTTING)
    rc = sr.main(["--central", "--artifact", "docs/plans/DR-207.md"])
    assert rc == 2
    assert "cross-cutting" in capsys.readouterr().err


def test_main_unknown_flag_rc1(stub_peers, capsys):
    rc = sr.main(["--bogus"])
    assert rc == 1
    assert "unknown flag" in capsys.readouterr().err


def test_main_print_map_rejects_subject_rc1(stub_peers, capsys):
    rc = sr.main(["--print-map", "--subject", "engine"])
    assert rc == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_main_print_map_rc0(stub_peers, capsys):
    rc = sr.main(["--print-map"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["subjects"]["engine"] == _state(_CLAUDE_KLABAUTER)


def test_main_subject_missing_arg_rc1(stub_peers, capsys):
    rc = sr.main(["--central", "--subject"])
    assert rc == 1
    assert "--subject requires an argument" in capsys.readouterr().err
