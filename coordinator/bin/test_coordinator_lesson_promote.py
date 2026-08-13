"""
test_coordinator_lesson_promote.py — smoke tests for the coordinator-lesson-promote CLI.

Spec backlink: docs/plans/2026-06-15-universal-lesson-routing-mechanical-capture.md § C1

Tests:
  1. --help exits 0 with non-empty stdout.
  2. Missing --title → exit non-zero, stderr names the missing field.
  3. Missing --change-kind → exit non-zero, stderr names the missing field.
  4. Invalid --change-kind value → exit non-zero, stderr names valid enum values.
  5. Valid invocation → exit 0, exactly one YAML file produced in temp outbox,
     file parseable as YAML with all required schema fields present.
  6. Roundtrip: write entry, read YAML back, all field values match what was passed.
  7. Schema missing → fail loud, non-zero exit, stderr names the schema problem.
  8. Multi-line --body round-trips byte-exact (|- strip chomping).
  9. --target-wiki not in the central wiki inventory → exit 2, stderr offers
     closest-match suggestions (design-as-offers), nothing written.
  10. --allow-new-wiki bypasses the inventory check for a genuine new-wiki target.
  11. --target-wiki 'unknown' bypasses the inventory check (schema sentinel).
  12. --target-wiki normalization: bare name / .md-suffixed / already-canonical all
      collapse to the identical 'docs/wiki/<name>.md' string in the written YAML.
  13. Coordinator-claude root unresolvable during --target-wiki validation → exit 3,
      remediation naming `machine-local set repos.example_doctrine_repo`, nothing written.
  14. Coordinator-claude root unresolvable at write time (validation bypassed via
      'unknown') → exit 3 via the legacy_fn path, nothing written.
  15. change_kind: skill-edit with a SKILL.md --target-wiki round-trips
      byte-identically (no docs/wiki/ collapse, no inventory validation) and
      exits 0 — regression test for the A7/A9 non-wiki-change_kind defect.
  16. change_kind: wiki-append with a bogus --target-wiki still fails loud
      (exit 2, closest-match suggestions) — confirms wiki-targeting kinds
      keep their full validation after the change_kind gate was added.
  17. --change-kind wiki-append --allow-new-wiki → exit 2 at argparse time
      (mismatch rejected before the inventory check runs) — --allow-new-wiki
      is only sound for a genuine wiki-new promotion.

Run with: python3 -m pytest coordinator/bin/test_coordinator_lesson_promote.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

try:
    import yaml as _yaml  # PyYAML — available on most coordinator installs
    _YAML_AVAILABLE = True
except ImportError:
    _yaml = None  # type: ignore[assignment]
    _YAML_AVAILABLE = False

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _script_path() -> str:
    """Return the absolute path to coordinator-lesson-promote."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "coordinator-lesson-promote")


def _python() -> str:
    """Return the Python interpreter to use for subprocess invocations.

    Uses sys.executable — the interpreter running this test script is always a
    valid Python interpreter. This is the Windows-compatible zero-probe pattern
    (avoids FileNotFoundError from subprocess probing python3/python on Windows).
    """
    return sys.executable


def _run_cli(args: list[str], env: dict[str, str | None] | None = None) -> subprocess.CompletedProcess:
    """Invoke the CLI as a subprocess.

    Always drives via `python <script>` — the script has no .py extension and is
    not directly executable on Windows. Same pattern as test_cross_repo_memo.py.

    A None value in `env` UNSETS that key for the child process rather than
    setting it to the empty string — load-bearing for ambient vars like
    REPO_EXAMPLE_DOCTRINE_REPO (exported into every login shell per the install surface):
    an explicitly-empty-string override is NOT equivalent to "absent" for every
    downstream consumer (the real `machine-local` binary treats them
    differently), so a real absence must be a real dict-key removal.
    """
    effective_env = {**os.environ}
    if env:
        for key, value in env.items():
            if value is None:
                effective_env.pop(key, None)
            else:
                effective_env[key] = value
    return subprocess.run(
        [_python(), _script_path()] + args,
        env=effective_env,
        capture_output=True,
        text=True,
    )


def _wiki_root_with(tmpdir: str, *names: str) -> str:
    """Seed a synthetic central-wiki inventory directory with the given filenames.

    Returns the directory path, suitable for LESSON_PROMOTE_WIKI_ROOT — this is the
    A7-validation-isolation counterpart to LESSON_PROMOTE_OUTBOX_ROOT: it lets tests
    exercise --target-wiki validation against a known, disposable inventory instead
    of depending on a real coordinator-claude checkout being present on the test runner.
    """
    wiki_dir = os.path.join(tmpdir, "wiki-inventory")
    os.makedirs(wiki_dir, exist_ok=True)
    for name in names:
        with open(os.path.join(wiki_dir, name), "w", encoding="utf-8") as fh:
            fh.write("# stub\n")
    return wiki_dir


