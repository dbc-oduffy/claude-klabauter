#!/usr/bin/env python3
"""
test_derive_file_attribution.py — Golden test for derive-file-attribution.py.

Spec backlink: docs/plans/2026-07-02-ccos-6-rehome-attribution-python.md § C1 test surface

Tests:
  1. Synthetic fixture transcript covering Read, Read(partial), Edit, Write(create),
     Bash-redirect, Bash-ambiguous(heredoc), Bash-ambiguous($VAR), Grep.
  2. Runs derive_rows + aggregate over --transcript-dir pointing at the fixture.
  3. Asserts per-(session, file) aggregate output matches expected counts and fields.

Fixture: tests/file-attribution/fixtures/transcript-golden.jsonl
  (SYNTHETIC — hand-written minimal transcript lines, no real session data)
"""

import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

# ---------------------------------------------------------------------------
# Import the module under test via importlib (filename has a dash, not importable
# as a regular identifier).
# ---------------------------------------------------------------------------
_BIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MODULE_PATH = os.path.join(_BIN_DIR, 'derive-file-attribution.py')

_spec = importlib.util.spec_from_file_location('derive_file_attribution', _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

derive_attribution = _mod.derive_attribution
derive_rows = _mod.derive_rows
aggregate = _mod.aggregate
parse_bash_for_writes = _mod.parse_bash_for_writes
mask_quotes = _mod.mask_quotes
is_token_ambiguous = _mod.is_token_ambiguous
parse_patch = _mod.parse_patch
encode_project_path = _mod.encode_project_path

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------
_FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
_TRANSCRIPT_FIXTURE = os.path.join(_FIXTURES_DIR, 'transcript-golden.jsonl')

# Review: code-reviewer (F11) — shutil and tempfile moved to top-level import
# block (were previously mid-file after module-level constants, violating PEP 8).
# The fixture transcript is written as session ID 'test-session-0001'
# but process_transcript reads session_id from the filename stem.
# We copy/symlink the fixture so the stem matches.


def _make_transcript_dir() -> tempfile.TemporaryDirectory:
    """Create a temp dir containing the fixture transcript named by session UUID."""
    tmpdir = tempfile.TemporaryDirectory(prefix='fa_test_')
    dest = os.path.join(tmpdir.name, 'test-session-0001.jsonl')
    shutil.copy(_TRANSCRIPT_FIXTURE, dest)
    return tmpdir


# ---------------------------------------------------------------------------
# Unit tests — low-level helpers
# ---------------------------------------------------------------------------

class TestMaskQuotes(unittest.TestCase):
    def test_single_quoted_hides_redirect(self):
        masked = mask_quotes("echo 'hello > world'")
        # The > inside single quotes should be masked to space
        self.assertNotIn('>', masked[masked.index(' ') + 1:])

    def test_double_quoted_hides_redirect(self):
        masked = mask_quotes('cat "a > b"')
        # Review: code-reviewer (F3) — previous assertion was vacuously true:
        # masked.replace('cat ','').replace('a','').strip() → '' and assertNotIn('>', '')
        # passes even if mask_quotes returned the input unchanged. Fix: assert directly.
        self.assertIn('>', mask_quotes('cat a > b'))
        self.assertNotIn('>', masked)
        self.assertIn('cat', masked)

    def test_unquoted_redirect_preserved(self):
        masked = mask_quotes('echo hello > out.txt')
        self.assertIn('>', masked)

    def test_backslash_in_double_quotes(self):
        # \" inside double quotes should not close the quote
        masked = mask_quotes(r'echo "a\"b" > out.txt')
        # The > after the closing " is outside quotes → preserved
        self.assertIn('>', masked)

    def test_no_quote_chars_fast_path_returns_identical_string(self):
        # 2026-07-29 perf fix: a command with no ' or " at all takes the fast-path
        # early return (no char-by-char scan). Must be byte-identical to the input,
        # since every char in that path would have fallen through to the `else`
        # branch of the char-loop unchanged anyway.
        cmd = 'echo hello > out.txt; ls -la /tmp && cat *.txt'
        self.assertEqual(mask_quotes(cmd), cmd)

    def test_no_quote_chars_empty_string(self):
        self.assertEqual(mask_quotes(''), '')


class TestParseBashForWrites(unittest.TestCase):
    def test_no_redirect_or_heredoc_char_fast_path(self):
        # 2026-07-29 perf fix: neither '>' nor '<' anywhere in the raw command means
        # neither a redirect nor a heredoc marker can survive masking — early return []
        # without ever calling mask_quotes.
        result = parse_bash_for_writes("echo 'hello world' && ls -la")
        self.assertEqual(result, [])

    def test_heredoc_without_gt_still_ambiguous(self):
        # A heredoc with zero '>' characters anywhere in the command must still hit
        # the heredoc-ambiguous branch — the fast-path checks for '<' too, so this
        # does NOT get short-circuited to [] by the '>' absence alone.
        result = parse_bash_for_writes("cat << EOF\nhello\nEOF")
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]['file_path'])
        self.assertTrue(result[0]['ambiguous'])

    def test_simple_redirect(self):
        result = parse_bash_for_writes('echo hello > /tmp/out.txt')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['file_path'], '/tmp/out.txt')
        self.assertFalse(result[0]['ambiguous'])

    def test_append_redirect(self):
        result = parse_bash_for_writes('echo foo >> /var/log/app.log')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['file_path'], '/var/log/app.log')
        self.assertFalse(result[0]['ambiguous'])

    def test_dev_null_filtered(self):
        result = parse_bash_for_writes('cmd 2>/dev/null')
        self.assertEqual(result, [])

    def test_heredoc_ambiguous(self):
        result = parse_bash_for_writes("cat << 'EOF'\nhello\nEOF")
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]['file_path'])
        self.assertTrue(result[0]['ambiguous'])

    def test_unquoted_dollar_var_ambiguous(self):
        result = parse_bash_for_writes('echo hello > $OUTPUT')
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['ambiguous'])

    def test_double_quoted_dollar_var_ambiguous(self):
        result = parse_bash_for_writes('echo hello > "$DEST"')
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['ambiguous'])

    def test_single_quoted_dollar_is_ambiguous(self):
        # The reference implementation (project.mjs isTokenAmbiguous) uses a binary
        # isDoubleQuoted flag: single-quoted tokens return isDoubleQuoted=False, which
        # triggers the unquoted ambiguity check — $ is treated as ambiguous even inside
        # single quotes. This mirrors the JS behaviour exactly (conservative / never-fabricate).
        result = parse_bash_for_writes("echo hello > '$OUTPUT'")
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['ambiguous'])

    def test_multiple_redirects_ambiguous(self):
        result = parse_bash_for_writes('cmd > out1.txt >> out2.txt')
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['ambiguous'])

    def test_no_redirect(self):
        result = parse_bash_for_writes('ls -la /tmp')
        self.assertEqual(result, [])

    def test_glob_in_target_ambiguous(self):
        result = parse_bash_for_writes('echo x > *.txt')
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['ambiguous'])

    def test_backslash_escaped_quote_no_fabrication(self):
        # Review: code-reviewer (F1) — before the fix, extract_shell_token short-read
        # "path\"name.txt" and returned the fabricated path 'path\', violating the
        # never-fabricate invariant. The fix handles \\ escapes inside double-quoted spans.
        result = parse_bash_for_writes(r'echo x > "path\"name.txt"')
        self.assertEqual(len(result), 1)
        # Must NOT return the truncated fabricated path.
        self.assertNotEqual(result[0].get('file_path'), 'path\\')
        # With the escape-handling fix the correct de-escaped token is extracted.
        self.assertEqual(result[0]['file_path'], 'path"name.txt')
        self.assertFalse(result[0]['ambiguous'])


