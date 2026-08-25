"""
Tests for coordinator_core.ops.copy_plugin_template — settlement A6
(repo_setup.copy_console_subprocess_tripwire).

Covers the settled branch matrix (dest absent → copy; byte-identical → skip;
differing bytes → skip-and-report NEVER clobber), the CC-4 double-invocation
no-op proof, the direct-pytest verification contract
([sys.executable, "-m", "pytest", dest]), and the CC-7 structured-error
premises. All filesystem work is tmp_path-hermetic; the DoE-root resolver is
never exercised (template_path is injected), so tests pass on a machine with
no DoE clone.
"""

from __future__ import annotations

import pytest

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops import copy_plugin_template as mod

_PASSING_TEMPLATE = "def test_tripwire_placeholder():\n    assert True\n"
_FAILING_TEMPLATE = "def test_tripwire_placeholder():\n    assert False\n"
_CUSTOMIZED = (
    "# hand-customized allowlist edits — must survive rerun\n"
    "def test_tripwire_placeholder():\n    assert True\n"
)


@pytest.fixture
def template(tmp_path):
    path = tmp_path / "template-src" / "test_no_bare_console_subprocess.py"
    path.parent.mkdir()
    path.write_text(_PASSING_TEMPLATE, encoding="utf-8")
    return path


@pytest.fixture
def target(tmp_path):
    root = tmp_path / "consuming-repo"
    root.mkdir()
    return root


def _no_pytest(monkeypatch, result=True):
    """Stub the pytest verification spawn for branch tests that don't need it."""
    calls = []

    def fake(dest, cwd):
        calls.append((dest, cwd))
        return result

    monkeypatch.setattr(mod, "_run_pytest", fake)
    return calls


def test_dest_absent_copies_and_verifies(template, target):
    result = mod.copy_console_subprocess_tripwire(str(target), template_path=template)
    dest = target / "tests" / "test_no_bare_console_subprocess.py"
    assert result == {"copied": True, "skipped_existing": False, "test_passed": True}
    assert dest.read_bytes() == template.read_bytes()


def test_double_invocation_is_content_idempotent_no_op(template, target, monkeypatch):
    """CC-4: second call with identical inputs is a safe no-op — no write,
    documented no-op shape, destination bytes untouched."""
    calls = _no_pytest(monkeypatch)
    first = mod.copy_console_subprocess_tripwire(str(target), template_path=template)
    assert first["copied"] is True

    dest = target / "tests" / "test_no_bare_console_subprocess.py"
    before = dest.read_bytes()
    second = mod.copy_console_subprocess_tripwire(str(target), template_path=template)
    assert second == {"copied": False, "skipped_existing": False, "test_passed": True}
    assert dest.read_bytes() == before
    assert len(calls) == 2  # verification runs on both invocations


def test_differing_dest_skip_and_report_never_clobbers(template, target, monkeypatch):
    _no_pytest(monkeypatch)
    dest = target / "tests" / "test_no_bare_console_subprocess.py"
    dest.parent.mkdir()
    dest.write_text(_CUSTOMIZED, encoding="utf-8")

    result = mod.copy_console_subprocess_tripwire(str(target), template_path=template)
    assert result["copied"] is False
    assert result["skipped_existing"] is True
    # The hand-customized bytes survive verbatim — the settled hazard closure.
    assert dest.read_text(encoding="utf-8") == _CUSTOMIZED


def test_failing_tripwire_reports_test_passed_false(target, tmp_path):
    template = tmp_path / "failing-template.py"
    template.write_text(_FAILING_TEMPLATE, encoding="utf-8")
    result = mod.copy_console_subprocess_tripwire(str(target), template_path=template)
    assert result["copied"] is True
    assert result["test_passed"] is False


def test_missing_target_root_raises_structured_error(template, tmp_path):
    missing = tmp_path / "no-such-repo"
    with pytest.raises(mod.TripwireCopyError, match="target_repo_root"):
        mod.copy_console_subprocess_tripwire(str(missing), template_path=template)


def test_missing_template_raises_structured_error(target, tmp_path):
    missing = tmp_path / "no-such-template.py"
    with pytest.raises(mod.TripwireCopyError, match="template"):
        mod.copy_console_subprocess_tripwire(str(target), template_path=missing)


def test_dest_is_directory_raises_structured_error(template, target, monkeypatch):
    _no_pytest(monkeypatch)
    dest = target / "tests" / "test_no_bare_console_subprocess.py"
    dest.mkdir(parents=True)
    with pytest.raises(mod.TripwireCopyError, match="not a regular file"):
        mod.copy_console_subprocess_tripwire(str(target), template_path=template)


def test_unresolvable_doe_root_raises_structured_error(target, monkeypatch):
    monkeypatch.setattr(mod, "coordinator_doe_root", lambda: None)
    with pytest.raises(mod.TripwireCopyError, match="unresolvable"):
        mod.copy_console_subprocess_tripwire(str(target))


def test_op_registered_and_handler_requires_param():
    handler = get_op_handler("repo_setup.copy_console_subprocess_tripwire")
    assert handler is not None
    with pytest.raises(mod.TripwireCopyError, match="target_repo_root"):
        handler({}, None)


def test_handler_delegates_to_pure_function(template, target, monkeypatch):
    _no_pytest(monkeypatch)
    monkeypatch.setattr(mod, "_resolve_template_path", lambda: template)
    handler = get_op_handler("repo_setup.copy_console_subprocess_tripwire")
    result = handler({"target_repo_root": str(target)}, None)
    assert result == {"copied": True, "skipped_existing": False, "test_passed": True}