def _parse_yaml_file(path: str) -> dict:
    """Parse a YAML file. Falls back to a minimal line-parser if PyYAML unavailable."""
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        if _YAML_AVAILABLE:
            try:
                parsed = _yaml.safe_load(content)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        # Minimal fallback: parse simple key: value lines between --- delimiters.
        return _minimal_yaml_parse(content)
    except OSError as exc:
        raise RuntimeError(f"could not read YAML file {path}: {exc}") from exc


def _minimal_yaml_parse(content: str) -> dict:
    """Minimal YAML frontmatter parser for simple key: value lines.

    Handles:
    - Scalar values (quoted and unquoted)
    - Block scalars (|) — collects lines until next key or end marker
    Sufficient for the coordinator-lesson-promote output schema.
    """
    result: dict = {}
    lines = content.splitlines()
    i = 0
    # Skip opening ---
    while i < len(lines) and lines[i].strip() == "---":
        i += 1

    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            break
        if ":" in line and not line.startswith(" "):
            key, _, rest = line.partition(":")
            key = key.strip()
            value = rest.strip()
            if value in ("|", "|-"):
                # Block scalar — collect subsequent indented lines.
                # Review: code-reviewer Slice-B — (B-F8) extended to handle "|-" (strip
                # chomping) in addition to "|" (clip chomping). The .rstrip("\n") already
                # normalises both — behavior is identical for the test's verification needs.
                block_lines = []
                i += 1
                while i < len(lines) and (lines[i].startswith("  ") or lines[i].strip() == ""):
                    block_lines.append(lines[i][2:] if lines[i].startswith("  ") else "")
                    i += 1
                result[key] = "\n".join(block_lines).rstrip("\n")
                continue
            elif value.startswith('"'):
                # Quoted string — unescape basic escapes.
                inner = value[1:]
                if inner.endswith('"'):
                    inner = inner[:-1]
                result[key] = inner.replace('\\"', '"').replace("\\\\", "\\")
            else:
                result[key] = value
        i += 1
    return result


# ---------------------------------------------------------------------------
# Test 1 — --help exits 0 with non-empty stdout
# ---------------------------------------------------------------------------

def test_help_exits_zero() -> None:
    name = "Test 1 — --help exits 0 with non-empty stdout"
    result = _run_cli(["--help"])
    if result.returncode != 0:
        raise AssertionError(f"{name}: " + (f"--help exited {result.returncode}, expected 0"))
    if not result.stdout.strip():
        raise AssertionError(f"{name}: " + ("--help produced empty stdout"))


# ---------------------------------------------------------------------------
# Test 2 — Missing --title → exit non-zero, stderr names the missing field
# ---------------------------------------------------------------------------

def test_missing_title_fails() -> None:
    name = "Test 2 — missing --title → exit non-zero, stderr names field"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--body", "some body",
                "--change-kind", "doctrine-edit",
                "--target-wiki", "docs/wiki/some.md",
            ],
            env={"LESSON_PROMOTE_OUTBOX_ROOT": os.path.join(tmpdir, "state", "lessons-outbox")},
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for missing --title; got 0"))
    combined = result.stdout + result.stderr
    if "title" not in combined.lower():
        raise AssertionError(f"{name}: " + (f"error output does not name 'title'. stderr: {result.stderr!r}"))


# ---------------------------------------------------------------------------
# Test 3 — Missing --change-kind → exit non-zero, stderr names the missing field
# ---------------------------------------------------------------------------

def test_missing_change_kind_fails() -> None:
    name = "Test 3 — missing --change-kind → exit non-zero, stderr names field"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--title", "some title",
                "--body", "some body",
                "--target-wiki", "docs/wiki/some.md",
            ],
            env={"LESSON_PROMOTE_OUTBOX_ROOT": os.path.join(tmpdir, "state", "lessons-outbox")},
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for missing --change-kind; got 0"))
    combined = result.stdout + result.stderr
    if "change-kind" not in combined.lower() and "change_kind" not in combined.lower():
        raise AssertionError(f"{name}: " + (f"error output does not name 'change-kind'. stderr: {result.stderr!r}"))


# ---------------------------------------------------------------------------
# Test 4 — Invalid --change-kind value → exit non-zero, stderr names valid values
# ---------------------------------------------------------------------------

