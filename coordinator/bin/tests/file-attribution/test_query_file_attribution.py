"""
test_query_file_attribution.py — Smoke test for query-file-attribution.py CLI.

Purpose: Verify the --session and --file query modes produce correct JSON output
when run against the golden transcript fixture.

Spec backlink: pln-ccos-6-rehome-attribution-python-9966da § C2

Test strategy:
  - Copies the golden fixture (transcript-golden.jsonl) into a temp directory
    named <session-id>.jsonl so process_transcript can derive the session ID from
    the filename stem, mirroring how real Claude Code transcripts are stored.
  - Invokes the CLI via subprocess to validate the full CLI surface, not just
    the importable functions.
  - Asserts JSON output shape for --session and --file queries.

Fixture: tests/file-attribution/fixtures/transcript-golden.jsonl
  Session: test-session-0001
  Files (5 distinct file_paths after aggregation):
    /project/src/main.py     read=1, edited=1, last_operation=edit
    /project/src/utils.py    read=1, edited=0
    /project/new/file.txt    edited=1, last_operation=create, lines_added=2
    /tmp/output.log          edited=1, last_operation=bash, completeness=partial
    /project/src/            referenced=1

Negative-spec:
  - Do NOT import query-file-attribution.py directly (its importlib load path is
    sensitive to cwd; subprocess is the authoritative test surface).
  - Do NOT modify the fixture file or test_derive_file_attribution.py.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused: every test invokes query-file-attribution.py as a REAL
# subprocess (this file's own module docstring negative-spec forbids importing it
# directly -- its importlib load path is cwd-sensitive) to exercise the actual CLI
# argv/exit-code contract (0/1/2) and stdout JSON/table rendering, which an
# in-process call cannot observe. The spawn ratchet's `_BASELINE` is shrink-only
# pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(os.path.dirname(_HERE))  # coordinator/bin/
_CLI = os.path.join(_BIN_DIR, 'query-file-attribution.py')
_FIXTURE = os.path.join(_HERE, 'fixtures', 'transcript-golden.jsonl')

# The real repo root this test file lives in — used as --project for tests
# that exercise the normal (Leg 1 / Leg 2) path, since AC9's fail-loud check
# requires a real git repo root (an arbitrary placeholder path like
# '/project' no longer passes Leg 1). --file query args below stay absolute
# fixture-style paths ('/project/...'), unaffected by this substitution.
_REPO_ROOT = os.path.dirname(os.path.dirname(_BIN_DIR))

# The session ID that the fixture transcript belongs to (the temp file will be
# named <SESSION_ID>.jsonl so derive_rows picks it up by stem).
_SESSION_ID = 'test-session-0001'


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_transcript_dir() -> tempfile.TemporaryDirectory:
    """Create a temp dir with the fixture transcript named by session UUID."""
    tmpdir = tempfile.TemporaryDirectory(prefix='fa_query_test_')
    dest = os.path.join(tmpdir.name, f'{_SESSION_ID}.jsonl')
    shutil.copy(_FIXTURE, dest)
    return tmpdir


def _make_relative_join_fixture(extra_jsonl_lines=()):
    """Build a temp git repo whose root doubles as the transcript directory,
    holding a rewritten copy of the golden fixture: every stored
    ``"file_path":"/project/`` (and ``"path":"/project/``) prefix is
    replaced with this repo's own root.

    Exists so relative-arg-join tests (AC9's Leg 1 requires --project be a
    genuine git repo root) keep depending on the CLI's project_root JOIN —
    not on the literal string '/project' — by making the fixture's records
    point at a real, freshly created, self-cleaning temp repo instead of
    requiring a fixed on-disk location. No shared/fixed path, so no
    skip-on-collision logic is needed: `tempfile.TemporaryDirectory` gives
    each call its own directory, cleaned up by its own teardown even across
    the crash/interrupt and concurrent-session cases a fixed drive-root
    location could not survive.

    Only rewrites the JSON `"file_path":"..."` / `"path":"..."` VALUE prefix
    (matched by requiring `/project/` to immediately follow the opening
    quote) — never touches separators, and never touches the unrelated
    `/project/new/file.txt` substring that appears inside a tool_result's
    fake-diff CONTENT string (that occurrence is not preceded by a quote,
    so the anchored replace leaves it alone). The fixture, as authored,
    never mixes separators inside a single stored file_path value, so a
    plain prefix substitution preserves its shape exactly; this deliberately
    does not attempt cross-separator normalisation of any kind.

    Returns (tmpdir, repo_root) — repo_root uses forward slashes throughout.
    """
    tmpdir = tempfile.TemporaryDirectory(prefix='fa_query_test_relroot_')
    subprocess.run(
        ['git', 'init', '-q'],
        cwd=tmpdir.name,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **no_console_creationflags(),
    )
    repo_root = tmpdir.name.replace('\\', '/')
    with open(_FIXTURE, 'r', encoding='utf-8') as f:
        text = f.read()
    rewritten = (
        text
        .replace('"file_path":"/project/', f'"file_path":"{repo_root}/')
        .replace('"path":"/project/', f'"path":"{repo_root}/')
    )
    lines = rewritten.splitlines()
    lines.extend(extra_jsonl_lines)
    dest = os.path.join(tmpdir.name, f'{_SESSION_ID}.jsonl')
    with open(dest, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return tmpdir, repo_root


def _run_cli(*extra_args: str, transcript_dir: str, project: str = _REPO_ROOT) -> subprocess.CompletedProcess:
    """Invoke the CLI via subprocess; returns the CompletedProcess."""
    cmd = [
        sys.executable,
        _CLI,
        '--project', project,              # real repo root — required by Leg 1 (AC9)
        '--transcript-dir', transcript_dir,
    ] + list(extra_args)
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **no_console_creationflags(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestQueryCLISessionMode(unittest.TestCase):
    """--session <id> → all files touched by that session."""

    def setUp(self):
        self._tmpdir = _make_transcript_dir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _query(self) -> subprocess.CompletedProcess:
        return _run_cli('--session', _SESSION_ID, transcript_dir=self._tmpdir.name)

    def test_exit_code_zero(self):
        result = self._query()
        self.assertEqual(result.returncode, 0, msg=f'stderr: {result.stderr}')

    def test_output_is_valid_json(self):
        result = self._query()
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            self.fail(f'stdout is not valid JSON: {e}\nstdout: {result.stdout[:500]}')
        self.assertIsInstance(data, list)

    def test_five_records_returned(self):
        result = self._query()
        data = json.loads(result.stdout)
        file_paths = [r['file_path'] for r in data]
        self.assertEqual(
            len(data), 5,
            msg=f'Expected 5 records, got {len(data)}: {file_paths}',
        )

    def test_all_session_ids_match(self):
        result = self._query()
        data = json.loads(result.stdout)
        for r in data:
            self.assertEqual(r['session_id'], _SESSION_ID)

    def test_main_py_counts(self):
        result = self._query()
        data = json.loads(result.stdout)
        by_path = {r['file_path']: r for r in data}
        self.assertIn('/project/src/main.py', by_path)
        r = by_path['/project/src/main.py']
        self.assertEqual(r['read_count'], 1)
        self.assertEqual(r['edited_count'], 1)
        self.assertEqual(r['last_operation'], 'edit')
        self.assertEqual(r['capture_source'], 'derived')

    def test_new_file_txt_create(self):
        result = self._query()
        data = json.loads(result.stdout)
        by_path = {r['file_path']: r for r in data}
        self.assertIn('/project/new/file.txt', by_path)
        r = by_path['/project/new/file.txt']
        self.assertEqual(r['edited_count'], 1)
        self.assertEqual(r['last_operation'], 'create')
        self.assertEqual(r['lines_added'], 2)
        self.assertEqual(r['lines_removed'], 0)

    def test_output_log_bash_partial(self):
        result = self._query()
        data = json.loads(result.stdout)
        by_path = {r['file_path']: r for r in data}
        self.assertIn('/tmp/output.log', by_path)
        r = by_path['/tmp/output.log']
        self.assertEqual(r['edited_count'], 1)
        self.assertEqual(r['last_operation'], 'bash')
        self.assertEqual(r['completeness'], 'partial')

    def test_src_dir_referenced(self):
        result = self._query()
        data = json.loads(result.stdout)
        by_path = {r['file_path']: r for r in data}
        self.assertIn('/project/src/', by_path)
        r = by_path['/project/src/']
        self.assertEqual(r['referenced_count'], 1)
        self.assertEqual(r['edited_count'], 0)
        self.assertEqual(r['read_count'], 0)


class TestQueryCLIFileMode(unittest.TestCase):
    """--file <path> → which sessions touched that file."""

    def setUp(self):
        self._tmpdir = _make_transcript_dir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _query(self, file_path: str) -> subprocess.CompletedProcess:
        return _run_cli('--file', file_path, transcript_dir=self._tmpdir.name)

    def test_exit_code_zero(self):
        result = self._query('/project/src/main.py')
        self.assertEqual(result.returncode, 0, msg=f'stderr: {result.stderr}')

    def test_output_is_valid_json(self):
        result = self._query('/project/src/main.py')
        data = json.loads(result.stdout)
        self.assertIsInstance(data, list)

    def test_one_session_touches_main_py(self):
        result = self._query('/project/src/main.py')
        data = json.loads(result.stdout)
        self.assertEqual(len(data), 1, msg=f'Expected 1 record, got: {data}')
        self.assertEqual(data[0]['session_id'], _SESSION_ID)
        self.assertEqual(data[0]['file_path'], '/project/src/main.py')
        self.assertEqual(data[0]['read_count'], 1)
        self.assertEqual(data[0]['edited_count'], 1)

    def test_no_results_exits_two(self):
        result = self._query('/does/not/exist.py')
        self.assertEqual(result.returncode, 2, msg=f'Expected exit 2, got {result.returncode}')

    def test_utils_py_read_only(self):
        result = self._query('/project/src/utils.py')
        data = json.loads(result.stdout)
        self.assertEqual(len(data), 1)
        r = data[0]
        self.assertEqual(r['read_count'], 1)
        self.assertEqual(r['edited_count'], 0)
        self.assertEqual(r['session_id'], _SESSION_ID)

    def test_capture_source_always_derived(self):
        result = self._query('/project/src/main.py')
        data = json.loads(result.stdout)
        for r in data:
            self.assertEqual(r['capture_source'], 'derived')


class TestQueryCLIFileModeResolution(unittest.TestCase):
    """--file resolution-aware matching: relative/absolute, separators, case.

    Spec backlink: cross-repo/inbox/2026-08-11-example-cockpit-repo-em-three-answers-back-relative-path-zero.md
    """

    def setUp(self):
        self._tmpdir = _make_transcript_dir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _query(self, file_path: str) -> subprocess.CompletedProcess:
        return _run_cli('--file', file_path, transcript_dir=self._tmpdir.name)

    def test_absolute_arg_still_matches(self):
        """Regression guard: absolute platform-native arg unchanged behaviour."""
        result = self._query('/project/src/main.py')
        data = json.loads(result.stdout)
        self.assertEqual(len(data), 1, msg=f'Expected 1 record, got: {data}')

    def test_repo_relative_posix_arg_matches_absolute_record(self):
        """The reported defect: a --project-relative arg must resolve, not
        string-compare, against the absolute stored file_path.

        Uses a fixture copy rewritten onto a real temp git repo (AC9's Leg 1
        requires --project be a genuine repo root) — see
        _make_relative_join_fixture(). Still depends on the project_root
        JOIN, not any literal path string.
        """
        tmpdir, repo_root = _make_relative_join_fixture()
        try:
            result = _run_cli(
                '--file', 'src/main.py',
                transcript_dir=tmpdir.name, project=repo_root,
            )
        finally:
            tmpdir.cleanup()
        data = json.loads(result.stdout)
        self.assertEqual(
            len(data), 1,
            msg=f'Expected 1 record for relative arg, got: {data}; stderr={result.stderr}',
        )
        self.assertEqual(data[0]['file_path'], f'{repo_root}/src/main.py')

    def test_backslash_arg_matches_forward_slash_record(self):
        tmpdir, repo_root = _make_relative_join_fixture()
        try:
            result = _run_cli(
                '--file', 'src\\main.py',
                transcript_dir=tmpdir.name, project=repo_root,
            )
        finally:
            tmpdir.cleanup()
        data = json.loads(result.stdout)
        self.assertEqual(len(data), 1, msg=f'Expected 1 record, got: {data}')
        self.assertEqual(data[0]['file_path'], f'{repo_root}/src/main.py')

    def test_same_basename_different_directory_does_not_match(self):
        """Anti-suffix guard: no bare endswith match."""
        result = self._query('vendor/main.py')
        self.assertEqual(result.returncode, 2, msg=f'Unexpected match: {result.stdout}')

    @unittest.skipUnless(sys.platform == 'win32', 'case-insensitive match is Windows-only')
    def test_case_insensitive_match_on_windows(self):
        result = self._query('/PROJECT/SRC/MAIN.PY')
        data = json.loads(result.stdout)
        self.assertEqual(len(data), 1, msg=f'Expected 1 record, got: {data}')


class TestQueryByFileBashRedirectDirection(unittest.TestCase):
    """Absolute query arg must match a relative record (Bash-redirect rows).

    Appends one extra Bash tool_use line (relative-path redirect) to a copy
    of the golden transcript, run via subprocess — per this test module's
    negative-spec, query-file-attribution.py is never imported directly.
    """

    def setUp(self):
        extra = json.dumps({
            'type': 'assistant',
            'sessionId': _SESSION_ID,
            'message': {
                'role': 'assistant',
                'content': [{
                    'type': 'tool_use', 'id': 't99', 'name': 'Bash',
                    'input': {
                        'command': 'echo hi > out/build.log',
                        'description': 'relative redirect write',
                    },
                }],
            },
        })
        extra_result = json.dumps({
            'type': 'user',
            'sessionId': _SESSION_ID,
            'message': {
                'role': 'user',
                'content': [{'type': 'tool_result', 'tool_use_id': 't99', 'content': ''}],
            },
        })
        self._tmpdir, self._repo_root = _make_relative_join_fixture((extra, extra_result))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_absolute_arg_matches_relative_record(self):
        """The relative-record row ('out/build.log', unaffected by the
        fixture rewrite since it never carried a '/project/' prefix) is
        joined against --project — which must be a real repo root (AC9's
        Leg 1) for the query's absolute arg to land on it."""
        result = _run_cli(
            '--file', f'{self._repo_root}/out/build.log',
            transcript_dir=self._tmpdir.name, project=self._repo_root,
        )
        data = json.loads(result.stdout)
        self.assertEqual(
            len(data), 1,
            msg=f'Expected 1 record, got: {data}; stderr={result.stderr}',
        )
        self.assertEqual(data[0]['session_id'], _SESSION_ID)


