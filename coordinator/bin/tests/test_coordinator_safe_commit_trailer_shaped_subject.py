from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "subject",
    [
        "Session-Id: 6ab7b0d8-1234-4a12-9abc-1234567890ab",
        "Deliverable-Id: some-deliverable",
        "Attempt-Id: deadbeef",
        "Co-Authored-By: Claude <noreply@anthropic.com>",
        "Closes: 84",
    ],
)
def test_a_bare_trailer_line_as_subject_is_refused(subject):
    mod = _load_cli_module()
    with pytest.raises(mod.UsageError, match="trailer line"):
        mod.parse_args([subject])


def test_a_bare_trailer_line_is_refused_on_the_pathspec_form():
    mod = _load_cli_module()
    with pytest.raises(mod.UsageError, match="trailer line"):
        mod.parse_args(["Session-Id: abc", "--", "some/file.py"])


@pytest.mark.parametrize(
    "subject",
    [
        "fix: the frobnicator",
        "grind(profile): row-1 committed",
        "closes the loop on retries",
        "Refactor session scope",
    ],
)
def test_an_ordinary_subject_still_parses(subject):
    mod = _load_cli_module()
    args = mod.parse_args([subject])
    assert args.subject == subject


def test_a_hyphenated_tool_prefix_subject_is_not_a_trailer():
    mod = _load_cli_module()
    assert mod.parse_args(["percolate-round: seed check"]).subject == "percolate-round: seed check"
