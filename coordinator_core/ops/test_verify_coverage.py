"""
coordinator_core.ops.test_verify_coverage

Independent parity tests for coordinator_core.ops.verify_coverage, the port of
Example-doctrine-repo's retired bash/JS coverage-sweep oracle. Each test builds its own
scaffolded fixture tree and asserts the surfaced/silenced orphan categories
directly against the ported functions -- these do NOT re-derive the port's own
transcription, they independently reconstruct the expected verdict from the
oracle's documented invariants (SKILL/AGENT/COMMAND/WORKER_ORPHANED) and check
the port produces it.

Run: cd claude-klabauter && python3 -m pytest coordinator_core/ops/test_verify_coverage.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from coordinator_core.ops import verify_coverage as vc


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _scaffold(tmp_path: Path, spec: dict, files: dict | None = None) -> Path:
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    for plugin, kinds in spec.items():
        for skill in kinds.get("skills", []):
            _write(root / plugin / "skills" / skill / "SKILL.md", f"# {skill}\n")
        for agent in kinds.get("agents", []):
            _write(root / plugin / "agents" / f"{agent}.md", f"# {agent}\n")
        for cmd in kinds.get("commands", []):
            _write(root / plugin / "commands" / f"{cmd}.md", f"# {cmd}\n")
    for rel_path, content in (files or {}).items():
        _write(root / rel_path, content)
    return root


def _run(root: Path, sweep_root: Path | None = None, extra: list[str] | None = None) -> dict:
    argv = ["--root", str(root), "--json"]
    if sweep_root is not None:
        argv += ["--sweep-root", str(sweep_root)]
    argv += extra or []
    out = []
    old_stdout = sys.stdout

    class _Capture:
        def write(self, s):
            out.append(s)

        def flush(self):
            pass

    sys.stdout = _Capture()
    try:
        code = vc.main(argv)
    finally:
        sys.stdout = old_stdout
    payload = "".join(out)
    return {"exit": code, "json": json.loads(payload) if payload.strip() else None}


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def test_empty_tree_zero_violations(tmp_path):
    root = _scaffold(tmp_path, {"coordinator": {}})
    r = _run(root, root)
    assert r["exit"] == 0
    assert r["json"]["summary"]["violations"] == 0
    assert r["json"]["ok"] is True


def test_all_refs_resolve(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"], "agents": ["staff-eng"], "commands": ["workday-start"]}},
        {
            "coordinator/skills/plan/SKILL.md": "# plan\n\nSee `coordinator:staff-eng` for review.\n",
            "coordinator/agents/staff-eng.md": "# staff-eng\n\nInvoke via `coordinator:plan` and `/coordinator:workday-start`.\n",
        },
    )
    r = _run(root, root)
    assert r["exit"] == 0, r["json"]
    assert r["json"]["summary"]["violations"] == 0


# ---------------------------------------------------------------------------
# QUALIFIED_ORPHANED
# ---------------------------------------------------------------------------


def test_qualified_orphan_typo(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {"coordinator/skills/plan/SKILL.md": "# plan\n\nSee `coordinator:plann` for typo.\n"},
    )
    r = _run(root, root)
    assert r["exit"] == 1
    assert r["json"]["summary"]["violations"] == 1
    assert r["json"]["violations"][0]["kind"] == "qualified"
    assert r["json"]["violations"][0]["ref"] == "coordinator:plann"


def test_external_plugin_prefix_skipped(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {
            "coordinator/skills/plan/SKILL.md": (
                "# plan\n\nDispatch `example-game-repo-control:ue-asset-author`. "
                "See `superpowers:writing-plans` too.\n"
            )
        },
    )
    r = _run(root, root)
    assert r["exit"] == 0
    assert r["json"]["summary"]["violations"] == 0


# ---------------------------------------------------------------------------
# SUBAGENT_ORPHANED
# ---------------------------------------------------------------------------


def test_subagent_bare_name_resolves_across_plugins(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}, "game-dev": {"agents": ["staff-game-dev"]}},
        {"coordinator/skills/plan/SKILL.md": '# plan\n\nDispatch with `subagent_type: "staff-game-dev"`.\n'},
    )
    r = _run(root, root)
    assert r["exit"] == 0, r["json"]


def test_subagent_typo_flagged(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"], "agents": ["staff-eng"]}},
        {"coordinator/skills/plan/SKILL.md": '# plan\n\nDispatch with `subagent_type: "staf-eng"`.\n'},
    )
    r = _run(root, root)
    assert r["exit"] == 1
    found = [v for v in r["json"]["violations"] if v["kind"] == "subagent" and v["ref"] == "staf-eng"]
    assert found, r["json"]["violations"]


def test_subagent_builtin_harness_type_resolves(tmp_path):
    # Regression: BUILTIN_AGENT_TYPES (general-purpose/Explore/Plan/statusline-setup)
    # have no on-disk artifact by construction (harness-provided, not repo-authored)
    # and previously fell through resolve()'s bare-name agents lookup as orphans,
    # halting /update-docs in every consumer repo. See cross-repo/inbox/2026-08-06-
    # example-retrieval-repo-em-verify-coverage-false-positive-orphans.md item 2.
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {
            "coordinator/skills/plan/SKILL.md": (
                "# plan\n\nDispatch with `subagent_type: general-purpose`.\n"
            )
        },
    )
    r = _run(root, root)
    assert r["exit"] == 0, r["json"]


def test_subagent_template_placeholder_not_flagged(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["session-start"]}},
        {
            "coordinator/skills/session-start/SKILL.md": (
                "# session-start\n\nExample: `Agent(subagent_type='example-game-repo-control:ue-{domain}')`.\n"
            )
        },
    )
    r = _run(root, root)
    assert r["exit"] == 0, r["json"]


# ---------------------------------------------------------------------------
# COMMAND_ORPHANED
# ---------------------------------------------------------------------------


def test_command_qualified_resolves(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"commands": ["workday-start"], "skills": ["plan"]}},
        {"coordinator/skills/plan/SKILL.md": "# plan\n\nRun /coordinator:workday-start to start.\n"},
    )
    r = _run(root, root)
    assert r["exit"] == 0


def test_command_bare_resolves_via_fallback(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"commands": ["workday-start"], "skills": ["plan"]}},
        {"coordinator/skills/plan/SKILL.md": "# plan\n\nRun `/coordinator:workday-start` to start.\n"},
    )
    r = _run(root, root)
    assert r["exit"] == 0


# ---------------------------------------------------------------------------
# WORKER_ORPHANED
# ---------------------------------------------------------------------------


def test_worker_bullet_resolves(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"agents": ["staff-eng", "test-evidence-parser", "security-audit-worker"]}},
        {
            "coordinator/agents/staff-eng.md": (
                "# staff-eng\n\nSome prose.\n\n## Worker Dispatch Recommendations\n\n"
                "- test-evidence-parser — parse vitest output\n- security-audit-worker — scan diff\n"
            )
        },
    )
    r = _run(root, root)
    assert r["exit"] == 0, r["json"]


def test_worker_typo_flagged(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"agents": ["staff-eng", "test-evidence-parser"]}},
        {
            "coordinator/agents/staff-eng.md": (
                "# staff-eng\n\n## Worker Dispatch Recommendations\n\n- test-evidence-pareser — typo\n"
            )
        },
    )
    r = _run(root, root)
    assert r["exit"] == 1
    found = [v for v in r["json"]["violations"] if v["kind"] == "worker" and v["ref"] == "test-evidence-pareser"]
    assert found, r["json"]["violations"]


def test_worker_block_ends_at_next_heading(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"agents": ["staff-eng"]}},
        {
            "coordinator/agents/staff-eng.md": (
                "# staff-eng\n\n## Worker Dispatch Recommendations\n\n- test-evidence-parser — ok\n\n"
                "## Other Section\n\n- not-a-real-worker — must NOT be flagged here\n"
            )
        },
    )
    r = _run(root, root)
    worker_orphans = [v for v in r["json"]["violations"] if v["kind"] == "worker"]
    assert len(worker_orphans) == 1, worker_orphans
    assert worker_orphans[0]["ref"] == "test-evidence-parser"


# ---------------------------------------------------------------------------
# Code-fence / frontmatter stripping
# ---------------------------------------------------------------------------


def test_code_fences_stripped(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {
            "coordinator/skills/plan/SKILL.md": (
                "# plan\n\nExample:\n\n```\ncoordinator:not-a-real-skill\n```\n"
            )
        },
    )
    r = _run(root, root)
    assert r["exit"] == 0, r["json"]


def test_frontmatter_stripped(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {"coordinator/skills/plan/SKILL.md": "---\nname: coordinator:plan\ndescription: anything\n---\n\n# plan\n"},
    )
    r = _run(root, root)
    assert r["exit"] == 0


# ---------------------------------------------------------------------------
# Excluded sweep directories -- dist/review-trail/archive/vendor
# ---------------------------------------------------------------------------


def test_vendor_dir_excluded_from_sweep(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {
            "coordinator/skills/plan/SKILL.md": "# plan\n\nAll good here.\n",
            "vendor/epic-docs/types.md": (
                "# Types\n\nA vendored struct field `state:int32` mis-parses as a "
                "bogus dispatch ref `coordinator:ghost-thing`.\n"
            ),
        },
    )
    r = _run(root, root)
    assert r["exit"] == 0, r["json"]
    assert r["json"]["summary"]["violations"] == 0


def test_non_excluded_dir_with_same_bogus_ref_is_flagged(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {
            "coordinator/skills/plan/SKILL.md": "# plan\n\nAll good here.\n",
            "corpus/epic-docs/types.md": (
                "# Types\n\nA vendored struct field `state:int32` mis-parses as a "
                "bogus dispatch ref `coordinator:ghost-thing`.\n"
            ),
        },
    )
    r = _run(root, root)
    assert r["exit"] == 1, r["json"]
    assert r["json"]["summary"]["violations"] == 1
    assert r["json"]["violations"][0]["ref"] == "coordinator:ghost-thing"


# ---------------------------------------------------------------------------
# default_root() -- sentinel read and OSS fallback
# ---------------------------------------------------------------------------


def test_default_root_uses_sentinel_when_present(tmp_path):
    tmp_home = tmp_path / "home"
    sentinel_dir = tmp_home / ".claude"
    sentinel_dir.mkdir(parents=True)
    fake_doe_root = "/fake/doe/clone/root"
    (sentinel_dir / ".doe-root").write_text(fake_doe_root + "\n", encoding="utf-8")
    result = vc.default_root(str(tmp_home))
    assert result == fake_doe_root


def test_default_root_falls_back_when_sentinel_absent(tmp_path):
    tmp_home = tmp_path / "home2"
    tmp_home.mkdir(parents=True)
    result = vc.default_root(str(tmp_home))
    expected = os.path.join(str(tmp_home), ".claude", "plugins", "coordinator-claude")
    assert result == expected


# ---------------------------------------------------------------------------
# Exit-code contract (A3): usage/config errors are 2, orphans (non-report-only) are 1
# ---------------------------------------------------------------------------


def test_missing_root_is_usage_error_exit_2(tmp_path):
    code = vc.main(["--root", str(tmp_path / "does-not-exist")])
    assert code == 2


def test_unknown_flag_is_usage_error_exit_2():
    code = vc.main(["--not-a-real-flag"])
    assert code == 2


def test_report_only_forces_exit_0_despite_violations(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {"coordinator/skills/plan/SKILL.md": "# plan\n\nSee `coordinator:plann` for typo.\n"},
    )
    r = _run(root, root, extra=["--report-only"])
    assert r["exit"] == 0
    assert r["json"]["summary"]["violations"] == 1


# ---------------------------------------------------------------------------
# Silent-success regression: an unscannable subtree/file must NOT report clean
# (state/audits/2026-07-22-silent-success-audit.md sites 1-2)
# ---------------------------------------------------------------------------


def test_unreadable_subdirectory_fails_gate_not_silently_clean(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {"coordinator/skills/plan/SKILL.md": "# plan\n"},
    )
    locked_dir = root / "locked"
    locked_dir.mkdir()
    (locked_dir / "hidden.md").write_text("# hidden\n", encoding="utf-8")
    os.chmod(locked_dir, 0o000)
    try:
        r = _run(root, root)
    finally:
        os.chmod(locked_dir, 0o755)
    # No orphan references exist among the readable files, but the scan of
    # `locked/` failed outright -- the gate must not report "ok": true.
    assert r["json"]["summary"]["violations"] == 0
    assert r["json"]["scanIncomplete"] is True
    assert r["json"]["ok"] is False
    assert any("locked" in e for e in r["json"]["scanErrors"])
    assert r["exit"] == 1


def test_undecodable_file_fails_gate_not_silently_clean(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {"coordinator/skills/plan/SKILL.md": "# plan\n"},
    )
    bad_file = root / "bad-encoding.md"
    # Invalid UTF-8 byte sequence -- fails decode as text, not as an OSError.
    bad_file.write_bytes(b"\xff\xfe\x00# not valid utf-8\x80\x81")
    r = _run(root, root)
    assert r["json"]["summary"]["violations"] == 0
    assert r["json"]["scanIncomplete"] is True
    assert r["json"]["ok"] is False
    assert any("bad-encoding.md" in e for e in r["json"]["scanErrors"])
    assert r["exit"] == 1


def test_scan_incomplete_still_exit_0_under_report_only(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {"coordinator/skills/plan/SKILL.md": "# plan\n"},
    )
    bad_file = root / "bad-encoding.md"
    bad_file.write_bytes(b"\xff\xfe\x00bad\x80")
    r = _run(root, root, extra=["--report-only"])
    # report_only suppresses the non-zero exit, but the diagnostic must still
    # surface -- report_only means "don't fail CI", not "hide the gap".
    assert r["exit"] == 0
    assert r["json"]["scanIncomplete"] is True
    assert r["json"]["ok"] is False


def test_files_scanned_count_excludes_unread_candidates(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"]}},
        {"coordinator/skills/plan/SKILL.md": "# plan\n"},
    )
    bad_file = root / "bad-encoding.md"
    bad_file.write_bytes(b"\xff\xfe\x00bad\x80")
    r = _run(root, root)
    # 1 SKILL.md actually read; bad-encoding.md is a candidate that was never
    # successfully read and must not inflate the scanned count.
    assert r["json"]["summary"]["filesScanned"] == 1


# ---------------------------------------------------------------------------
# Independent oracle-parity check -- re-derive the same fixture's expected
# violation set from first principles (not by re-reading verify_coverage.py's
# own logic) and confirm the port matches.
#
# The former JS-oracle byte-for-byte cross-check
# (test_json_output_matches_js_oracle_byte_for_byte) is retired: the recovered
# JS oracle (last independent at example-doctrine-repo `93887f6f^`) diverges from the native op
# at HEAD on `scanIncomplete`/`scanErrors`/`filesScanned` -- fields the native
# op legitimately added after the JS oracle was retired. The byte-parity
# assertion was already false, not merely
# skipped; retiring it removes a test that could never pass again. Its
# successor is `test_json_envelope_top_level_keys_are_stable` below (an exact
# key-set schema assertion, honest about what it checks) plus this test (a
# from-first-principles violation-set reconstruction). See
# state/review-trail/findings/2026-07-22-parity-retire-fold-plan.md § 4.4.
# ---------------------------------------------------------------------------


def test_independent_reconstruction_of_expected_violations(tmp_path):
    # Two skills, one referenced correctly, one orphaned; one agent referenced by
    # a typo'd subagent_type; a worker bullet naming a nonexistent agent.
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"], "agents": ["staff-eng"]}},
        {
            "coordinator/skills/plan/SKILL.md": (
                "# plan\n\nRef `coordinator:staff-eng` (ok), `coordinator:ghost-skill` (orphan), "
                "`subagent_type=\"ghost-agent\"` (orphan).\n\n"
                "## Worker Dispatch Recommendations\n\n- ghost-worker — orphan\n"
            )
        },
    )
    r = _run(root, root)
    refs_expected_orphan = {"coordinator:ghost-skill", "ghost-agent", "ghost-worker"}
    got = {v["ref"] for v in r["json"]["violations"]}
    assert got == refs_expected_orphan, got
    assert r["json"]["summary"]["violations"] == 3


def test_json_envelope_top_level_keys_are_stable(tmp_path):
    root = _scaffold(
        tmp_path,
        {"coordinator": {"skills": ["plan"], "agents": ["staff-eng"]}},
        {
            "coordinator/skills/plan/SKILL.md": (
                "# plan\n\nRef `coordinator:staff-eng` (ok), `coordinator:ghost-skill` (orphan).\n"
            )
        },
    )
    r = _run(root, root)
    assert set(r["json"].keys()) == {
        "ok",
        "scanIncomplete",
        "scanErrors",
        "root",
        "sweepRoot",
        "summary",
        "violations",
    }, r["json"].keys()
    assert set(r["json"]["summary"].keys()) == {
        "skills",
        "agents",
        "commands",
        "filesScanned",
        "violations",
    }, r["json"]["summary"].keys()


# ---------------------------------------------------------------------------
# Marker-vocabulary discriminator -- fence/sentinel/marker/block prose
# (cross-repo memo 2026-08-06-example-doctrine-repo-em-verify-coverage-extractor-
# marker-vocabulary.md)
# ---------------------------------------------------------------------------


def test_undocumented_marker_token_in_fence_prose_not_flagged(tmp_path):
    # `coordinator:totally-undocumented-marker` is NEVER listed anywhere
    # (not REF_ALLOWLIST, not the two real examples from the sender) -- this
    # proves the discriminator is shape-based (same-line marker noun), not a
    # hardcoded token list.
    root = _scaffold(
        tmp_path,
        {"coordinator": {}},
        {
            "coordinator/docs/wiki/some-doc.md": (
                "# doc\n\nThis needs a `coordinator:totally-undocumented-marker` "
                "fence to stop early.\n"
            )
        },
    )
    r = _run(root, root)
    assert r["exit"] == 0, r["json"]
    assert r["json"]["violations"] == []


def test_dispatch_reference_with_distant_marker_noun_still_flagged(tmp_path):
    """Regression pin for coordinator-code-reviewer bd2f004c's P2 finding: a
    genuine dispatch reference must not be dropped merely because its line
    ALSO mentions a marker noun far away in the prose -- only a marker noun
    immediately trailing the ref (the shape both real marker-documentation
    examples share) suppresses it."""
    root = _scaffold(
        tmp_path,
        {"coordinator": {}},
        {
            "coordinator/docs/wiki/some-doc.md": (
                "# doc\n\nDispatch `coordinator:totally-undocumented-worker` "
                "to check the marker file before proceeding.\n"
            )
        },
    )
    r = _run(root, root)
    assert r["exit"] == 1, r["json"]
    refs = {v["ref"] for v in r["json"]["violations"]}
    assert "coordinator:totally-undocumented-worker" in refs


def test_genuine_dispatch_reference_still_flagged(tmp_path):
    # Same token shape, same plugin prefix, but no marker noun anywhere on the
    # line -- a genuine orphaned dispatch reference must still be caught.
    root = _scaffold(
        tmp_path,
        {"coordinator": {}},
        {
            "coordinator/docs/wiki/some-doc.md": (
                "# doc\n\nDispatch `coordinator:totally-undocumented-worker` "
                "to do the work.\n"
            )
        },
    )
    r = _run(root, root)
    assert r["exit"] == 1, r["json"]
    refs = {v["ref"] for v in r["json"]["violations"]}
    assert "coordinator:totally-undocumented-worker" in refs