def test_invalid_change_kind_fails() -> None:
    name = "Test 4 — invalid --change-kind → exit non-zero, stderr names valid enum values"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--title", "some title",
                "--body", "some body",
                "--change-kind", "bogus",
                "--target-wiki", "docs/wiki/some.md",
            ],
            env={"LESSON_PROMOTE_OUTBOX_ROOT": os.path.join(tmpdir, "state", "lessons-outbox")},
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for invalid --change-kind; got 0"))
    combined = result.stdout + result.stderr
    # stderr should name at least one valid value to serve as a hint.
    if "doctrine-edit" not in combined:
        raise AssertionError(f"{name}: " + (f"error output should name valid enum values (e.g. 'doctrine-edit'). stderr: {result.stderr!r}"))


# ---------------------------------------------------------------------------
# Test 5 — Valid invocation produces exactly one YAML file with required fields
# ---------------------------------------------------------------------------

_REQUIRED_SCHEMA_FIELDS = ("id", "created", "from_repo", "title", "body", "change_kind", "target_wiki")


def test_valid_invocation_writes_yaml() -> None:
    name = "Test 5 — valid invocation → exit 0, one YAML file with required fields"
    with tempfile.TemporaryDirectory() as tmpdir:
        outbox = os.path.join(tmpdir, "state", "lessons-outbox")
        wiki_root = _wiki_root_with(tmpdir, "executor-discipline.md")
        result = _run_cli(
            [
                "--title", "Lesson about executor retries",
                "--body", "Executors should not retry after structural failure.",
                "--change-kind", "doctrine-edit",
                "--target-wiki", "docs/wiki/executor-discipline.md",
            ],
            env={
                "LESSON_PROMOTE_OUTBOX_ROOT": outbox,
                "LESSON_PROMOTE_WIKI_ROOT": wiki_root,
            },
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))

        # Exactly one YAML file must exist.
        if not os.path.isdir(outbox):
            raise AssertionError(f"{name}: " + (f"outbox directory not created: {outbox}"))

        yaml_files = [f for f in os.listdir(outbox) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected exactly 1 YAML file; found {len(yaml_files)}: {yaml_files}"))

        yaml_path = os.path.join(outbox, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))

        missing = [f for f in _REQUIRED_SCHEMA_FIELDS if f not in parsed]
        if missing:
            raise AssertionError(f"{name}: " + (f"YAML missing required fields: {missing}. Parsed: {parsed}"))


# ---------------------------------------------------------------------------
# Test 6 — Roundtrip: field values match what was passed
# ---------------------------------------------------------------------------

def test_roundtrip_field_values() -> None:
    name = "Test 6 — roundtrip: all passed field values preserved in YAML"
    title = "Roundtrip test lesson title"
    body = "This is the roundtrip lesson body prose."
    change_kind = "wiki-append"
    target_wiki = "docs/wiki/roundtrip-test.md"
    scope_tags = "executor,plan-authoring"
    evidence = "abc1234def5678"

    with tempfile.TemporaryDirectory() as tmpdir:
        outbox = os.path.join(tmpdir, "state", "lessons-outbox")
        wiki_root = _wiki_root_with(tmpdir, "roundtrip-test.md")
        result = _run_cli(
            [
                "--title", title,
                "--body", body,
                "--change-kind", change_kind,
                "--target-wiki", target_wiki,
                "--scope-tags", scope_tags,
                "--evidence", evidence,
            ],
            env={
                "LESSON_PROMOTE_OUTBOX_ROOT": outbox,
                "LESSON_PROMOTE_WIKI_ROOT": wiki_root,
            },
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))

        yaml_files = [f for f in os.listdir(outbox) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML file; got {len(yaml_files)}"))

        yaml_path = os.path.join(outbox, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))

        # Verify all passed fields round-trip exactly.
        checks = [
            ("title", title),
            ("body", body),
            ("change_kind", change_kind),
            ("target_wiki", target_wiki),
            ("evidence", evidence),
        ]
        for field, expected in checks:
            got = parsed.get(field)
            if got != expected:
                raise AssertionError(f"{name}: " + (f"field {field!r}: expected {expected!r}, got {got!r}"))

        # id must be a non-empty UUID-shaped string.
        entry_id = parsed.get("id", "")
        if not entry_id or len(entry_id) < 32:
            raise AssertionError(f"{name}: " + (f"id field looks wrong: {entry_id!r}"))

        # created must be a non-empty ISO timestamp.
        created = parsed.get("created", "")
        if not created or "T" not in created:
            raise AssertionError(f"{name}: " + (f"created field looks wrong: {created!r}"))

        # from_repo must be non-empty.
        from_repo = parsed.get("from_repo", "")
        if not from_repo:
            raise AssertionError(f"{name}: " + ("from_repo field is empty"))