class TestParsePatch(unittest.TestCase):
    def test_create_patch(self):
        patch = '--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+line1\n+line2\n'
        la, lr, is_create = parse_patch(patch)
        self.assertEqual(la, 2)
        self.assertEqual(lr, 0)
        self.assertTrue(is_create)

    def test_edit_patch(self):
        patch = '--- a/file.txt\n+++ b/file.txt\n@@ -1,3 +1,3 @@\n-old\n+new\n context\n'
        la, lr, is_create = parse_patch(patch)
        self.assertEqual(la, 1)
        self.assertEqual(lr, 1)
        self.assertFalse(is_create)

    def test_empty_returns_none(self):
        la, lr, is_create = parse_patch('')
        self.assertIsNone(la)
        self.assertIsNone(lr)
        self.assertFalse(is_create)

    def test_non_patch_string(self):
        # A non-patch string is still a valid string — parsePatch counts 0 additions/removals.
        # None is only returned for empty or non-string input (mirrors parsePatch in project.mjs).
        # In practice, derive_attribution guards with startswith('---') so parsePatch is
        # never called on a success-message string.
        la, lr, is_create = parse_patch('File updated successfully.')
        self.assertEqual(la, 0)
        self.assertEqual(lr, 0)
        self.assertFalse(is_create)


class TestEncodeProjectPath(unittest.TestCase):
    def test_meta_repo(self):
        result = encode_project_path('/Users/example-operator/.claude')
        self.assertEqual(result, '-Users-example-operator--claude')

    def test_no_dots(self):
        result = encode_project_path('/Users/example-operator/X/example-os-repo')
        self.assertEqual(result, '-Users-example-operator-X-example-os-repo')

    def test_multiple_dots(self):
        result = encode_project_path('/home/user/.config/my.app')
        self.assertEqual(result, '-home-user--config-my-app')

    def test_windows_drive_letter_colon_encodes_to_a_dash(self):
        """A drive-letter colon is encoded as '-', exactly like a separator.

        Claude Code writes this project's transcripts to
        `~/.claude/projects/X--claude-klabauter/`. Encoding only the backslash
        yields `X:-claude-klabauter`, which matches no directory on disk — and
        every layer above swallows the miss (`derive_rows` returns [] for an
        absent transcript dir, the emit section porter turns any producer
        failure into [], and the snapshot ships an empty `file_attributions`).
        That silent chain is what dropped 2566 attribution rows from the
        cockpit emission on 2026-07-28, so the encoding is pinned here rather
        than left to the integration test that cannot run without a live
        transcript tree.
        """
        self.assertEqual(
            encode_project_path(r'X:\claude-klabauter'), 'X--claude-klabauter'
        )

    def test_windows_forward_slash_form_encodes_identically(self):
        # os.path.abspath may hand back either separator form depending on
        # how the caller spelled the path; both must land on the same
        # directory name.
        self.assertEqual(
            encode_project_path('X:/claude-klabauter'),
            encode_project_path(r'X:\claude-klabauter'),
        )

    def test_windows_user_profile_path(self):
        self.assertEqual(
            encode_project_path(r'C:\Users\example-operator\.claude'),
            'C--Users-example-operator--claude',
        )


