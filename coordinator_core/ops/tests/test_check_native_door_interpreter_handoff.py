"""Tests for the native-door interpreter-handoff census.

The load-bearing test here is NOT the live-population ratchet -- that one
prints green today and would print green if the guard's detector were
deleted. What makes the ratchet mean anything is the planted-control pair:
a specimen carrying the exact defect that MUST be flagged, and a specimen
doing the same job correctly that MUST NOT be. Both live in
``fixtures/native_door_handoff/`` as ``.py.txt`` so no collector, importer
or sibling guard in this tree treats them as source.

Negative-spec: never silence a control by editing the control. A positive
control that stops being flagged is a broken guard; a negative control that
starts being flagged is an over-firing guard. Either way the fix is in
``check_native_door_interpreter_handoff``, not here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import check_native_door_interpreter_handoff as guard

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "native_door_handoff"

_CONTROLS = (
    "positive_python_consumer.py.txt",
    "negative_python_consumer.py.txt",
    "positive_shell_emitter.py.txt",
    "negative_shell_emitter.py.txt",
    "positive_suffix_dispatch.py.txt",
    "negative_suffix_dispatch.py.txt",
)


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_control_fixtures_are_exactly_the_declared_pair():
    """The fixture directory is excluded from the live scan. That exclusion is
    only safe while the directory holds nothing but declared controls."""
    present = sorted(p.name for p in _FIXTURES.iterdir() if p.is_file())
    assert present == sorted(_CONTROLS)
    assert guard.CONTROL_FIXTURE_DIR.endswith("native_door_handoff/")


# ---------------------------------------------------------------------------
# Planted controls -- the proof that the guard discriminates
# ---------------------------------------------------------------------------


def test_positive_python_control_is_flagged():
    findings = guard.classify_python_source(
        "positive_python_consumer.py", _fixture("positive_python_consumer.py.txt")
    )
    assert findings, "the planted defect was not flagged -- the guard is asleep"
    assert any("argv literal prefixes an interpreter" in f.shape for f in findings)
    assert any(f.scope == "run_sentinel" for f in findings)


def test_negative_python_control_is_not_flagged():
    findings = guard.classify_python_source(
        "negative_python_consumer.py", _fixture("negative_python_consumer.py.txt")
    )
    assert findings == [], f"resolve_launchable delegation must pass: {findings}"


def test_positive_shell_control_is_flagged():
    findings = guard.classify_text_source(
        "positive_shell_emitter.py", _fixture("positive_shell_emitter.py.txt")
    )
    assert findings, "the planted emitted-shell defect was not flagged"
    assert "no native-image probe" in findings[0].shape


def test_negative_shell_control_is_not_flagged():
    findings = guard.classify_text_source(
        "negative_shell_emitter.py", _fixture("negative_shell_emitter.py.txt")
    )
    assert findings == [], f"a native-image probe must clear the rung: {findings}"


def test_extension_bearing_names_are_not_the_hazard():
    """``<settings-home>/bin/_machine_local.py`` under ``sys.executable`` is
    what ``resolve_launchable`` itself produces. The door image occupies the
    bare name or ``.exe``, never a ``.py``/``.json`` name -- flagging those
    reported 18 correct call sites on the first live run."""
    source = (
        "import os, sys, subprocess\n"
        "from coordinator_core._settings_home import settings_home\n"
        "def impl():\n"
        "    return os.path.join(str(settings_home()), 'bin', '_machine_local.py')\n"
        "def get(key):\n"
        "    return subprocess.run([sys.executable, impl(), 'get', key])\n"
        "def manifest():\n"
        "    return (settings_home() / 'bin' / '.coordinator-bin-manifest.json').read_text()\n"
    )
    assert guard.producers_in_source(source) == set(), (
        "a resolver returning a `.py` name must not be treated as a producer"
    )
    assert guard.classify_python_source("x.py", source) == []


def test_magic_byte_refusal_clears_the_scope():
    source = (
        "import sys, subprocess\n"
        "from coordinator_core.install import door_install\n"
        "from coordinator_core._settings_home import settings_home\n"
        "def leg():\n"
        "    launcher = settings_home() / 'bin' / 'claude-doe'\n"
        "    if launcher.read_bytes().startswith(door_install.NATIVE_IMAGE_MAGIC):\n"
        "        return 1\n"
        "    return subprocess.run([sys.executable, str(launcher)])\n"
    )
    assert guard.classify_python_source("x.py", source) == []


def test_exec_bit_discrimination_clears_the_scope():
    """The shape the two fixed in-tree sites actually landed. Not preferred --
    it is a second answer to the question ``resolve_launchable`` already
    answers -- but correct, and REDing fixed code trains authors to route
    around the guard."""
    source = (
        "import os, sys, subprocess\n"
        "from coordinator_core._settings_home import settings_home\n"
        "def argv_for():\n"
        "    cli = settings_home() / 'bin' / 'session-claim-cli'\n"
        "    if os.name != 'nt' and not str(cli).endswith('.py') and os.access(cli, os.X_OK):\n"
        "        return [str(cli)]\n"
        "    return [sys.executable, str(cli)]\n"
    )
    assert guard.classify_python_source("x.py", source) == []


def test_inert_exemption_reference_does_not_clear_an_unrelated_unconditional_handoff():
    """Code-reviewer Finding 2 (2026-09-02): `_scope_is_exempt` used to be
    evaluated over the WHOLE enclosing scope, so an inert mention of an
    exemption-shaped token anywhere in a function cleared every candidate in
    it -- including a completely unconditional, ungated interpreter handoff
    elsewhere in the same function. This scope contains both: a dead branch
    that references `NATIVE_IMAGE_MAGIC` and never runs, plus an unconditional
    handoff of a settings-home bin/ path to the interpreter. The handoff must
    still be flagged -- the inert reference does not gate it.
    """
    source = (
        "import sys, subprocess\n"
        "from coordinator_core.install import door_install\n"
        "from coordinator_core._settings_home import settings_home\n"
        "def leg():\n"
        "    if False:\n"
        "        _ignored = door_install.NATIVE_IMAGE_MAGIC\n"
        "    cli = settings_home() / 'bin' / 'session-claim-cli'\n"
        "    return subprocess.run([sys.executable, str(cli)])\n"
    )
    findings = guard.classify_python_source("x.py", source)
    assert findings, (
        "an inert NATIVE_IMAGE_MAGIC reference cleared an unconditional, "
        "ungated interpreter handoff sharing its scope -- the exemption is "
        "not tied to the flagged call site"
    )


# ---------------------------------------------------------------------------
# End-to-end: the same controls through the real scan, greps included
# ---------------------------------------------------------------------------


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_scan_root_flags_exactly_the_positive_controls(tmp_path):
    """Exercises the FULL path -- candidate selection by ``git grep``,
    cross-file producer/parameter taint, both arms -- not just the pure
    classifiers.

    This is not redundant with the classifier tests: an earlier revision
    passed every one of them while its shell-arm ``git grep`` pattern used a
    non-greedy quantifier POSIX ERE has no spelling for, so the live scan
    silently selected zero files.
    """
    repo = tmp_path / "planted"
    (repo / "pkg").mkdir(parents=True)
    for name in _CONTROLS:
        (repo / "pkg" / name.replace(".py.txt", ".py")).write_text(
            _fixture(name), encoding="utf-8"
        )

    # Real git, deliberately: the scan SELECTS its corpus with `git grep`,
    # so a real index is the thing under test here. Plain files cannot
    # produce one, and it was a grep-pattern bug -- not a classifier bug --
    # that this test exists to catch.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "GIT_CONFIG_SYSTEM": ""}
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            env=env,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    flagged = {f.relpath for f in guard.scan_root(str(repo), root_label="planted")}
    assert flagged == {
        "pkg/positive_python_consumer.py",
        "pkg/positive_shell_emitter.py",
        # Selected by the suffix arm's OWN grep pair, not by the settings-home
        # greps -- this fixture names no settings-home token at all, which is
        # the property that made the live defect unselectable before the arm
        # existed. Its presence here is the end-to-end proof of that selector.
        "pkg/positive_suffix_dispatch.py",
    }


# ---------------------------------------------------------------------------
# The live ratchet, and the second-root contract
# ---------------------------------------------------------------------------


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_this_repo_has_no_interpreter_mediated_site():
    findings = guard.scan_root(str(_REPO_ROOT), root_label="claude-klabauter")
    assert findings == [], "\n".join(f.render() for f in findings) + f"\n\nFix: {guard.REMEDY}"


def test_this_repo_is_always_a_root_and_a_missing_doe_is_a_skip(monkeypatch):
    """A box with no DoE clone is a normal box. The second root is reported as
    a skip with its reason, never as a failure, and the first root still
    scans."""
    import coordinator_core.ops.coordinator_doe_root as doe

    monkeypatch.setattr(doe, "coordinator_doe_root", lambda: None)
    monkeypatch.setattr(doe, "_RESOLVED_DOE_ROOT", None, raising=False)
    monkeypatch.setattr(doe, "_DOE_ROOT_RESOLVED", False, raising=False)

    roots, skips = guard.resolve_roots()
    assert [label for label, _ in roots] == ["claude-klabauter"]
    assert any("DoE-claude" in skip for skip in skips)


def test_explicit_extra_root_is_scanned_and_a_bad_one_is_a_skip(tmp_path):
    real = tmp_path / "sibling"
    real.mkdir()
    roots, skips = guard.resolve_roots([str(real), str(tmp_path / "absent")])
    assert (real.name, str(real)) in [(label, path) for label, path in roots]
    assert any("absent" in skip for skip in skips)


def test_remedy_names_the_one_function_and_the_doctrine_anchor():
    assert "resolve_launchable" in guard.REMEDY
    assert "NATIVE_IMAGE_MAGIC" in guard.REMEDY
    assert "an-extensionless-settings-home-bin-entry-is-not-python-source.md" in guard.REMEDY


def test_cli_exits_zero_on_a_clean_root(tmp_path, capsys, monkeypatch):
    # Not a git repo on purpose: `_git` collapses an unusable root to "no
    # candidate files", so an operator pointing `--root` at a plain directory
    # gets a clean report, never a traceback. DoE resolution is stubbed out
    # (`--no-doe` was removed as a dead flag with no caller outside its own
    # test -- overengineering-reviewer, 2026-09-02) so the assertion doesn't
    # depend on whether this box has a DoE-claude clone.
    import coordinator_core.ops.coordinator_doe_root as doe

    monkeypatch.setattr(doe, "coordinator_doe_root", lambda: None)
    monkeypatch.setattr(doe, "_RESOLVED_DOE_ROOT", None, raising=False)
    monkeypatch.setattr(doe, "_DOE_ROOT_RESOLVED", False, raising=False)

    empty = tmp_path / "empty"
    empty.mkdir()
    rc = guard.main(["--root", str(empty)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "SKIP DoE-claude:" in out


def test_module_is_runnable_as_a_module():
    """The guard's own invocation route. `python -m` is the whole CLI surface
    it needs -- no bin/ launcher, no `.cmd` twin, no publish-allowlist row."""
    assert hasattr(guard, "main")
    assert sys.version_info >= (3, 11)


# ---------------------------------------------------------------------------
# Suffix-dispatch arm -- the parameter-taking helper the taint arms cannot see
# ---------------------------------------------------------------------------


def test_suffix_dispatch_positive_control_is_flagged():
    """No producers, no params: the whole point of this arm is that it needs
    neither. A helper that takes the bin directory as a parameter names no
    settings-home token, so the taint arms never select it."""
    findings = guard.classify_python_source(
        "positive_suffix_dispatch.py", _fixture("positive_suffix_dispatch.py.txt")
    )
    assert findings, "the planted suffix dispatch was not flagged -- the arm is asleep"
    assert all("suffix test was FALSE" in f.shape for f in findings)
    assert {f.scope for f in findings} == {"sentinel_argv"}


def test_suffix_dispatch_negative_control_is_not_flagged():
    findings = guard.classify_python_source(
        "negative_suffix_dispatch.py", _fixture("negative_suffix_dispatch.py.txt")
    )
    assert findings == [], f"positive .py and exec-bit forms must pass: {findings}"


def test_positive_py_suffix_test_never_fires():
    """The asymmetry this arm rests on, asserted directly rather than only
    through a fixture: interpreting because a suffix IS `.py` is safe, because
    a door image never occupies such a name."""
    source = (
        "import sys\n"
        "def argv(p):\n"
        "    if p.suffix == '.py':\n"
        "        return [sys.executable, str(p)]\n"
        "    return [str(p)]\n"
    )
    assert guard.classify_python_source("x.py", source) == []


def test_absent_extension_dispatch_fires_without_any_taint():
    source = (
        "import sys\n"
        "def argv(p):\n"
        "    if p.suffix == '.exe':\n"
        "        return [str(p)]\n"
        "    return [sys.executable, str(p)]\n"
    )
    findings = guard.classify_python_source("x.py", source)
    assert len(findings) == 1
    assert findings[0].lineno == 5


def test_the_taint_is_file_local_and_that_is_the_contract():
    """The rebuild (2026-09-02) replaced a repo-wide producer/parameter
    registry with a file-local one. That is a deliberate narrowing, pinned
    here so it reads as a contract rather than as an accident.

    Why it is safe: every site this census has ever had to reason about
    composes and hands off inside ONE file -- the planted control, and both
    live exempted sites (`wsc-session-disposition` resolves the bin path in
    `find_session_claim_cli` and prefixes the interpreter in
    `_session_claim_cli_argv`, same module). The one real defect the census
    ever found, the DoE plane's `forwarder_argv`, carries no composition at
    all and belongs to the suffix arm, which needs no taint. The repo-wide
    form, keyed by unqualified callee name, never produced a finding in
    either root while reporting seven false sites until a "defined in exactly
    one file" rule was added to suppress them.

    A cross-file consumer is therefore a KNOWN blind spot, not an oversight.
    If one ever occurs, widen this deliberately -- and note that the suffix
    arm already covers the parameter-taking shape it would most likely take.
    """
    producer_file = (
        "from coordinator_core._settings_home import settings_home\n"
        "def resolve_cli():\n"
        "    return settings_home() / 'bin' / 'session-claim-cli'\n"
    )
    consumer_file = (
        "import sys, subprocess\n"
        "def run(cli_path):\n"
        "    return subprocess.run([sys.executable, str(cli_path)])\n"
    )
    assert guard.producers_in_source(producer_file) == {"resolve_cli"}
    assert guard.classify_python_source("consumer.py", consumer_file) == [], (
        "the consumer names no composition of its own, so it is out of scope "
        "by design -- see this test's docstring before 'fixing' it"
    )
    # Put the same two halves in ONE file and the arm fires, which is the
    # boundary this test exists to draw.
    assert guard.classify_python_source(
        "both.py", producer_file + "\n" + consumer_file + "\ndef main():\n    return run(resolve_cli())\n"
    )