# ---------------------------------------------------------------------------
# Test 7 — schema missing → fail loud with non-zero exit and remediation in stderr
# ---------------------------------------------------------------------------

def test_schema_missing_fails_loud() -> None:
    """Test 7 — bad CLAUDE_KLABAUTER_ROOT (native schema seam unreachable) causes fail-loud exit.

    Rewired (de-node cutover, 480ad8f8): schema-cli.js — which honored
    COORDINATOR_SCHEMAS_DIR — was deleted with no legacy fallback; its parity
    successor coordinator_core/frontmatter/schema_cli.py does NOT support that
    override (documented negative-spec, see coordinator/tests/test_schema_cli.py
    and coordinator_core/frontmatter/schema_cli.py:55) — so it can no longer force
    a schema-load failure. An unresolvable CLAUDE_KLABAUTER_ROOT is schema.describe's own
    fail-loud trigger instead (mirrors coordinator/tests/test_lesson_promote_node_enum.py
    ::test_schema_load_failure_fails_loud, the sibling rewire for this exact class
    of test in this CLI's other pytest home).
    """
    name = "Test 7 — bad CLAUDE_KLABAUTER_ROOT: native schema seam unreachable → exit non-zero, stderr names error"
    with tempfile.TemporaryDirectory() as tmpdir:
        bad_claude_klabauter_root = os.path.join(tmpdir, "not-a-real-claude-klabauter-checkout")
        outbox = os.path.join(tmpdir, "state", "lessons-outbox")
        result = _run_cli(
            [
                "--title", "some title",
                "--body", "some body",
                "--change-kind", "doctrine-edit",
                "--target-wiki", "docs/wiki/some.md",
            ],
            env={
                "CLAUDE_KLABAUTER_ROOT": bad_claude_klabauter_root,
                "LESSON_PROMOTE_OUTBOX_ROOT": outbox,
            },
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit when CLAUDE_KLABAUTER_ROOT has no coordinator_core; got 0"))
    combined = result.stdout + result.stderr
    if "schema" not in combined.lower():
        raise AssertionError(f"{name}: " + (f"error output does not mention 'schema'. stderr: {result.stderr!r}"))


# ---------------------------------------------------------------------------
# Test 8 — multi-line --body round-trip: |- header, no trailing newline
# ---------------------------------------------------------------------------

def test_multiline_body_roundtrip() -> None:
    """Review: code-reviewer Slice-B — (B-F8) multi-line --body must round-trip without a
    trailing newline. The _yaml_str fix changes | (clip chomping) to |- (strip chomping)
    so 'First line.\\nSecond line.' parses back as exactly that string with NO trailing
    newline added.
    """
    name = "Test 8 — multi-line --body roundtrip: |- header, exact bytes, no trailing newline"
    body_input = "First line.\nSecond line."

    with tempfile.TemporaryDirectory() as tmpdir:
        outbox = os.path.join(tmpdir, "state", "lessons-outbox")
        wiki_root = _wiki_root_with(tmpdir, "roundtrip-multiline.md")
        result = _run_cli(
            [
                "--title", "Multiline body roundtrip lesson",
                "--body", body_input,
                "--change-kind", "wiki-append",
                "--target-wiki", "docs/wiki/roundtrip-multiline.md",
            ],
            env={
                "LESSON_PROMOTE_OUTBOX_ROOT": outbox,
                "LESSON_PROMOTE_WIKI_ROOT": wiki_root,
            },
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))

        if not os.path.isdir(outbox):
            raise AssertionError(f"{name}: " + (f"outbox directory not created: {outbox}"))

        yaml_files = [f for f in os.listdir(outbox) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected exactly 1 YAML file; found {len(yaml_files)}: {yaml_files}"))

        yaml_path = os.path.join(outbox, yaml_files[0])

        # Check the raw YAML contains the |- header (strip chomping, not clip).
        with open(yaml_path, encoding="utf-8") as fh:
            raw = fh.read()
        if "body: |-" not in raw:
            raise AssertionError(f"{name}: " + (f"expected 'body: |-' (strip chomping) in raw YAML; got: {raw!r}"))

        # Parse and verify no trailing newline (byte-fidelity guarantee).
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))

        got_body = parsed.get("body", None)
        if got_body is None:
            raise AssertionError(f"{name}: " + ("body field missing from parsed YAML"))
        if got_body != body_input:
            raise AssertionError(f"{name}: " + (f"body roundtrip mismatch: expected {body_input!r}, got {got_body!r}. "
                f"(trailing newline indicates | clip chomping instead of |- strip chomping)"))


