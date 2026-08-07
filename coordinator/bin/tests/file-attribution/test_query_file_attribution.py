#!/usr/bin/env python3
"""
test_query_file_attribution.py — Smoke test for query-file-attribution.py CLI.

Purpose: Verify the --session and --file query modes produce correct JSON output
when run against the golden transcript fixture.

Spec backlink: docs/plans/2026-07-02-ccos-6-rehome-attribution-python.md § C2

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

from coordinator_core.win_portability import no_console_creationflags

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(os.path.dirname(_HERE))  # coordinator/bin/
_CLI = os.path.join(_BIN_DIR, 'query-file-attribution.py')
_FIXTURE = os.path.join(_HERE, 'fixtures', 'transcript-golden.jsonl')

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


def _run_cli(*extra_args: str, transcript_dir: str) -> subprocess.CompletedProcess:
    """Invoke the CLI via subprocess; returns the CompletedProcess."""
    cmd = [
        sys.executable,
        _CLI,
        '--project', '/project',          # arbitrary root (not used when transcript-dir given)
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