# ---------------------------------------------------------------------------
# Integration test — full derive_rows + aggregate over fixture transcript.
# ---------------------------------------------------------------------------

class TestGoldenTranscript(unittest.TestCase):
    """
    Golden test: fixture transcript → expected aggregate output.

    Fixture covers (in order):
      t1: Read /project/src/main.py             → read, wasPartial=False
      t2: Read /project/src/utils.py +offset    → read, wasPartial=True
      t3: Edit /project/src/main.py             → edited, operation=edit
      t4: Write /project/new/file.txt (patch)   → edited, operation=create, lines_added=2
      t5: Bash echo done > /tmp/output.log      → edited, operation=bash
      t6: Bash heredoc (ambiguous)              → unknown, file_path=None → skipped
      t7: Bash echo > $OUTPUT (ambiguous)       → unknown, file_path=None → skipped
      t8: Grep def main /project/src/           → referenced

    Expected aggregate (5 unique file_paths):
      /project/src/main.py   : read=1, edited=1, last_op=edit, completeness=complete
      /project/src/utils.py  : read=1, edited=0
      /project/new/file.txt  : read=0, edited=1, last_op=create, lines_added=2
      /tmp/output.log        : read=0, edited=1, last_op=bash, completeness=partial
      /project/src/          : read=0, edited=0, referenced=1, completeness=complete
    """

    def setUp(self):
        self._tmpdir = _make_transcript_dir()
        self._transcript_dir = self._tmpdir.name

        rows = derive_rows(
            '/project',  # project root (unused for path, transcript_dir is explicit)
            transcript_dir=self._transcript_dir,
        )
        self._records = aggregate(
            rows,
            repo='test-repo',
            # Review: code-reviewer (F9) — pass a real coordinator_root_path so
            # test_repo_and_root_path exercises the actual contract rather than
            # asserting the default placeholder '.'.
            coordinator_root_path='/project',
            git_branch='main',
            git_sha='abc123',
            observed_at='2026-07-02T00:00:00Z',
            transcript_dir=self._transcript_dir,
        )
        # Index by file_path for easy lookup
        self._by_path = {r['file_path']: r for r in self._records}

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_record_count(self):
        self.assertEqual(len(self._records), 5, msg=f'Expected 5 records, got {len(self._records)}: {[r["file_path"] for r in self._records]}')

    def test_all_file_paths_present(self):
        expected = {
            '/project/src/main.py',
            '/project/src/utils.py',
            '/project/new/file.txt',
            '/tmp/output.log',
            '/project/src/',
        }
        self.assertEqual(set(self._by_path.keys()), expected)

    def test_main_py_counts(self):
        r = self._by_path['/project/src/main.py']
        self.assertEqual(r['read_count'], 1)
        self.assertEqual(r['edited_count'], 1)
        self.assertEqual(r['referenced_count'], 0)
        self.assertEqual(r['last_operation'], 'edit')
        self.assertEqual(r['completeness'], 'complete')
        self.assertEqual(r['capture_source'], 'derived')
        self.assertEqual(r['provenance_completeness'], 'complete')
        # lines null because Edit result is a success string, not a patch
        self.assertIsNone(r['lines_added'])
        self.assertIsNone(r['lines_removed'])

    def test_utils_py_counts(self):
        r = self._by_path['/project/src/utils.py']
        self.assertEqual(r['read_count'], 1)
        self.assertEqual(r['edited_count'], 0)
        self.assertEqual(r['referenced_count'], 0)
        self.assertIsNone(r['last_operation'])
        self.assertEqual(r['completeness'], 'complete')
        self.assertEqual(r['capture_source'], 'derived')

    def test_new_file_txt_create(self):
        r = self._by_path['/project/new/file.txt']
        self.assertEqual(r['read_count'], 0)
        self.assertEqual(r['edited_count'], 1)
        self.assertEqual(r['last_operation'], 'create')
        self.assertEqual(r['lines_added'], 2)
        self.assertEqual(r['lines_removed'], 0)
        self.assertEqual(r['completeness'], 'complete')
        self.assertEqual(r['capture_source'], 'derived')

    def test_output_log_bash(self):
        r = self._by_path['/tmp/output.log']
        self.assertEqual(r['edited_count'], 1)
        self.assertEqual(r['last_operation'], 'bash')
        # Bash-redirect completeness is partial (heuristic parse)
        self.assertEqual(r['completeness'], 'partial')
        self.assertEqual(r['capture_source'], 'derived')

    def test_src_dir_referenced(self):
        r = self._by_path['/project/src/']
        self.assertEqual(r['referenced_count'], 1)
        self.assertEqual(r['edited_count'], 0)
        self.assertEqual(r['read_count'], 0)
        self.assertEqual(r['completeness'], 'complete')

    def test_null_file_path_rows_absent(self):
        # Ambiguous Bash rows (t6 heredoc, t7 $OUTPUT) have file_path=None → skipped
        for r in self._records:
            self.assertIsNotNone(r['file_path'])
            self.assertIsInstance(r['file_path'], str)

    def test_session_id_correct(self):
        for r in self._records:
            self.assertEqual(r['session_id'], 'test-session-0001')

    def test_provenance_fields(self):
        r = self._by_path['/project/src/main.py']
        prov = r['provenance']
        self.assertEqual(prov['source_kind'], 'coordinator_artifact')
        self.assertEqual(prov['repo'], 'test-repo')
        # ref MUST be None for source_kind:coordinator_artifact — non-git source
        # kinds are contractually required to carry ref:null (see
        # derive-file-attribution.py's inline citation of
        # artifact-shape-contract.schema.json ~line 4482-4485, and the "Review:
        # code-reviewer" annotation at that call site). This is intentional,
        # reviewer-ratified behavior, not an omission — git_branch/git_sha
        # (setUp's 'main'/'abc123') are accepted params but deliberately unused
        # for this source_kind.
        self.assertIsNone(prov['ref'])
        self.assertEqual(prov['observed_at'], '2026-07-02T00:00:00Z')
        self.assertEqual(prov['derivation'], 'derived')
        # Path should point to the session transcript file
        self.assertIn('test-session-0001.jsonl', prov['path'])

    def test_capture_source_always_derived(self):
        for r in self._records:
            self.assertEqual(r['capture_source'], 'derived')

    def test_repo_and_root_path(self):
        # Review: code-reviewer (F9) — assert the actual path passed, not the '.'
        # default, so this test exercises the real coordinator_root_path contract.
        for r in self._records:
            self.assertEqual(r['repo'], 'test-repo')
            self.assertEqual(r['coordinator_root_path'], '/project')