# ---------------------------------------------------------------------------
# Shared env-forcing helpers for A7/A13 tests
# ---------------------------------------------------------------------------

def _force_doe_unresolvable_env() -> dict[str, str | None]:
    """Env overrides that make doe_root() raise _DoeUnresolvable deterministically.

    UNSETS DOE_ROOT/REPO_EXAMPLE_DOCTRINE_REPO (None → _run_cli removes the key entirely,
    not just blanks it — REPO_EXAMPLE_DOCTRINE_REPO is exported into every login shell by
    this install surface, so it is present ambiently on a real dev machine; an
    empty-string override is NOT equivalent to absence here, since the real
    `machine-local` binary — used by coordinator_registry.py's own
    schema-manifest bootstrap at import time, a resolution independent of and
    unaffected by MACHINE_LOCAL_IMPL — treats an explicitly-empty env var
    differently from an absent one). Also points MACHINE_LOCAL_IMPL at a
    nonexistent script so the registry lookup coordinator_registry.doe_root()
    performs AT RUNTIME (distinct from the import-time bootstrap above) fails
    closed to None — independent of whatever repos.example_doctrine_repo happens to be
    registered on the machine actually running this test.
    """
    return {
        "DOE_ROOT": None,
        "REPO_EXAMPLE_DOCTRINE_REPO": None,
        "MACHINE_LOCAL_IMPL": "/nonexistent/coordinator-lesson-promote-test-probe.py",
    }


def _force_legacy_route_env(tmpdir: str) -> dict[str, str]:
    """CLAUDE_KLABAUTER_ROOT override pointing at a directory with no coordinator_core.invoke,
    forcing cc_invoke.route()'s State-1 (seam-absent) branch — the CLI's own
    legacy_fn write path — regardless of whether this claude-klabauter checkout is itself
    registered/importable on the machine running this test.
    """
    fake_root = os.path.join(tmpdir, "not-a-claude-klabauter-checkout")
    os.makedirs(fake_root, exist_ok=True)
    return {"CLAUDE_KLABAUTER_ROOT": fake_root}


# ---------------------------------------------------------------------------
# Test 9 — --target-wiki not in inventory → exit 2, offers closest matches
# ---------------------------------------------------------------------------

def test_target_wiki_not_in_inventory_fails() -> None:
    name = "Test 9 — --target-wiki not in inventory (wiki-new) → exit 2, stderr offers closest matches"
    with tempfile.TemporaryDirectory() as tmpdir:
        outbox = os.path.join(tmpdir, "state", "lessons-outbox")
        wiki_root = _wiki_root_with(tmpdir, "close-match.md", "another-wiki.md")
        result = _run_cli(
            [
                "--title", "some title",
                "--body", "some body",
                "--change-kind", "wiki-new",
                "--target-wiki", "clos-match",
            ],
            env={
                "LESSON_PROMOTE_OUTBOX_ROOT": outbox,
                "LESSON_PROMOTE_WIKI_ROOT": wiki_root,
            },
        )
    if result.returncode != 2:
        raise AssertionError(f"{name}: " + (f"expected exit 2; got {result.returncode}. stderr: {result.stderr!r}"))
    if "close-match.md" not in result.stderr:
        raise AssertionError(f"{name}: " + (f"stderr does not offer the closest match 'close-match.md': {result.stderr!r}"))
    if os.path.isdir(outbox) and os.listdir(outbox):
        raise AssertionError(f"{name}: " + ("outbox has entries but validation should have rejected before any write"))


# ---------------------------------------------------------------------------
# Test 10 — --allow-new-wiki bypasses the inventory check
# ---------------------------------------------------------------------------

