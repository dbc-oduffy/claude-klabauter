r"""coordinator/bin/tests/test_census_revalidate_shell.py — the census executor's interpreter.

Subject: `coordinator/bin/mise-census-revalidate.py`'s `resolve_posix_shell` and the
`run_entry` call that uses it.

WHY THIS SURFACE EXISTS. A `census[].command` is POSIX shell — `screen()` parses it with
`shlex` in POSIX mode, and the corpus's dominant idioms are POSIX. The executor used to
hand the string to `subprocess.run(..., shell=True)`, which is `cmd.exe` on Windows: it
splits `'a\|b'` mid-quote and treats `;` as a literal, so the entry reported UNRUNNABLE
for a reason that had nothing to do with the premise. Measured over 18 certified plans on
a Windows host: 37 UNRUNNABLE, none of them premise drift. It is the same mid-quote cut
`screen()`'s own docstring records fixing one layer down — the screen learned POSIX while
the executor had not.

These tests pin the two halves that made the bug invisible: the interpreter is NAMED
rather than inherited from the platform, and a host with no POSIX shell says so by name
instead of falling back to one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL = Path(__file__).resolve().parents[1] / "mise-census-revalidate.py"


def _load():
    spec = importlib.util.spec_from_file_location("mise_census_revalidate", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mcr():
    assert TOOL.is_file(), f"the census revalidation leg is missing: {TOOL}"
    return _load()


def test_the_executor_never_inherits_the_platform_shell(mcr):
    """`shell=True` is the defect, not a style choice: on Windows it IS `cmd.exe`.

    Asserted over the PARSED tree, not the source text — the module's own docstrings name
    `shell=True` to explain why it is wrong, and a substring scan cannot tell an
    explanation from a call.
    """
    import ast

    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = ast.unparse(node.func)
        if not target.endswith("subprocess.run") and target != "run":
            continue
        for kw in node.keywords:
            if kw.arg == "shell" and not (
                isinstance(kw.value, ast.Constant) and kw.value.value is False
            ):
                offenders.append((node.lineno, ast.unparse(kw.value)))
    assert not offenders, (
        "the census executor must name its interpreter, never inherit it — shell=True "
        f"resolves to cmd.exe on Windows and mis-parses the corpus's POSIX idioms: {offenders}"
    )


def test_a_posix_shell_resolves_on_this_host(mcr):
    shell = mcr.resolve_posix_shell()
    assert shell, "no bash/sh on PATH — this host cannot run a census at all"


def test_an_explicit_override_is_used_as_is(mcr, monkeypatch):
    monkeypatch.setenv(mcr._SHELL_ENV_OVERRIDE, "/somewhere/odd/bash")
    assert mcr.resolve_posix_shell() == "/somewhere/odd/bash"


def test_the_wsl_launcher_is_never_chosen(mcr, monkeypatch):
    """`System32\bash.exe` shares a basename with a real shell and cannot take a Windows cwd."""
    monkeypatch.delenv(mcr._SHELL_ENV_OVERRIDE, raising=False)
    monkeypatch.setattr(
        mcr.shutil, "which", lambda name: r"C:\Windows\System32\bash.exe"
    )
    assert mcr.resolve_posix_shell() is None


def test_no_shell_reports_unrunnable_by_name_rather_than_falling_back(mcr, monkeypatch):
    monkeypatch.delenv(mcr._SHELL_ENV_OVERRIDE, raising=False)
    monkeypatch.setattr(mcr.shutil, "which", lambda name: None)
    row = mcr.run_entry(
        {"question": "q", "command": "grep -c x README.md", "result": "1"},
        REPO_ROOT,
        30,
    )
    assert row["state"] == mcr.UNRUNNABLE
    assert "no POSIX shell" in row["detail"]
    assert mcr._SHELL_ENV_OVERRIDE in row["detail"]


@pytest.mark.parametrize(
    "command",
    [
        # The grep alternation cmd.exe cut mid-quote — the canonical UNRUNNABLE.
        r"grep -rln 'coordinator\|doctrine' coordinator/bin | wc -l",
        # `;` as a separator, which cmd.exe passes through as a literal.
        r"ls coordinator/bin | wc -l ; ls coordinator/hooks | wc -l",
        r"grep -c 'def ' coordinator/bin/mise-census-revalidate.py",
    ],
)
def test_posix_idioms_execute_rather_than_reporting_unrunnable(mcr, command):
    """The verdict may be MATCH or DRIFT — what must never recur is UNRUNNABLE."""
    row = mcr.run_entry(
        {"question": "q", "command": command, "result": "1"}, REPO_ROOT, 30
    )
    assert row["state"] in (mcr.MATCH, mcr.DRIFT, mcr.UNDECIDABLE), row
    assert row["state"] != mcr.UNRUNNABLE, (
        f"{command!r} failed to parse, not to match — the interpreter is wrong again"
    )


# An empty `census:` must not roll up to MATCH via `set() - {MATCH} == set()`,
# the strongest verdict earned by measuring nothing. Pin both the empty-census
# REFUSED result and a non-empty MATCH roll-up so a refactor of the roll-up logic
# cannot reintroduce the silent fallthrough.
def test_empty_census_is_refused_not_matched(mcr, tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(
        "---\nmise_prepped_sha: abc123\ncensus: []\n---\n\nbody\n",
        encoding="utf-8",
    )
    report = mcr.revalidate(plan, REPO_ROOT, 30)
    assert report["state"] == mcr.REFUSED
    assert "no entries" in report["detail"]


def test_nonempty_all_match_census_still_reports_match(mcr, monkeypatch):
    monkeypatch.setattr(
        mcr,
        "run_entry",
        lambda entry, repo_root, timeout: {**entry, "state": mcr.MATCH},
    )
    plan = REPO_ROOT / "plan.md"
    text = (
        "---\nmise_prepped_sha: abc123\ncensus:\n"
        "  - question: q\n    command: c\n    result: '1'\n---\n\nbody\n"
    )
    fake_read = lambda self, encoding=None, errors=None: text
    monkeypatch.setattr(mcr.Path, "read_text", fake_read)
    report = mcr.revalidate(plan, REPO_ROOT, 30)
    assert report["state"] == mcr.MATCH