# ---------------------------------------------------------------------------
# F5: Edge-case tests — malformed JSON, empty transcript, tool_use without result
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):
    """
    Review: code-reviewer (F5) — exercise error-path branches that the golden
    fixture doesn't cover: malformed JSON lines, empty transcript, and a
    tool_use with no subsequent tool_result.
    """

    def test_malformed_json_lines_skipped(self):
        # A malformed JSON line intermixed with valid lines must be silently
        # skipped; the valid rows before and after it are still returned.
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, 'test-session-malformed.jsonl')
            with open(fpath, 'w', encoding="utf-8") as f:
                f.write('{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"u1","name":"Read","input":{"file_path":"/good.py"}}]}}\n')
                f.write('THIS IS NOT JSON\n')
                f.write('{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"u1","content":"ok"}]}}\n')
            rows = derive_rows('/unused', transcript_dir=tmpdir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['file_path'], '/good.py')

    def test_empty_transcript_returns_empty_list(self):
        # An empty .jsonl file must produce no rows (not an error).
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, 'test-session-empty.jsonl')
            open(fpath, 'w', encoding="utf-8").close()
            rows = derive_rows('/unused', transcript_dir=tmpdir)
        self.assertEqual(rows, [])

    def test_line_prefilter_skips_pure_text_turns(self):
        # 2026-07-29 perf fix: a line with neither '"tool_use"' nor '"tool_result"'
        # substring is skipped before json.loads. A pure-text assistant turn (no tool
        # blocks) must still be silently skipped, same as before the fast path existed.
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, 'test-session-textonly.jsonl')
            with open(fpath, 'w', encoding="utf-8") as f:
                f.write('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"just chatting, no tools here"}]}}\n')
                f.write('{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"u1","name":"Read","input":{"file_path":"/good.py"}}]}}\n')
                f.write('{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"u1","content":"ok"}]}}\n')
            rows = derive_rows('/unused', transcript_dir=tmpdir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['file_path'], '/good.py')

    def test_line_prefilter_false_positive_still_parses_correctly(self):
        # A pure-text line that happens to MENTION the word tool_use in its content
        # is a substring false positive (still contains '"tool_use"' as literal text)
        # — it must still be parsed (and correctly produce no row, since it has no
        # actual tool_use block), never silently miscounted.
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, 'test-session-falsepos.jsonl')
            with open(fpath, 'w', encoding="utf-8") as f:
                f.write('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"the tool_use block format is documented here"}]}}\n')
            rows = derive_rows('/unused', transcript_dir=tmpdir)
        self.assertEqual(rows, [])

    def test_tool_use_without_tool_result_produces_no_row(self):
        # A tool_use block with no subsequent tool_result (e.g. crashed session)
        # must be silently discarded at EOF — no row emitted.
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, 'test-session-noresult.jsonl')
            with open(fpath, 'w', encoding="utf-8") as f:
                f.write('{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"u1","name":"Read","input":{"file_path":"/pending.py"}}]}}\n')
                # No tool_result follows — file ends here.
            rows = derive_rows('/unused', transcript_dir=tmpdir)
        # pending discarded at EOF per process_transcript docstring.
        self.assertEqual(rows, [])


# ---------------------------------------------------------------------------
# F6: Tool coverage — Glob, LS, MultiEdit, NotebookEdit produce correct rows
# ---------------------------------------------------------------------------

class TestToolCoverage(unittest.TestCase):
    """
    Review: code-reviewer (F6) — the golden fixture has Grep but no Glob, LS,
    MultiEdit, or NotebookEdit. Unit tests via derive_attribution directly catch
    typos in the tool-name mapping (e.g. 'MultiEdits') without fixture churn.
    """

    def test_glob_produces_referenced(self):
        rows = derive_attribution('Glob', {'path': '/project/src/'}, 'uid1', 'output', 'sess')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['link_type'], 'referenced')
        self.assertEqual(rows[0]['file_path'], '/project/src/')
        self.assertEqual(rows[0]['system']['capture_source'], 'derived')

    def test_ls_produces_referenced(self):
        rows = derive_attribution('LS', {'path': '/project/'}, 'uid2', 'output', 'sess')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['link_type'], 'referenced')
        self.assertEqual(rows[0]['file_path'], '/project/')

    def test_multiedit_produces_edited(self):
        rows = derive_attribution('MultiEdit', {'file_path': '/project/main.py'}, 'uid3', 'ok', 'sess')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['link_type'], 'edited')
        self.assertEqual(rows[0]['file_path'], '/project/main.py')
        self.assertEqual(rows[0]['metadata']['toolName'], 'MultiEdit')

    def test_notebookedit_produces_edited(self):
        rows = derive_attribution('NotebookEdit', {'file_path': '/project/nb.ipynb'}, 'uid4', 'ok', 'sess')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['link_type'], 'edited')
        self.assertEqual(rows[0]['file_path'], '/project/nb.ipynb')
        self.assertEqual(rows[0]['metadata']['toolName'], 'NotebookEdit')


