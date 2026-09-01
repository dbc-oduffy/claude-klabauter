"""A git spawn that never answers must not read as a schema verdict.

`check_schema_drift` and `check_schema_ahead_of_doe` are gating tamper-checks.
Both carefully raise a could-not-check when the pre-probe cannot reach the DoE
clone -- and both then made five bounded `subprocess.run` calls whose TIMEOUT
was unhandled, so an expired spawn escaped as a raw `subprocess.TimeoutExpired`:
neither the drift finding the message would have disclaimed, nor the
could-not-check the module's own discipline requires, but a third type no
caller reading `except SchemaDriftError` was looking for.

These tests inject the timeout rather than provoking one, so they pin the
BEHAVIOUR at a spawn budget and stay silent about how fast any given box is.
That matters here: the defect was found because real spawns on a loaded box
exceeded `FOREIGN_REPO_GIT_TIMEOUT_SECONDS`, and a test that reproduced it that
way would be a box-speed detector, red and green by luck. See
state/bug-backlog/2026-09-01-process-creation-is-fifty-times-the-dr-344-basis-*.yaml.

Negative-spec: these do NOT assert that a timeout is harmless. The raised type
is a `SchemaDriftError` subclass, so every existing gating caller still fails
closed. What is pinned is that the failure names itself as a comparison that
never ran.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import (
    SchemaDriftError,
    SchemaProbeUnavailableError,
    check_schema_ahead_of_doe,
    check_schema_drift,
)


@pytest.fixture()
def usable_clone(monkeypatch, tmp_path: Path) -> Path:
    """A DoE path the PRE-PROBE accepts, so each test reaches the spawn itself.

    Without this the pre-probe short-circuits and these tests would pass while
    proving nothing about the five reads downstream of it.
    """
    monkeypatch.setattr(
        "coordinator_core.frontmatter.schema_validate.foreign_repo_unusable_reason",
        lambda *_a, **_k: None,
    )
    repo = tmp_path / "DoE-clone"
    (repo / "coordinator" / "schemas").mkdir(parents=True)
    return repo


@pytest.fixture()
def vendored(tmp_path: Path) -> Path:
    local = tmp_path / "widget.schema.json"
    local.write_text('{"x-schema-version": "1.1.0"}\n', encoding="utf-8")
    return local


def _timeout(*_a, **_k):
    raise subprocess.TimeoutExpired(cmd=["git"], timeout=2.0)


def _oserror(*_a, **_k):
    raise OSError(24, "Too many open files")


@pytest.mark.parametrize("boom,label", [(_timeout, "timeout"), (_oserror, "OSError")])
def test_drift_check_reports_an_unrunnable_spawn_as_never_ran(
    monkeypatch, usable_clone, vendored, boom, label
):
    monkeypatch.setattr(subprocess, "run", boom)

    with pytest.raises(SchemaProbeUnavailableError) as excinfo:
        check_schema_drift(vendored, usable_clone, ref="HEAD")

    message = str(excinfo.value)
    assert "NOT a drift finding" in message, (
        f"an unrunnable spawn ({label}) must disclaim itself, or a reader takes "
        "it as evidence the vendored file was tampered with"
    )
    assert "diverges" not in message


@pytest.mark.parametrize("boom,label", [(_timeout, "timeout"), (_oserror, "OSError")])
def test_ahead_check_reports_an_unrunnable_spawn_as_never_ran(
    monkeypatch, usable_clone, vendored, boom, label
):
    monkeypatch.setattr(subprocess, "run", boom)

    with pytest.raises(SchemaProbeUnavailableError) as excinfo:
        check_schema_ahead_of_doe(
            vendored, usable_clone, doe_ref="HEAD", reason="test", provenance="test",
        )

    message = str(excinfo.value)
    assert "NOT an ahead-pin finding" in message, (
        f"an unrunnable spawn ({label}) must disclaim itself rather than read "
        "as a STALE / leaf-retention verdict about the pin"
    )
    assert "STALE" not in message


def test_the_disclaiming_error_still_fails_a_gating_caller_closed():
    """The subclass exists to be DISTINGUISHABLE, never to be softer.

    A revendor run or a tamper gate catching `SchemaDriftError` must keep
    failing on it; only a caller that explicitly asks about the probe should
    treat it differently.
    """
    assert issubclass(SchemaProbeUnavailableError, SchemaDriftError)