class TestQueryCLITableFormat(unittest.TestCase):
    """--format table produces non-JSON human-readable output (smoke check)."""

    def setUp(self):
        self._tmpdir = _make_transcript_dir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_table_session_non_empty(self):
        result = _run_cli(
            '--session', _SESSION_ID, '--format', 'table',
            transcript_dir=self._tmpdir.name,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(_SESSION_ID, result.stdout)
        self.assertIn('/project/src/main.py', result.stdout)

    def test_table_file_non_empty(self):
        result = _run_cli(
            '--file', '/project/src/main.py', '--format', 'table',
            transcript_dir=self._tmpdir.name,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(_SESSION_ID, result.stdout)


class TestQueryCLIErrorHandling(unittest.TestCase):
    """Error conditions: missing flags, nonexistent transcript dir."""

    def test_no_query_flag_exits_nonzero(self):
        """No --session or --file → should exit non-zero (argparse error)."""
        result = subprocess.run(
            [sys.executable, _CLI, '--project', '/project', '--transcript-dir', '/tmp'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **no_console_creationflags(),
        )
        self.assertNotEqual(result.returncode, 0)

    def test_nonexistent_transcript_dir_exits_one(self):
        result = subprocess.run(
            [
                sys.executable, _CLI,
                '--session', 'some-session',
                '--transcript-dir', '/nonexistent/path/that/does/not/exist',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **no_console_creationflags(),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('not found', result.stderr)


class TestQueryCLIRepoSlug(unittest.TestCase):
    """AC5/AC6/AC9/AC10 — owner-qualified repo slug via canonical producer.

    Spec backlink: pln-three-query-trampolines-and-th-309bf9 § C5
    """

    def setUp(self):
        self._tmpdir = _make_transcript_dir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_repo_matches_canonical_producer(self):
        """AC5 — equality against resolve_repo_name(repo_root), not a hardcoded
        owner/repo literal (which would fail on any other clone)."""
        claude_klabauter_root = _REPO_ROOT
        if claude_klabauter_root not in sys.path:
            sys.path.insert(0, claude_klabauter_root)
        from coordinator_core.ops.emit.context import resolve_repo_name

        expected = resolve_repo_name(_REPO_ROOT)
        result = _run_cli('--session', _SESSION_ID, transcript_dir=self._tmpdir.name)
        data = json.loads(result.stdout)
        self.assertTrue(data, msg=f'no records: stderr={result.stderr}')
        for r in data:
            self.assertEqual(r['repo'], expected)

    def test_coordinator_root_path_is_dot(self):
        """AC6 — coordinator_root_path is left at aggregate()'s own default
        ('.'), never the absolute --project path."""
        result = _run_cli('--session', _SESSION_ID, transcript_dir=self._tmpdir.name)
        data = json.loads(result.stdout)
        self.assertTrue(data, msg=f'no records: stderr={result.stderr}')
        for r in data:
            self.assertEqual(r['coordinator_root_path'], '.')

    def test_subdirectory_project_root_fails_loud(self):
        """AC9 — Leg 1: --project naming a subdirectory of a repo (not the
        repo root itself) fails loud, naming both project_root and the
        resolved toplevel, rather than silently stamping the enclosing
        repo's slug."""
        subdir = os.path.join(_REPO_ROOT, 'coordinator')
        self.assertTrue(os.path.isdir(subdir), msg=f'fixture assumption broken: {subdir}')
        result = subprocess.run(
            [
                sys.executable, _CLI,
                '--session', _SESSION_ID,
                '--project', subdir,
                '--transcript-dir', self._tmpdir.name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **no_console_creationflags(),
        )
        self.assertEqual(
            result.returncode, 1,
            msg=f'stdout={result.stdout!r} stderr={result.stderr!r}',
        )
        self.assertIn(repr(subdir), result.stderr)

    def test_remoteless_repo_stamps_local_basename(self):
        """AC10 — a valid repo with no parseable origin remote gets the
        documented local/<basename> fallback, stamped as-is, not rejected."""
        remoteless = tempfile.mkdtemp(prefix='fa_remoteless_')
        try:
            subprocess.run(
                ['git', 'init', '-q'],
                cwd=remoteless,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **no_console_creationflags(),
            )
            result = subprocess.run(
                [
                    sys.executable, _CLI,
                    '--session', _SESSION_ID,
                    '--project', remoteless,
                    '--transcript-dir', self._tmpdir.name,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **no_console_creationflags(),
            )
            data = json.loads(result.stdout)
            self.assertTrue(data, msg=f'no records: stderr={result.stderr}')
            expected = f'local/{os.path.basename(remoteless)}'
            for r in data:
                self.assertEqual(r['repo'], expected)
        finally:
            shutil.rmtree(remoteless, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
