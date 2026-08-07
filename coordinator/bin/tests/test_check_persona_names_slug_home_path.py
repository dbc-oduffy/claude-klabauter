"""Regression: a home-directory path built from the operator handle must be a
finding, not silently permitted by HANDLE_SLUG_RE's slug exemption.

Spec: docs/plans/2026-08-07-publish-identity-scrub-and-two-repo-gates.md, chunk C1b.
"""
from __future__ import annotations

import importlib.util
import pathlib

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "dist"
    / "mirror-native"
    / "claude-klabauter"
    / ".github"
    / "scripts"
    / "check-persona-names.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("check_persona_names", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_home_directory_path_is_a_finding():
    module = _load_module()
    text = "/Users/dbc-example-operator/repos/claude-klabauter"  # abs-path-ok: fixture data, not a real path reference
    spans = module.permitted_spans(text, "some/file.py")
    assert spans == [], f"home-directory path was wrongly permitted: {spans}"


def test_backslash_predecessor_home_path_is_a_finding():
    module = _load_module()
    text = r"C:\Users\dbc-example-operator/claude-klabauter"  # abs-path-ok: fixture data, not a real path reference
    spans = module.permitted_spans(text, "some/file.py")
    assert spans == [], f"backslash-predecessor home path was wrongly permitted: {spans}"


def test_tilde_predecessor_home_path_is_a_finding():
    module = _load_module()
    text = "~/dbc-example-operator/repos/claude-klabauter"  # abs-path-ok: fixture data, not a real path reference
    spans = module.permitted_spans(text, "some/file.py")
    assert spans == [], f"tilde-predecessor home path was wrongly permitted: {spans}"


def test_genuine_owner_repo_slug_still_permitted():
    module = _load_module()
    text = "See dbc-oduffy/claude-klabauter for details."
    spans = module.permitted_spans(text, "some/file.py")
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:end] == "dbc-example-operator"


def test_github_url_still_permitted():
    module = _load_module()
    text = "Clone from https://github.com/dbc-oduffy/claude-klabauter.git"
    spans = module.permitted_spans(text, "some/file.py")
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:end] == "dbc-example-operator"


def test_slug_at_start_of_line_still_permitted():
    module = _load_module()
    text = "dbc-oduffy/claude-klabauter is the source repo."
    spans = module.permitted_spans(text, "some/file.py")
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:end] == "dbc-example-operator"


def test_slug_after_punctuation_still_permitted():
    module = _load_module()
    for prefix, suffix in [("(", ")"), ('"', '"'), ("[", "]"), (": ", "")]:
        text = f"see {prefix}dbc-oduffy/claude-klabauter{suffix} for details"
        spans = module.permitted_spans(text, "some/file.py")
        assert len(spans) == 1, f"prefix {prefix!r} unexpectedly excluded: {spans}"
        start, end = spans[0]
        assert text[start:end] == "dbc-example-operator"


def test_codename_in_repo_segment_is_a_finding():
    # The permitted span must cover only the handle, not the repo segment --
    # an internal codename landing there (e.g. an `owner/.example-doctrine-mirror-repo` slug)
    # must remain a finding while the handle itself stays permitted.
    module = _load_module()
    text = 'repo: "dbc-oduffy/.example-doctrine-mirror-repo"'
    spans = module.permitted_spans(text, "some/file.py")
    assert len(spans) == 1
    start, end = spans[0]
    assert text[start:end] == "dbc-example-operator"
    codename_start = text.index(".example-doctrine-mirror-repo")
    codename_end = codename_start + len(".example-doctrine-mirror-repo")
    assert not any(a <= codename_start and codename_end <= b for a, b in spans)