# ---------------------------------------------------------------------------
# F7: wasPartial metadata — raw-row-only field, not propagated to aggregate
# ---------------------------------------------------------------------------

class TestDeriveAttributionMetadata(unittest.TestCase):
    """
    Review: code-reviewer (F7) — wasPartial is computed in the raw row but
    silently dropped in aggregate(). These tests document and assert the
    raw-row contract so regressions are caught.
    """

    def test_waspartial_true_with_offset(self):
        rows = derive_attribution('Read', {'file_path': '/test.py', 'offset': 10}, 'uid', 'content', 'sess')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['metadata']['wasPartial'])

    def test_waspartial_true_with_limit(self):
        rows = derive_attribution('Read', {'file_path': '/test.py', 'limit': 20}, 'uid', 'content', 'sess')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['metadata']['wasPartial'])

    def test_waspartial_true_with_offset_and_limit(self):
        rows = derive_attribution('Read', {'file_path': '/test.py', 'offset': 10, 'limit': 20}, 'uid', 'content', 'sess')
        self.assertTrue(rows[0]['metadata']['wasPartial'])

    def test_waspartial_false_without_offset_or_limit(self):
        rows = derive_attribution('Read', {'file_path': '/test.py'}, 'uid', 'content', 'sess')
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['metadata']['wasPartial'])


# ---------------------------------------------------------------------------
# Performance cache — folded into this producer 2026-07-29 (see module docstring
# "Performance cache"). Covers: cold/warm byte-identical, invalidation on stat change,
# and every fail-open degrade path (corrupt cache, schema mismatch, derivation-version
# mismatch, unwritable cache dir) — each must fall through to full-scan output,
# byte-identical, no crash.
# ---------------------------------------------------------------------------

