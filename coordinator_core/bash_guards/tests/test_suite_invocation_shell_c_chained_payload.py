"""A chained `sh -c` / `bash -c` payload is judged segment by segment, like the unwrapped chain."""

from __future__ import annotations

import shlex

import pytest
from pathlib import Path

pytestmark = pytest.mark.complete

from coordinator_core.bash_guards import check_test_suite_invocation as guard

_FAST = "python3 bin/run-fast-tests.py"


@pytest.fixture
def real_verdict():
    """Request this to run the shared test-run verdict (leg 0) unstubbed."""


@pytest.fixture(autouse=True)
def _isolate_leg_zero(request, monkeypatch: pytest.MonkeyPatch) -> None:
    """The verdict admits and the ceiling reads healthy, unless the test requests `real_verdict`."""
    from coordinator_core.bash_guards import _test_run_admission as adm

    monkeypatch.setattr(adm, "over_admit_ceiling", lambda payload: None)
    if "real_verdict" not in request.fixturenames:
        monkeypatch.setattr(adm, "test_run_verdict", lambda *a, **k: adm.TestRunVerdict(True))


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
    monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
    (tmp_path / ".git").mkdir()
    (tmp_path / "coordinator.local.md").write_text(
        f"---\nfast_test_cmd: {_FAST}\n---\n", encoding="utf-8"
    )
    return str(tmp_path)


def _verdict(cmd: str, cwd: str):
    res = guard.check({
        "tool_name": "Bash",
        "session_id": "e641c238-68e3-480a-9e44-3ed73e8c5c94",
        "agent_id": "agent-1",
        "agent_type": "coordinator:executor",
        "cwd": cwd,
        "tool_input": {"command": cmd},
    })
    return None if res is None else getattr(res, "verdict_kind", "other")


def _tiers(cmd: str, cwd: str) -> list[tuple[str, str]]:
    return [(m.tier, m.detected) for m in guard.classify_command(cmd, cwd=cwd)]


_CHAINED = [
    (f'bash -c "{_FAST}; echo exit=$?"', f"{_FAST}; echo exit=$?"),
    ('sh -c "pytest; echo done"', "pytest; echo done"),
    ('bash -c "cd x && pytest"', "cd x && pytest"),
    ("bash -c \"sh -c 'pytest; echo a'\"", "pytest; echo a"),
    (f"with-suite-mutex -- bash -c '{_FAST}; echo x'", f"with-suite-mutex -- {_FAST}; echo x"),
]


@pytest.mark.parametrize("wrapped,unwrapped", _CHAINED)
def test_chained_payload_is_denied_and_classified_like_the_unwrapped_chain(repo, wrapped, unwrapped) -> None:
    assert _verdict(unwrapped, repo) == "deny"
    assert _verdict(wrapped, repo) == "deny"
    assert _tiers(unwrapped, repo)
    assert _tiers(wrapped, repo) == _tiers(unwrapped, repo)


def test_file_scoped_chained_payload_is_not_denied_like_its_unwrapped_chain(repo) -> None:
    unwrapped = "python -m pytest tests/test_a.py; echo exit=$?"
    wrapped = 'bash -c "python -m pytest tests/test_a.py; echo exit=$?"'
    assert _verdict(unwrapped, repo) != "deny"
    assert _verdict(wrapped, repo) != "deny"
    assert _tiers(wrapped, repo) == _tiers(unwrapped, repo)


def test_executor_is_refused_a_file_scoped_chained_payload_by_the_verdict(repo, real_verdict) -> None:
    for cmd in (
        "python -m pytest tests/test_a.py; echo exit=$?",
        'bash -c "python -m pytest tests/test_a.py; echo exit=$?"',
    ):
        assert _verdict(cmd, repo) == "deny"


def test_single_segment_payload_behaves_as_before(repo) -> None:
    cmd = "with-suite-mutex -- bash -c 'python3 -m pytest -m \"not cadence\"'"
    bare = "with-suite-mutex -- python3 -m pytest -m 'not cadence'"
    assert _tiers(cmd, repo) == _tiers(bare, repo)
    assert _verdict(cmd, repo) == _verdict(bare, repo)


def test_payload_without_a_runner_stays_silent(repo) -> None:
    assert _verdict('bash -c "echo a; echo b"', repo) is None
    assert _tiers('bash -c "echo a; echo b"', repo) == []


def test_depth_past_the_cap_still_unwraps_the_single_argv(repo) -> None:
    # Single-segment nesting: pins the unwrap fallback only, not the expansion cap.
    cmd = "pytest"
    for _ in range(guard._SHELL_C_EXPANSION_DEPTH + 2):
        cmd = "bash -c " + shlex.quote(cmd)
    assert _tiers(cmd, repo)
    assert _verdict(cmd, repo) == "deny"