def test_allow_new_wiki_bypasses_validation() -> None:
    name = "Test 10 — --allow-new-wiki bypasses inventory check for a genuine new-wiki target"
    with tempfile.TemporaryDirectory() as tmpdir:
        outbox = os.path.join(tmpdir, "state", "lessons-outbox")
        wiki_root = _wiki_root_with(tmpdir, "unrelated.md")
        result = _run_cli(
            [
                "--title", "Introduce a brand new wiki",
                "--body", "This lesson introduces a concept with no existing wiki home.",
                "--change-kind", "wiki-new",
                "--target-wiki", "docs/wiki/brand-new-wiki.md",
                "--allow-new-wiki",
            ],
            env={
                "LESSON_PROMOTE_OUTBOX_ROOT": outbox,
                "LESSON_PROMOTE_WIKI_ROOT": wiki_root,
            },
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
        yaml_files = [f for f in os.listdir(outbox) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected exactly 1 YAML file; found {len(yaml_files)}: {yaml_files}"))


# ---------------------------------------------------------------------------
# Test 11 — --target-wiki 'unknown' bypasses the inventory check
# ---------------------------------------------------------------------------

def test_target_wiki_unknown_bypasses_validation() -> None:
    name = "Test 11 — --target-wiki 'unknown' bypasses inventory check (schema sentinel)"
    with tempfile.TemporaryDirectory() as tmpdir:
        outbox = os.path.join(tmpdir, "state", "lessons-outbox")
        wiki_root = _wiki_root_with(tmpdir, "unrelated.md")
        result = _run_cli(
            [
                "--title", "Lesson with no resolved wiki target yet",
                "--body", "Classifier could not resolve a central-wiki target.",
                "--change-kind", "doctrine-edit",
                "--target-wiki", "unknown",
            ],
            env={
                "LESSON_PROMOTE_OUTBOX_ROOT": outbox,
                "LESSON_PROMOTE_WIKI_ROOT": wiki_root,
            },
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
        yaml_files = [f for f in os.listdir(outbox) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected exactly 1 YAML file; found {len(yaml_files)}: {yaml_files}"))
        yaml_path = os.path.join(outbox, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
        if parsed.get("target_wiki") != "unknown":
            raise AssertionError(f"{name}: " + (f"expected target_wiki 'unknown'; got {parsed.get('target_wiki')!r}"))


# ---------------------------------------------------------------------------
# Test 12 — target_wiki normalization: all equivalent input forms collapse
# ---------------------------------------------------------------------------

def test_target_wiki_normalization_collapses_equivalent_forms() -> None:
    name = "Test 12 — target_wiki normalization: bare/.md/canonical forms all collapse identically"
    variants = ["concurrent-em-hazards", "concurrent-em-hazards.md", "docs/wiki/concurrent-em-hazards.md"]
    expected = "docs/wiki/concurrent-em-hazards.md"
    for variant in variants:
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = os.path.join(tmpdir, "state", "lessons-outbox")
            wiki_root = _wiki_root_with(tmpdir, "concurrent-em-hazards.md")
            result = _run_cli(
                [
                    "--title", "Normalization probe",
                    "--body", "Probing target_wiki normalization.",
                    "--change-kind", "wiki-append",
                    "--target-wiki", variant,
                ],
                env={
                    "LESSON_PROMOTE_OUTBOX_ROOT": outbox,
                    "LESSON_PROMOTE_WIKI_ROOT": wiki_root,
                },
            )
            if result.returncode != 0:
                raise AssertionError(f"{name}: " + (f"variant {variant!r}: CLI exited {result.returncode}: {result.stderr!r}"))
            yaml_files = [f for f in os.listdir(outbox) if f.endswith(".yaml")]
            if len(yaml_files) != 1:
                raise AssertionError(f"{name}: " + (f"variant {variant!r}: expected 1 YAML file; got {len(yaml_files)}"))
            yaml_path = os.path.join(outbox, yaml_files[0])
            try:
                parsed = _parse_yaml_file(yaml_path)
            except RuntimeError as exc:
                raise AssertionError(f"{name}: " + (f"variant {variant!r}: YAML parse error: {exc}"))
            got = parsed.get("target_wiki")
            if got != expected:
                raise AssertionError(f"{name}: " + (f"variant {variant!r}: expected target_wiki {expected!r}; got {got!r}"))


# ---------------------------------------------------------------------------
# Test 13 — coordinator-claude root unresolvable during --target-wiki validation → exit 3
# ---------------------------------------------------------------------------

def test_doe_unresolvable_during_validation_exits_three() -> None:
    name = "Test 13 — coordinator-claude root unresolvable during --target-wiki validation → exit 3, nothing written"
    with tempfile.TemporaryDirectory() as tmpdir:
        outbox = os.path.join(tmpdir, "state", "lessons-outbox")
        env = {"LESSON_PROMOTE_OUTBOX_ROOT": outbox}
        env.update(_force_doe_unresolvable_env())
        result = _run_cli(
            [
                "--title", "some title",
                "--body", "some body",
                "--change-kind", "wiki-append",
                "--target-wiki", "docs/wiki/some-real-target.md",
            ],
            env=env,
        )
        if result.returncode != 3:
            raise AssertionError(f"{name}: " + (f"expected exit 3; got {result.returncode}. stderr: {result.stderr!r}"))
        if "machine-local set repos.example_doctrine_repo" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"stderr missing remediation text: {result.stderr!r}"))
        if os.path.isdir(outbox) and os.listdir(outbox):
            raise AssertionError(f"{name}: " + ("outbox has entries but the write should have been skipped"))


# ---------------------------------------------------------------------------
# Test 14 — coordinator-claude root unresolvable at write time (validation bypassed) → exit 3
# ---------------------------------------------------------------------------

def test_doe_unresolvable_at_write_time_exits_three() -> None:
    """Test 14 — coordinator-claude root unresolvable at write time (validation bypassed via 'unknown') → exit 3.

    Rewired (de-node cutover, 480ad8f8 / W0.5 Option B+C, 2026-07-19): this test used
    to force a totally seam-absent CLAUDE_KLABAUTER_ROOT (`_force_legacy_route_env`) to route the
    OUTER queue.promote op to legacy_fn(), while `_force_doe_unresolvable_env()` made
    legacy_fn()'s own doe_root() call raise `_DoeUnresolvable` — pinning the CLI's
    distinctive "machine-local set repos.example_doctrine_repo" remediation text, which only the
    legacy_fn() handler prints (the native op's {skipped:true} path returns a
    different, shorter reason string with no remediation line — see
    coordinator_core/ops/queue_promote.py's `_DoeUnresolvable` handler).
    That combination is no longer reachable via subprocess: main() calls
    `_describe_schema_node("lessons-outbox")` UNCONDITIONALLY before the routing gate,
    and that call routes through the SAME CLAUDE_KLABAUTER_ROOT-keyed seam check as the outer
    queue.promote op — with schema ops having no legacy fallback post-cutover, a truly
    seam-absent CLAUDE_KLABAUTER_ROOT now fails at the schema step, before legacy_fn is ever
    reached (empirically confirmed: exit 1, "schema load failed", not exit 3). There is
    no environment shape where "schema seam present" and "queue.promote seam absent"
    diverge, since both resolve the same CLAUDE_KLABAUTER_ROOT. Converted to load the CLI as a
    module and mock `_cc_route`/`_write_entry` directly (the same in-process pattern
    already used by coordinator/bin/tests/test_lesson_promote.py's AC6 routing-branch
    tests) so legacy_fn()'s own `_DoeUnresolvable` handler — and its remediation text —
    stays covered independent of native-seam plumbing.
    """
    import importlib.machinery
    import importlib.util as _importlib_util
    import io as _io
    import unittest.mock as _mock

    name = "Test 14 — coordinator-claude root unresolvable at write time (validation bypassed via 'unknown') → exit 3"
    cli_path = _script_path()
    loader = importlib.machinery.SourceFileLoader("coordinator_lesson_promote_t14", cli_path)
    spec = _importlib_util.spec_from_loader("coordinator_lesson_promote_t14", loader)
    cli_mod = _importlib_util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(cli_mod)

    def _fake_route(op, params, repo_root, legacy_fn):
        # Simulate seam-absent for queue.promote specifically: call legacy_fn directly.
        return legacy_fn()

    captured_err = _io.StringIO()
    captured_out = _io.StringIO()
    with (
        _mock.patch.object(cli_mod, "_cc_route", side_effect=_fake_route),
        _mock.patch.object(
            cli_mod, "_describe_schema_node",
            return_value={"enums": {"change_kind": ["doctrine-edit", "wiki-append", "skill-edit"]}},
        ),
        _mock.patch.object(cli_mod, "_resolve_from_repo", return_value="coordinator-claude"),
        _mock.patch.object(cli_mod, "_current_repo_root", return_value="/fake/repo"),
        _mock.patch.object(
            cli_mod, "_write_entry",
            side_effect=cli_mod._DoeUnresolvable(
                "repos.example_doctrine_repo not set in machine-local registry and REPO_EXAMPLE_DOCTRINE_REPO env var not set"
            ),
        ),
        _mock.patch("sys.stderr", captured_err),
        _mock.patch("sys.stdout", captured_out),
    ):
        rc = cli_mod.main([
            "--title", "some title",
            "--body", "some body",
            "--change-kind", "doctrine-edit",
            "--target-wiki", "unknown",
        ])

    if rc != 3:
        raise AssertionError(f"{name}: " + (f"expected exit 3; got {rc}. stderr: {captured_err.getvalue()!r}"))
    if "Lesson outbox entry written" in captured_out.getvalue():
        raise AssertionError(f"{name}: " + (f"stdout claims a write happened despite unresolvable coordinator-claude root: {captured_out.getvalue()!r}"))
    if "machine-local set repos.example_doctrine_repo" not in captured_err.getvalue():
        raise AssertionError(f"{name}: " + (f"stderr missing remediation text: {captured_err.getvalue()!r}"))


# ---------------------------------------------------------------------------
# Test 15 — skill-edit with a SKILL.md target_wiki round-trips unchanged, no
# inventory validation (A7/A9 non-wiki change_kind regression)
# ---------------------------------------------------------------------------

def test_skill_edit_target_wiki_bypasses_wiki_normalization() -> None:
    name = (
        "Test 15 — skill-edit + SKILL.md --target-wiki round-trips byte-identically, "
        "no docs/wiki/ collapse, no inventory validation, exit 0"
    )
    target = "coordinator/skills/pickup/SKILL.md"
    with tempfile.TemporaryDirectory() as tmpdir:
        outbox = os.path.join(tmpdir, "state", "lessons-outbox")
        # Deliberately empty inventory (and no SKILL.md name in it) — if the CLI
        # regressed to validating this target_wiki against the wiki inventory, this
        # would fail either with a corrupted 'docs/wiki/coordinator/skills/pickup/
        # SKILL.md' value or an inventory-miss exit 2. Neither should happen.
        wiki_root = _wiki_root_with(tmpdir, "unrelated.md")
        result = _run_cli(
            [
                "--title", "Skill edit lesson promotion",
                "--body", "Adding a missing step to the pickup skill.",
                "--change-kind", "skill-edit",
                "--target-wiki", target,
            ],
            env={
                "LESSON_PROMOTE_OUTBOX_ROOT": outbox,
                "LESSON_PROMOTE_WIKI_ROOT": wiki_root,
            },
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
        yaml_files = [f for f in os.listdir(outbox) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected exactly 1 YAML file; found {len(yaml_files)}: {yaml_files}"))
        yaml_path = os.path.join(outbox, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
        got = parsed.get("target_wiki")
        if got != target:
            raise AssertionError(f"{name}: " + (f"expected target_wiki to round-trip UNCHANGED as {target!r}; got {got!r} "
                "(a docs/wiki/-prefixed value here means the change_kind gate regressed)"))


# ---------------------------------------------------------------------------
# Test 16 — wiki-append with a bogus target_wiki still fails loud (validation
# retained for wiki-targeting change_kinds after the change_kind gate)
# ---------------------------------------------------------------------------

def test_wiki_append_bogus_target_still_validates() -> None:
    name = (
        "Test 16 — wiki-append + bogus --target-wiki → exit 2, closest-match "
        "suggestions still offered (wiki-targeting kinds keep full validation)"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        outbox = os.path.join(tmpdir, "state", "lessons-outbox")
        wiki_root = _wiki_root_with(tmpdir, "close-match.md", "another-wiki.md")
        result = _run_cli(
            [
                "--title", "some title",
                "--body", "some body",
                "--change-kind", "wiki-append",
                "--target-wiki", "clos-match",
            ],
            env={
                "LESSON_PROMOTE_OUTBOX_ROOT": outbox,
                "LESSON_PROMOTE_WIKI_ROOT": wiki_root,
            },
        )
    if result.returncode != 2:
        raise AssertionError(f"{name}: " + (f"expected exit 2; got {result.returncode}. stderr: {result.stderr!r}"))
    if "close-match.md" not in result.stderr:
        raise AssertionError(f"{name}: " + (f"stderr does not offer the closest match 'close-match.md': {result.stderr!r}"))
    if os.path.isdir(outbox) and os.listdir(outbox):
        raise AssertionError(f"{name}: " + ("outbox has entries but validation should have rejected before any write"))


# ---------------------------------------------------------------------------
# Test 17 — --allow-new-wiki rejected for change_kind != wiki-new (Finding 2)
# ---------------------------------------------------------------------------

def test_allow_new_wiki_rejected_for_wiki_append() -> None:
    name = (
        "Test 17 — --change-kind wiki-append --allow-new-wiki → exit 2 at argparse "
        "time (mismatch rejected before the inventory check runs)"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        outbox = os.path.join(tmpdir, "state", "lessons-outbox")
        wiki_root = _wiki_root_with(tmpdir, "unrelated.md")
        result = _run_cli(
            [
                "--title", "some title",
                "--body", "some body",
                "--change-kind", "wiki-append",
                "--target-wiki", "nonexistent.md",
                "--allow-new-wiki",
            ],
            env={
                "LESSON_PROMOTE_OUTBOX_ROOT": outbox,
                "LESSON_PROMOTE_WIKI_ROOT": wiki_root,
            },
        )
    if result.returncode != 2:
        raise AssertionError(f"{name}: " + (f"expected exit 2; got {result.returncode}. stderr: {result.stderr!r}"))
    if "wiki-new" not in result.stderr:
        raise AssertionError(f"{name}: " + (f"stderr does not name the wiki-new requirement: {result.stderr!r}"))
    if os.path.isdir(outbox) and os.listdir(outbox):
        raise AssertionError(f"{name}: " + ("outbox has entries but the mismatch should have been rejected before any write"))

