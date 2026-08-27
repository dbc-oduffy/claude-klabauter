"""
coordinator_core.ops.test_run_shellcheck_sweep

Characterization tests for the "ci.run_shellcheck_sweep" op
(coordinator_core.ops.run_shellcheck_sweep) — the shellcheck-lint port of
the workweek-complete fence's inline `git ls-files '*.sh' | while read f;
do tr -d '\\r' <"$f" | shellcheck ...; done` pipeline.

The `shellcheck` binary is never required to be installed: every test that
exercises `_lint_one_file`/`run_shellcheck_sweep` monkeypatches
`subprocess.run` so the suite is green on a machine without shellcheck on
PATH. Only `git` (already a hard dependency of this repo's own tooling) is
invoked for real, against tmp_path-scoped throwaway repos.

Coverage:
  (a) registered under exactly "ci.run_shellcheck_sweep" on import
  (b) no tracked .sh files -> {"findings": [], "files_checked": 0}
  (c) happy path: one tracked .sh file, mocked shellcheck emits one finding
      -> finding's "file" is rewritten to the repo-relative path, not the
      temp-file path shellcheck itself would report
  (d) CRLF content is normalized (no "\\r" survives) before being handed to
      the mocked shellcheck subprocess
  (e) shellcheck binary missing (mocked FileNotFoundError) raises
      RuntimeError naming the remediation
  (f) params["repo_root"] takes priority over the injected repo_root kwarg
  (g) injected repo_root kwarg is used when params carries none
  (h) idempotency (AC7): two back-to-back invocations against an unchanged
      repo return byte-identical results

Spec backlink: pln-coordinator-ops-buildout-from--903224
               § Wave 2 — Low-risk new modules, "run" cluster.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.run_shellcheck_sweep  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.run_shellcheck_sweep import (
    _run_shellcheck_sweep,
    run_shellcheck_sweep,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_OP_NAME = "ci.run_shellcheck_sweep"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.run_shellcheck_sweep @register_op did not fire"
)


def _init_repo(base: Path) -> Path:
    """git-init a throwaway repo under `base` and return its root."""
    repo = base / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, capture_output=True)
    return repo


def _track_file(repo: Path, rel_path: str, content: str) -> Path:
    """Write `content` to `repo/rel_path` and `git add` it (staged is enough
    for `git ls-files`; no commit required).
    """
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    subprocess.run(["git", "add", rel_path], cwd=repo, capture_output=True, check=True)
    return path


def _mock_shellcheck_run(findings_by_content, call_counter=None):
    """Build a `subprocess.run` stand-in that intercepts only shellcheck
    invocations (argv[0] == "shellcheck"). Batched call shape: one shellcheck
    invocation names every temp file in a single argv tail
    (`shellcheck ... f1 f2 ... fN`) — this stand-in reads EACH trailing temp
    file, calls `findings_by_content` per file's content, stamps each
    resulting finding's `file` field with that file's own temp path (mirrors
    real shellcheck's `-f json` per-finding `file` field), and returns the
    UNION as one JSON stdout blob — so a test asserting on exactly what
    content shellcheck was shown (e.g. proving CRLF was stripped first)
    keeps working unchanged whether one file or many are batched together.
    Any other argv (git) is passed through to the real subprocess.run.
    `call_counter`, if given, is incremented once per shellcheck invocation
    (never once per file) — the process-count-does-not-grow-with-N pin.
    """
    real_run = subprocess.run

    def _fake_run(argv, *args, **kwargs):
        if argv[0] == "shellcheck":
            if call_counter is not None:
                call_counter["shellcheck"] = call_counter.get("shellcheck", 0) + 1
            tmp_files = [Path(a) for a in argv if a.endswith(".sh")]
            all_findings = []
            for tmp_file in tmp_files:
                content = tmp_file.read_text(encoding="utf-8")
                for finding in findings_by_content(content):
                    finding = dict(finding)
                    finding["file"] = str(tmp_file)
                    all_findings.append(finding)
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(all_findings), stderr=""
            )
        return real_run(argv, *args, **kwargs)

    return _fake_run


def test_op_registered():
    assert _OP_NAME in _REGISTRY


def test_no_tracked_sh_files_returns_empty(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(subprocess, "run", _mock_shellcheck_run(lambda c: []))
    result = run_shellcheck_sweep(repo)
    assert result == {"findings": [], "files_checked": 0}


def test_happy_path_rewrites_file_field_to_repo_relative_path(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _track_file(repo, "scripts/build.sh", "#!/bin/bash\necho $1\n")

    def _findings(content):
        return [
            {
                "file": "/tmp/some-temp-copy.sh",
                "line": 2,
                "column": 6,
                "level": "warning",
                "code": "SC2086",
                "message": "Double quote to prevent globbing and word splitting.",
            }
        ]

    monkeypatch.setattr(subprocess, "run", _mock_shellcheck_run(_findings))
    result = run_shellcheck_sweep(repo)

    assert result["files_checked"] == 1
    assert len(result["findings"]) == 1
    finding = result["findings"][0]
    assert finding["file"] == "scripts/build.sh"
    assert finding["code"] == "SC2086"
    assert finding["line"] == 2


def test_crlf_is_normalized_before_shellcheck_sees_it(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _track_file(repo, "windows.sh", "#!/bin/bash\r\necho hi\r\n")

    seen_content = {}

    def _findings(content):
        seen_content["value"] = content
        return []

    monkeypatch.setattr(subprocess, "run", _mock_shellcheck_run(_findings))
    run_shellcheck_sweep(repo)

    assert "\r" not in seen_content["value"]
    assert seen_content["value"] == "#!/bin/bash\necho hi\n"


def test_shellcheck_binary_missing_raises_runtime_error(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _track_file(repo, "a.sh", "#!/bin/bash\necho hi\n")

    real_run = subprocess.run

    def _fake_run(argv, *args, **kwargs):
        if argv[0] == "shellcheck":
            raise FileNotFoundError("no such file: shellcheck")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError, match="shellcheck"):
        run_shellcheck_sweep(repo)


def test_params_repo_root_takes_priority_over_injected(tmp_path, monkeypatch):
    repo_a = _init_repo(tmp_path / "a")
    _track_file(repo_a, "one.sh", "#!/bin/bash\necho a\n")
    repo_b = _init_repo(tmp_path / "b")
    _track_file(repo_b, "two.sh", "#!/bin/bash\necho b\n")
    _track_file(repo_b, "three.sh", "#!/bin/bash\necho c\n")

    monkeypatch.setattr(subprocess, "run", _mock_shellcheck_run(lambda c: []))

    result = _run_shellcheck_sweep({"repo_root": str(repo_a)}, repo_root=repo_b)
    assert result["files_checked"] == 1


def test_injected_repo_root_used_when_params_empty(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _track_file(repo, "only.sh", "#!/bin/bash\necho hi\n")

    monkeypatch.setattr(subprocess, "run", _mock_shellcheck_run(lambda c: []))

    result = _run_shellcheck_sweep({}, repo_root=repo)
    assert result["files_checked"] == 1


def test_process_count_does_not_grow_with_the_set(tmp_path, monkeypatch):
    """Pin: N tracked .sh files -> exactly ONE `shellcheck` spawn, not N.
    Model: test_schema_drift_watch.py::TestSchemaAdvisoryBatch::
    test_process_count_does_not_grow_with_the_set.
    """
    repo = _init_repo(tmp_path)
    for i in range(5):
        _track_file(repo, f"scripts/s{i}.sh", f"#!/bin/bash\necho {i}\n")

    call_counter: dict = {}
    monkeypatch.setattr(
        subprocess, "run", _mock_shellcheck_run(lambda c: [], call_counter=call_counter)
    )

    result = run_shellcheck_sweep(repo)
    assert result["files_checked"] == 5
    assert call_counter.get("shellcheck", 0) == 1


def test_double_invocation_is_idempotent(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _track_file(repo, "stable.sh", "#!/bin/bash\necho hi\n")

    def _findings(content):
        return [
            {
                "file": "irrelevant.sh",
                "line": 1,
                "column": 1,
                "level": "info",
                "code": "SC2148",
                "message": "Add shebang.",
            }
        ]

    monkeypatch.setattr(subprocess, "run", _mock_shellcheck_run(_findings))

    first = run_shellcheck_sweep(repo)
    second = run_shellcheck_sweep(repo)
    assert first == second