class TestPerformanceCache(unittest.TestCase):
    def setUp(self):
        self._cache_tmp = tempfile.TemporaryDirectory(prefix='fa_cache_')
        self.cache_dir = self._cache_tmp.name
        self.addCleanup(self._cache_tmp.cleanup)

    def _uncached_rows(self, transcript_dir):
        return sorted(
            _mod.derive_rows(transcript_dir, transcript_dir=transcript_dir),
            key=lambda r: (r['session_id'], r['file_path'] or '', r['link_type'], r.get('tool_use_id') or ''),
        )

    def _cached_rows(self, transcript_dir, cache_dir=None):
        return sorted(
            _mod.derive_rows(transcript_dir, transcript_dir=transcript_dir, cache_dir=cache_dir or self.cache_dir),
            key=lambda r: (r['session_id'], r['file_path'] or '', r['link_type'], r.get('tool_use_id') or ''),
        )

    def test_default_cache_dir_is_not_state(self):
        # Durable-data-plane placement, never this repo's state/ (see CLAUDE.md
        # § Durable-data plane and the negative-spec narrowing above).
        cache_dir = _mod._default_cache_dir()
        self.assertIn('claude-klabauter', cache_dir)
        self.assertIn('file-attribution-cache', cache_dir)
        self.assertNotIn(os.path.join('claude-klabauter', 'state'), cache_dir)

    # -- Rooted-override rejection (Review: code-reviewer, Finding 1) ----------
    # `_default_cache_dir` mirrors coordinator_core/_settings_home.py's precedence AND
    # its `is_absolute() or root` rejection. A relative override must fail loud, not be
    # joined against a cwd this producer never pins (it is spawned with no explicit
    # `cwd=`), which would relocate the cache per run and silently disable it.

    def test_relative_coordinator_settings_home_is_rejected(self):
        with mock.patch.dict(os.environ, {'COORDINATOR_SETTINGS_HOME': 'relative/dir'}, clear=False):
            with self.assertRaises(ValueError) as ctx:
                _mod._default_cache_dir()
        self.assertIn('COORDINATOR_SETTINGS_HOME', str(ctx.exception))

    def test_relative_claude_home_is_rejected(self):
        env = {'CLAUDE_HOME': 'relative/dir'}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop('COORDINATOR_SETTINGS_HOME', None)
            with self.assertRaises(ValueError) as ctx:
                _mod._default_cache_dir()
        self.assertIn('CLAUDE_HOME', str(ctx.exception))

    def test_absolute_overrides_are_accepted(self):
        abs_home = os.path.abspath(os.path.join(os.sep, 'srv', 'settings-home'))
        with mock.patch.dict(os.environ, {'COORDINATOR_SETTINGS_HOME': abs_home}, clear=False):
            self.assertTrue(_mod._default_cache_dir().startswith(abs_home))

        abs_claude = os.path.abspath(os.path.join(os.sep, 'srv', 'claude-home'))
        with mock.patch.dict(os.environ, {'CLAUDE_HOME': abs_claude}, clear=False):
            os.environ.pop('COORDINATOR_SETTINGS_HOME', None)
            self.assertTrue(_mod._default_cache_dir().startswith(abs_claude))

    def test_drive_relative_rejected_but_posix_rooted_accepted(self):
        # Both halves of the `is_absolute() or root` predicate, on every platform:
        # 'C:foo' means "foo relative to the cwd on drive C:" and is NOT absolute on
        # Windows, so `is_absolute()` alone would accept a cwd-anchored path; '/srv/x'
        # is rooted-but-driveless under a Windows interpreter, so `root` alone is what
        # accepts it there. Neither assertion is platform-conditional.
        for rejected in ('C:foo', 'C:relative\\dir'):
            with mock.patch.dict(os.environ, {'COORDINATOR_SETTINGS_HOME': rejected}, clear=False):
                with self.assertRaises(ValueError):
                    _mod._default_cache_dir()

        with mock.patch.dict(os.environ, {'COORDINATOR_SETTINGS_HOME': '/srv/x'}, clear=False):
            self.assertIn('file-attribution-cache', _mod._default_cache_dir())

    def test_cold_and_warm_runs_byte_identical_to_uncached(self):
        with _make_transcript_dir() as tdir:
            uncached = self._uncached_rows(tdir)
            cold = self._cached_rows(tdir)
            warm = self._cached_rows(tdir)
        self.assertEqual(uncached, cold)
        self.assertEqual(cold, warm)

    def test_warm_run_skips_process_transcript_for_unchanged_file(self):
        with _make_transcript_dir() as tdir:
            self._cached_rows(tdir)  # cold: populates the cache
            with mock.patch.object(_mod, 'process_transcript') as spy:
                self._cached_rows(tdir)  # warm: every file should be a stat hit
            spy.assert_not_called()

    def test_modified_transcript_forces_recompute_and_stays_correct(self):
        with _make_transcript_dir() as tdir:
            cold = self._cached_rows(tdir)
            fpath = os.path.join(tdir, 'test-session-0001.jsonl')
            with open(fpath, 'a', encoding='utf-8') as fh:
                fh.write('\n')  # size changes → stat mismatch → forced miss
            os.utime(fpath, None)
            with mock.patch.object(_mod, 'process_transcript', wraps=_mod.process_transcript) as spy:
                rows = self._cached_rows(tdir)
            spy.assert_called_once()
            # Content unchanged aside from a trailing blank line the transcript
            # parser ignores, so the derived rows are unchanged too.
            self.assertEqual(cold, sorted(
                rows, key=lambda r: (r['session_id'], r['file_path'] or '', r['link_type'], r.get('tool_use_id') or '')
            ))

    def test_same_size_same_mtime_different_content_serves_stale_rows_BY_DESIGN(self):
        """PINS THE ACCEPTED RISK — this is NOT a bug report, do not "fix" it.

        The cache's invalidation predicate is a stat heuristic, not a content hash — the
        same class make/ccache/rsync default to, chosen deliberately (see the producer's
        module docstring). Its inherent cost is that a same-size edit which also
        preserves `mtime_ns` is invisible, so the previous rows are served. This test
        exists so a future editor tightening or reusing this cache pattern can SEE the
        accepted failure mode rather than rediscovering it, and so that anyone who
        decides the tradeoff is wrong has to change this test on purpose.

        Review: code-reviewer (Finding 3).
        """
        with tempfile.TemporaryDirectory() as tdir:
            fpath = os.path.join(tdir, 'stale-session.jsonl')
            TestRecursiveSubagentEnumeration._write_read_transcript(fpath, '/aaa.py', 'u1')
            cold = self._cached_rows(tdir)
            self.assertEqual({r['file_path'] for r in cold}, {'/aaa.py'})

            before = os.stat(fpath)
            # Equal-length path ⇒ byte-identical file size; mtime_ns restored exactly.
            TestRecursiveSubagentEnumeration._write_read_transcript(fpath, '/bbb.py', 'u1')
            os.utime(fpath, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = os.stat(fpath)
            self.assertEqual(after.st_size, before.st_size)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

            warm = self._cached_rows(tdir)
            uncached = self._uncached_rows(tdir)

        self.assertEqual(warm, cold, 'stat-identical edit is invisible to the cache — accepted')
        self.assertEqual({r['file_path'] for r in uncached}, {'/bbb.py'})

    def test_lost_update_from_overlapping_run_degrades_to_a_miss_not_a_stale_hit(self):
        """Two overlapping runs share one cache file; the later `os.replace` wins and the
        other run's entries are lost. This pins the consequence: a lost update costs a
        recompute, never a wrong or torn read.

        Deliberately a SERIALIZED simulation of the interleave, not a threaded race — the
        clobber is produced by writing the cache exactly as the losing run would leave it
        (one transcript's entry present, the other's absent). A racy test here would be
        worse than none: it could pass while the interleave it claims to exercise never
        happened. Atomicity itself is `os.replace`'s contract on POSIX and Windows alike
        and is not restated here.

        Review: code-reviewer (Finding 4).
        """
        with tempfile.TemporaryDirectory() as tdir:
            a = os.path.join(tdir, 'session-a.jsonl')
            b = os.path.join(tdir, 'session-b.jsonl')
            TestRecursiveSubagentEnumeration._write_read_transcript(a, '/a-touched.py', 'ua')
            TestRecursiveSubagentEnumeration._write_read_transcript(b, '/b-touched.py', 'ub')

            expected = self._uncached_rows(tdir)
            full = self._cached_rows(tdir)
            self.assertEqual(full, expected)

            # Simulate the loser's write surviving: drop one entry, keeping the file
            # otherwise valid (current schema + derivation version), exactly as a run
            # that had only seen `session-a` would have left it.
            cache_path = _mod._cache_file_path(self.cache_dir, tdir)
            with open(cache_path, 'r', encoding='utf-8') as fh:
                obj = json.load(fh)
            dropped = [k for k in obj['files'] if 'session-b' in k]
            self.assertEqual(len(dropped), 1)
            del obj['files'][dropped[0]]
            with open(cache_path, 'w', encoding='utf-8') as fh:
                json.dump(obj, fh)

            with mock.patch.object(_mod, 'process_transcript', wraps=_mod.process_transcript) as spy:
                rows = self._cached_rows(tdir)
            recomputed = {os.path.basename(call.args[0]) for call in spy.call_args_list}

        self.assertEqual(rows, expected)
        self.assertEqual(recomputed, {'session-b.jsonl'}, 'only the lost entry recomputes')

    def test_corrupt_cache_file_falls_back_to_full_scan(self):
        with _make_transcript_dir() as tdir:
            uncached = self._uncached_rows(tdir)
            cache_path = _mod._cache_file_path(self.cache_dir, tdir)
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(cache_path, 'w', encoding='utf-8') as fh:
                fh.write('{not valid json')
            rows = self._cached_rows(tdir)
        self.assertEqual(uncached, rows)

    def test_schema_version_mismatch_falls_back_to_full_scan(self):
        with _make_transcript_dir() as tdir:
            uncached = self._uncached_rows(tdir)
            cache_path = _mod._cache_file_path(self.cache_dir, tdir)
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(cache_path, 'w', encoding='utf-8') as fh:
                json.dump({
                    'schema_version': _mod._CACHE_SCHEMA_VERSION + 1,
                    'derivation_version': _mod._DERIVATION_VERSION,
                    'transcript_dir': os.path.abspath(tdir),
                    'files': {},
                }, fh)
            rows = self._cached_rows(tdir)
        self.assertEqual(uncached, rows)

    def test_derivation_version_mismatch_forces_full_rescan(self):
        # The exact scenario the module docstring warns about: transcripts unchanged,
        # but the derivation LOGIC changed — a stat check alone cannot catch this, so
        # a stale-looking cache entry with the OLD derivation_version must still miss.
        with _make_transcript_dir() as tdir:
            uncached = self._uncached_rows(tdir)
            self._cached_rows(tdir)  # populate a real, valid cache at current version
            cache_path = _mod._cache_file_path(self.cache_dir, tdir)
            with open(cache_path, 'r', encoding='utf-8') as fh:
                obj = json.load(fh)
            obj['derivation_version'] = _mod._DERIVATION_VERSION - 1
            with open(cache_path, 'w', encoding='utf-8') as fh:
                json.dump(obj, fh)
            with mock.patch.object(_mod, 'process_transcript', wraps=_mod.process_transcript) as spy:
                rows = self._cached_rows(tdir)
            spy.assert_called()  # forced miss, not served from the stale-version entry
        self.assertEqual(uncached, sorted(
            rows, key=lambda r: (r['session_id'], r['file_path'] or '', r['link_type'], r.get('tool_use_id') or '')
        ))

    def test_transcript_dir_mismatch_falls_back_to_full_scan(self):
        with _make_transcript_dir() as tdir:
            uncached = self._uncached_rows(tdir)
            cache_path = _mod._cache_file_path(self.cache_dir, tdir)
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(cache_path, 'w', encoding='utf-8') as fh:
                json.dump({
                    'schema_version': _mod._CACHE_SCHEMA_VERSION,
                    'derivation_version': _mod._DERIVATION_VERSION,
                    'transcript_dir': '/some/other/path',
                    'files': {},
                }, fh)
            rows = self._cached_rows(tdir)
        self.assertEqual(uncached, rows)

    def test_unwritable_cache_dir_does_not_crash_and_still_produces_rows(self):
        with _make_transcript_dir() as tdir:
            uncached = self._uncached_rows(tdir)
            os.chmod(self.cache_dir, stat.S_IREAD | stat.S_IEXEC)
            try:
                rows = self._cached_rows(tdir)
            finally:
                os.chmod(self.cache_dir, stat.S_IRWXU)
        self.assertEqual(uncached, rows)

    def test_successful_save_leaves_no_leftover_temp_file(self):
        with _make_transcript_dir() as tdir:
            self._cached_rows(tdir)
        leftovers = [f for f in os.listdir(self.cache_dir) if f.startswith('.file-attribution-cache-')]
        self.assertEqual(leftovers, [])

    def test_no_cache_dir_argument_behaves_like_uncached(self):
        with _make_transcript_dir() as tdir:
            uncached = self._uncached_rows(tdir)
            rows = sorted(
                _mod.derive_rows(tdir, transcript_dir=tdir, cache_dir=None),
                key=lambda r: (r['session_id'], r['file_path'] or '', r['link_type'], r.get('tool_use_id') or ''),
            )
        self.assertEqual(uncached, rows)

    def test_session_filter_bypasses_cache_and_does_not_write_one(self):
        with _make_transcript_dir() as tdir:
            rows = _mod.derive_rows(
                tdir, transcript_dir=tdir, session_filter='test-session-0001', cache_dir=self.cache_dir,
            )
        self.assertTrue(rows)
        self.assertEqual(os.listdir(self.cache_dir), [])


class TestRecursiveSubagentEnumeration(unittest.TestCase):
    """
    Q3 (DR-244 § Amendment 2026-07-29): transcript enumeration recurses one level
    into `<session>/subagents/agent-*.jsonl` alongside the existing top-level
    `<session>.jsonl` scan. A subagent transcript's OWN stem becomes its rows'
    `session_id` — it is deliberately not collapsed onto the parent session (see
    the enumeration helper's docstring in derive-file-attribution.py).
    """

    @staticmethod
    def _write_read_transcript(fpath, file_path, tool_use_id):
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                'type': 'assistant',
                'message': {'role': 'assistant', 'content': [
                    {'type': 'tool_use', 'id': tool_use_id, 'name': 'Read', 'input': {'file_path': file_path}},
                ]},
            }) + '\n')
            f.write(json.dumps({
                'type': 'user',
                'message': {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': tool_use_id, 'content': 'ok'},
                ]},
            }) + '\n')

    def test_nested_subagent_transcript_discovered(self):
        with tempfile.TemporaryDirectory() as tdir:
            top = os.path.join(tdir, 'parent-session.jsonl')
            self._write_read_transcript(top, '/parent-touched.py', 'u1')
            sub_dir = os.path.join(tdir, 'parent-session', 'subagents')
            os.makedirs(sub_dir)
            sub_path = os.path.join(sub_dir, 'agent-child-uuid.jsonl')
            self._write_read_transcript(sub_path, '/child-touched.py', 'u2')

            rows = derive_rows('/unused', transcript_dir=tdir)

        file_paths = {r['file_path'] for r in rows}
        self.assertEqual(file_paths, {'/parent-touched.py', '/child-touched.py'})
        self.assertEqual(len(rows), 2)

    def test_subagent_row_carries_own_stem_as_session_id(self):
        with tempfile.TemporaryDirectory() as tdir:
            top = os.path.join(tdir, 'parent-session.jsonl')
            self._write_read_transcript(top, '/parent-touched.py', 'u1')
            sub_dir = os.path.join(tdir, 'parent-session', 'subagents')
            os.makedirs(sub_dir)
            sub_path = os.path.join(sub_dir, 'agent-child-uuid.jsonl')
            self._write_read_transcript(sub_path, '/child-touched.py', 'u2')

            rows = derive_rows('/unused', transcript_dir=tdir)

        by_file = {r['file_path']: r['session_id'] for r in rows}
        self.assertEqual(by_file['/parent-touched.py'], 'parent-session')
        # The subagent's own transcript stem, NOT 'parent-session' — collapsing
        # onto the parent is the exact lossy pre-aggregation DR-244 rejects.
        self.assertEqual(by_file['/child-touched.py'], 'agent-child-uuid')

    def test_session_without_subagents_dir_still_works(self):
        with tempfile.TemporaryDirectory() as tdir:
            top = os.path.join(tdir, 'solo-session.jsonl')
            self._write_read_transcript(top, '/solo-touched.py', 'u1')

            rows = derive_rows('/unused', transcript_dir=tdir)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session_id'], 'solo-session')
        self.assertEqual(rows[0]['file_path'], '/solo-touched.py')

    def test_absent_transcript_dir_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tdir:
            missing = os.path.join(tdir, 'does-not-exist')
            rows = derive_rows(missing, transcript_dir=missing)
        self.assertEqual(rows, [])

    def test_session_filter_includes_matching_sessions_own_subagents(self):
        with tempfile.TemporaryDirectory() as tdir:
            top_a = os.path.join(tdir, 'session-a.jsonl')
            self._write_read_transcript(top_a, '/a-touched.py', 'u1')
            sub_dir_a = os.path.join(tdir, 'session-a', 'subagents')
            os.makedirs(sub_dir_a)
            self._write_read_transcript(os.path.join(sub_dir_a, 'agent-a-child.jsonl'), '/a-child.py', 'u2')

            top_b = os.path.join(tdir, 'session-b.jsonl')
            self._write_read_transcript(top_b, '/b-touched.py', 'u3')
            sub_dir_b = os.path.join(tdir, 'session-b', 'subagents')
            os.makedirs(sub_dir_b)
            self._write_read_transcript(os.path.join(sub_dir_b, 'agent-b-child.jsonl'), '/b-child.py', 'u4')

            rows = derive_rows('/unused', transcript_dir=tdir, session_filter='session-a')

        file_paths = {r['file_path'] for r in rows}
        self.assertEqual(file_paths, {'/a-touched.py', '/a-child.py'})

    def test_recursive_read_uses_cache_with_distinct_keys_per_nested_file(self):
        # Cache keys must be per-relative-path (not bare fname), or a nested
        # agent-*.jsonl would collide with a top-level file of the same leaf name.
        with tempfile.TemporaryDirectory() as tdir, tempfile.TemporaryDirectory() as cdir:
            top = os.path.join(tdir, 'parent-session.jsonl')
            self._write_read_transcript(top, '/parent-touched.py', 'u1')
            sub_dir = os.path.join(tdir, 'parent-session', 'subagents')
            os.makedirs(sub_dir)
            sub_path = os.path.join(sub_dir, 'agent-child-uuid.jsonl')
            self._write_read_transcript(sub_path, '/child-touched.py', 'u2')

            rows_cold = derive_rows('/unused', transcript_dir=tdir, cache_dir=cdir)
            rows_warm = derive_rows('/unused', transcript_dir=tdir, cache_dir=cdir)

        self.assertEqual(len(rows_cold), 2)
        self.assertEqual(
            sorted((r['session_id'], r['file_path']) for r in rows_cold),
            sorted((r['session_id'], r['file_path']) for r in rows_warm),
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
