"""Guards the C2a consolidation: one ``_is_harness_process`` predicate site
in ``coordinator_core/session/core.py``, replacing three previously-
triplicated ``== "claude"`` / ``!= "claude"`` literal comparisons, with the
breadcrumb vocabulary at each site left byte-identical.

Also guards C3's fix at the same consolidated site: the name compared is
now derived by ``_harness_process_comm`` from ``cmdline()[0]``'s basename
(``name()`` degraded to a version string on this box — see
docs/research/2026-08-14-harness-process-identity-problem-set.md § Q2/Q4),
falling back to ``name()`` (today's field) when ``cmdline()`` is
unavailable/empty/raises.

Spec: docs/plans/2026-08-13-session-identity-earns-its-keep.md § C2 (row
split into C2a/C2b by the EM; C2a covered predicate-consolidation only)
and § C3 (this chunk — the field fix).
"""

import inspect
import re

from coordinator_core.session import core


class _FakeProcess:
    """Minimal psutil.Process double for ``_harness_process_comm``."""

    def __init__(self, cmdline=None, name="", cmdline_exc=None, name_exc=None):
        self._cmdline = cmdline
        self._name = name
        self._cmdline_exc = cmdline_exc
        self._name_exc = name_exc

    def cmdline(self):
        if self._cmdline_exc is not None:
            raise self._cmdline_exc
        return self._cmdline

    def name(self):
        if self._name_exc is not None:
            raise self._name_exc
        return self._name


def test_is_harness_process_defined_exactly_once():
    source = inspect.getsource(core)
    definitions = re.findall(r"^def _is_harness_process\(", source, re.MULTILINE)
    assert len(definitions) == 1, (
        "expected exactly one _is_harness_process definition, "
        f"found {len(definitions)}"
    )


def test_no_literal_claude_name_comparisons_outside_predicate():
    source = inspect.getsource(core)
    predicate_source = inspect.getsource(core._is_harness_process)
    remainder = source.replace(predicate_source, "", 1)
    literal_comparisons = re.findall(r'[=!]=\s*"claude"', remainder)
    assert literal_comparisons == [], (
        "found literal claude-name comparison(s) outside the consolidated "
        f"predicate: {literal_comparisons}"
    )


def test_predicate_exact_match_semantics():
    assert core._is_harness_process("claude") is True
    assert core._is_harness_process("Claude") is False
    assert core._is_harness_process("claude-helper") is False
    assert core._is_harness_process("") is False
    assert core._is_harness_process("claude.exe") is False


def test_env_resolve_breadcrumb_vocabulary_unchanged():
    source = inspect.getsource(core._resolve_claude_pid_from_env)
    assert '"env-miss:name-mismatch"' in source
    assert "_is_harness_process(comm)" in source


def test_windows_ancestor_walk_routes_through_predicate():
    source = inspect.getsource(core._find_windows_claude_ancestor)
    assert "_is_harness_process(comm)" in source
    assert 'comm == "claude"' not in source


def test_posix_breadcrumb_vocabulary_unchanged():
    source = inspect.getsource(core)
    assert '"posix-parent-hit"' in source
    assert '"posix-parent-miss:name-mismatch"' in source
    assert '"posix-parent-miss:no-create-time"' in source
    assert "posix-parent-miss:{type(posix_capture_exc).__name__}" in source


# ---------------------------------------------------------------------------
# C3 — argv0-basename derivation fix
# ---------------------------------------------------------------------------


def test_version_string_name_but_claude_argv0_is_accepted():
    """The exact live-measured shape this chunk fixes: name() reports a
    version string, but argv0's basename is "claude"."""
    proc = _FakeProcess(cmdline=["/home/example-user/.local/bin/claude", "--flag"], name="2.1.231")  # abs-path-ok: fixture argv0, not a real host path
    comm = core._harness_process_comm(proc)
    assert comm == "claude"
    assert core._is_harness_process(comm) is True


def test_neither_argv0_nor_name_is_claude_is_rejected():
    proc = _FakeProcess(cmdline=["/usr/bin/bash", "-c", "x"], name="bash")
    comm = core._harness_process_comm(proc)
    assert comm != "claude"
    assert core._is_harness_process(comm) is False


def test_windows_argv0_exe_suffix_stripped():
    proc = _FakeProcess(cmdline=["C:\\Users\\x\\claude.exe", "--flag"], name="2.1.231")
    comm = core._harness_process_comm(proc)
    assert comm == "claude"
    assert core._is_harness_process(comm) is True


def test_cmdline_empty_falls_back_to_name_todays_behaviour():
    proc = _FakeProcess(cmdline=[], name="claude")
    assert core._harness_process_comm(proc) == "claude"

    proc_mismatch = _FakeProcess(cmdline=[], name="2.1.231")
    assert core._harness_process_comm(proc_mismatch) == "2.1.231"


def test_cmdline_raising_falls_back_to_name_and_never_raises():
    import psutil

    for exc in (
        psutil.AccessDenied(pid=1),
        psutil.ZombieProcess(pid=1),
        psutil.NoSuchProcess(pid=1),
        RuntimeError("unexpected"),
    ):
        proc = _FakeProcess(cmdline=None, name="claude", cmdline_exc=exc)
        assert core._harness_process_comm(proc) == "claude"


def test_cmdline_none_falls_back_to_name():
    proc = _FakeProcess(cmdline=None, name="claude")
    assert core._harness_process_comm(proc) == "claude"


def test_fallback_name_exception_propagates_to_caller():
    """The name()-fallback call is deliberately unguarded inside
    _harness_process_comm — its exception must reach the caller's own
    except block, exactly as a raw ``proc.name()`` call did before this
    chunk. Verified directly against the helper (call-site behaviour is
    covered by the walk/env-resolve/posix breadcrumb tests elsewhere)."""
    import psutil

    proc = _FakeProcess(cmdline=[], name_exc=psutil.AccessDenied(pid=1))
    try:
        core._harness_process_comm(proc)
    except psutil.AccessDenied:
        pass
    else:
        raise AssertionError("expected AccessDenied to propagate from the name() fallback")


def test_all_three_call_sites_route_through_harness_process_comm():
    """AC2 still holds: derivation is centralized in one helper, not
    reintroduced ad hoc at each site."""
    for fn in (
        core._find_windows_claude_ancestor,
        core._resolve_claude_pid_from_env,
        core.init,
    ):
        source = inspect.getsource(fn)
        assert "_harness_process_comm(" in source, (
            f"{fn.__name__} does not route through _harness_process_comm"
        )
