"""
coordinator_core.ops.tests.test_backfill_deliverable_spine

Unit tests for coordinator_core.ops.backfill_deliverable_spine._stamp_yaml_document
(C5b whole-document sizing-YAML deliverable_id stamp).

Coverage — the four hazards `_stamp_yaml_document`'s own docstring (Review:
staff-eng — Finding 5(a-d)) names as closed by its rewrite, none of which had
a regression test before this module existed:
  (a) lock ordering       — the read-modify-write routes through
                             `locked_write.locked_rmw`'s cross-process flock;
                             a `LockTimeout` from that primitive surfaces as a
                             named skip, not a crash, and the target file is
                             left unchanged.
  (b) decode-failure       — an undecodable (non-UTF-8) file surfaces as a
      propagation            named skip via `UnicodeDecodeError`, not a
                              silently-persisted `errors="replace"` corruption.
  (c) insertion-point       — `deliverable_id:` is inserted after every
      robustness              leading blank/`#`-comment line, never
                               unconditionally at line index 1.
  (d) post-mutation          — the mutated document is parsed and
      schema validation        schema-validated before the write is allowed
                                to land; a value the vendored
                                `sizing-object.schema.json` rejects aborts the
                                stamp via `MutateAbort` rather than persisting
                                a broken document.

Reviewer finding backlink: state/subagent-share/06a8a386-7b86-47f8-b163-
8ad6593830a4/coordinatorcode-reviewer-fc183a78.md, Finding [P2] "zero test
coverage".

Per standing PM rule, these tests are WRITTEN but NOT RUN by this dispatch —
see the review-integrator's completion report for the invocation to run them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.backfill_deliverable_spine import _stamp_yaml_document

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with a state/sizings/ directory.

    Returns repo_root (the main worktree root) — the caller passes this
    directly as `_stamp_yaml_document`'s `repo_root` (it forwards to
    `locked_rmw`, which resolves the git common dir from any path inside the
    repo, worktree root included).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "backfill-test@claude-klabauter.test")
    _git("config", "user.name", "Backfill Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "sizings" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


def _sizing_body(
    *,
    status: str = "routed",
    deliverable_id: str | None = None,
    leading: str = "",
) -> str:
    """A schema-valid whole-document sizing-object YAML body (1.8.0).

    `leading`, if given, is prepended verbatim (comment/blank lines) before
    the `schema:` line — exercises the leading-comment-aware insertion point.
    """
    lines = [
        "schema: sizing-object",
        "intent: Test intent, verbatim.",
        "estimate:",
        "  tshirt: M",
        "  provisional: true",
        "route: plan",
        "detents: []",
        "fork: null",
        "xl_exit: null",
        f"status: {status}",
        "premise:",
        "  provenance: read",
        "  evidence: test fixture, no real premise verified",
    ]
    if deliverable_id is not None:
        lines.append(f"deliverable_id: {deliverable_id}")
    body = "\n".join(lines) + "\n"
    return leading + body


def _seed_sizing(repo: Path, name: str, text: str) -> Path:
    path = repo / "state" / "sizings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (a) lock ordering — LockTimeout surfaces as a named skip, file untouched
# ---------------------------------------------------------------------------


def test_lock_timeout_is_named_skip_and_leaves_file_unchanged(tmp_path, monkeypatch, capsys):
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(repo, "20260101-a.yaml", _sizing_body())
    original = sizing.read_text(encoding="utf-8")

    def _raise_lock_timeout(*_args, **_kwargs):
        raise LockTimeout("Could not acquire coordinator lock within 10s")

    # `_stamp_yaml_document` imports `locked_rmw` function-locally from
    # `coordinator_core.locked_write` on every call — patching the module
    # attribute here is visible to that fresh import.
    monkeypatch.setattr("coordinator_core.locked_write.locked_rmw", _raise_lock_timeout)

    _stamp_yaml_document(str(sizing), "dlv-test-000000", str(repo))

    captured = capsys.readouterr()
    assert "timed out waiting for file lock" in captured.err

    assert sizing.read_text(encoding="utf-8") == original, (
        "LockTimeout must leave the target file byte-identical — no read, "
        "no mutate, no write occurred"
    )


# ---------------------------------------------------------------------------
# (b) decode-failure propagation — undecodable file is a named skip
# ---------------------------------------------------------------------------


def test_undecodable_file_is_named_skip_and_leaves_file_unchanged(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    sizing = repo / "state" / "sizings" / "20260101-b.yaml"
    sizing.parent.mkdir(parents=True, exist_ok=True)
    # Invalid UTF-8 byte sequence — real bytes, not mocked, so this exercises
    # locked_rmw's own `read_text(encoding="utf-8")` read leg genuinely
    # raising UnicodeDecodeError, propagating through `_stamp_yaml_document`
    # rather than being silently persisted as U+FFFD replacement characters.
    bad_bytes = b"schema: sizing-object\n\xff\xfe not valid utf-8\n"
    sizing.write_bytes(bad_bytes)

    _stamp_yaml_document(str(sizing), "dlv-test-000000", str(repo))

    captured = capsys.readouterr()
    assert "undecodable file" in captured.err

    assert sizing.read_bytes() == bad_bytes, (
        "an undecodable file must be left byte-identical — no write attempted"
    )


# ---------------------------------------------------------------------------
# (c) insertion-point robustness — leading comment/blank lines are respected
# ---------------------------------------------------------------------------


def test_insertion_point_skips_leading_comment_and_blank_lines(tmp_path):
    repo = _make_git_repo(tmp_path)
    leading = "# a hand-authored comment\n# a second comment line\n\n"
    sizing = _seed_sizing(repo, "20260101-c.yaml", _sizing_body(leading=leading))

    _stamp_yaml_document(str(sizing), "dlv-test-000000", str(repo))

    new_lines = sizing.read_text(encoding="utf-8").splitlines()
    assert new_lines[0] == "# a hand-authored comment"
    assert new_lines[1] == "# a second comment line"
    assert new_lines[2] == ""
    assert new_lines[3] == "deliverable_id: dlv-test-000000", (
        "deliverable_id: must land immediately after the leading "
        f"blank/comment block, not at a fixed line index; got {new_lines[:5]!r}"
    )
    assert new_lines[4] == "schema: sizing-object"


def test_insertion_point_at_start_when_no_leading_comments(tmp_path):
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(repo, "20260101-d.yaml", _sizing_body())

    _stamp_yaml_document(str(sizing), "dlv-test-000000", str(repo))

    new_lines = sizing.read_text(encoding="utf-8").splitlines()
    assert new_lines[0] == "deliverable_id: dlv-test-000000"
    assert new_lines[1] == "schema: sizing-object"


def test_pre_existing_deliverable_id_is_dropped_before_reinsertion(tmp_path):
    """Idempotency-guard parity (should be unreachable given the caller's own
    exclusion, kept here for defense — see the function's own docstring)."""
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(
        repo, "20260101-e.yaml", _sizing_body(deliverable_id="dlv-old-000000")
    )

    _stamp_yaml_document(str(sizing), "dlv-new-000000", str(repo))

    text = sizing.read_text(encoding="utf-8")
    assert text.count("deliverable_id:") == 1
    assert "dlv-new-000000" in text
    assert "dlv-old-000000" not in text


# ---------------------------------------------------------------------------
# (d) post-mutation schema validation — an invalid document aborts the write
# ---------------------------------------------------------------------------


def test_schema_invalid_deliverable_id_aborts_and_leaves_file_unchanged(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(repo, "20260101-f.yaml", _sizing_body())
    original = sizing.read_text(encoding="utf-8")

    # Does not match the schema's `^dlv-(?!placeholder-replace-with)...`
    # pattern for `deliverable_id` — the post-mutation validate_frontmatter
    # call must reject this and MutateAbort before the write lands.
    _stamp_yaml_document(str(sizing), "not-a-valid-deliverable-id", str(repo))

    captured = capsys.readouterr()
    assert "schema validation failed" in captured.err

    assert sizing.read_text(encoding="utf-8") == original, (
        "a schema-invalid mutation must never be written — MutateAbort keeps "
        "the file byte-identical"
    )


def test_valid_stamp_passes_schema_validation_and_writes(tmp_path):
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(repo, "20260101-g.yaml", _sizing_body())

    _stamp_yaml_document(str(sizing), "dlv-test-000000", str(repo))

    text = sizing.read_text(encoding="utf-8")
    assert "deliverable_id: dlv-test-000000" in text


# ---------------------------------------------------------------------------
# File-not-found — pre-existing, unaffected by the rewrite, kept for parity
# ---------------------------------------------------------------------------


def test_file_not_found_is_named_skip(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    missing = repo / "state" / "sizings" / "does-not-exist.yaml"

    _stamp_yaml_document(str(missing), "dlv-test-000000", str(repo))

    captured = capsys.readouterr()
    assert "file not found" in captured.err
