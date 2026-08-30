"""
coordinator_core.test_pickup_assemble_stamp_check — co-located pytest for
coordinator_core.pickup_assemble.stamp_check.

Pins the `stamp-check` CLI verb (chunk C3a) against a fixture plan: asserts
the emitted gate matches `compute_execution_stamp_match`'s own return, and
that `main()`'s dispatch table routes `stamp-check` to it.

Run: python -m pytest coordinator_core/test_pickup_assemble_stamp_check.py -q
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import coordinator_core.pickup_assemble as pa
from coordinator_core.pickup_assemble.stamp_check import stamp_check
from coordinator_core.win_portability import no_console_creationflags

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _canonical_body_sha(repo: Path, text: str) -> str:
    fm_count = 0
    out_lines: list[str] = []
    for line in text.splitlines():
        if line.rstrip(" \t") == "---":
            fm_count += 1
            continue
        if fm_count >= 2:
            out_lines.append(line + "\n")
    body = "".join(out_lines)
    result = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=str(repo),
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=15,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.decode("utf-8").strip()


def _seed_plan(repo: Path, name: str = "plan-a.md") -> tuple[Path, str]:
    path = repo / "docs" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# Plan\n\n## Summary\n\nreviewed body\n"
    unstamped = f'---\ntitle: "Test Plan"\ncreated: 2026-01-01\n---\n\n{body}'
    sha = _canonical_body_sha(repo, unstamped)
    fm = (
        'title: "Test Plan"\n'
        "created: 2026-01-01\n"
        'execution_authorized_by: "PM (Test)"\n'
        "execution_authorized_at: 2026-01-01\n"
        f"execution_authorized_sha: {sha}\n"
    )
    stamped_text = f"---\n{fm}---\n\n{body}"
    path.write_text(stamped_text, encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path, sha


class TestStampCheckWrapper:
    def test_stamp_check_matches_compute_execution_stamp_match(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path, expected_sha = _seed_plan(repo)
        rel = str(plan_path.relative_to(repo))

        fm = pa._parse_fm_dict(pa.split_frontmatter(plan_path.read_text(encoding="utf-8")).fm_text)
        direct_hit = pa.compute_execution_stamp_match(repo, fm, rel)
        assert direct_hit is not None
        expected_gate, _target = direct_hit

        exit_code, gate = stamp_check(rel, repo_root=repo)

        assert exit_code == pa.EXIT_OK
        assert gate == expected_gate
        assert gate["verdict"] == "match"
        assert gate["computed_sha"] == expected_sha
        assert gate["stamped_sha"] == expected_sha
        assert set(gate.keys()) == {
            "verdict",
            "stamped_sha",
            "computed_sha",
            "stamp_commit",
            "delta_class",
            "next_move",
        }

    def test_stamp_check_business_fail_on_unreadable_artifact(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        exit_code, gate = stamp_check("docs/plans/does-not-exist.md", repo_root=repo)

        assert exit_code == pa.EXIT_BUSINESS_FAIL
        assert "error" in gate

    def test_main_dispatch_routes_stamp_check(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path, expected_sha = _seed_plan(repo)
        rel = str(plan_path.relative_to(repo))

        monkeypatch.chdir(repo)
        exit_code = pa.main(["stamp-check", rel])
        captured = capsys.readouterr()

        assert exit_code == pa.EXIT_OK
        assert f'"computed_sha": "{expected_sha}"' in captured.out
        assert '"verdict": "match"' in captured.out
